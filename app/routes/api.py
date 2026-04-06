from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.models import BatchUploadJob, BatchUploadResponse, JobResponse, QueueClearResponse, UploadResponse
from app.services.job_queue import JobQueueProcessor, get_job_queue_processor
from app.services.ocr_orchestrator import OcrOrchestrator
from app.services.storage_service import JobStore, get_job_store

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def get_orchestrator(job_store: JobStore = Depends(get_job_store)) -> OcrOrchestrator:
    return OcrOrchestrator(job_store=job_store)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="index.html", context={"app_name": settings.app_name})


async def _validate_pdf_upload(file: UploadFile) -> tuple[str, bytes]:
    if file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF uploads are supported.")

    filename = file.filename or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file must have a .pdf extension.")

    payload = await file.read()
    max_size_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(payload) > max_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Upload exceeds the limit of {settings.max_upload_size_mb} MB.",
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


@router.post("/api/jobs", response_model=UploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    file: UploadFile = File(...),
    processor: JobQueueProcessor = Depends(get_job_queue_processor),
    orchestrator: OcrOrchestrator = Depends(get_orchestrator),
) -> UploadResponse:
    filename, payload = await _validate_pdf_upload(file)
    return _schedule_job(processor, orchestrator, filename, payload)


@router.post("/api/jobs/batch", response_model=BatchUploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_batch_jobs(
    files: list[UploadFile] = File(...),
    processor: JobQueueProcessor = Depends(get_job_queue_processor),
    orchestrator: OcrOrchestrator = Depends(get_orchestrator),
) -> BatchUploadResponse:
    if not files:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one PDF is required.")

    jobs: list[BatchUploadJob] = []
    for file in files:
        filename, payload = await _validate_pdf_upload(file)
        scheduled = _schedule_job(processor, orchestrator, filename, payload)
        jobs.append(BatchUploadJob(job_id=scheduled.job_id, filename=filename, status=scheduled.status))

    return BatchUploadResponse(jobs=jobs)


@router.get("/api/jobs/download-batch")
async def download_batch_results(
    job_ids: list[str] = Query(...),
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
    processor: JobQueueProcessor = Depends(get_job_queue_processor),
) -> QueueClearResponse:
    cleared_count, processing_count = processor.clear_pending_jobs()
    return QueueClearResponse(cleared_count=cleared_count, processing_count=processing_count)


@router.get("/api/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, job_store: JobStore = Depends(get_job_store)) -> JobResponse:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    return job.to_response()


@router.get("/api/jobs/{job_id}/download")
async def download_result(job_id: str, job_store: JobStore = Depends(get_job_store)) -> Response:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found.")
    if job.status != "completed" or not job.output_pdf_path:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Job is not ready for download.")
    return FileResponse(job.output_pdf_path, filename=f"{Path(job.filename).stem}-searchable.pdf", media_type="application/pdf")
