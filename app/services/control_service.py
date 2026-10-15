"""Explicit owner-scoped cancellation and idempotent derived job admission."""
from datetime import datetime, timezone
import os
import shutil
from threading import Lock
from uuid import uuid4

from app.application import ApplicationError
from app.control_models import CancelResult, CancelPage, RetryReceipt
from app.options import ProcessingOptions
from app.services.batch_service import request_identity
from app.services.capability_service import get_capability_service
from app.services.storage_service import JobRecord
from app.services.upload_service import CHUNK, get_upload_service


class ControlService:
    def __init__(self, uploads):
        self.uploads, self.jobs = uploads, uploads.jobs
        with self.jobs._lock, self.jobs._get_conn():
            self.jobs._get_conn().execute("""CREATE TABLE IF NOT EXISTS job_retries (
                owner TEXT NOT NULL, key_hash TEXT NOT NULL, request_hash TEXT NOT NULL,
                receipt TEXT NOT NULL, PRIMARY KEY(owner,key_hash))""")

    def cancel(self, principal, job_id):
        principal.require("jobs:cancel")
        job = self.jobs.request_cancel(job_id, principal.subject)
        if job is None:
            raise ApplicationError(404, "job_not_found", "Job not found.")
        outcome = {"canceled": "canceled", "cancel_requested": "requested"}.get(job.status, "already_terminal")
        return CancelResult(job_id=job_id, status=job.status, outcome=outcome)

    def cancel_many(self, principal, body):
        principal.require("jobs:cancel")
        items = []
        for job_id in body.job_ids:
            try:
                items.append(self.cancel(principal, job_id))
            except ApplicationError as exc:
                if exc.status != 404:
                    raise
                items.append(CancelResult(job_id=job_id, outcome="not_found"))
        return CancelPage(items=items)

    def _parent(self, principal, job_id):
        job = self.jobs.jobs.get(job_id)
        if job is None or job.effective_owner != principal.subject:
            raise ApplicationError(404, "job_not_found", "Job not found.")
        return job

    def retry(self, principal, job_id, body, key):
        principal.require("jobs:submit")
        key_hash, request_hash = request_identity(key, {"parent_job_id": job_id, "options": body.options.resolved().model_dump(mode="json") if body.options else None})
        conn = self.jobs._get_conn()
        def replay():
            row = conn.execute("SELECT request_hash,receipt FROM job_retries WHERE owner=? AND key_hash=?", (principal.subject, key_hash)).fetchone()
            if row:
                if row[0] != request_hash:
                    raise ApplicationError(409, "idempotency_conflict", "This key was used for a different retry.")
                return RetryReceipt.model_validate_json(row[1])
        with self.jobs._lock:
            existing = replay()
            if existing:
                return existing, True
            parent = self._parent(principal, job_id)
            if parent.status not in ("completed", "failed", "canceled"):
                raise ApplicationError(409, "job_not_terminal", "Wait for the original job to reach a terminal state.")
            if not parent.input_pdf_path.is_file():
                raise ApplicationError(410, "input_expired", "Original input is unavailable; submit a new upload.")
            if body.options is None and parent.options_requested is None:
                raise ApplicationError(422, "retry_options_required", "Legacy jobs require explicit processing options.")
            requested = body.options or parent.options_requested
        # Resolve against current capabilities, outside the store transaction.
        effective = get_capability_service().resolve(requested)
        directory = None
        with self.jobs._lock:
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = replay()
                if existing:
                    conn.rollback()
                    return existing, True
                parent = self._parent(principal, job_id)
                if parent.status not in ("completed", "failed", "canceled"):
                    raise ApplicationError(409, "job_not_terminal", "Wait for a terminal original job.")
                if not parent.input_pdf_path.is_file():
                    raise ApplicationError(410, "input_expired", "Original input is unavailable; submit a new upload.")
                from app.config import settings
                if sum(j.effective_owner == principal.subject and j.status in ("queued", "processing", "cancel_requested") for j in self.jobs.jobs.values()) >= settings.job_owner_max_pending:
                    raise ApplicationError(429, "job_quota_exceeded", "Pending job limit reached.")
                self.uploads._capacity(principal.subject, parent.input_pdf_path.stat().st_size)
                derived_id = uuid4().hex
                directory = self.uploads.admissions / derived_id
                directory.mkdir()
                target = directory / "original.pdf"
                with parent.input_pdf_path.open("rb") as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, CHUNK)
                    output.flush()
                    os.fsync(output.fileno())
                now = datetime.now(timezone.utc)
                job = JobRecord(derived_id, parent.filename, directory, "queued", principal.user_id, 0, now, now, target,
                                owner_principal_id=principal.subject, options_requested=requested, options_effective=effective,
                                parent_job_id=job_id)
                self.jobs._persist(job, commit=False)
                receipt = RetryReceipt(job_id=derived_id, parent_job_id=job_id)
                conn.execute("INSERT INTO job_retries VALUES (?,?,?,?)", (principal.subject, key_hash, request_hash, receipt.model_dump_json()))
                conn.commit()
                self.jobs.jobs[derived_id] = job
                return receipt, False
            except BaseException:
                conn.rollback()
                if directory is not None:
                    (directory / "original.pdf").unlink(missing_ok=True)
                    directory.rmdir()
                raise


def retry_and_enqueue(service, queue, principal, *args):
    receipt, replayed = service.retry(principal, *args)
    try:
        job = service.jobs.get_snapshot(receipt.job_id)
        if job and job.status == "queued":
            queue.enqueue(receipt.job_id)
    except Exception:
        raise ApplicationError(503, "queue_unavailable", "Retry is persisted; repeat the same key and payload.") from None
    return receipt, replayed


_service = None
_lock = Lock()


def get_control_service():
    global _service
    with _lock:
        if _service is None:
            _service = ControlService(get_upload_service())
        return _service
