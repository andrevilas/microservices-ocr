"""Passive API/MCP control contracts, safe to import in a stateless stdio client."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.bulk_models import ResourceId
from app.models import JobStatus
from app.options import ProcessingOptions


class CancelResult(BaseModel):
    job_id: ResourceId
    status: JobStatus | None = None
    outcome: Literal["canceled", "requested", "already_terminal", "not_found"]
    poll_after_seconds: int = 1


class CancelInput(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"job_ids": ["0" * 32]}]})
    job_ids: list[ResourceId] = Field(min_length=1, max_length=25)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.job_ids)) != len(self.job_ids):
            raise ValueError("Duplicate job IDs.")
        return self


class CancelPage(BaseModel):
    items: list[CancelResult]


class RetryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{}]})
    options: ProcessingOptions | None = None


class RetryReceipt(BaseModel):
    job_id: ResourceId
    parent_job_id: ResourceId
    status: Literal["queued"] = "queued"
    poll_after_seconds: int = 2
