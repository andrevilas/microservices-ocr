"""Bounded internal document execution; no transport or application wiring.

Manual permits must remain open through all in-flight I/O and context cleanup.
Cancellation is cooperative: blocking I/O retains capacity until it returns.
A client timeout can follow a commit; retries must use the same key and body.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import re
from threading import Event, Lock
from time import monotonic

from app.application import ApplicationError, Principal


_ERRORS = {
    "document_operations_busy": (429, "Document operations are busy."),
    "document_operations_unavailable": (503, "Document operations are unavailable."),
    "document_operations_active": (503, "Document operations are still active."),
    "invalid_document_principal": (422, "Invalid document principal."),
    "operation_timeout": (408, "Document operation timed out."),
    "operation_cancelled": (408, "Document operation was cancelled."),
}


def _error(code):
    status, message = _ERRORS[code]
    return ApplicationError(status, code, message)


def _seconds(value, *, allow_zero=False, maximum=120):
    if type(value) not in (int, float):
        raise ValueError("Invalid timeout.")
    try:
        seconds = float(value)
    except (ValueError, OverflowError):
        raise ValueError("Invalid timeout.") from None
    if (not math.isfinite(seconds) or seconds < 0
            or (seconds == 0 and not allow_zero)
            or (maximum is not None and seconds > maximum)):
        raise ValueError("Invalid timeout.")
    return seconds


class OperationControl:
    """Trusted deadline and cancellation signal, independent of JobStore locks."""

    def __init__(self, timeout_seconds=120):
        timeout = _seconds(timeout_seconds)
        self._deadline = monotonic() + timeout
        self._cancelled = Event()
        self._lock = Lock()
        self._reason = None

    @property
    def remaining_seconds(self):
        return max(0.0, self._deadline - monotonic())

    def _signal(self, reason):
        with self._lock:
            if not self._cancelled.is_set():
                self._reason = reason
                self._cancelled.set()

    def cancel(self):
        self._signal("operation_cancelled")

    def _timeout(self):
        self._signal("operation_timeout")

    def checkpoint(self):
        # Event observation needs no coordinator or JobStore lock, I/O or wait.
        if self._cancelled.is_set():
            raise _error(self._reason)
        if monotonic() >= self._deadline:
            raise _error("operation_timeout")


class _Permit:
    __slots__ = ("_execution", "_control")

    def __init__(self, execution, control):
        self._execution = execution
        self._control = control

    @property
    def control(self):
        return self._control

    def close(self):
        self._execution._release(self)


@dataclass(frozen=True)
class _Outcome:
    value: object = None
    error: BaseException | None = None


def _deliver(outcome):
    error = outcome.error
    if error is None:
        return outcome.value
    if not isinstance(error, Exception):
        raise error
    if (isinstance(error, ApplicationError)
            and type(error.status) is int and 400 <= error.status <= 599
            and type(error.code) is str
            and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", error.code)):
        raise ApplicationError(error.status, error.code, "Document operation failed.") from None
    raise _error("document_operations_unavailable") from None


def _consume(future):
    # Worker control exceptions are data inside _Outcome, never Future errors.
    # Also consume executor/loop failures without logging their values.
    try:
        future.exception()
    except BaseException:
        pass


class DocumentExecution:
    """Two global permits, one per subject; admission never queues or waits.

    Active permits are the bounded outstanding-work registry. Abandoning an
    awaiter does not remove a queued or running worker from that registry.
    """

    def __init__(self, timeout_seconds=120):
        self._timeout_seconds = _seconds(timeout_seconds)
        self._lock = Lock()
        self._closed = False
        self._active: dict[_Permit, str] = {}
        self._subjects: set[str] = set()

    @property
    def active_count(self):
        with self._lock:
            return len(self._active)

    def acquire(self, principal):
        # Subject validation is solely a capacity key, never authorization.
        if (type(principal) is not Principal or type(principal.subject) is not str
                or re.fullmatch(r"(?:user:[1-9][0-9]*|service:[0-9a-f]{32})",
                                principal.subject) is None):
            raise _error("invalid_document_principal")
        with self._lock:
            if self._closed:
                raise _error("document_operations_unavailable")
            subject = principal.subject
            if len(self._active) >= 2 or subject in self._subjects:
                raise _error("document_operations_busy")
            permit = _Permit(self, OperationControl(self._timeout_seconds))
            self._active[permit] = subject
            self._subjects.add(subject)
            return permit

    def _release(self, permit):
        with self._lock:
            subject = self._active.pop(permit, None)
            if subject is not None:
                permit.control.cancel()  # A closed permit grants no authority.
                self._subjects.remove(subject)

    @staticmethod
    def _worker(permit, work):
        try:
            permit.control.checkpoint()
            return _Outcome(value=work(permit.control))
        except BaseException as error:
            return _Outcome(error=error)
        finally:
            # Never release from an awaiter's timeout/cancellation path.
            permit.close()

    async def run(self, principal, work):
        permit = self.acquire(principal)
        submission_error = None
        try:
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(None, self._worker, permit, work)
        except BaseException as error:
            submission_error = error
        if submission_error is not None:
            permit.close()
            return _deliver(_Outcome(error=submission_error))
        future.add_done_callback(_consume)
        timeout_error = None
        try:
            outcome = await asyncio.wait_for(
                asyncio.shield(future), timeout=permit.control.remaining_seconds)
        except asyncio.CancelledError:
            permit.control.cancel()
            raise
        except TimeoutError:
            permit.control._timeout()
            timeout_error = _error("operation_timeout")
        if timeout_error is not None:
            raise timeout_error
        # No post-return checkpoint: the operation may already have committed.
        return _deliver(outcome)

    async def shutdown(self, timeout_seconds=30):
        timeout = _seconds(timeout_seconds, allow_zero=True, maximum=None)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        with self._lock:
            self._closed = True
            for permit in self._active:
                permit.control.cancel()
        while True:
            with self._lock:
                if not self._active:
                    return
            remaining = deadline - loop.time()
            if remaining <= 0:
                # Capacity remains owned. A future main must retain its
                # RuntimeLease when shutdown reports this failure.
                raise _error("document_operations_active")
            await asyncio.sleep(min(0.01, remaining))
