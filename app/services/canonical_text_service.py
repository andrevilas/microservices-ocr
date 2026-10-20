"""Canonical ``pages.jsonl`` text representation, built incrementally from persisted pages.

Nothing is written to disk and the document is never held in memory: every page is read
(``read_page`` validates digest, size and character count), serialized, hashed and dropped.
Only the manifest and a small per-page index (<= 300 entries) are retained.

Canonical format (``ocr-canonical-pages/1`` manifest, ``ocr-canonical-page/1`` records)
-----------------------------------------------------------------------------------
One JSON line per page, in order 1..N, including empty pages. Each record holds:
``schema_version``, ``normalization`` ("identity-utf8-v1"), ``offset_unit``
("unicode_code_points"), ``job_id``, ``document_id`` (== job_id), ``artifact_version`` (1),
``text_version`` (1), ``page``, ``source``, ``text`` (complete persisted text),
``char_start`` (0), ``char_end`` (len(text); half-open interval [start, end) in the page),
``text_sha256`` / ``text_utf8_bytes`` (of the original UTF-8 page bytes),
``input_pdf_sha256`` and ``output_pdf_sha256``.
Serialization is exactly::

    json.dumps(record, ensure_ascii=False, sort_keys=True,
               separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\\n"

The LF terminator belongs to the JSONL hash. The text is never normalized (no NFC/NFKC, no
newline or whitespace changes). ``identity-utf8-v1`` is the identity of the *stored* text:
it makes no claim of fidelity to the original PDF and its offsets are Python code points,
not UTF-8 bytes, UTF-16 units, grapheme clusters or page coordinates.

Versions are fixed integers (1): a job has one final artifact and one persisted text
representation; a retry is another job and therefore another ``document_id``. Evidence
links use document_id + artifact_version + text_version + page + text_sha256 + [start, end).
``parent_job_id`` is copied from the validated job only; it grants no access to the parent
and lineage is never inferred from coinciding hashes.

Lifecycle and streaming caveats
-------------------------------
``CanonicalTextService.open`` is a context manager holding ONE artifact lease for the whole
block. A transport adapter must keep the context open until the stream has finished or the
client disconnected, and must not rely on ``__del__``/GC. After the block ends the prepared
object and any iterator raise ``RuntimeError``. The lease prevents purge, not tampering by
a party with filesystem access: the second pass re-validates every page and compares each
line with the index from the first pass. A failure during streaming may happen after a
prefix was already emitted; the adapter must abort the transfer and consumers must compare
the received size/hash with the manifest. Emitted network bytes cannot be rolled back.
TTL is not re-checked during an admitted read (existing lease semantics).

Concurrency limits (global 2 / per owner 1), the 120 s deadline and disconnect handling are
requirements of the future transport layer and are NOT implemented here.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from typing import Iterator

from app.application import ApplicationError, JobApplication, Principal
from app.metadata_models import (
    MAX_CANONICAL_CHUNK_CHARS as MAX_CHUNK_CHARS,
    MAX_CANONICAL_JSONL_BYTES as MAX_JSONL_BYTES,
)
from app.results import MAX_TOTAL_CHARS, PageInfo, read_page
from app.services.storage_service import JobStore

PAGE_SCHEMA_VERSION = "ocr-canonical-page/1"
MANIFEST_SCHEMA_VERSION = "ocr-canonical-pages/1"
NORMALIZATION = "identity-utf8-v1"
OFFSET_UNIT = "unicode_code_points"
ARTIFACT_VERSION = 1
TEXT_VERSION = 1
STREAM_CHUNK_BYTES = 256 * 1024


def _unavailable() -> ApplicationError:
    return ApplicationError(409, "result_unavailable", "Result was not recorded or is no longer retained.")


def _integrity() -> ApplicationError:
    return ApplicationError(503, "result_integrity_error", "Stored result failed validation.")


@contextmanager
def _sanitized():
    """Map storage failures to sanitized application errors (no paths, values or raw exceptions)."""
    try:
        yield
    except ApplicationError:
        raise
    except FileNotFoundError:
        raise _unavailable() from None
    except (ValueError, UnicodeError):
        raise _integrity() from None
    except OSError:
        raise ApplicationError(503, "storage_unavailable", "Result storage is unavailable.") from None


def _limit() -> ApplicationError:
    return ApplicationError(422, "canonical_text_limit_exceeded", "Canonical text exceeds the supported limits.")


def _strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class PreparedCanonicalText:
    """Validated canonical snapshot bound to an open lease. Created only by the service."""

    def __init__(self, directory, job_id: str, parent_job_id: str | None, pages: tuple[PageInfo, ...],
                 input_sha256: str, output_sha256: str):
        self._directory = directory
        self._job_id = job_id
        self._pages = pages
        self._input_sha256 = input_sha256
        self._output_sha256 = output_sha256
        self._index: list[tuple[str, int]] = []  # (sha256, size) of each canonical line
        self._closed = False
        total_chars, digest, size = 0, hashlib.sha256(), 0
        for info in pages:
            with _sanitized():
                text = read_page(directory, info)
            total_chars += len(text)
            if total_chars > MAX_TOTAL_CHARS:
                raise _limit()
            line = self._line(info, text)
            size += len(line)
            if size > MAX_JSONL_BYTES:
                raise _limit()
            digest.update(line)
            self._index.append((hashlib.sha256(line).hexdigest(), len(line)))
            del text, line
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION, "normalization": NORMALIZATION,
            "offset_unit": OFFSET_UNIT, "job_id": job_id, "document_id": job_id,
            "artifact_version": ARTIFACT_VERSION, "text_version": TEXT_VERSION,
            "page_count": len(pages), "total_characters": total_chars, "size_bytes": size,
            "sha256": digest.hexdigest(), "input_pdf_sha256": input_sha256,
            "output_pdf_sha256": output_sha256,
        }
        if parent_job_id:
            manifest["parent_job_id"] = parent_job_id
        self._manifest = manifest

    def _line(self, info: PageInfo, text: str) -> bytes:
        record = {
            "schema_version": PAGE_SCHEMA_VERSION, "normalization": NORMALIZATION,
            "offset_unit": OFFSET_UNIT, "job_id": self._job_id, "document_id": self._job_id,
            "artifact_version": ARTIFACT_VERSION, "text_version": TEXT_VERSION,
            "page": info.page, "source": info.source, "text": text,
            "char_start": 0, "char_end": len(text),
            "text_sha256": info.digest.sha256, "text_utf8_bytes": info.digest.size_bytes,
            "input_pdf_sha256": self._input_sha256, "output_pdf_sha256": self._output_sha256,
        }
        return json.dumps(record, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Canonical text context is closed.")

    def _close(self) -> None:
        self._closed = True

    @property
    def manifest(self) -> dict:
        self._ensure_open()
        return dict(self._manifest)

    def _read_checked(self, info: PageInfo) -> tuple[str, bytes]:
        with _sanitized():
            text = read_page(self._directory, info)
            line = self._line(info, text)
        expected = self._index[info.page - 1]
        if (len(line), hashlib.sha256(line).hexdigest()) != (expected[1], expected[0]):
            raise _integrity()
        return text, line

    def iter_jsonl(self) -> Iterator[bytes]:
        """Return an iterator of canonical JSONL bytes in chunks of at most 256 KiB."""
        self._ensure_open()
        return self._stream()

    def _stream(self) -> Iterator[bytes]:
        digest, size = hashlib.sha256(), 0
        for info in self._pages:
            self._ensure_open()
            _, line = self._read_checked(info)
            digest.update(line)
            size += len(line)
            view = memoryview(line)
            for start in range(0, len(line), STREAM_CHUNK_BYTES):
                self._ensure_open()
                yield bytes(view[start:start + STREAM_CHUNK_BYTES])
            del view, line
        self._ensure_open()
        if size !=self._manifest["size_bytes"] or digest.hexdigest() != self._manifest["sha256"]:
            raise _integrity()

    def read_page_text(self, page: int) -> dict:
        """Return one complete, checked page while this preparation remains open.

        The fixed internal mapping contains text and canonical identity only.
        PDF hashes in the manifest remain persisted report identifiers; this
        operation neither rehashes PDFs nor performs PDF/A validation.
        """
        self._ensure_open()
        if not (_strict_int(page) and 1 <= page <= len(self._pages)):
            raise ApplicationError(422, "invalid_text_range", "Invalid page, offset or character limit.")
        info = self._pages[page - 1]
        text, _ = self._read_checked(info)
        return {
            "document_id": self._job_id,
            "artifact_version": ARTIFACT_VERSION,
            "text_version": TEXT_VERSION,
            "canonical_sha256": self._manifest["sha256"],
            "page": info.page,
            "text_sha256": info.digest.sha256,
            "text": text,
        }

    def page_chunk(self, page: int, offset: int = 0, max_chars: int = 8000) -> dict:
        self._ensure_open()
        count = len(self._pages)
        if not (_strict_int(page) and _strict_int(offset) and _strict_int(max_chars)
                and 1 <= page <= count and offset >= 0 and 1 <= max_chars <= MAX_CHUNK_CHARS):
            raise ApplicationError(422, "invalid_text_range", "Invalid page, offset or character limit.")
        info = self._pages[page - 1]
        if offset > info.character_count:
            raise ApplicationError(422, "invalid_text_range", "Offset exceeds page length.")
        text, _ = self._read_checked(info)
        piece = text[offset:offset + max_chars]
        end = offset + len(piece)
        truncated = end < len(text)
        next_page = page if truncated else (page + 1 if page < count else None)
        next_offset = end if truncated else (0 if next_page else None)
        return {
            "job_id": self._job_id, "document_id": self._job_id,
            "artifact_version": ARTIFACT_VERSION, "text_version": TEXT_VERSION,
            "page": page, "page_count": count, "text": piece,
            "offset": offset, "end_offset": end, "page_character_count": len(text),
            "text_sha256": info.digest.sha256, "canonical_sha256": self._manifest["sha256"],
            "normalization": NORMALIZATION, "offset_unit": OFFSET_UNIT, "truncated": truncated,
            "next_page": next_page, "next_offset": next_offset,
        }


class CanonicalTextService:
    def __init__(self, store: JobStore):
        self.store = store

    @contextmanager
    def open(self, principal: Principal, job_id: str):
        """Yield a PreparedCanonicalText; the artifact lease is held until the block exits.

        Keep the block open for as long as the stream/pages are being consumed.
        """
        principal.require("jobs:read")
        principal.require("artifacts:read")
        with self.store.artifact_lease(job_id, principal.subject):
            job, report = JobApplication(self.store)._result(principal, job_id)
            prepared = PreparedCanonicalText(
                job.working_dir, job.job_id, job.parent_job_id, tuple(report.pages),
                report.input.sha256, report.output.sha256)
            try:
                yield prepared
            finally:
                prepared._close()
