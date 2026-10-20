"""Internal bounded download lifetimes; transport adapters must enclose send in the context.

One executor worker owns preparation, the source lease, iteration and cleanup.
A one-slot mailbox and acknowledgements retain that ownership through each send.
Cancellation is cooperative: blocked or queued work retains its execution permit.
No HTTP, MCP, application startup or shutdown wiring is installed here.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import re
from threading import Event, Lock

from app.application import ApplicationError
from app.metadata_models import MAX_SNAPSHOT_BYTES, snapshot_bytes
from app.services.canonical_text_service import (
    CanonicalTextService, MAX_JSONL_BYTES, STREAM_CHUNK_BYTES,
)
from app.services.document_execution import (
    DocumentExecution, _Outcome, _consume, _deliver, _error,
)
from app.services.document_service import DocumentMetadataService
from app.services.metadata_store import _guard, _require


_METADATA_CHUNK_BYTES = 65536
_POLL_SECONDS = 0.05
_EOF = object()


@dataclass(frozen=True, slots=True)
class _Header:
    size_bytes: int
    sha256: str
    media_type: str
    filename: str

    def __post_init__(self):
        limits = {
            ("application/json", "metadata.json"): MAX_SNAPSHOT_BYTES,
            ("application/x-ndjson", "pages.jsonl"): MAX_JSONL_BYTES,
        }
        if (type(self.media_type) is not str or type(self.filename) is not str
                or (self.media_type, self.filename) not in limits
                or type(self.size_bytes) is not int
                or not 0 <= self.size_bytes <= limits[self.media_type, self.filename]
                or type(self.sha256) is not str
                or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None):
            raise ValueError("Invalid document download header.")


@dataclass(slots=True)
class _Message:
    value: _Header | bytes
    acknowledged: Event = field(default_factory=Event)


def _completed(future):
    try:
        outcome = future.result()
    except BaseException as error:
        outcome = _Outcome(error=error)
    return _deliver(outcome)


class _Transfer:
    """One-slot mailbox: no producer coroutine can outlive a closing event loop.

    Only the worker writes the slot. It waits for acknowledgement before writing
    again, including after the last chunk. Event notifications carry no payload.
    """

    def __init__(self, loop, permit):
        self.loop = loop
        self.permit = permit
        self.future = None
        self._lock = Lock()
        self._message = None
        self._ready = asyncio.Event()

    def offer(self, value):
        control = self.permit.control
        message = _Message(value)
        control.checkpoint()
        with self._lock:
            control.checkpoint()
            if self._message is not None:
                raise RuntimeError("Document mailbox is occupied.")
            self._message = message
        # Unlike run_coroutine_threadsafe(queue.put(...)), this schedules no
        # coroutine that could become an unawaited coroutine on loop closure.
        self.loop.call_soon_threadsafe(self._ready.set)
        while True:
            control.checkpoint()
            if message.acknowledged.wait(
                    min(_POLL_SECONDS, control.remaining_seconds)):
                control.checkpoint()
                return

    def abort(self):
        # Signal BEFORE freeing the slot or acknowledging any message.
        self.permit.control.cancel()
        with self._lock:
            message, self._message = self._message, None
            self._ready.clear()
        if message is not None:
            message.acknowledged.set()

    async def receive(self):
        control, future = self.permit.control, self.future
        while True:
            if future.done():
                _completed(future)
                return _EOF
            control.checkpoint()
            ready = asyncio.create_task(self._ready.wait())
            worker = asyncio.shield(future)
            worker.add_done_callback(_consume)
            try:
                done, _ = await asyncio.wait(
                    (ready, worker), timeout=control.remaining_seconds,
                    return_when=asyncio.FIRST_COMPLETED)
                # A simultaneous notification must never hide a worker failure.
                if future.done():
                    _completed(future)
                    return _EOF
                if ready in done:
                    control.checkpoint()
                    with self._lock:
                        message, self._message = self._message, None
                        self._ready.clear()
                    if message is not None:
                        return message
                    continue  # An abort can leave a harmless stale notification.
                control._timeout()
                raise _error("operation_timeout")
            finally:
                ready.cancel()
                worker.cancel()  # Cancels only the shield, never the worker.
                await asyncio.gather(ready, return_exceptions=True)


class DocumentDownload:
    """Read-only header and a single-use iterator bound to its enclosing context."""

    __slots__ = ("_transfer", "_header", "_used", "_closed", "_pending")

    def __init__(self, transfer: _Transfer, header: _Header):
        self._transfer = transfer
        self._header = header
        self._used = False
        self._closed = False
        self._pending = None

    @property
    def size_bytes(self):
        return self._header.size_bytes

    @property
    def sha256(self):
        return self._header.sha256

    @property
    def media_type(self):
        return self._header.media_type

    @property
    def filename(self):
        return self._header.filename

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("Document download context is closed.")

    def iter_bytes(self):
        self._ensure_open()
        if self._used:
            raise RuntimeError("Document download iterator is single-use.")
        self._used = True
        return self._iterate()

    async def _iterate(self):
        try:
            while True:
                self._ensure_open()
                message = await self._transfer.receive()
                self._ensure_open()
                if message is _EOF:
                    return
                if type(message.value) is not bytes:
                    raise _error("document_operations_unavailable")
                self._pending = message
                try:
                    yield message.value
                except BaseException:
                    self._transfer.abort()
                    raise
                finally:
                    # Resumption follows the adapter's awaited send. Closing a
                    # generator signals cancellation before acknowledging instead.
                    self._pending = None
                    message.acknowledged.set()
        except BaseException:
            self._transfer.abort()
            raise

    def _close(self):
        self._transfer.abort()
        self._closed = True
        if self._pending is not None:
            self._pending.acknowledged.set()
            self._pending = None


class DocumentStreams:
    """Download helper sharing the document runtime's existing execution capacity."""

    @_guard
    def __init__(self, document, canonical_service, execution):
        _require(isinstance(document, DocumentMetadataService)
                 and isinstance(canonical_service, CanonicalTextService)
                 and isinstance(execution, DocumentExecution))
        _require(document.canonical is canonical_service
                 and canonical_service.store is document.bundle.jobs)
        self.document = document
        self.canonical_service = canonical_service
        self.execution = execution
        self._shared = (document, canonical_service, execution,
                        document.bundle, document.bundle.jobs)
        self._lock = Lock()
        self._workers = set()

    def _check_shared(self):
        document, canonical, execution, bundle, jobs = self._shared
        _require(self.document is document and self.canonical_service is canonical
                 and self.execution is execution and document.bundle is bundle
                 and document.canonical is canonical and bundle.jobs is jobs
                 and canonical.store is jobs)
        document._check_shared()

    def _forget(self, transfer):
        with self._lock:
            self._workers.discard(transfer)

    @staticmethod
    def _source_error(header):
        code = ("metadata_unavailable" if header.media_type == "application/json"
                else "result_integrity_error")
        return ApplicationError(503, code, "Stored document failed validation.")

    def _pump(self, transfer, header, chunks, chunk_limit):
        control = transfer.permit.control
        control.checkpoint()
        transfer.offer(header)
        iterator = iter(chunks)
        digest, size = hashlib.sha256(), 0
        try:
            while True:
                control.checkpoint()  # Before advancing the source iterator.
                try:
                    chunk = next(iterator)
                except StopIteration:
                    break
                control.checkpoint()
                if type(chunk) is not bytes or not 0 < len(chunk) <= chunk_limit:
                    raise self._source_error(header)
                size += len(chunk)
                if size > header.size_bytes:
                    raise self._source_error(header)
                digest.update(chunk)
                transfer.offer(chunk)  # Includes acknowledgement, even for last.
                del chunk
            if size != header.size_bytes or digest.hexdigest() != header.sha256:
                raise self._source_error(header)
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()

    def _worker(self, transfer, kind, principal, job_id, revision_id, sha256):
        control = transfer.permit.control
        try:
            control.checkpoint()
            self._check_shared()
            document, canonical, _, bundle, _ = self._shared
            bundle.ensure_healthy()  # Outside any SQL transaction or wait.
            control.checkpoint()
            if kind == "metadata":
                with document.open_snapshot(
                        principal, job_id, revision_id, sha256=sha256,
                        checkpoint=control.checkpoint) as snapshot:
                    control.checkpoint()
                    data = snapshot_bytes(snapshot)
                    header = _Header(len(data), hashlib.sha256(data).hexdigest(),
                                     "application/json", "metadata.json")
                    if header.sha256 != sha256:
                        raise self._source_error(header)
                    control.checkpoint()
                    chunks = (data[start:start + _METADATA_CHUNK_BYTES]
                              for start in range(0, len(data), _METADATA_CHUNK_BYTES))
                    self._pump(transfer, header, chunks, _METADATA_CHUNK_BYTES)
            else:
                with canonical.open(principal, job_id) as prepared:
                    control.checkpoint()  # Preparation itself can block in I/O.
                    manifest = prepared.manifest
                    header = _Header(manifest["size_bytes"], manifest["sha256"],
                                     "application/x-ndjson", "pages.jsonl")
                    self._pump(transfer, header, prepared.iter_jsonl(), STREAM_CHUNK_BYTES)
            return _Outcome()
        except BaseException as error:
            # KeyboardInterrupt/SystemExit remain data until delivered to caller.
            return _Outcome(error=error)
        finally:
            # Neither timeout nor cancellation in an awaiter can execute this.
            transfer.permit.close()
            self._forget(transfer)

    @asynccontextmanager
    async def _open(self, kind, principal, job_id, revision_id=None, sha256=None):
        # Use the captured execution identity even if a public reference mutates.
        permit = self._shared[2].acquire(principal)
        transfer, submission_error = None, None
        try:
            loop = asyncio.get_running_loop()
            transfer = _Transfer(loop, permit)
            with self._lock:
                self._workers.add(transfer)
            future = loop.run_in_executor(
                None, self._worker, transfer, kind, principal, job_id, revision_id, sha256)
        except BaseException as error:
            submission_error = error
        if submission_error is not None:
            if transfer is not None:
                self._forget(transfer)
            permit.close()
            _deliver(_Outcome(error=submission_error))
        transfer.future = future
        future.add_done_callback(_consume)
        download = None
        try:
            message = await transfer.receive()
            if message is _EOF or type(message.value) is not _Header:
                raise _error("document_operations_unavailable")
            download = DocumentDownload(transfer, message.value)
            message.acknowledged.set()
            yield download
        finally:
            if download is None:
                transfer.abort()
            else:
                download._close()

    def open_metadata(self, principal, job_id, revision_id, *, sha256):
        return self._open("metadata", principal, job_id, revision_id, sha256)

    def open_canonical(self, principal, job_id):
        return self._open("canonical", principal, job_id)
