from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from app.application import (ArtifactResponse, IdentityResponse, JobApplication,
                             JobPage, MetricsResponse, Principal, get_job_application)
from app.models import JobResponse, JobStatus
from app.security import require_principal
from app.http_contract import error_responses, example, operation

router = APIRouter(prefix="/api/v1", tags=["API v1"],
                   responses=error_responses(401, 403, 404, 405, 409, 422, 500))
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
              status: JobStatus | None = None, principal: Principal = Depends(require_principal),
              service: JobApplication = Depends(get_job_application)):
    """List caller-owned jobs in creation order; pass next_cursor unchanged to continue.

    Limit is 1–100 (default 50). Results are live; concurrent inserts can appear on later pages.
    """
    return service.list_jobs(principal, limit, cursor, status)


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
