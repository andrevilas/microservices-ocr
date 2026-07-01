import time
from collections import defaultdict
from io import BytesIO
from pathlib import Path
from threading import Lock
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pypdf import PdfReader

from app.config import settings
from app.models import BatchUploadJob, BatchUploadResponse, JobResponse, QueueClearResponse, UploadResponse
from app.services.job_queue import JobQueueProcessor, get_job_queue_processor
from app.services.ocr_orchestrator import OcrOrchestrator
from app.routes.auth import get_current_user
from app.services.storage_service import JobStore, get_job_store

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# ---------------------------------------------------------------------------
# In-memory rate limiter (per-IP, sliding window of 60s)
# ---------------------------------------------------------------------------

_rate_lock = Lock()
_rate_buckets: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(request: Request) -> None:
    """Raise 429 if upload_rate_limit_per_minute > 0 and IP exceeded the limit."""
    limit = settings.upload_rate_limit_per_minute
    if limit <= 0:
        return

    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    window_start = now - 60.0

    with _rate_lock:
        bucket = _rate_buckets[client_ip]
        # Prune old entries
        _rate_buckets[client_ip] = bucket = [ts for ts in bucket if ts > window_start]
        if len(bucket) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Upload rate limit exceeded. Try again later.",
            )
        bucket.append(now)


# ---------------------------------------------------------------------------
# JWT or optional API key authentication
# ---------------------------------------------------------------------------


def _require_auth_or_api_key(request: Request) -> None:
    """Allow access if valid API key OR valid JWT token is present."""
    # API key takes priority for automation clients
    if settings.api_key:
        provided = request.headers.get("X-API-Key", "")
        if provided == settings.api_key:
            return
    # Fall back to JWT authentication
    user = get_current_user(request)
    if user is not None:
        return
    # If API key is configured but not provided, and no JWT
    if settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.",
        )
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required.",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def get_orchestrator(job_store: JobStore = Depends(get_job_store)) -> OcrOrchestrator:
    return OcrOrchestrator(job_store=job_store)


async def _validate_pdf_upload(file: UploadFile) -> tuple[str, bytes]:
    if file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF uploads are supported.")

    filename = file.filename or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file must have a .pdf extension.")

    # Read in chunks to enforce size limit without loading unbounded data
    max_size_bytes = settings.max_upload_size_mb * 1024 * 1024
    chunks: list[bytes] = []
    total_read = 0
    chunk_size = 256 * 1024  # 256 KB

    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total_read += len(chunk)
        if total_read > max_size_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds the limit of {settings.max_upload_size_mb} MB.",
            )
        chunks.append(chunk)

    payload = b"".join(chunks)

    # Validate PDF magic bytes
    if not payload[:5] == b"%PDF-":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File does not appear to be a valid PDF (invalid magic bytes).",
        )

    # Minimal structural validation with pypdf
    try:
        reader = PdfReader(BytesIO(payload))
        if len(reader.pages) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File is not a valid PDF (zero pages).",
            )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File is not a valid PDF (parsing failed).",
        )

    return Path(filename).name, payload


def _schedule_job(
    processor: JobQueueProcessor,
    orchestrator: OcrOrchestrator,
    filename: str,
    payload: bytes,
) -> UploadResponse:
    job = orchestrator.create_job(filename=filename, payload=payload)
    processor.enqueue(job.job_id)
    return UploadResponse(job_id=job.job_id, status=job.status)


# ---------------------------------------------------------------------------
# Public endpoints (no API key required)
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    user = get_current_user(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"app_name": settings.app_name, "user": user},
    )


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/metrics")
async def metrics(
    _auth: None = Depends(_require_auth_or_api_key),
    job_store: JobStore = Depends(get_job_store),
) -> dict:
    """Lightweight JSON metrics: job counts by status and queue size."""
    jobs = job_store.list_all()
    counts: dict[str, int] = {}
    for job in jobs:
        counts[job.status] = counts.get(job.status, 0) + 1
    return {
        "total_jobs": len(jobs),
        "by_status": counts,
    }


