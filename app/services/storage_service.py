from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Dict
from uuid import uuid4

from app.config import settings
from app.models import JobResponse, JobStatus, QualityLabel


@dataclass
class JobRecord:
    job_id: str
    filename: str
    working_dir: Path
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    input_pdf_path: Path
    output_pdf_path: Path | None = None
    quality: QualityLabel | None = None
    error: str | None = None

    def to_response(self) -> JobResponse:
        download_url = f"/api/jobs/{self.job_id}/download" if self.status == "completed" and self.output_pdf_path else None
        return JobResponse(
            job_id=self.job_id,
            filename=self.filename,
            status=self.status,
            created_at=self.created_at,
            updated_at=self.updated_at,
            quality=self.quality,
            download_url=download_url,
            error=self.error,
        )


@dataclass
class JobStore:
    root_dir: Path = field(default_factory=lambda: settings.ocr_tmp_dir)
    jobs: Dict[str, JobRecord] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def __post_init__(self) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def create(self, filename: str, payload: bytes) -> JobRecord:
        job_id = uuid4().hex
        working_dir = self.root_dir / job_id
        working_dir.mkdir(parents=True, exist_ok=True)
        input_pdf_path = working_dir / "original.pdf"
        input_pdf_path.write_bytes(payload)
        now = datetime.now(timezone.utc)
        job = JobRecord(
            job_id=job_id,
            filename=filename,
            working_dir=working_dir,
            status="queued",
            created_at=now,
            updated_at=now,
            input_pdf_path=input_pdf_path,
        )
        with self._lock:
            self.jobs[job_id] = job
        return job

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self.jobs.get(job_id)

    def update(self, job_id: str, **kwargs: object) -> JobRecord:
        with self._lock:
            job = self.jobs[job_id]
            for key, value in kwargs.items():
                setattr(job, key, value)
            job.updated_at = datetime.now(timezone.utc)
            return job

    def cleanup(self, job_id: str) -> None:
        job = self.get(job_id)
        if job:
            shutil.rmtree(job.working_dir, ignore_errors=True)


_job_store = JobStore()


def get_job_store() -> JobStore:
    return _job_store
