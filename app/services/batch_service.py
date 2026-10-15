"""Atomic batch admission using the same SQLite transaction and runtime queue."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

from app.application import ApplicationError
from app.bulk_models import BatchInput, BatchItem, BatchPage, BatchReceipt, BatchView
from app.config import settings
from app.services.storage_service import JobRecord
from app.services.upload_service import CHUNK, MIB, get_upload_service


def request_identity(key, payload):
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key):
        raise ApplicationError(422, "invalid_idempotency_key", "Use 8 to 128 ASCII letters, digits, dots, underscores, colons or hyphens.")
    return hashlib.sha256(key.encode()).hexdigest(), hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class BatchService:
    def __init__(self, jobs, uploads=None):
        self.jobs, self.uploads = jobs, uploads
        with jobs._lock, jobs._get_conn():
            jobs._get_conn().execute("""CREATE TABLE IF NOT EXISTS batches (
                batch_id TEXT PRIMARY KEY, owner TEXT NOT NULL, key_hash TEXT NOT NULL,
                request_hash TEXT NOT NULL, created_at TEXT NOT NULL, receipt TEXT NOT NULL,
                UNIQUE(owner, key_hash))""")

    def _replay(self, owner, key_hash, request_hash):
        row = self.jobs._get_conn().execute("SELECT request_hash,receipt FROM batches WHERE owner=? AND key_hash=?", (owner, key_hash)).fetchone()
        if row:
            if row[0] != request_hash:
                raise ApplicationError(409, "idempotency_conflict", "This key was used for a different batch.")
            return BatchReceipt.model_validate_json(row[1])

    def create(self, principal, body: BatchInput, key):
        from app.services.capability_service import get_capability_service
        principal.require("jobs:submit")
        key_hash, request_hash = request_identity(key, {"items": [dict(client_item_id=i.client_item_id, upload_id=i.upload_id,
                                                                       options=i.options.resolved().model_dump(mode="json")) for i in body.items]})
        with self.jobs._lock:
            replay = self._replay(principal.subject, key_hash, request_hash)
            if replay:
                return replay, True
            for item in body.items:
                self.uploads._owned(principal, item.upload_id)
        effective = [get_capability_service().resolve(item.options) for item in body.items]
        with self.jobs._lock:
            conn = self.jobs._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                replay = self._replay(principal.subject, key_hash, request_hash)
                if replay:
                    conn.rollback()
                    return replay, True
                rows = []
                for item, options in zip(body.items, effective):
                    row = self.uploads._owned(principal, item.upload_id)
                    if row["state"] != "ready":
                        raise ApplicationError(409, "upload_not_ready", "Every upload must be ready; no jobs admitted.")
                    rows.append(dict(filename=row["filename"], size=row["size_bytes"], source=self.uploads.root / f"{item.upload_id}.pdf",
                                     client_item_id=item.client_item_id, upload_id=item.upload_id, requested=item.options, effective=options))
                self._limits(principal, rows)
                self.uploads._capacity(principal.subject, sum(r["size"] for r in rows))
                return self._persist(principal, rows, key_hash, request_hash), False
            except BaseException:
                conn.rollback()
                raise

    def _limits(self, principal, rows):
        if not rows or len(rows) > min(settings.max_batch_files, 25):
            raise ApplicationError(422, "batch_limit_exceeded", "Batch item count exceeds the configured limit.")
        if sum(row["size"] for row in rows) > settings.batch_max_total_mb * MIB:
            raise ApplicationError(413, "batch_too_large", "Aggregate input size exceeds the batch limit.")
        pending = sum(j.effective_owner == principal.subject and j.status in ("queued", "processing") for j in self.jobs.jobs.values())
        if pending + len(rows) > settings.job_owner_max_pending:
            raise ApplicationError(429, "job_quota_exceeded", "The complete batch would exceed the pending job limit.")

    def _persist(self, principal, rows, key_hash, request_hash):
        """Caller holds store lock and transaction. Publish all cache records after commit."""
        directory_root = self.jobs.root_dir / "admitted-jobs"
        directory_root.mkdir(exist_ok=True)
        directories, jobs = [], []
        now, batch_id = datetime.now(timezone.utc), uuid4().hex
        conn = self.jobs._get_conn()
        try:
            for row in rows:
                job_id = uuid4().hex
                directory = directory_root / job_id
                directory.mkdir()
                directories.append(directory)
                target = directory / "original.pdf"
                with row["source"].open("rb") as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, CHUNK)
                    output.flush()
                    os.fsync(output.fileno())
                job = JobRecord(job_id, row["filename"], directory, "queued", principal.user_id, 0, now, now, target,
                                owner_principal_id=principal.subject, options_requested=row["requested"], options_effective=row["effective"])
                self.jobs._persist(job, commit=False)
                jobs.append(job)
            receipt = BatchReceipt(batch_id=batch_id, created_at=now,
                                   items=[BatchItem(client_item_id=r["client_item_id"], upload_id=r["upload_id"], job_id=j.job_id,
                                                    filename=j.filename) for r, j in zip(rows, jobs)])
            conn.execute("INSERT INTO batches VALUES (?,?,?,?,?,?)", (batch_id, principal.subject, key_hash, request_hash, now.isoformat(), receipt.model_dump_json()))
            conn.commit()
        except BaseException:
            conn.rollback()
            for directory in directories:
                (directory / "original.pdf").unlink(missing_ok=True)
                directory.rmdir()
            raise
        self.jobs.jobs.update({j.job_id: j for j in jobs})
        return receipt

    def create_legacy(self, principal, prepared):
        """Validated disk-spooled legacy uploads retain legacy processing options."""
        principal.require("jobs:submit")
        rows = [dict(filename=name, source=path, size=path.stat().st_size, client_item_id=str(n),
                     upload_id=None, requested=None, effective=None) for n, (name, path) in enumerate(prepared, 1)]
        with self.jobs._lock:
            conn = self.jobs._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._limits(principal, rows)
                total = sum(r["size"] for r in rows)
                if shutil.disk_usage(self.jobs.root_dir).free < total + settings.upload_min_free_mb * MIB:
                    raise ApplicationError(503, "storage_unavailable", "Insufficient free disk space.")
                key_hash, request_hash = request_identity(uuid4().hex, [r["filename"] for r in rows])
                return self._persist(principal, rows, key_hash, request_hash)
            except BaseException:
                conn.rollback()
                raise

    def _view(self, receipt):
        counts = {}
        for item in receipt.items:
            job = self.jobs.jobs[item.job_id]
            item.status, item.progress_percent = job.status, job.progress_percent
            item.error_code = job.error_code or ("processing_failed" if job.status == "failed" else None)
            counts[job.status] = counts.get(job.status, 0) + 1
        return BatchView(**receipt.model_dump(), counts=counts,
                         progress_percent=sum(i.progress_percent for i in receipt.items) // len(receipt.items),
                         terminal=all(i.status in ("completed", "failed", "canceled") for i in receipt.items))

    def get(self, principal, batch_id):
        principal.require("jobs:read")
        with self.jobs._lock:
            row = self.jobs._get_conn().execute("SELECT receipt FROM batches WHERE batch_id=? AND owner=?", (batch_id, principal.subject)).fetchone()
            if row is None:
                raise ApplicationError(404, "batch_not_found", "Batch not found.")
            return self._view(BatchReceipt.model_validate_json(row[0]))

    def list(self, principal, limit=50, cursor=None):
        principal.require("jobs:read")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ApplicationError(422, "invalid_limit", "Limit must be between 1 and 100.")
        binding = hashlib.sha256(("batches:" + principal.subject).encode()).hexdigest()
        after_time, after_id = "", ""
        if cursor:
            try:
                if len(cursor) > 512:
                    raise ValueError()
                after_time, after_id, owner = json.loads(base64.urlsafe_b64decode(cursor.encode()))
                if owner != binding or not isinstance(after_time, str) or not re.fullmatch(r"[0-9a-f]{32}", after_id):
                    raise ValueError()
                datetime.fromisoformat(after_time)
            except (ValueError, TypeError, UnicodeError):
                raise ApplicationError(422, "invalid_cursor", "Invalid cursor for this identity.") from None
        with self.jobs._lock:
            rows = self.jobs._get_conn().execute("SELECT created_at,batch_id,receipt FROM batches WHERE owner=? AND (created_at,batch_id)>(?,?) ORDER BY created_at,batch_id LIMIT ?",
                                                (principal.subject, after_time, after_id, limit + 1)).fetchall()
            next_cursor = base64.urlsafe_b64encode(json.dumps([*rows[limit-1][:2], binding]).encode()).decode() if len(rows) > limit else None
            return BatchPage(items=[self._view(BatchReceipt.model_validate_json(r[2])) for r in rows[:limit]], next_cursor=next_cursor)


def dispatch_batch(service, queue, principal, body, key):
    receipt, replayed = service.create(principal, body, key)
    for item in receipt.items:
        job = service.jobs.get_snapshot(item.job_id)
        if job and job.status == "queued":
            try:
                queue.enqueue(job.job_id)
            except Exception:
                raise ApplicationError(503, "queue_unavailable", "Batch is persisted; retry with the same key to recover dispatch.") from None
    return receipt, replayed


_service = None
_init_lock = Lock()


def get_batch_service():
    global _service
    with _init_lock:
        if _service is None:
            uploads = get_upload_service()
            _service = BatchService(uploads.jobs, uploads)
        return _service