@router.get("/readiness")
async def readiness(job_store: JobStore = Depends(get_job_store)) -> Response:
    tmp_dir = settings.ocr_tmp_dir
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        if not tmp_dir.is_dir():
            raise OSError("Not a directory")
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Temporary directory is not available.",
        )
    if not job_store.check_db():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job database is not available.",
        )
    return Response(
        content='{"status":"ready"}',
        media_type="application/json",
        status_code=status.HTTP_200_OK,
    )


# ---------------------------------------------------------------------------
# Protected endpoints (require API key when configured)
# ---------------------------------------------------------------------------


@router.post("/api/jobs", response_model=UploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    file: UploadFile = File(...),
    _auth: None = Depends(_require_auth_or_api_key),
    _rate: None = Depends(_check_rate_limit),
    processor: JobQueueProcessor = Depends(get_job_queue_processor),
    orchestrator: OcrOrchestrator = Depends(get_orchestrator),
) -> UploadResponse:
    filename, payload = await _validate_pdf_upload(file)
    return _schedule_job(processor, orchestrator, filename, payload)


@router.post("/api/jobs/batch", response_model=BatchUploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_batch_jobs(
    files: list[UploadFile] = File(...),
    _auth: None = Depends(_require_auth_or_api_key),
    _rate: None = Depends(_check_rate_limit),
    processor: JobQueueProcessor = Depends(get_job_queue_processor),
    orchestrator: OcrOrchestrator = Depends(get_orchestrator),
) -> BatchUploadResponse:
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one PDF is required.")

    if len(files) > settings.max_batch_files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Batch upload limited to {settings.max_batch_files} files.",
        )

    jobs: list[BatchUploadJob] = []
    for file in files:
        filename, payload = await _validate_pdf_upload(file)
        scheduled = _schedule_job(processor, orchestrator, filename, payload)
        jobs.append(BatchUploadJob(job_id=scheduled.job_id, filename=filename, status=scheduled.status))

    return BatchUploadResponse(jobs=jobs)


@router.get("/api/jobs/download-batch")
async def download_batch_results(
    job_ids: list[str] = Query(...),
    _auth: None = Depends(_require_auth_or_api_key),
    job_store: JobStore = Depends(get_job_store),
) -> Response:
    if not job_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one job_id is required.")

    completed_jobs = []
    for job_id in job_ids:
        job = job_store.get(job_id)
        if job and job.status == "completed" and job.output_pdf_path:
            completed_jobs.append(job)

    if not completed_jobs:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No completed jobs are available for batch download.")

    archive_buffer = BytesIO()
    with ZipFile(archive_buffer, mode="w", compression=ZIP_DEFLATED) as archive:
        for job in completed_jobs:
            archive.write(job.output_pdf_path, arcname=f"{Path(job.filename).stem}-searchable.pdf")
    archive_buffer.seek(0)

    headers = {"Content-Disposition": 'attachment; filename="ocr-results-batch.zip"'}
    return StreamingResponse(archive_buffer, media_type="application/zip", headers=headers)


@router.post("/api/jobs/clear-queue", response_model=QueueClearResponse)
async def clear_queue(
    _auth: None = Depends(_require_auth_or_api_key),
    processor: JobQueueProcessor = Depends(get_job_queue_processor),
) -> QueueClearResponse:
    cleared_count, processing_count = processor.clear_pending_jobs()
    return QueueClearResponse(cleared_count=cleared_count, processing_count=processing_count)


@router.get("/api/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, _auth: None = Depends(_require_auth_or_api_key), job_store: JobStore = Depends(get_job_store)) -> JobResponse:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return job.to_response()


@router.get("/api/jobs/{job_id}/download")
async def download_result(job_id: str, _auth: None = Depends(_require_auth_or_api_key), job_store: JobStore = Depends(get_job_store)) -> Response:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    if job.status != "completed" or not job.output_pdf_path:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job is not ready for download.")
    return FileResponse(job.output_pdf_path, filename=f"{Path(job.filename).stem}-searchable.pdf", media_type="application/pdf")
