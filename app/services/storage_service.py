from __future__ import annotations

import logging
import shutil
import sqlite3
from dataclasses import dataclass, field, fields as dc_fields
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Dict, get_args
from uuid import uuid4

from app.config import settings
from app.models import JobResponse, JobStatus, QualityLabel

_VALID_STATUSES: frozenset[str] = frozenset(get_args(JobStatus))
_VALID_QUALITIES: frozenset[str] = frozenset(get_args(QualityLabel))

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowed fields for update (all mutable fields of JobRecord minus identifiers)
# ---------------------------------------------------------------------------
_JOB_RECORD_FIELDS: frozenset[str] | None = None


def _allowed_update_fields() -> frozenset[str]:
    global _JOB_RECORD_FIELDS
    if _JOB_RECORD_FIELDS is None:
        _JOB_RECORD_FIELDS = frozenset(f.name for f in dc_fields(JobRecord)) - {"job_id", "created_at", "owner_user_id"}
    return _JOB_RECORD_FIELDS


@dataclass
class JobRecord:
    job_id: str
    filename: str
    working_dir: Path
    status: JobStatus
    owner_user_id: int | None
    progress_percent: int
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
            progress_percent=self.progress_percent,
            created_at=self.created_at,
            updated_at=self.updated_at,
            quality=self.quality,
            download_url=download_url,
            error=self.error,
        )


