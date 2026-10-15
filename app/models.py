from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from app.options import EffectiveOptions, ProcessingOptions


JobStatus = Literal["queued", "processing", "completed", "failed", "canceled"]
QualityLabel = Literal["HIGH", "LOW"]


class UploadResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress_percent: int = 0


class BatchUploadJob(BaseModel):
    job_id: str
    filename: str
    status: JobStatus
    progress_percent: int = 0


class BatchUploadResponse(BaseModel):
    jobs: list[BatchUploadJob]
    batch_id: str | None = None
    dispatch_pending: bool = False


class QueueClearResponse(BaseModel):
    cleared_count: int
    processing_count: int


class JobResponse(BaseModel):
    job_id: str
    filename: str
    status: JobStatus
    progress_percent: int = 0
    created_at: datetime
    updated_at: datetime
    quality: QualityLabel | None = None
    download_url: str | None = None
    error: str | None = None
    error_code: str | None = None
    options_requested: ProcessingOptions | None = None
    options_effective: EffectiveOptions | None = None


class OCRResult(BaseModel):
    text: str
    quality: QualityLabel
    valid_ratio: float
    character_count: int
    output_pdf_path: str
    used_fallback: bool = False
