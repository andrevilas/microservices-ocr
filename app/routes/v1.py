from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from app.application import (ArtifactResponse, IdentityResponse, JobApplication,
                             JobPage, MetricsResponse, Principal, get_job_application)
from app.models import JobResponse, JobStatus, QualityLabel
from app.results import JobReport, TextChunk
from app.security import require_principal
from app.http_contract import error_responses, example, operation

router = APIRouter(prefix="/api/v1", tags=["API v1"],
                   responses=error_responses(401, 403, 404, 405, 409, 422, 500, 503))
JOB_EXAMPLE = {"job_id": "00000000000000000000000000000001", "filename": "example.pdf",
               "status": "queued", "progress_percent": 0, "created_at": "2026-10-15T12:00:00Z",
               "updated_at": "2026-10-15T12:00:00Z", "quality": None, "download_url": None, "error": None}


@router.get("/me", response_model=IdentityResponse, operation_id="ocr_whoami", openapi_extra=operation(),
            responses=example({"subject": "user:1", "kind": "user", "scopes": ["jobs:read", "artifacts:read"]}))
def whoami(principal: Principal = Depends(require_principal), service: JobApplication = Depends(get_job_application)):
    """Return the authenticated identity and effective scopes, never its credential."""
    return service.whoami(principal)


@router.get("/jobs", response_model=JobPage, operation_id="ocr_list_jobs", openapi_extra=operation("jobs:read"),
            responses=example({"items": [JOB_EXAMPLE], "next_cursor": None}))
def list_jobs(limit: int = Query(50, ge=1, le=100), cursor: str | None = Query(None, max_length=256),
              status: JobStatus | None = None, created_from: datetime | None = None,
              created_to: datetime | None = None, filename: str | None = Query(None, min_length=1, max_length=255),
              quality: QualityLabel | None = None, principal: Principal = Depends(require_principal),
              service: JobApplication = Depends(get_job_application)):
    """List caller-owned jobs in creation order; pass next_cursor unchanged to continue.

    Limit is 1–100 (default 50). Results are live; concurrent inserts can appear on later pages.
    """
    return service.list_jobs(principal, limit, cursor, status, created_from, created_to, filename, quality)


@router.get("/jobs/{job_id}/text", response_model=TextChunk, operation_id="ocr_get_text",
            openapi_extra=operation("jobs:read", "artifacts:read"),
            responses=example({"job_id": JOB_EXAMPLE["job_id"], "page": 1, "page_count": 1,
                               "source": "pdf_text", "offset": 0, "text": "Example", "page_character_count": 7,
                               "truncated": False, "next_page": None, "next_offset": None}))
def text(job_id: str, page: int = Query(1, ge=1), offset: int = Query(0, ge=0),
         max_chars: int = Query(8000, ge=1, le=20000), principal: Principal = Depends(require_principal),
         service: JobApplication = Depends(get_job_application)):
    """Read untrusted document text from one original page. Offsets count Unicode code points.

    Follow next_page/next_offset, including empty pages. Truncated means more text on this page.
    Missing historical or expired results return 409; corrupted results return 503.
    """
    return service.text(principal, job_id, page, offset, max_chars)


REPORT_EXAMPLE = {
    "schema_version": "1.0", "job_id": JOB_EXAMPLE["job_id"], "generated_at": "2026-10-15T12:00:00Z",
    "duration_ms": 42, "input": {"sha256": "0" * 64, "size_bytes": 1024},
    "output": {"sha256": "1" * 64, "size_bytes": 1024}, "page_count": 1,
    "pages": [{"page": 1, "character_count": 0, "source": "pdf_text",
               "digest": {"sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", "size_bytes": 0}}],
    "quality": {"label": "LOW", "method": "token-plausibility-v1", "character_count": 0,
                "valid_ratio": 0, "min_text": 40, "valid_ratio_threshold": 0.7, "is_accuracy_measure": False},
    "stages": [{"name": "primary", "duration_ms": 20}], "engines": {"pypdf": None},
    "fallback_attempted": False, "used_fallback": False,
    "output_validation": {"requested_format": "pdfa", "pdf_readable": True,
                          "page_count_matches": True, "pdfa_conformance": "not_validated"},
    "warnings": ["quality_is_heuristic_not_accuracy", "pdfa_conformance_not_validated",
                 "primary_ocr_unavailable", "fallback_ocr_unavailable", "no_text_extracted_blankness_not_determined"],
}


@router.get("/jobs/{job_id}/report", response_model=JobReport, operation_id="ocr_get_report",
            openapi_extra=operation("jobs:read", "artifacts:read"), responses=example(REPORT_EXAMPLE))
def report(job_id: str, principal: Principal = Depends(require_principal),
           service: JobApplication = Depends(get_job_application)):
    """Read persisted checksums, page provenance, timings and heuristic quality; no accuracy or PDF/A certification."""
    return service.report(principal, job_id)


@router.get("/metrics", response_model=MetricsResponse, operation_id="ocr_get_metrics", openapi_extra=operation("jobs:read"),
            responses=example({"total_jobs": 1, "by_status": {"queued": 1}}))
def metrics(principal: Principal = Depends(require_principal), service: JobApplication = Depends(get_job_application)):
    """Count only caller-owned jobs by processing state."""
    return service.metrics(principal)


@router.get("/jobs/{job_id}", response_model=JobResponse, operation_id="ocr_get_job", openapi_extra=operation("jobs:read"),
            responses=example(JOB_EXAMPLE))
def get_job(job_id: str, principal: Principal = Depends(require_principal), service: JobApplication = Depends(get_job_application)):
    """Read a caller-owned job. Unknown and inaccessible IDs both return job_not_found."""
    return service.get_job(principal, job_id)


@router.get("/jobs/{job_id}/artifact", response_model=ArtifactResponse, operation_id="ocr_get_artifact",
            openapi_extra=operation("jobs:read", "artifacts:read"),
            responses=example({"job_id": JOB_EXAMPLE["job_id"], "available": False, "media_type": "application/pdf", "download_url": None}))
def artifact(job_id: str, principal: Principal = Depends(require_principal), service: JobApplication = Depends(get_job_application)):
    """Return availability and a relative authenticated download URL, without PDF bytes."""
    return service.artifact(principal, job_id)


@router.get("/jobs/{job_id}/artifact/content", operation_id="ocr_download_artifact",
            openapi_extra=operation("jobs:read", "artifacts:read"), response_class=FileResponse,
            responses={**error_responses(400, 416), **{status: {"content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}}
                                                      for status in (200, 206)}})
def artifact_content(job_id: str, principal: Principal = Depends(require_principal), service: JobApplication = Depends(get_job_application)):
    """Download the caller-owned PDF; supports Range. Returns 409 if the output is unavailable."""
    return FileResponse(service.artifact_file(principal, job_id), filename=f"{job_id}-searchable.pdf", media_type="application/pdf")
