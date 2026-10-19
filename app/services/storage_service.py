from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field, fields as dc_fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Dict, get_args
from uuid import uuid4

from app.config import settings
from app.models import JobResponse, JobStatus, QualityLabel
from app.options import EffectiveOptions, ProcessingOptions
from app.retention_models import retention_info
from app.services.artifact_retention import ArtifactRetentionMixin, TERMINAL

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
        _JOB_RECORD_FIELDS = frozenset(f.name for f in dc_fields(JobRecord)) - {"job_id", "created_at", "owner_user_id", "owner_principal_id", "options_requested", "options_effective", "parent_job_id", "artifacts_expires_at", "artifacts_removed_at", "artifacts_removal_reason", "artifacts_purged_at"}
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
    owner_principal_id: str | None = None
    options_requested: ProcessingOptions | None = None
    options_effective: EffectiveOptions | None = None
    error_code: str | None = None
    parent_job_id: str | None = None
    artifacts_expires_at: datetime | None = None
    artifacts_removed_at: datetime | None = None
    artifacts_removal_reason: str | None = None
    artifacts_purged_at: datetime | None = None

    @property
    def effective_owner(self) -> str | None:
        return self.owner_principal_id or (f"user:{self.owner_user_id}" if self.owner_user_id is not None else None)

    def to_response(self) -> JobResponse:
        retention = retention_info(self)
        download_url = f"/api/jobs/{self.job_id}/download" if self.status == "completed" and self.output_pdf_path and retention.state == "retained" else None
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
            error_code=self.error_code,
            options_requested=self.options_requested,
            options_effective=self.options_effective,
            parent_job_id=self.parent_job_id,
            retention=retention,
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
class JobStore(ArtifactRetentionMixin):
    root_dir: Path = field(default_factory=lambda: settings.ocr_tmp_dir)
    jobs: Dict[str, JobRecord] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)
    _db_path: Path | None = field(default=None, repr=False)
    _conn: sqlite3.Connection | None = field(default=None, repr=False)
    _artifact_leases: dict[str, int] = field(default_factory=dict, repr=False)
    _sqlite_quarantined: bool = field(default=False, init=False, repr=False)

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

    def _quarantine_locked(self) -> None:
        """Caller holds _lock; only a fresh JobStore can restore availability."""
        self._sqlite_quarantined = True

    def _check_available(self) -> None:
        if self._sqlite_quarantined:
            from app.application import ApplicationError
            failure = ApplicationError(503, "metadata_unavailable", "Metadata is unavailable.")
            try:
                raise failure from None
            finally:
                failure.__context__ = None
                failure.__cause__ = None

    def _get_conn(self) -> sqlite3.Connection:
        self._check_available()
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
        if "owner_principal_id" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN owner_principal_id TEXT")
        for column in ("options_requested", "options_effective", "error_code", "parent_job_id", "artifacts_expires_at", "artifacts_removed_at", "artifacts_removal_reason", "artifacts_purged_at"):
            if column not in columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
        conn.execute("""CREATE TABLE IF NOT EXISTS artifact_audit (
            job_id TEXT NOT NULL, event TEXT NOT NULL, actor TEXT NOT NULL,
            reason TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(job_id,event))""")
        # Only known human ownership can be mapped; orphan records remain restricted.
        conn.execute("UPDATE jobs SET owner_principal_id='user:' || owner_user_id WHERE owner_principal_id IS NULL AND owner_user_id IS NOT NULL")
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
            if record.status in TERMINAL and record.artifacts_expires_at is None:
                self._persist(record)
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
            owner_principal_id=data.get("owner_principal_id"),
            options_requested=ProcessingOptions.model_validate_json(data["options_requested"]) if data.get("options_requested") else None,
            options_effective=EffectiveOptions.model_validate_json(data["options_effective"]) if data.get("options_effective") else None,
            error_code=data.get("error_code"),
            parent_job_id=data.get("parent_job_id"),
            artifacts_expires_at=datetime.fromisoformat(data["artifacts_expires_at"]) if data.get("artifacts_expires_at") else None,
            artifacts_removed_at=datetime.fromisoformat(data["artifacts_removed_at"]) if data.get("artifacts_removed_at") else None,
            artifacts_removal_reason=data.get("artifacts_removal_reason"),
            artifacts_purged_at=datetime.fromisoformat(data["artifacts_purged_at"]) if data.get("artifacts_purged_at") else None,
        )

    def _persist(self, job: JobRecord, *, commit: bool = True) -> None:
        conn = self._get_conn()
        if job.status in TERMINAL and job.artifacts_expires_at is None:
            job.artifacts_expires_at = job.updated_at + timedelta(seconds=settings.job_retention_seconds)
        conn.execute(
            """
            INSERT OR REPLACE INTO jobs
                (job_id, filename, working_dir, status, owner_user_id, progress_percent, created_at, updated_at,
                 input_pdf_path, output_pdf_path, quality, error, owner_principal_id, options_requested, options_effective, error_code, parent_job_id, artifacts_expires_at, artifacts_removed_at, artifacts_removal_reason, artifacts_purged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                job.owner_principal_id,
                job.options_requested.model_dump_json() if job.options_requested else None,
                job.options_effective.model_dump_json() if job.options_effective else None,
                job.error_code,
                job.parent_job_id,
                job.artifacts_expires_at.isoformat() if job.artifacts_expires_at else None,
                job.artifacts_removed_at.isoformat() if job.artifacts_removed_at else None,
                job.artifacts_removal_reason,
                job.artifacts_purged_at.isoformat() if job.artifacts_purged_at else None,
            ),
        )
        if commit:
            conn.commit()

    # -- public API (same semantics) -----------------------------------------

    def create(self, filename: str, payload: bytes, owner_user_id: int | None = None,
               *, owner_principal_id: str | None = None) -> JobRecord:
        if owner_user_id is not None:
            expected = f"user:{owner_user_id}"
            if owner_principal_id not in (None, expected):
                raise ValueError("Conflicting job owners.")
            owner_principal_id = expected
        elif owner_principal_id and owner_principal_id.startswith("user:"):
            raise ValueError("Human principal requires owner_user_id.")
        if owner_principal_id is not None and not re.fullmatch(r"(?:user:[1-9][0-9]*|service:[0-9a-f]{32}|legacy:api-key)", owner_principal_id):
            raise ValueError("Invalid owner principal.")
        with self._lock:
            self._check_available()
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
            owner_principal_id=owner_principal_id,
        )
        with self._lock:
            self._check_available()
            self._persist(job)
            self.jobs[job_id] = job
        return job

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self.jobs.get(job_id)

    def get_snapshot(self, job_id: str) -> JobRecord | None:
        with self._lock:
            record = self.jobs.get(job_id)
            return replace(record) if record else None

    def list_all(self) -> list[JobRecord]:
        with self._lock:
            return list(self.jobs.values())

    def snapshot(self) -> list[JobRecord]:
        """Copy records while locked so transport serialization sees a stable view."""
        with self._lock:
            return [replace(job) for job in self.jobs.values()]

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
            self._check_available()
            job = self.jobs[job_id]
            for key, value in kwargs.items():
                setattr(job, key, value)
            job.updated_at = datetime.now(timezone.utc)
            self._persist(job)
            return job

    def _transition_locked(self, job, **changes):
        record = replace(job, updated_at=datetime.now(timezone.utc), **changes)
        try:
            self._persist(record)
        except BaseException:
            self._get_conn().rollback()
            raise
        # Preserve legacy live references, publishing them only after durable commit.
        for name in (*changes, "updated_at", "artifacts_expires_at"):
            setattr(job, name, getattr(record, name))
        return replace(record)

    def claim(self, job_id):
        """Compare-and-set shared with cancel; a terminal job can never be claimed."""
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None or job.status != "queued":
                return None
            return self._transition_locked(job, status="processing", progress_percent=5, error=None, error_code=None)

    def cancellation_requested(self, job_id):
        with self._lock:
            return self.jobs[job_id].status in ("cancel_requested", "canceled")

    def worker_update(self, job_id, **changes):
        from app.services.cancellation import JobCancelled
        with self._lock:
            job = self.jobs[job_id]
            if job.status != "processing":
                raise JobCancelled()
            return self._transition_locked(job, **changes)

    def request_cancel(self, job_id, owner, *, queued_only=False):
        """Owner check and state decision happen under the same lock as claim/finish."""
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None or job.effective_owner != owner:
                return None
            if queued_only and job.status != "queued":
                return None
            if job.status == "queued":
                return self._transition_locked(job, status="canceled", error_code="job_canceled", error=None)
            if job.status == "processing" and not queued_only:
                return self._transition_locked(job, status="cancel_requested", error_code=None, error=None)
            return replace(job)

    def finish_cancellation(self, job_id):
        with self._lock:
            job = self.jobs[job_id]
            if job.status == "cancel_requested":
                return self._transition_locked(job, status="canceled", output_pdf_path=None,
                                               quality=None, error=None, error_code="job_canceled")
            return replace(job)

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


_job_store: JobStore | None = None
_store_init_lock = Lock()


def get_job_store() -> JobStore:
    global _job_store
    with _store_init_lock:
        if _job_store is None:
            _job_store = JobStore()
    return _job_store
