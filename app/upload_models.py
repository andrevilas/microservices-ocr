"""Passive upload/admission receipts shared by API and MCP clients."""
from typing import Literal
from pydantic import BaseModel


class UploadReceipt(BaseModel):
    upload_id: str
    filename: str
    state: Literal["reserved", "receiving", "ready", "expired"]
    expected_size: int | None
    size_bytes: int | None
    sha256: str | None
    page_count: int | None
    expires_at: float
    content_url: str


class JobAdmission(BaseModel):
    job_id: str
    upload_id: str
    status: Literal["queued"] = "queued"
    poll_after_seconds: int = 2


