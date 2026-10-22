"""Request correlation, per-principal request rate and content-free logging (OCR-AM-15).

One server-generated request_id follows an operation across HTTP v1, MCP and the job it
admits: the job row keeps it as origin_request_id and workers log with it. Log output never
carries document content, exception messages or credentials: exception messages are dropped
(only types and code locations remain), known secret shapes are masked and lines are capped.
"""
from __future__ import annotations

import contextvars
import logging
import math
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from uuid import uuid4

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("ocr_request_id", default=None)
job_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("ocr_job_id", default=None)


def new_request_id() -> str:
    value = uuid4().hex
    request_id_var.set(value)
    return value


def current_request_id() -> str | None:
    return request_id_var.get()


@contextmanager
def bound(*, request_id: str | None = None, job_id: str | None = None):
    """Bind correlation for background work; values not given keep the current binding."""
    tokens = []
    if request_id is not None:
        tokens.append((request_id_var, request_id_var.set(request_id)))
    if job_id is not None:
        tokens.append((job_id_var, job_id_var.set(job_id)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


# ── per-principal request rate ──────────────────────────────────────────────────────────

class PrincipalRateLimiter:
    """Token bucket per authenticated subject; capacity equals the per-minute limit."""

    def __init__(self, per_minute: int, *, clock=time.monotonic, max_subjects: int = 10000):
        self.per_minute, self.clock, self.max_subjects = per_minute, clock, max_subjects
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def admit(self, subject: str) -> int | None:
        """Consume one request; return None when admitted, else seconds until the next token."""
        if self.per_minute <= 0:
            return None
        rate = self.per_minute / 60.0
        now = self.clock()
        with self._lock:
            tokens, last = self._buckets.get(subject, (float(self.per_minute), now))
            tokens = min(float(self.per_minute), tokens + (now - last) * rate)
            if tokens < 1:
                self._buckets[subject] = (tokens, now)
                return max(1, math.ceil((1 - tokens) / rate))
            if subject not in self._buckets and len(self._buckets) >= self.max_subjects:
                # Full buckets carry no state worth keeping; drop them before growing.
                for key in [k for k, (t, s) in self._buckets.items() if t + (now - s) * rate >= self.per_minute]:
                    del self._buckets[key]
                if len(self._buckets) >= self.max_subjects:
                    # Hard bound: forget the least recently seen subject (it restarts with a full bucket).
                    del self._buckets[min(self._buckets, key=lambda k: self._buckets[k][1])]
            self._buckets[subject] = (tokens - 1, now)
            return None


_limiter: PrincipalRateLimiter | None = None
_limiter_lock = threading.Lock()


def principal_limiter() -> PrincipalRateLimiter:
    global _limiter
    from app.config import settings
    with _limiter_lock:
        if _limiter is None or _limiter.per_minute != settings.principal_requests_per_minute:
            _limiter = PrincipalRateLimiter(settings.principal_requests_per_minute)
        return _limiter


def admit_principal(principal) -> None:
    from app.application import ApplicationError
    wait = principal_limiter().admit(principal.subject)
    if wait is not None:
        logging.getLogger(__name__).warning("Request rate limit reached for a %s principal", principal.kind)
        raise ApplicationError(429, "rate_limited", "Request rate limit reached for this identity.", retry_after=wait)


# ── content-free log output ─────────────────────────────────────────────────────────────

MAX_LINE = 2000
_SECRET_PATTERNS = (
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+"), r"\1 [redacted]"),
    (re.compile(r"\bocr_svc_[A-Za-z0-9_-]+"), "ocr_svc_[redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), "[jwt-redacted]"),
    (re.compile(r"(?i)\b(password|passwd|secret|client_secret|token|access_token|refresh_token|api[_-]?key|"
                r"authorization|cookie|x-api-key)(\"?\s*[=:]\s*\"?)[^\s&\",;']+"), r"\1\2[redacted]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email-redacted]"),
)


def redact(text: str) -> str:
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    if len(text) > MAX_LINE:
        text = text[:MAX_LINE] + " [truncated]"
    return text


def describe_exception(exc_info) -> str:
    """Types and code locations of the exception chain; messages may carry content and are dropped."""
    import traceback
    _, error, tb = exc_info
    lines, seen = [], set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        frames = traceback.extract_tb(error.__traceback__ if error.__traceback__ is not None else tb)
        location = " <- ".join(f"{os.path.basename(f.filename)}:{f.lineno} {f.name}" for f in reversed(frames[-6:]))
        lines.append(f"{type(error).__module__}.{type(error).__qualname__} at {location or 'unknown'}")
        error, tb = error.__cause__ or error.__context__, None
    return "exception: " + " | caused by ".join(lines)


class ContentFreeFormatter(logging.Formatter):
    """Wraps any formatter: exception text becomes types and locations, then secrets are masked."""

    def __init__(self, inner: logging.Formatter | None = None, fmt: str | None = None):
        super().__init__(fmt)
        self.inner = inner

    def formatException(self, ei):
        return describe_exception(ei)

    def format(self, record):
        if record.exc_info:
            record.exc_text = describe_exception(record.exc_info)
        record.stack_info = None
        if self.inner is not None:
            exc_info, record.exc_info = record.exc_info, None
            try:
                text = self.inner.format(record)
            finally:
                record.exc_info = exc_info
            if record.exc_text and record.exc_text not in text:
                text = text + "\n" + record.exc_text
        else:
            text = super().format(record)
        return "\n".join(redact(line) for line in text.split("\n"))


class CorrelationFilter(logging.Filter):
    """Adds request_id/job_id and strips query strings from access log paths."""

    def filter(self, record):
        record.request_id = request_id_var.get() or "-"
        record.job_id = job_id_var.get() or "-"
        if record.name == "uvicorn.access" and isinstance(record.args, tuple) and len(record.args) >= 3:
            args = list(record.args)
            if isinstance(args[2], str) and "?" in args[2]:
                args[2] = args[2].split("?", 1)[0] + "?[query-redacted]"
            record.args = tuple(args)
        return True


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s job_id=%(job_id)s %(message)s"
_INSTALLED = "_ocr_content_free"


def _guard(handler: logging.Handler, formatter: logging.Formatter) -> None:
    if getattr(handler, _INSTALLED, False):
        return
    handler.addFilter(CorrelationFilter())
    handler.setFormatter(formatter)
    setattr(handler, _INSTALLED, True)


def install_logging(level: str | None = None) -> None:
    """Idempotent: one root stderr handler for application loggers; uvicorn handlers get the same guard."""
    root = logging.getLogger()
    if not any(getattr(h, _INSTALLED, False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        _guard(handler, ContentFreeFormatter(fmt=LOG_FORMAT))
        root.addHandler(handler)
    root.setLevel((level or os.environ.get("LOG_LEVEL") or "INFO").upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            _guard(handler, ContentFreeFormatter(inner=handler.formatter or logging.Formatter()))