# ---------------------------------------------------------------------------
# SQLite-backed JobStore
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    filename        TEXT NOT NULL,
    working_dir     TEXT NOT NULL,
    status          TEXT NOT NULL,
    owner_user_id   INTEGER,
    progress_percent INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    input_pdf_path  TEXT NOT NULL,
    output_pdf_path TEXT,
    quality         TEXT,
    error           TEXT
);
"""


@dataclass
class JobStore:
    root_dir: Path = field(default_factory=lambda: settings.ocr_tmp_dir)
    jobs: Dict[str, JobRecord] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)
    _db_path: Path | None = field(default=None, repr=False)
    _conn: sqlite3.Connection | None = field(default=None, repr=False)

    # -- lifecycle -----------------------------------------------------------

    def __post_init__(self) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        if self._db_path is None:
            self._db_path = self.root_dir / "jobs.sqlite3"
        self._init_db()
        self._load_from_db()

    @property
    def db_path(self) -> Path:
        assert self._db_path is not None
        return self._db_path

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=10)
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.execute(_CREATE_TABLE_SQL)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "owner_user_id" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN owner_user_id INTEGER")
        if "progress_percent" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN progress_percent INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    def _load_from_db(self) -> None:
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM jobs").fetchall()
        col_names = [d[0] for d in conn.execute("SELECT * FROM jobs LIMIT 0").description or []]
        if not col_names:
            col_names = [
                "job_id", "filename", "working_dir", "status",
                "owner_user_id", "progress_percent",
                "created_at", "updated_at", "input_pdf_path",
                "output_pdf_path", "quality", "error",
            ]
        for row in rows:
            data = dict(zip(col_names, row))
            record = self._row_to_record(data)
            self.jobs[record.job_id] = record

    @staticmethod
    def _row_to_record(data: dict) -> JobRecord:
        return JobRecord(
            job_id=data["job_id"],
            filename=data["filename"],
            working_dir=Path(data["working_dir"]),
            status=data["status"],
            owner_user_id=data.get("owner_user_id"),
            progress_percent=int(data.get("progress_percent") or 0),
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            input_pdf_path=Path(data["input_pdf_path"]),
            output_pdf_path=Path(data["output_pdf_path"]) if data.get("output_pdf_path") else None,
            quality=data.get("quality"),
            error=data.get("error"),
        )

    def _persist(self, job: JobRecord) -> None:
        conn = self._get_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO jobs
                (job_id, filename, working_dir, status, owner_user_id, progress_percent, created_at, updated_at,
                 input_pdf_path, output_pdf_path, quality, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.job_id,
                job.filename,
                str(job.working_dir),
                job.status,
                job.owner_user_id,
                job.progress_percent,
                job.created_at.isoformat(),
                job.updated_at.isoformat(),
                str(job.input_pdf_path),
                str(job.output_pdf_path) if job.output_pdf_path else None,
                job.quality,
                job.error,
            ),
        )
        conn.commit()

    # -- public API (same semantics) -----------------------------------------

    def create(self, filename: str, payload: bytes, owner_user_id: int | None = None) -> JobRecord:
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
            owner_user_id=owner_user_id,
            progress_percent=0,
            created_at=now,
            updated_at=now,
            input_pdf_path=input_pdf_path,
        )
        with self._lock:
            self.jobs[job_id] = job
            self._persist(job)
        return job

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self.jobs.get(job_id)

    def list_all(self) -> list[JobRecord]:
        with self._lock:
            return list(self.jobs.values())

    def list_for_owner(self, owner_user_id: int | None) -> list[JobRecord]:
        if owner_user_id is None:
            return self.list_all()
        with self._lock:
            return [job for job in self.jobs.values() if job.owner_user_id == owner_user_id]

    def update(self, job_id: str, **kwargs: object) -> JobRecord:
        allowed = _allowed_update_fields()
        bad_keys = set(kwargs.keys()) - allowed
        if bad_keys:
            raise ValueError(f"Unknown or immutable fields: {', '.join(sorted(bad_keys))}")
        if "status" in kwargs and kwargs["status"] not in _VALID_STATUSES:
            raise ValueError(
                f"Invalid status {kwargs['status']!r}; must be one of {sorted(_VALID_STATUSES)}"
            )
        if "quality" in kwargs and kwargs["quality"] is not None and kwargs["quality"] not in _VALID_QUALITIES:
            raise ValueError(
                f"Invalid quality {kwargs['quality']!r}; must be one of {sorted(_VALID_QUALITIES)} or None"
            )
        if "progress_percent" in kwargs:
            progress = kwargs["progress_percent"]
            if not isinstance(progress, int) or progress < 0 or progress > 100:
                raise ValueError("progress_percent must be an integer between 0 and 100")
        with self._lock:
            job = self.jobs[job_id]
            for key, value in kwargs.items():
                setattr(job, key, value)
            job.updated_at = datetime.now(timezone.utc)
            self._persist(job)
            return job

    def cleanup(self, job_id: str) -> None:
        job = self.get(job_id)
        if job:
            shutil.rmtree(job.working_dir, ignore_errors=True)
            with self._lock:
                if job.output_pdf_path is not None:
                    job.output_pdf_path = None
                    self._persist(job)

    # -- TTL cleanup ---------------------------------------------------------

    def cleanup_expired(self, retention_seconds: int | None = None) -> list[str]:
        """Remove files for completed/failed/canceled jobs older than TTL.

        Returns list of cleaned job_ids. DB records are preserved but
        output_pdf_path is set to None when files are removed.
        """
        ttl = retention_seconds if retention_seconds is not None else settings.job_retention_seconds
        now = datetime.now(timezone.utc)
        cleaned: list[str] = []
        with self._lock:
            for job in list(self.jobs.values()):
                if job.status not in ("completed", "failed", "canceled"):
                    continue
                age = (now - job.updated_at).total_seconds()
                if age < ttl:
                    continue
                # Remove filesystem artifacts
                if job.working_dir.exists():
                    shutil.rmtree(job.working_dir, ignore_errors=True)
                job.output_pdf_path = None
                job.updated_at = now
                self._persist(job)
                cleaned.append(job.job_id)
        return cleaned

    # -- recovery helpers ----------------------------------------------------

    def get_processing_jobs(self) -> list[JobRecord]:
        with self._lock:
            return [j for j in self.jobs.values() if j.status == "processing"]

    def get_queued_jobs(self) -> list[JobRecord]:
        with self._lock:
            return [j for j in self.jobs.values() if j.status == "queued"]

    # -- DB connectivity check -----------------------------------------------

    def check_db(self) -> bool:
        """Return True if the database is reachable and operational."""
        try:
            with self._lock:
                conn = self._get_conn()
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False


_job_store = JobStore()


def get_job_store() -> JobStore:
    return _job_store
