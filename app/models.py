from datetime import datetime
from typing import Literal

from pydantic import BaseModel


JobStatus = Literal["queued", "processing", "completed", "failed"]
QualityLabel = Literal["HIGH", "LOW"]


class UploadResponse(BaseModel):
    job_id: str
    status: JobStatus


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
