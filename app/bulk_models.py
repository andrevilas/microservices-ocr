"""Bounded, passive batch/export contracts shared by HTTP and MCP."""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.models import JobStatus
from app.options import ProcessingOptions

ResourceId = Annotated[str, Field(pattern="^[0-9a-f]{32}$")]


class BatchInputItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_item_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")
    upload_id: ResourceId
    options: ProcessingOptions = Field(default_factory=ProcessingOptions)


class BatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"items": [{"client_item_id": "item-1", "upload_id": "0" * 32, "options": {"processing_mode": "extract_text"}}]}]})
    items: list[BatchInputItem] = Field(min_length=1, max_length=25)

    @model_validator(mode="after")
    def unique_items(self):
        if len({i.client_item_id for i in self.items}) != len(self.items):
            raise ValueError("client_item_id must be unique within a batch")
        return self


class BatchItem(BaseModel):
    client_item_id: str
    upload_id: str | None
    job_id: str
    filename: str
    status: JobStatus = "queued"
    progress_percent: int = 0
    error_code: str | None = None


class BatchReceipt(BaseModel):
    batch_id: str
    created_at: datetime
    admission_policy: Literal["all_or_nothing"] = "all_or_nothing"
    items: list[BatchItem]
    poll_after_seconds: int = 2


class BatchView(BatchReceipt):
    counts: dict[str, int]
    progress_percent: int
    terminal: bool


class BatchPage(BaseModel):
    items: list[BatchView]
    next_cursor: str | None = None


class ExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"job_ids": ["0" * 32]}]})
    job_ids: list[ResourceId] = Field(min_length=1, max_length=25)

    @model_validator(mode="after")
    def unique_jobs(self):
        if len(set(self.job_ids)) != len(self.job_ids):
            raise ValueError("job_ids must be unique")
        return self


class ExportItem(BaseModel):
    job_id: str
    filename: str
    status: JobStatus
    included: bool
    archive_name: str | None = None
    reason: Literal["job_not_completed", "artifact_unavailable"] | None = None
    size_bytes: int | None = None
    sha256: str | None = None


class ExportReceipt(BaseModel):
    export_id: str
    created_at: datetime
    expires_at: datetime | None = None
    status: Literal["queued", "processing", "ready", "failed"] = "queued"
    media_type: Literal["application/zip"] = "application/zip"
    size_bytes: int | None = None
    sha256: str | None = None
    download_url: str | None = None
    items: list[ExportItem] = Field(default_factory=list)
    error_code: str | None = None
    poll_after_seconds: int = 2
