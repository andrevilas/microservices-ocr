"""Bounded disk-backed ZIP exports with persistent manifests and download leases."""
import hashlib
import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread
from uuid import uuid4
from zipfile import ZipFile, ZIP_STORED

from app.application import ApplicationError, Principal
from app.bulk_models import ExportInput, ExportItem, ExportReceipt
from app.config import settings
from app.services.batch_service import request_identity
from app.services.storage_service import get_job_store
from app.services.upload_service import CHUNK, MIB
from app.retention_models import retention_info


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class ExportService:
    def __init__(self, jobs):
        self.jobs = jobs
        self.root = jobs.root_dir / "exports"
        self.root.mkdir(exist_ok=True)
        self.leases = {}
        self._stop, self._wake = Event(), Event()
        self._thread = None
        with jobs._lock, jobs._get_conn():
            jobs._get_conn().execute("""CREATE TABLE IF NOT EXISTS exports (
                export_id TEXT PRIMARY KEY, owner TEXT NOT NULL, key_hash TEXT NOT NULL,
                request_hash TEXT NOT NULL, expires_at REAL NOT NULL, size_bytes INTEGER NOT NULL,
                receipt TEXT NOT NULL, UNIQUE(owner,key_hash))""")
            jobs._get_conn().execute("""CREATE TABLE IF NOT EXISTS export_requests (
                export_id TEXT PRIMARY KEY, owner TEXT NOT NULL, key_hash TEXT NOT NULL,
                request_hash TEXT NOT NULL, request TEXT NOT NULL, admission TEXT NOT NULL,
                state TEXT NOT NULL, error_code TEXT, UNIQUE(owner,key_hash))""")
            jobs._get_conn().execute("UPDATE export_requests SET state='queued' WHERE state='processing'")
            known = {r[0] for r in jobs._get_conn().execute("SELECT export_id FROM exports")}
            # Initialization occurs under the runtime lease, before serving clients.
            for path in self.root.iterdir():
                if re.fullmatch(r"[0-9a-f]{32}\.(part|zip)", path.name) and (path.suffix == ".part" or path.stem not in known):
                    path.unlink()
            self._expire()

    def _expire(self):
        for export_id, expires_at, receipt in self.jobs._get_conn().execute("SELECT export_id,expires_at,receipt FROM exports"):
            if (expires_at <= time.time() or self._source_removed(receipt)) and not self.leases.get(export_id):
                (self.root / f"{export_id}.zip").unlink(missing_ok=True)

    def _source_removed(self, receipt):
        for item in json.loads(receipt)["items"]:
            if item["included"]:
                job = self.jobs.jobs.get(item["job_id"])
                if job is None or retention_info(job).state in ("expired", "deleted"):
                    return True
        return False

    def _owned(self, principal, export_id):
        row = self.jobs._get_conn().execute("SELECT receipt,expires_at FROM exports WHERE export_id=? AND owner=?", (export_id, principal.subject)).fetchone()
        if row is None:
            raise ApplicationError(404, "export_not_found", "Export not found.")
        if row[1] <= time.time():
            raise ApplicationError(410, "export_expired", "Export expired; use a new key to request a new snapshot.")
        if self._source_removed(row[0]):
            raise ApplicationError(410, "export_source_removed", "A source job is no longer retained.")
        if not (self.root / f"{export_id}.zip").is_file():
            raise ApplicationError(409, "export_unavailable", "Export artifact is unavailable.")
        return ExportReceipt.model_validate_json(row[0])

    def get(self, principal, export_id):
        principal.require("jobs:read")
        principal.require("artifacts:read")
        with self.jobs._lock:
            row = self.jobs._get_conn().execute("SELECT admission,state,error_code FROM export_requests WHERE export_id=? AND owner=?", (export_id, principal.subject)).fetchone()
            if row is None:
                raise ApplicationError(404, "export_not_found", "Export not found.")
            if row[1] == "ready":
                return self._owned(principal, export_id)
            return ExportReceipt.model_validate_json(row[0]).model_copy(update={"status": row[1], "error_code": row[2]})

    def create(self, principal, body: ExportInput, key):
        principal.require("jobs:read")
        principal.require("artifacts:read")
        key_hash, request_hash = request_identity(key, body.model_dump(mode="json"))
        with self.jobs._lock, self.jobs._get_conn():
            conn = self.jobs._get_conn()
            existing = conn.execute("SELECT request_hash,admission,state,export_id FROM export_requests WHERE owner=? AND key_hash=?", (principal.subject, key_hash)).fetchone()
            if existing:
                if existing[0] != request_hash:
                    raise ApplicationError(409, "idempotency_conflict", "This key was used for a different export.")
                if existing[2] == "ready":
                    self._owned(principal, existing[3])  # Expired snapshot cannot be recreated with the old key.
                return ExportReceipt.model_validate_json(existing[1]), True
            jobs = [self.jobs.jobs.get(job_id) for job_id in body.job_ids]
            if any(job is None or job.effective_owner != principal.subject for job in jobs):
                raise ApplicationError(404, "job_not_found", "Job not found.")
            pending, own = conn.execute("SELECT COUNT(*),COALESCE(SUM(owner=?),0) FROM export_requests WHERE state IN ('queued','processing')", (principal.subject,)).fetchone()
            if pending >= settings.export_max_pending or own >= settings.export_owner_max_pending:
                raise ApplicationError(429, "export_queue_full", "Pending export limit reached.")
            size = sum(j.output_pdf_path.stat().st_size for j in jobs if retention_info(j).state == "retained" and j.status == "completed" and j.output_pdf_path and j.output_pdf_path.is_file())
            if size > settings.export_max_total_mb * MIB:
                raise ApplicationError(413, "export_too_large", "Aggregate result size exceeds the export limit.")
            self._capacity(principal.subject, size + 65536)
            receipt = ExportReceipt(export_id=uuid4().hex, created_at=datetime.now(timezone.utc))
            conn.execute("INSERT INTO export_requests VALUES (?,?,?,?,?,?, 'queued',NULL)",
                         (receipt.export_id, principal.subject, key_hash, request_hash, body.model_dump_json(), receipt.model_dump_json()))
        self._wake.set()
        return receipt, False

    def start(self):
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = Thread(target=self._worker, name="ocr-export-worker", daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread is None:
            return
        self._stop.set()
        self._wake.set()
        self._thread.join(settings.worker_shutdown_timeout_seconds)
        if self._thread.is_alive():
            raise RuntimeError("Export worker did not stop before the shutdown deadline.")
        self._thread = None

    def _worker(self):
        while not self._stop.is_set():
            with self.jobs._lock, self.jobs._get_conn():
                row = self.jobs._get_conn().execute("SELECT export_id,owner,key_hash,request_hash,request,admission FROM export_requests WHERE state='queued' ORDER BY rowid LIMIT 1").fetchone()
                if row:
                    self.jobs._get_conn().execute("UPDATE export_requests SET state='processing' WHERE export_id=?", (row[0],))
            if row is None:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            try:
                principal = Principal(row[1], None, frozenset({"jobs:read", "artifacts:read"}))
                self._build(principal, ExportInput.model_validate_json(row[4]), row[2], row[3],
                            row[0], ExportReceipt.model_validate_json(row[5]).created_at)
                state, error = "ready", None
            except ApplicationError as exc:
                state, error = "failed", exc.code
            except Exception:
                state, error = "failed", "export_failed"
            with self.jobs._lock, self.jobs._get_conn():
                self.jobs._get_conn().execute("UPDATE export_requests SET state=?,error_code=? WHERE export_id=?", (state, error, row[0]))

    def _capacity(self, owner, extra):
        self._expire()
        total, own = 0, 0
        for export_id, export_owner in self.jobs._get_conn().execute("SELECT export_id,owner FROM exports"):
            path = self.root / f"{export_id}.zip"
            if path.is_file():
                size = path.stat().st_size
                total += size
                own += size if export_owner == owner else 0
        if total + extra > settings.export_storage_limit_mb * MIB or own + extra > settings.export_owner_limit_mb * MIB:
            raise ApplicationError(429, "export_quota_exceeded", "Export storage quota is exhausted.")
        if shutil.disk_usage(self.root).free < extra + settings.upload_min_free_mb * MIB:
            raise ApplicationError(503, "storage_unavailable", "Insufficient free disk space for the export.")

    def _build(self, principal, body: ExportInput, key_hash, request_hash, export_id, created_at):
        principal.require("jobs:read")
        principal.require("artifacts:read")
        with self.jobs._lock:
            conn = self.jobs._get_conn()
            existing = conn.execute("SELECT request_hash,export_id FROM exports WHERE owner=? AND key_hash=?", (principal.subject, key_hash)).fetchone()
            if existing:
                if existing[0] != request_hash:
                    raise ApplicationError(409, "idempotency_conflict", "This key was used for a different export.")
                return self._owned(principal, existing[1]), True
            # Validate every owner before writing anything or reporting exclusions.
            jobs = [self.jobs.jobs.get(job_id) for job_id in body.job_ids]
            if any(job is None or job.effective_owner != principal.subject for job in jobs):
                raise ApplicationError(404, "job_not_found", "Job not found.")
            limit = settings.export_max_total_mb * MIB
            total = sum(j.output_pdf_path.stat().st_size for j in jobs if retention_info(j).state == "retained" and j.status == "completed" and j.output_pdf_path and j.output_pdf_path.is_file())
            if total > limit:
                raise ApplicationError(413, "export_too_large", "Aggregate result size exceeds the export limit.")
            # Stored PDFs avoid compression CPU spikes; reserve 64 KiB for manifest/ZIP metadata.
            self._capacity(principal.subject, total + 65536)
            now = datetime.now(timezone.utc)
            part, target = self.root / f"{export_id}.part", self.root / f"{export_id}.zip"
            items, copied = [], 0
            try:
                with ZipFile(part, "x", compression=ZIP_STORED, allowZip64=True) as archive:
                    for index, job in enumerate(jobs, 1):
                        item = ExportItem(job_id=job.job_id, filename=job.filename, status=job.status, included=False)
                        if job.status != "completed":
                            item.reason = "job_not_completed"
                        elif retention_info(job).state in ("expired", "deleted") or not job.output_pdf_path or not job.output_pdf_path.is_file():
                            item.reason = "artifact_unavailable"
                        else:
                            item.archive_name = f"{index:03}-{job.job_id}.pdf"
                            digest, size = hashlib.sha256(), 0
                            with job.output_pdf_path.open("rb") as source, archive.open(item.archive_name, "w", force_zip64=True) as output:
                                while chunk := source.read(CHUNK):
                                    size += len(chunk)
                                    copied += len(chunk)
                                    if copied > limit or copied > total:
                                        raise ApplicationError(413, "export_too_large", "Artifact size changed or aggregate limit exceeded.")
                                    output.write(chunk)
                                    digest.update(chunk)
                            item.included, item.size_bytes, item.sha256 = True, size, digest.hexdigest()
                        items.append(item)
                    manifest = {"schema_version": "1.0", "export_id": export_id, "created_at": now.isoformat(),
                                "items": [i.model_dump(mode="json") for i in items]}
                    archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                if part.stat().st_size > total + 65536:
                    raise ApplicationError(413, "export_too_large", "Archive metadata exceeds the limit.")
                with part.open("rb+") as completed:
                    os.fsync(completed.fileno())
                digest, size = file_digest(part), part.stat().st_size
                os.replace(part, target)
                receipt = ExportReceipt(export_id=export_id, created_at=created_at, status="ready", expires_at=now + timedelta(seconds=settings.export_ttl_seconds),
                                        size_bytes=size, sha256=digest, download_url=f"/api/v1/exports/{export_id}/content", items=items)
                with conn:
                    conn.execute("INSERT INTO exports VALUES (?,?,?,?,?,?,?)", (export_id, principal.subject, key_hash, request_hash,
                                                                              receipt.expires_at.timestamp(), size, receipt.model_dump_json()))
                return receipt, False
            except BaseException:
                conn.rollback()
                part.unlink(missing_ok=True)
                target.unlink(missing_ok=True)
                raise

    def open_download(self, principal, export_id):
        principal.require("jobs:read")
        principal.require("artifacts:read")
        if self.get(principal, export_id).status != "ready":
            raise ApplicationError(409, "export_not_ready", "Export is not ready; poll its status.")
        with self.jobs._lock:
            receipt = self._owned(principal, export_id)
            source = (self.root / f"{export_id}.zip").open("rb")
            self.leases[export_id] = self.leases.get(export_id, 0) + 1
            return receipt, source

    def close_download(self, export_id, source):
        with self.jobs._lock:
            source.close()
            self.leases[export_id] -= 1
            if not self.leases[export_id]:
                del self.leases[export_id]


_service = None
_init_lock = Lock()


def get_export_service():
    global _service
    with _init_lock:
        if _service is None:
            _service = ExportService(get_job_store())
        return _service
