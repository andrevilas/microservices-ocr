"""Pending-only metadata recovery; this is not a namespace readiness gate.

The caller must run startup recovery before starting writers while holding the
runtime lease. Periodic recovery considers cleanup rows only. Neither entry
point inventories storage or proves that all storage has been accounted for.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from app.services.metadata_recovery_files import MetadataRecoveryFiles
from app.services.metadata_store import (
    MetadataStore, _error, _guard, _hex, _input, _owner, _require,
    _stored_time,
)
from app.services.runtime_lease import RuntimeLease
from app.services.storage_service import JobRecord


@dataclass(frozen=True, slots=True)
class RecoveryCounters:
    scanned: int = 0
    recovered: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(slots=True)
class _Ownership:
    pending_id: str
    job_id: str | None = None
    cached_job: JobRecord | None = None
    claimed: bool = False
    leased: bool = False


def _absolute_path(value):
    _require(isinstance(value, Path) and value.is_absolute()
             and '..' not in value.parts)
    return value


def _effective_owner(principal, user_id):
    if principal is not None:
        _require(type(principal) is str)
    if principal:
        _require(_owner(principal))
        return principal
    _require(type(user_id) is int and user_id > 0)
    return 'user:' + str(user_id)


def _same_time(cached, stored):
    if stored is None:
        return cached is None
    return (isinstance(cached, datetime) and cached.tzinfo is not None
            and cached.utcoffset() is not None
            and cached.astimezone(timezone.utc) == stored)


class MetadataRecovery:
    @_guard
    def __init__(self, store: MetadataStore, physical: MetadataRecoveryFiles):
        _require(isinstance(store, MetadataStore)
                 and isinstance(physical, MetadataRecoveryFiles))
        self.store = store
        self.physical = physical
        self.jobs = store.jobs
        self.root = _absolute_path(self.jobs.root_dir)
        self._periodic_lock = Lock()
        self._periodic_after = ''
        self._periodic_ceiling = None
        # _target is the existing pure path/binding validator. A real JobRecord
        # probe also checks the physical root binding when the job cache is empty;
        # no directory is opened, created, scanned, or resolved here.
        job_id = '0' * 32
        directory = self.root / job_id
        now = datetime.now(timezone.utc)
        probe = JobRecord(
            job_id=job_id, filename='', working_dir=directory,
            status='completed', owner_user_id=None, progress_percent=100,
            created_at=now, updated_at=now,
            input_pdf_path=directory / 'original.pdf',
            owner_principal_id='service:' + '0' * 32,
        )
        physical._files._target(probe, '1' * 32, '0' * 64, 1)

    @contextmanager
    def _transaction(self):
        # Preserve control exceptions originating in our body even though the
        # existing store transaction sanitizes exceptions thrown through yield.
        control = None
        try:
            with self.store._transaction() as conn:
                try:
                    yield conn
                except (KeyboardInterrupt, SystemExit) as exc:
                    control = exc
                    raise
        finally:
            if control is not None:
                raise control

    def _runtime_authority(self, lease):
        _require(isinstance(lease, RuntimeLease))
        path = _absolute_path(lease.path)
        _require(path == self.root / '.ocr-runtime.lock')
        handle = lease._file
        _require(handle is not None and handle.closed is False)

    @_guard
    def collect_cleanup(self, limit=128) -> RecoveryCounters:
        """Recover at most limit cleanup rows; active rows never age into eligibility."""
        _input(type(limit) is int and 1 <= limit <= 1024)
        with self._periodic_lock:
            return self._collect(limit=limit, runtime_lease=None)

    @_guard
    def recover_pending_startup(self, runtime_lease: RuntimeLease) -> RecoveryCounters:
        """Visit the finite pending set before writers; not a full startup sweep."""
        self._runtime_authority(runtime_lease)
        return self._collect(limit=None, runtime_lease=runtime_lease)

    @_guard
    def _ceiling(self, runtime_lease):
        with self._transaction() as conn:
            if runtime_lease is not None:
                self._runtime_authority(runtime_lease)
            condition = "state IN ('active','cleanup')" if runtime_lease is not None else "state='cleanup'"
            row = conn.execute(
                'SELECT pending_id FROM metadata_pending WHERE ' + condition +
                ' ORDER BY pending_id DESC LIMIT 1').fetchone()
            if row is not None:
                _require(len(row) == 1 and _hex(row[0], 32))
        return None if row is None else row[0]

    @_guard
    def _page(self, after, ceiling, size, runtime_lease):
        _require(type(size) is int and 1 <= size <= 32)
        with self._transaction() as conn:
            if runtime_lease is not None:
                self._runtime_authority(runtime_lease)
            condition = "state IN ('active','cleanup')" if runtime_lease is not None else "state='cleanup'"
            # Materialization is bounded by the SQL LIMIT, including at startup.
            cursor = conn.execute(
                'SELECT pending_id FROM metadata_pending WHERE ' + condition +
                ' AND pending_id>? AND pending_id<=? ORDER BY pending_id LIMIT ?',
                (after, ceiling, size))
            try:
                rows = tuple(cursor)
            finally:
                cursor.close()
            _require(len(rows) <= size)
            previous = after
            for row in rows:
                _require(len(row) == 1 and _hex(row[0], 32)
                         and previous < row[0] <= ceiling)
                previous = row[0]
        return tuple(row[0] for row in rows)

    def _collect(self, *, limit, runtime_lease):
        periodic = runtime_lease is None
        if periodic:
            # collect_cleanup owns the dedicated lock throughout this call.
            # Freeze the ceiling across calls so arrivals cannot delay wrap.
            if self._periodic_ceiling is None:
                self._periodic_ceiling = self._ceiling(None)
            ceiling = self._periodic_ceiling
            after = self._periodic_after
        else:
            ceiling = self._ceiling(runtime_lease)
            after = ''
        scanned = recovered = skipped = failed = 0
        while ceiling is not None and (limit is None or scanned < limit):
            size = 32 if limit is None else min(32, limit - scanned)
            ids = self._page(after, ceiling, size, runtime_lease)
            if not ids:
                if periodic:
                    self._periodic_after = ''
                    self._periodic_ceiling = None
                break
            for pending_id in ids:
                scanned += 1
                outcome = self._recover_one(pending_id, runtime_lease)
                # Publish only completed attempts, including failures and skips.
                # A later page failure preserves all progress already made;
                # an interrupted item remains eligible for the next attempt.
                after = pending_id
                if periodic:
                    self._periodic_after = after
                if outcome == 'recovered':
                    recovered += 1
                elif outcome == 'skipped':
                    skipped += 1
                else:
                    failed += 1
            if periodic and after == ceiling:
                # Start a fresh cycle on the NEXT call; never revisit an ID
                # within this call, even when its budget has room remaining.
                self._periodic_after = ''
                self._periodic_ceiling = None
                break
        return RecoveryCounters(scanned, recovered, skipped, failed)

    def _job_locked(self, conn, pending):
        """Validate durable identity against the cache without admission or I/O."""
        row = conn.execute(
            'SELECT job_id,status,owner_principal_id,owner_user_id,parent_job_id,'
            'working_dir,artifacts_removed_at,artifacts_purged_at '
            'FROM jobs WHERE job_id=?', (pending.job_id,)).fetchone()
        _require(row is not None and len(row) == 8)
        job_id, status, principal, user_id, parent, working, removed, purged = row
        _require(_hex(job_id, 32) and job_id == pending.job_id
                 and type(status) is str and status == 'completed')
        owner = _effective_owner(principal, user_id)
        _require(owner == pending.owner)
        _require(parent is None or (_hex(parent, 32) and parent != job_id))
        _require(type(working) is str)
        directory = _absolute_path(Path(working))
        _require(directory in (self.root / job_id,
                               self.root / 'admitted-jobs' / job_id))
        removed_time = None if removed is None else _stored_time(removed)
        purged_time = self.store._purge_marker(
            removed, purged, datetime.now(timezone.utc))
        cached = self.jobs.jobs.get(job_id)
        _require(isinstance(cached, JobRecord)
                 and cached.job_id == job_id and cached.status == status
                 and cached.parent_job_id == parent
                 and _absolute_path(cached.working_dir) == directory
                 and _effective_owner(cached.owner_principal_id,
                                      cached.owner_user_id) == owner
                 and _same_time(cached.artifacts_removed_at, removed_time)
                 and _same_time(cached.artifacts_purged_at, purged_time))
        snapshot = replace(cached)
        self.physical._files._target(
            snapshot, pending.revision_id, pending.sha256, pending.size_bytes)
        return cached, snapshot, purged_time

    def _uncommitted_locked(self, conn, pending):
        """Focused global reference checks; filenames never establish authority."""
        r = pending
        for table in ('metadata_revisions', 'metadata_operations',
                      'metadata_audit', 'metadata_heads'):
            _require(conn.execute(
                'SELECT 1 FROM ' + table + ' WHERE revision_id=? LIMIT 1',
                (r.revision_id,)).fetchone() is None)
        _require(conn.execute(
            'SELECT 1 FROM metadata_operations WHERE owner=? AND job_id=? '
            'AND operation=? AND key_hash=? LIMIT 1',
            (r.owner, r.job_id, r.operation, r.key_hash)).fetchone() is None)
        _require(conn.execute(
            'SELECT 1 FROM metadata_revisions WHERE parent_revision_id=? LIMIT 1',
            (r.revision_id,)).fetchone() is None)
        _require(conn.execute(
            'SELECT 1 FROM metadata_pending WHERE expected_head=? OR '
            '(pending_id<>? AND (revision_id=? OR job_id=?)) LIMIT 1',
            (r.revision_id, r.pending_id, r.revision_id, r.job_id)).fetchone() is None)
        # Audit parent references are contradictory too, even without a revision.
        _require(conn.execute(
            'SELECT 1 FROM metadata_audit WHERE parent_revision_id=? LIMIT 1',
            (r.revision_id,)).fetchone() is None)
        head = self.store._head(conn, r.owner, r.job_id)
        self.store._cas(head, r.expected_head, r.expected_sha256)
        _require((r.charged_slots, r.charged_bytes,
                  r.approval_slots, r.approval_bytes)
                 == self.store._charges(head, r.size_bytes))

    @staticmethod
    def _binding(job):
        return (job.job_id, job.effective_owner, job.parent_job_id, job.working_dir)

    def _recover_one(self, pending_id, runtime_lease):
        owned = _Ownership(pending_id)
        outcome = 'failed'
        control = None
        try:
            original = file_job = None
            purged = None
            with self._transaction() as conn:
                if runtime_lease is not None:
                    self._runtime_authority(runtime_lease)
                pending = self.store._pending(conn, pending_id)
                eligible = ('active', 'cleanup') if runtime_lease is not None else ('cleanup',)
                if (pending is None or pending.state not in eligible
                        or pending_id in self.store._active_writes):
                    outcome = 'skipped'
                else:
                    self.store._quota(conn, pending.owner, pending.job_id,
                                      _check_limits=False)
                    cached, file_job, purged = self._job_locked(conn, pending)
                    original = pending
                    if purged is None:
                        self.store._validate_purged_ledger_locked(
                            conn, pending.owner, pending.job_id)
                        self._uncommitted_locked(conn, pending)
                        count = self.jobs._artifact_leases.get(pending.job_id, 0)
                        _require(type(count) is int and count >= 0)
                        owned.job_id = pending.job_id
                        owned.cached_job = cached
                        self.store._active_writes.add(pending_id)
                        owned.claimed = True
                        self.jobs._artifact_leases[pending.job_id] = count + 1
                        owned.leased = True
                        self.store._mark_cleanup_locked(conn, pending_id)
            # No physical action is authorized until the diagnostic commit exits
            # normally. The outer finally already owns any in-memory changes.
            if original is not None:
                if purged is not None:
                    # C3cA owns this durable-purge route, outside our transaction
                    # and without a maintenance lease or filesystem observation.
                    outcome = ('recovered' if self.store.reconcile_purged_job(
                        original.job_id) else 'skipped')
                else:
                    with self.physical.open(file_job) as directory:
                        directory.remove_pending(original.pending_id,
                                                 original.revision_id)
                    # Context exit includes physical synchronization and closure.
                    with self._transaction() as conn:
                        _require(owned.claimed and owned.leased
                                 and pending_id in self.store._active_writes)
                        actual = self.store._pending(conn, pending_id)
                        _require(actual is not None and actual.state == 'cleanup'
                                 and replace(actual, state=original.state) == original)
                        self.store._quota(conn, actual.owner, actual.job_id,
                                          _check_limits=False)
                        cached, current_job, current_purge = self._job_locked(conn, actual)
                        _require(current_purge is None
                                 and cached is owned.cached_job
                                 and self._binding(current_job) == self._binding(file_job))
                        # The complete-history helper rejects our active claim;
                        # never remove that claim to bypass its invariant.
                        self._uncommitted_locked(conn, actual)
                        self.store._release_pending_after_cleanup_locked(conn, pending_id)
                    outcome = 'recovered'
        except (KeyboardInterrupt, SystemExit) as exc:
            control = exc
        except Exception:
            outcome = 'failed'
        finally:
            try:
                self._release_owned(owned)
            except (KeyboardInterrupt, SystemExit) as exc:
                if control is None:
                    control = exc
            except Exception:
                outcome = 'failed'
        if control is not None:
            raise control
        return outcome

    def _release_owned(self, owned):
        """Release only this attempt's memory; never repair an uncertain SQL phase."""
        if not owned.claimed and not owned.leased:
            return
        failed = False
        with self.jobs._lock:
            if owned.claimed:
                owned.claimed = False
                if owned.pending_id in self.store._active_writes:
                    self.store._active_writes.remove(owned.pending_id)
                else:
                    failed = True
            if owned.leased:
                owned.leased = False
                count = self.jobs._artifact_leases.get(owned.job_id)
                last = False
                if type(count) is not int or count <= 0:
                    failed = True
                elif count > 1:
                    self.jobs._artifact_leases[owned.job_id] = count - 1
                else:
                    self.jobs._artifact_leases.pop(owned.job_id)
                    last = True
                if last:
                    healthy = False
                    try:
                        # Check the existing connection while holding the same
                        # lock used for any following retention operation. Never
                        # create a connection, commit, or roll back to repair it.
                        conn = self.jobs._conn
                        healthy = (conn is not None
                                   and conn is self.jobs._get_conn()
                                   and conn.in_transaction is False)
                    except Exception:
                        healthy = False
                    if not healthy:
                        failed = True
                    cached = self.jobs.jobs.get(owned.job_id)
                    if cached is not owned.cached_job:
                        failed = True
                    if (healthy and not failed and cached is not None
                            and cached.artifacts_removed_at is not None):
                        self.jobs._purge_locked(cached)
        if failed:
            raise _error('metadata_unavailable') from None
