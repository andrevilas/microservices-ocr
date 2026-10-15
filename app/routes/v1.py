from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from app.application import (ArtifactResponse, ErrorResponse, IdentityResponse, JobApplication,
                             JobPage, MetricsResponse, Principal, get_job_application)
from app.models import JobResponse, JobStatus
from app.security import require_principal

router = APIRouter(prefix="/api/v1", tags=["API v1"], responses={
    code: {"model": ErrorResponse} for code in (401, 403, 404, 409, 422)
})


@router.get("/me", response_model=IdentityResponse, operation_id="ocr_whoami")
def whoami(principal: Principal = Depends(require_principal), service: JobApplication = Depends(get_job_application)):
    return service.whoami(principal)


@router.get("/jobs", response_model=JobPage, operation_id="ocr_list_jobs")
def list_jobs(limit: int = Query(50, ge=1, le=100), cursor: str | None = Query(None, max_length=256),
              status: JobStatus | None = None, principal: Principal = Depends(require_principal),
              service: JobApplication = Depends(get_job_application)):
    return service.list_jobs(principal, limit, cursor, status)


@router.get("/metrics", response_model=MetricsResponse, operation_id="ocr_get_metrics")
def metrics(principal: Principal = Depends(require_principal), service: JobApplication = Depends(get_job_application)):
    return service.metrics(principal)


@router.get("/jobs/{job_id}", response_model=JobResponse, operation_id="ocr_get_job")
def get_job(job_id: str, principal: Principal = Depends(require_principal), service: JobApplication = Depends(get_job_application)):
    return service.get_job(principal, job_id)


@router.get("/jobs/{job_id}/artifact", response_model=ArtifactResponse, operation_id="ocr_get_artifact")
def artifact(job_id: str, principal: Principal = Depends(require_principal), service: JobApplication = Depends(get_job_application)):
    return service.artifact(principal, job_id)


@router.get("/jobs/{job_id}/artifact/content", operation_id="ocr_download_artifact",
            response_class=FileResponse, responses={200: {"content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}}})
def artifact_content(job_id: str, principal: Principal = Depends(require_principal), service: JobApplication = Depends(get_job_application)):
    return FileResponse(service.artifact_file(principal, job_id), filename=f"{job_id}-searchable.pdf", media_type="application/pdf")
