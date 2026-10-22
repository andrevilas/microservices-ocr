"""Durable logical removal, best-effort physical purge and runtime read leases."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat

from app.retention_models import retention_info

logger = logging.getLogger(__name__)
TERMINAL = ("completed", "failed", "canceled")


def _is_link(path):
    # Path.is_junction was added in 3.12; the application also supports 3.11.
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def safe_rmtree(root, directory, allowed_relative):
    """Validate absolute resolved target and reject links/junctions before recursion."""
    root, directory = Path(root).resolve(), Path(directory).absolute()
    expected = root / allowed_relative
    if directory != expected or directory.resolve() != expected or directory == root:
        raise OSError("Unmanaged artifact directory.")
    for part in [directory, *directory.parents]:
        if part == root:
            break
        if _is_link(part):
            raise OSError("Linked artifact directory.")
    if not directory.exists():
        return
    for current, directories, files in os.walk(directory, followlinks=False):
        for name in directories + files:
            path = Path(current) / name
            if _is_link(path):
                raise OSError("Linked artifact entry.")
    shutil.rmtree(directory)


class ArtifactRetentionMixin:
    def _delivery_hold_locked(self, job_id):
        return self._get_conn().execute(
            "SELECT 1 FROM rag_outbox WHERE job_id=? AND state NOT IN ('indexed','canceled')",
            (job_id,)).fetchone() is not None

    @staticmethod
    def check_retained(job):
        from app.application import ApplicationError
        state = retention_info(job).state
        if state in ("expired", "deleted"):
            raise ApplicationError(410, "artifacts_" + state, "Job files are no longer available.")

    def acquire_artifacts(self, job_id, owner):
        from app.application import ApplicationError
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None or job.effective_owner != owner:
                raise ApplicationError(404, "job_not_found", "Job not found.")
            self.check_retained(job)
            self._artifact_leases[job_id] = self._artifact_leases.get(job_id, 0) + 1
            return replace(job)

    def release_artifacts(self, job_id):
        with self._lock:
            count = self._artifact_leases.get(job_id, 0)
            if count <= 1:
                self._artifact_leases.pop(job_id, None)
                job = self.jobs.get(job_id)
                if job and job.artifacts_removed_at:
                    self._purge_locked(job)
            else:
                self._artifact_leases[job_id] = count - 1

    @contextmanager
    def artifact_lease(self, job_id, owner):
        job = self.acquire_artifacts(job_id, owner)
        try:
            yield job
        finally:
            self.release_artifacts(job_id)

    def _retention_write(self, job, changes, event, actor):
        """Commit metadata and content-free audit before mutating cache/files."""
        record = replace(job, **changes)
        conn = self._get_conn()
        try:
            self._persist(record, commit=False)
            conn.execute("INSERT OR IGNORE INTO artifact_audit VALUES (?,?,?,?,?)",
                         (job.job_id, event, actor, record.artifacts_removal_reason, datetime.now(timezone.utc).isoformat()))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        for key in changes:
            setattr(job, key, getattr(record, key))

    def _purge_locked(self, job):
        if self._artifact_leases.get(job.job_id) or job.artifacts_purged_at or not job.artifacts_removed_at:
            return
        # Lease counters may already be released; quarantine still preserves files.
        self._check_available()
        if self._delivery_hold_locked(job.job_id):
            return
        try:
            if not re.fullmatch("[0-9a-f]{32}", job.job_id):
                raise OSError("Invalid managed job ID.")
            relative = job.job_id if job.working_dir.absolute().parent == self.root_dir.resolve() else "admitted-jobs/" + job.job_id
            safe_rmtree(self.root_dir, job.working_dir, relative)
            self._retention_write(job, {"artifacts_purged_at": datetime.now(timezone.utc)}, "purged", "system:retention")
        except (OSError, sqlite3.Error):
            # Tombstone already committed; periodic collection retries physical removal.
            logger.warning("Artifact purge deferred for job %s", job.job_id)

    def _remove_locked(self, job, reason, actor):
        from app.application import ApplicationError
        if self._delivery_hold_locked(job.job_id):
            raise ApplicationError(409, "rag_delivery_hold", "Pending delivery protects these files.")
        if job.status not in TERMINAL:
            raise ApplicationError(409, "job_not_terminal", "Only terminal job files may be removed.")
        if job.artifacts_removed_at is None:
            actual_reason = "expired" if retention_info(job).state == "expired" else reason
            self._retention_write(job, {"artifacts_removed_at": datetime.now(timezone.utc),
                                       "artifacts_removal_reason": actual_reason, "output_pdf_path": None},
                                  "unavailable", actor)
        self._purge_locked(job)
        return retention_info(job)

    def remove_artifacts(self, job_id, owner, actor):
        from app.application import ApplicationError
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None or job.effective_owner != owner:
                raise ApplicationError(404, "job_not_found", "Job not found.")
            return self._remove_locked(job, "deleted", actor)

    def cleanup(self, job_id):
        with self._lock:
            job = self.jobs.get(job_id)
            if job:
                self._remove_locked(job, "deleted", "system:cleanup")

    def cleanup_expired(self, retention_seconds=None):
        now = datetime.now(timezone.utc)
        cleaned = []
        with self._lock:
            for job in self.jobs.values():
                if job.status not in TERMINAL:
                    continue
                if self._delivery_hold_locked(job.job_id):
                    continue
                deadline = (job.updated_at + timedelta(seconds=retention_seconds) if retention_seconds is not None
                            else job.artifacts_expires_at)
                if job.artifacts_removed_at or (deadline and deadline <= now):
                    first = job.artifacts_removed_at is None
                    self._remove_locked(job, "expired", "system:retention")
                    if first:
                        cleaned.append(job.job_id)
        return cleaned
