from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.models import JobResponse, UploadResponse
from app.services.ocr_orchestrator import OcrOrchestrator
from app.services.storage_service import JobStore, get_job_store

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def get_orchestrator(job_store: JobStore = Depends(get_job_store)) -> OcrOrchestrator:
    return OcrOrchestrator(job_store=job_store)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request=request, name="index.html", context={"app_name": settings.app_name})


@router.post("/api/jobs", response_model=UploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    orchestrator: OcrOrchestrator = Depends(get_orchestrator),
) -> UploadResponse:
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

    job = orchestrator.create_job(filename=Path(filename).name, payload=payload)
    background_tasks.add_task(orchestrator.process_job, job.job_id)
    return UploadResponse(job_id=job.job_id, status=job.status)


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
