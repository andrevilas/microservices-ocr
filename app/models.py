from datetime import datetime
from typing import Literal

from pydantic import BaseModel


JobStatus = Literal["queued", "processing", "completed", "failed", "canceled"]
QualityLabel = Literal["HIGH", "LOW"]


class UploadResponse(BaseModel):
    job_id: str
    status: JobStatus


class BatchUploadJob(BaseModel):
    job_id: str
    filename: str
    status: JobStatus


class BatchUploadResponse(BaseModel):
    jobs: list[BatchUploadJob]


class QueueClearResponse(BaseModel):
    cleared_count: int
    processing_count: int


class JobResponse(BaseModel):
    job_id: str
    filename: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    quality: QualityLabel | None = None
    download_url: str | None = None
    error: str | None = None


class OCRResult(BaseModel):
    text: str
    quality: QualityLabel
    valid_ratio: float
    character_count: int
    output_pdf_path: str
    used_fallback: bool = False
