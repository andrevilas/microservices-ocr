"""Passive retention contracts shared by API and MCP."""
from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel


class RetentionInfo(BaseModel):
    state: Literal["active", "retained", "expired", "deleted"]
    expires_at: datetime | None = None
    unavailable_at: datetime | None = None
    purged_at: datetime | None = None
    purge_pending: bool = False


class DeletionReceipt(BaseModel):
    job_id: str
    retention: RetentionInfo


def retention_info(job):
    state = job.artifacts_removal_reason
    if state is None:
        state = ("expired" if job.artifacts_expires_at and job.artifacts_expires_at <= datetime.now(timezone.utc)
                 else "retained" if job.status in ("completed", "failed", "canceled") else "active")
    return RetentionInfo(state=state, expires_at=job.artifacts_expires_at,
                         unavailable_at=job.artifacts_removed_at or (job.artifacts_expires_at if state == "expired" else None),
                         purged_at=job.artifacts_purged_at,
                         purge_pending=state in ("expired", "deleted") and job.artifacts_purged_at is None)
