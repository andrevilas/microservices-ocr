"""Owned job queries and artifact operations shared by HTTP and MCP."""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.api_models import ArtifactResponse, IdentityResponse, JobPage, MetricsResponse
from app.models import JobResponse, JobStatus, QualityLabel
from app.results import JobReport, TextChunk, read_page, read_report
from app.services.storage_service import JobRecord, JobStore, get_job_store
from app.retention_models import DeletionReceipt, retention_info


@dataclass(frozen=True)
class Principal:
    subject: str
    user_id: int | None
    scopes: frozenset[str]
    kind: Literal["user", "service", "legacy"] = "user"
    credential_id: str | None = None
    is_admin: bool = False
    auth_source: Literal["unknown", "local_jwt", "service", "oauth", "legacy"] = "unknown"

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise ApplicationError(403, "insufficient_scope", "Required permission is missing.")


class ApplicationError(Exception):
    def __init__(self, status: int, code: str, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.retry_after = retry_after


class ValidationDetail(BaseModel):
    location: list[str | int]
    code: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[ValidationDetail] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: ErrorBody
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")


class JobApplication:
    def __init__(self, store: JobStore):
        self.store = store

    def whoami(self, principal: Principal) -> IdentityResponse:
        return IdentityResponse(subject=principal.subject, kind=principal.kind, scopes=sorted(principal.scopes))

    def capabilities(self, principal: Principal):
        from app.services.capability_service import get_capability_service
        return get_capability_service().snapshot()

    @staticmethod
    def _owned(principal: Principal, job: JobRecord) -> bool:
        # None never grants wildcard access; no administrator bypass for documents.
        return job.effective_owner is not None and job.effective_owner == principal.subject

    def _record(self, principal: Principal, job_id: str) -> JobRecord:
        principal.require("jobs:read")
        job = self.store.get_snapshot(job_id)
        if job is None or not self._owned(principal, job):
            raise ApplicationError(404, "job_not_found", "Job not found.")
        return job

    @staticmethod
    def _response(job: JobRecord) -> JobResponse:
        result = job.to_response()
        if result.error:
            result.error = "Processing did not complete normally; consult the operator."
        if result.download_url:
            result.download_url = f"/api/v1/jobs/{job.job_id}/artifact/content"
        return result

    def get_job(self, principal: Principal, job_id: str) -> JobResponse:
        return self._response(self._record(principal, job_id))

    def list_jobs(self, principal: Principal, limit: int = 50, cursor: str | None = None,
                  status: JobStatus | None = None, created_from: datetime | None = None,
                  created_to: datetime | None = None, filename: str | None = None,
                  quality: QualityLabel | None = None, batch_id: str | None = None) -> JobPage:
        principal.require("jobs:read")
        if not 1 <= limit <= 100:
            raise ApplicationError(422, "invalid_limit", "Limit must be between 1 and 100.")
        if (any(t is not None and t.tzinfo is None for t in (created_from, created_to))
                or (created_from is not None and created_to is not None and created_from > created_to)):
            raise ApplicationError(422, "invalid_period", "Supply an ordered period with timezone offsets.")
        if filename is not None and not 1 <= len(filename) <= 255:
            raise ApplicationError(422, "invalid_filename_filter", "Filename filter must contain 1 to 255 characters.")
        batch_jobs = None
        if batch_id is not None:
            with self.store._lock:
                conn = self.store._get_conn()
                row = (conn.execute("SELECT receipt FROM batches WHERE batch_id=? AND owner=?", (batch_id, principal.subject)).fetchone()
                       if conn.execute("SELECT 1 FROM sqlite_master WHERE name='batches' AND type='table'").fetchone() else None)
            if row is None:
                raise ApplicationError(404, "batch_not_found", "Batch not found.")
            batch_jobs = {item["job_id"] for item in json.loads(row[0])["items"]}
        binding = hashlib.sha256(json.dumps([principal.subject, status,
            created_from.isoformat() if created_from else None,
            created_to.isoformat() if created_to else None, filename, quality, batch_id]).encode()).hexdigest()
        after = None
        if cursor:
            try:
                if len(cursor) > 256:
                    raise ValueError()
                value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if (not isinstance(value, list) or len(value) != 3
                        or not all(isinstance(x, str) for x in value) or value[2] != binding):
                    raise ValueError()
                after = (datetime.fromisoformat(value[0]), value[1])
                if after[0].tzinfo is None:
                    raise ValueError()
            except (ValueError, TypeError, UnicodeDecodeError):
                raise ApplicationError(422, "invalid_cursor", "Invalid pagination cursor.") from None
        jobs = sorted((j for j in self.store.snapshot() if self._owned(principal, j)
                       and (batch_jobs is None or j.job_id in batch_jobs)
                       and (status is None or j.status == status)
                       and (quality is None or j.quality == quality)
                       and (created_from is None or j.created_at >= created_from)
                       and (created_to is None or j.created_at <= created_to)
                       and (filename is None or filename.casefold() in j.filename.casefold())),
                      key=lambda j: (j.created_at, j.job_id))
        if after:
            jobs = [j for j in jobs if (j.created_at, j.job_id) > after]
        page = jobs[:limit]
        next_cursor = None
        if len(jobs) > limit:
            last = page[-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([last.created_at.isoformat(), last.job_id, binding]).encode()).decode()
        return JobPage(items=[self._response(j) for j in page], next_cursor=next_cursor)

    def _result(self, principal: Principal, job_id: str, *,
                allow_failed: bool = False) -> tuple[JobRecord, JobReport]:
        principal.require("artifacts:read")
        job = self._record(principal, job_id)
        # A failed job may retain its diagnostic report (e.g. PDF/A verdict); text/artifact never.
        if job.status != "completed" and not (allow_failed and job.status == "failed"):
            raise ApplicationError(409, "result_not_ready", "Result is not ready.")
        try:
            report = read_report(job.working_dir)
            if report.job_id != job.job_id:
                raise ValueError("Result job mismatch.")
        except FileNotFoundError:
            raise ApplicationError(409, "result_unavailable", "Result was not recorded or is no longer retained.") from None
        except (ValueError, UnicodeError):
            raise ApplicationError(503, "result_integrity_error", "Stored result failed validation.") from None
        except OSError:
            raise ApplicationError(503, "storage_unavailable", "Result storage is unavailable.") from None
        return job, report

    def report(self, principal: Principal, job_id: str) -> JobReport:
        principal.require("jobs:read")
        principal.require("artifacts:read")
        with self.store.artifact_lease(job_id, principal.subject):
            return self._result(principal, job_id, allow_failed=True)[1]

    def text(self, principal: Principal, job_id: str, page: int = 1, offset: int = 0,
             max_chars: int = 8000) -> TextChunk:
        principal.require("jobs:read")
        principal.require("artifacts:read")
        with self.store.artifact_lease(job_id, principal.subject):
            return self._text(principal, job_id, page, offset, max_chars)

    def _text(self, principal, job_id, page, offset, max_chars):
        job, report = self._result(principal, job_id)
        if not 1 <= page <= report.page_count or offset < 0 or not 1 <= max_chars <= 20000:
            raise ApplicationError(422, "invalid_text_range", "Invalid page, offset or character limit.")
        info = report.pages[page - 1]
        if offset > info.character_count:
            raise ApplicationError(422, "invalid_text_range", "Offset exceeds page length.")
        try:
            text = read_page(job.working_dir, info)
        except FileNotFoundError:
            raise ApplicationError(409, "result_unavailable", "Result was not recorded or is no longer retained.") from None
        except (ValueError, UnicodeError):
            raise ApplicationError(503, "result_integrity_error", "Stored result failed validation.") from None
        except OSError:
            raise ApplicationError(503, "storage_unavailable", "Result storage is unavailable.") from None
        chunk = text[offset:offset + max_chars]
        truncated = offset + len(chunk) < len(text)
        next_page = page if truncated else (page + 1 if page < report.page_count else None)
        next_offset = offset + len(chunk) if truncated else (0 if next_page else None)
        return TextChunk(job_id=job_id, page=page, page_count=report.page_count, source=info.source,
                         offset=offset, text=chunk, page_character_count=len(text), truncated=truncated,
                         next_page=next_page, next_offset=next_offset)

    def metrics(self, principal: Principal) -> MetricsResponse:
        principal.require("jobs:read")
        jobs = [j for j in self.store.snapshot() if self._owned(principal, j)]
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job.status] = counts.get(job.status, 0) + 1
        return MetricsResponse(total_jobs=len(jobs), by_status=counts)

    def artifact(self, principal: Principal, job_id: str) -> ArtifactResponse:
        principal.require("artifacts:read")
        job = self._record(principal, job_id)
        retention = retention_info(job)
        available = retention.state == "retained" and job.status == "completed" and job.output_pdf_path is not None and job.output_pdf_path.is_file()
        return ArtifactResponse(job_id=job_id, available=available,
                                download_url=f"/api/v1/jobs/{job_id}/artifact/content" if available else None, retention=retention)

    def delete_artifacts(self, principal, job_id) -> DeletionReceipt:
        principal.require("artifacts:delete")
        return DeletionReceipt(job_id=job_id, retention=self.store.remove_artifacts(job_id, principal.subject, principal.subject))

    def artifact_download(self, principal, job_id):
        from app.leased_response import LeasedFileResponse
        principal.require("jobs:read")
        principal.require("artifacts:read")
        return LeasedFileResponse(self.store, job_id, principal.subject, filename=f"{job_id}-searchable.pdf")

    def artifact_file(self, principal: Principal, job_id: str):
        principal.require("artifacts:read")
        job = self._record(principal, job_id)
        self.store.check_retained(job)
        if job.status != "completed" or job.output_pdf_path is None or not job.output_pdf_path.is_file():
            raise ApplicationError(409, "artifact_unavailable", "Artifact is not available.")
        return job.output_pdf_path


def get_job_application() -> JobApplication:
    return JobApplication(get_job_store())
