"""Passive API response models, safe to import in clients."""
from typing import Literal
from pydantic import BaseModel
from app.models import JobResponse


class IdentityResponse(BaseModel):
    subject: str
    kind: Literal["user", "service", "legacy"] = "user"
    scopes: list[str]


class JobPage(BaseModel):
    items: list[JobResponse]
    next_cursor: str | None


class MetricsResponse(BaseModel):
    total_jobs: int
    by_status: dict[str, int]


class ArtifactResponse(BaseModel):
    job_id: str
    available: bool
    media_type: str = "application/pdf"
    download_url: str | None = None


