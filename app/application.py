"""Transport-independent read operations. Mutations are introduced in later increments."""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models import JobResponse, JobStatus, QualityLabel
from app.results import JobReport, TextChunk, read_page, read_report
from app.services.storage_service import JobRecord, JobStore, get_job_store


@dataclass(frozen=True)
class Principal:
    subject: str
    user_id: int | None
    scopes: frozenset[str]
    kind: Literal["user", "service", "legacy"] = "user"
    credential_id: str | None = None
    is_admin: bool = False

    def require(self, scope: str) -> None:
        if scope not in self.scopes:
            raise ApplicationError(403, "insufficient_scope", "Required permission is missing.")


class ApplicationError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


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


class IdentityResponse(BaseModel):
    subject: str
    kind: Literal["user", "service", "legacy"] = "user"
    scopes: list[str]


class JobPage(BaseModel):
    items: list[JobResponse]
    next_cursor: str | None


class MetricsResponse(BaseModel):
    total_jobs: int
    by_status: dict[str, int]


class ArtifactResponse(BaseModel):
    job_id: str
    available: bool
    media_type: str = "application/pdf"
    download_url: str | None = None


class JobApplication:
    def __init__(self, store: JobStore):
        self.store = store

    def whoami(self, principal: Principal) -> IdentityResponse:
        return IdentityResponse(subject=principal.subject, kind=principal.kind, scopes=sorted(principal.scopes))

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
                  quality: QualityLabel | None = None) -> JobPage:
        principal.require("jobs:read")
        if not 1 <= limit <= 100:
            raise ApplicationError(422, "invalid_limit", "Limit must be between 1 and 100.")
        if (any(t is not None and t.tzinfo is None for t in (created_from, created_to))
                or (created_from is not None and created_to is not None and created_from > created_to)):
            raise ApplicationError(422, "invalid_period", "Supply an ordered period with timezone offsets.")
        if filename is not None and not 1 <= len(filename) <= 255:
            raise ApplicationError(422, "invalid_filename_filter", "Filename filter must contain 1 to 255 characters.")
        binding = hashlib.sha256(json.dumps([principal.subject, status,
            created_from.isoformat() if created_from else None,
            created_to.isoformat() if created_to else None, filename, quality]).encode()).hexdigest()
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

    def _result(self, principal: Principal, job_id: str) -> tuple[JobRecord, JobReport]:
        principal.require("artifacts:read")
        job = self._record(principal, job_id)
        if job.status != "completed":
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
        return self._result(principal, job_id)[1]

    def text(self, principal: Principal, job_id: str, page: int = 1, offset: int = 0,
             max_chars: int = 8000) -> TextChunk:
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
        available = job.status == "completed" and job.output_pdf_path is not None and job.output_pdf_path.is_file()
        return ArtifactResponse(job_id=job_id, available=available,
                                download_url=f"/api/v1/jobs/{job_id}/artifact/content" if available else None)

    def artifact_file(self, principal: Principal, job_id: str):
        principal.require("artifacts:read")
        job = self._record(principal, job_id)
        if job.status != "completed" or job.output_pdf_path is None or not job.output_pdf_path.is_file():
            raise ApplicationError(409, "artifact_unavailable", "Artifact is not available.")
        return job.output_pdf_path


def get_job_application() -> JobApplication:
    return JobApplication(get_job_store())
