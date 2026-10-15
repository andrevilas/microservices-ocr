"""Persistent admission under the single-runtime lease, shared by HTTP and MCP adapters."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel

from app.application import ApplicationError, Principal
from app.config import settings
from app.services.storage_service import JobRecord, JobStore, get_job_store

CHUNK = 256 * 1024
MIB = 1024 * 1024


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


def filename_checked(filename: str) -> str:
    if not isinstance(filename, str) or not 1 <= len(filename) <= 255 or any(ord(c) < 32 for c in filename) or any(c in filename for c in '/\\:') or not filename.lower().endswith(".pdf"):
        raise ApplicationError(422, "invalid_filename", "Use a PDF filename without a path, at most 255 characters.")
    return filename


def validate_pdf(path: Path) -> int:
    command = [sys.executable, "-m", "app.pdf_validation", str(path), str(settings.upload_validation_memory_mb * MIB),
               str(min(settings.upload_max_pages, 300)), str(settings.upload_max_page_pixels)]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=settings.upload_validation_timeout_seconds,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        raise ApplicationError(422, "pdf_validation_limit", "PDF validation exceeded its time limit.") from None
    if result.returncode != 0:
        raise ApplicationError(422, "pdf_validation_limit", "PDF validation failed within its resource limits.")
    try:
        data = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError):
        raise ApplicationError(503, "validation_unavailable", "PDF validation is unavailable.") from None
    if "error" in data:
        code = data["error"]
        raise ApplicationError(503 if code == "validation_unavailable" else 422, code, "PDF could not be accepted within the configured validation limits.")
    return int(data["pages"])


class UploadService:
    def __init__(self, jobs: JobStore):
        self.jobs = jobs
        self.root = jobs.root_dir / "uploads"
        self.admissions = jobs.root_dir / "admitted-jobs"
        self.root.mkdir(exist_ok=True)
        self.admissions.mkdir(exist_ok=True)
        self.slots = BoundedSemaphore(settings.upload_concurrency)
        with jobs._lock, jobs._get_conn():
            jobs._get_conn().executescript("""
                CREATE TABLE IF NOT EXISTS uploads (
                    upload_id TEXT PRIMARY KEY, owner TEXT NOT NULL, filename TEXT NOT NULL,
                    state TEXT NOT NULL, expected_size INTEGER, expected_sha256 TEXT,
                    reserved_bytes INTEGER NOT NULL, inflight_bytes INTEGER NOT NULL DEFAULT 0,
                    size_bytes INTEGER, sha256 TEXT, page_count INTEGER, expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS uploads_owner ON uploads(owner);
                CREATE TABLE IF NOT EXISTS job_admissions (
                    owner TEXT NOT NULL, key_hash TEXT NOT NULL, request_hash TEXT NOT NULL,
                    job_id TEXT NOT NULL, response_json TEXT NOT NULL,
                    PRIMARY KEY(owner, key_hash), FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                );
            """)
            # Called once after acquiring the runtime lease, before workers start.
            jobs._get_conn().execute("UPDATE uploads SET state=CASE WHEN sha256 IS NULL THEN 'reserved' ELSE 'ready' END, inflight_bytes=0 WHERE state='receiving'")
            for (upload_id,) in jobs._get_conn().execute("SELECT upload_id FROM uploads WHERE state='reserved' AND sha256 IS NULL").fetchall():
                (self.root / f"{upload_id}.pdf").unlink(missing_ok=True)
            for path in self.root.iterdir():
                if re.fullmatch(r"[0-9a-f]{32}\.part", path.name):
                    path.unlink(missing_ok=True)
            # A crash between file rename and DB commit can only leave our own input file.
            for directory in self.admissions.iterdir():
                if (re.fullmatch(r"[0-9a-f]{32}", directory.name)
                        and directory.resolve() == self.admissions.resolve() / directory.name
                        and not jobs._get_conn().execute("SELECT 1 FROM jobs WHERE job_id=?", (directory.name,)).fetchone()):
                    (directory / "original.pdf").unlink(missing_ok=True)
                    directory.rmdir()
            self._expire()

    def _row(self, upload_id: str):
        cursor = self.jobs._get_conn().execute("SELECT * FROM uploads WHERE upload_id=?", (upload_id,))
        row = cursor.fetchone()
        return dict(zip([c[0] for c in cursor.description], row)) if row else None

    def _owned(self, principal, upload_id):
        row = self._row(upload_id)
        if row is None or row["owner"] != principal.subject:
            raise ApplicationError(404, "upload_not_found", "Upload not found.")
        if row["expires_at"] <= time.time() or row["state"] == "expired":
            raise ApplicationError(410, "upload_expired", "Upload has expired.")
        return row

    @staticmethod
    def _receipt(row):
        fields = {name: row[name] for name in UploadReceipt.model_fields if name != "content_url"}
        return UploadReceipt(**fields, content_url=f"/api/v1/uploads/{row['upload_id']}/content")

    def _expire(self):
        conn = self.jobs._get_conn()
        for (upload_id,) in conn.execute("SELECT upload_id FROM uploads WHERE expires_at<=? AND state NOT IN ('receiving','expired')", (time.time(),)).fetchall():
            (self.root / f"{upload_id}.pdf").unlink(missing_ok=True)
            conn.execute("UPDATE uploads SET state='expired', reserved_bytes=0 WHERE upload_id=?", (upload_id,))

    def _capacity(self, owner, extra):
        self._expire()
        conn = self.jobs._get_conn()
        total, own = conn.execute("SELECT COALESCE(SUM(reserved_bytes+inflight_bytes),0), COALESCE(SUM(CASE WHEN owner=? THEN reserved_bytes+inflight_bytes ELSE 0 END),0) FROM uploads", (owner,)).fetchone()
        for job in self.jobs.jobs.values():
            size = job.input_pdf_path.stat().st_size if job.input_pdf_path.is_file() else 0
            total += size
            if job.effective_owner == owner:
                own += size
        if total + extra > settings.upload_storage_limit_mb * MIB or own + extra > settings.upload_owner_limit_mb * MIB:
            raise ApplicationError(429, "storage_quota_exceeded", "Input storage quota is exhausted.")
        unwritten = conn.execute("SELECT COALESCE(SUM(CASE WHEN state IN ('reserved','receiving') THEN reserved_bytes ELSE 0 END + inflight_bytes),0) FROM uploads").fetchone()[0]
        if shutil.disk_usage(self.root).free < unwritten + extra + settings.upload_min_free_mb * MIB:
            raise ApplicationError(503, "storage_unavailable", "Insufficient free disk space.")

    def reserve(self, principal: Principal, filename: str, size: int | None, checksum: str | None = None):
        principal.require("jobs:submit")
        filename_checked(filename)
        maximum = settings.max_upload_size_mb * MIB
        if size is not None and (type(size) is not int or size <= 0 or size > maximum):
            raise ApplicationError(413, "file_too_large", "Upload size is outside the accepted range.")
        if checksum is not None and not re.fullmatch("[0-9a-f]{64}", checksum):
            raise ApplicationError(422, "invalid_checksum", "Expected checksum must be a lowercase SHA-256 digest.")
        with self.jobs._lock, self.jobs._get_conn():
            self._capacity(principal.subject, size or maximum)
            count = self.jobs._get_conn().execute("SELECT COUNT(*) FROM uploads WHERE owner=? AND state!='expired'", (principal.subject,)).fetchone()[0]
            if count >= settings.upload_owner_max_reservations:
                raise ApplicationError(429, "upload_quota_exceeded", "Too many active upload reservations.")
            upload_id = uuid4().hex
            self.jobs._get_conn().execute("INSERT INTO uploads (upload_id,owner,filename,state,expected_size,expected_sha256,reserved_bytes,expires_at) VALUES (?,?,?,'reserved',?,?,?,?)",
                                          (upload_id, principal.subject, filename, size, checksum, size or maximum, time.time() + settings.upload_ttl_seconds))
            return self._receipt(self._row(upload_id))

    def get(self, principal, upload_id):
        principal.require("jobs:submit")
        with self.jobs._lock:
            with self.jobs._get_conn():
                self._expire()
            return self._receipt(self._owned(principal, upload_id))

    def begin(self, principal, upload_id):
        principal.require("jobs:submit")
        if not self.slots.acquire(blocking=False):
            raise ApplicationError(429, "upload_busy", "Concurrent upload limit reached.")
        try:
            with self.jobs._lock, self.jobs._get_conn():
                row = self._owned(principal, upload_id)
                if row["state"] == "receiving":
                    raise ApplicationError(409, "upload_in_progress", "Upload is already receiving content.")
                extra = (row["size_bytes"] or 0) if row["state"] == "ready" else 0
                self._capacity(principal.subject, extra)
                self.jobs._get_conn().execute("UPDATE uploads SET state='receiving', inflight_bytes=? WHERE upload_id=?", (extra, upload_id))
                session = UploadSession(self, row)
            return session
        except BaseException:
            self.slots.release()
            raise

    def discard(self, principal, upload_id):
        """Remove a failed convenience upload whose ID was never delivered."""
        with self.jobs._lock, self.jobs._get_conn():
            row = self._row(upload_id)
            if row and row["owner"] == principal.subject and row["state"] != "receiving":
                (self.root / f"{upload_id}.pdf").unlink(missing_ok=True)
                self.jobs._get_conn().execute("DELETE FROM uploads WHERE upload_id=?", (upload_id,))

    def submit(self, principal, upload_id: str, key: str, options=None):
        from app.options import ProcessingOptions
        from app.services.capability_service import get_capability_service
        principal.require("jobs:submit")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key):
            raise ApplicationError(422, "invalid_idempotency_key", "Use an Idempotency-Key of 8 to 128 ASCII letters, digits, dots, underscores, colons or hyphens.")
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        requested = options if isinstance(options, ProcessingOptions) else ProcessingOptions.model_validate({} if options is None else options)
        legacy_hash = hashlib.sha256(json.dumps({"upload_id": upload_id}, sort_keys=True).encode()).hexdigest()
        request_hash = hashlib.sha256(json.dumps({"upload_id": upload_id, "options": requested.resolved().model_dump(mode="json")}, sort_keys=True).encode()).hexdigest()
        conn = self.jobs._get_conn()
        def replay(existing):
            compatible_legacy = not requested.model_dump(exclude_defaults=True) and existing[0] == legacy_hash
            if existing[0] != request_hash and not compatible_legacy:
                raise ApplicationError(409, "idempotency_conflict", "This key was used for a different request.")
            return JobAdmission.model_validate_json(existing[1]), True

        with self.jobs._lock:
            existing = conn.execute("SELECT request_hash,response_json FROM job_admissions WHERE owner=? AND key_hash=?", (principal.subject, key_hash)).fetchone()
            if existing:
                return replay(existing)
            self._owned(principal, upload_id)
        # Expensive engine probes never hold the jobs lock or a write transaction.
        effective = get_capability_service().resolve(requested)
        directory = None
        with self.jobs._lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute("SELECT request_hash,response_json FROM job_admissions WHERE owner=? AND key_hash=?", (principal.subject, key_hash)).fetchone()
                if existing:
                    conn.rollback()
                    return replay(existing)
                row = self._owned(principal, upload_id)
                if row["state"] != "ready":
                    raise ApplicationError(409, "upload_not_ready", "Upload is not ready.")
                self._capacity(principal.subject, row["size_bytes"])
                if sum(j.effective_owner == principal.subject and j.status in ("queued", "processing") for j in self.jobs.jobs.values()) >= settings.job_owner_max_pending:
                    raise ApplicationError(429, "job_quota_exceeded", "Pending job limit reached.")
                job_id = uuid4().hex
                directory = self.admissions / job_id
                directory.mkdir()
                source, target = self.root / f"{upload_id}.pdf", directory / "original.pdf"
                with source.open("rb") as input_file, target.open("xb") as output:
                    shutil.copyfileobj(input_file, output, CHUNK)
                    output.flush()
                    os.fsync(output.fileno())
                now = datetime.now(timezone.utc)
                job = JobRecord(job_id, row["filename"], directory, "queued", principal.user_id, 0, now, now, target,
                                owner_principal_id=principal.subject, options_requested=requested, options_effective=effective)
                self.jobs._persist(job, commit=False)
                response = JobAdmission(job_id=job_id, upload_id=upload_id)
                conn.execute("INSERT INTO job_admissions VALUES (?,?,?,?,?)", (principal.subject, key_hash, request_hash, job_id, response.model_dump_json()))
                conn.commit()
                self.jobs.jobs[job_id] = job
                return response, False
            except BaseException:
                conn.rollback()
                if directory is not None:
                    (directory / "original.pdf").unlink(missing_ok=True)
                    directory.rmdir()
                raise


class UploadSession:
    def __init__(self, service, row):
        self.service, self.row = service, row
        self.path = service.root / (row["upload_id"] + ".part")
        self.file = self.path.open("xb")
        self.size, self.digest, self.done = 0, hashlib.sha256(), False
        self.filename = row["filename"]

    def write(self, data):
        self.size += len(data)
        if self.size > self.row["reserved_bytes"]:
            if self.row["sha256"]:
                raise ApplicationError(409, "upload_content_conflict", "A ready upload cannot be replaced with different bytes.")
            raise ApplicationError(413, "file_too_large", "Content exceeds the reserved upload size.")
        self.file.write(data)
        self.digest.update(data)

    def finish(self):
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()
        digest = self.digest.hexdigest()
        if self.row["sha256"] and digest != self.row["sha256"]:
            raise ApplicationError(409, "upload_content_conflict", "A ready upload cannot be replaced with different bytes.")
        if self.row["expected_size"] is not None and self.size != self.row["expected_size"]:
            raise ApplicationError(422, "size_mismatch", "Content size differs from the reservation.")
        if self.row["expected_sha256"] and digest != self.row["expected_sha256"]:
            raise ApplicationError(409, "checksum_mismatch", "Content checksum differs from the reservation.")
        pages = validate_pdf(self.path)
        service = self.service
        with service.jobs._lock, service.jobs._get_conn():
            if self.row["expires_at"] <= time.time():
                raise ApplicationError(410, "upload_expired", "Upload has expired.")
            if self.row["sha256"]:
                self.path.unlink()
            else:
                os.replace(self.path, service.root / (self.row["upload_id"] + ".pdf"))
            service.jobs._get_conn().execute("UPDATE uploads SET state='ready', filename=?,size_bytes=?,sha256=?,page_count=?,reserved_bytes=?,inflight_bytes=0 WHERE upload_id=?",
                                             (filename_checked(self.filename), self.size, digest, pages, self.size, self.row["upload_id"]))
            result = service._receipt(service._row(self.row["upload_id"]))
        self.done = True
        return result

    def close(self):
        try:
            self.file.close()
            self.path.unlink(missing_ok=True)
            if not self.done:
                service = self.service
                with service.jobs._lock, service.jobs._get_conn():
                    service.jobs._get_conn().execute("UPDATE uploads SET state=?,inflight_bytes=0 WHERE upload_id=?",
                                                     ("ready" if self.row["sha256"] else "reserved", self.row["upload_id"]))
                    if not self.row["sha256"]:
                        (service.root / (self.row["upload_id"] + ".pdf")).unlink(missing_ok=True)
        finally:
            self.service.slots.release()


_service = None
_init_lock = Lock()


def get_upload_service():
    global _service
    with _init_lock:
        if _service is None:
            _service = UploadService(get_job_store())
        return _service
