"""Internal, read-only SQL authority for a future startup namespace coordinator.

Integration is deferred: the caller must hold RuntimeLease before writers start,
plus the existing shared lock and transaction. Every result is provisional until
that transaction commits healthily; only then may physical actions begin. This
module performs no filesystem observation, mutation, claims, leases or recovery.
Its internal records are neither serialization DTOs nor namespace readiness proof.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from app.services.metadata_recovery import (
    MetadataRecovery, _absolute_path, _same_time,
)
from app.services.metadata_recovery_files import (
    MAX_INVENTORY_ENTRIES, ConfirmedExpectation,
)
from app.services.metadata_store import (
    MAX_REVISIONS_PER_JOB, MetadataPending, MetadataStore,
    _REVISION_COLUMNS, _guard, _hex, _owner, _require, _stored_time,
)
from app.services.storage_service import JobRecord


_TABLES = (
    'metadata_revisions', 'metadata_operations', 'metadata_audit',
    'metadata_heads', 'metadata_pending',
)
_STATUSES = frozenset((
    'queued', 'processing', 'cancel_requested', 'completed', 'failed', 'canceled',
))
_FINAL_REFERENCES = (
    ('metadata_revisions', 'revision_id'),
    ('metadata_heads', 'revision_id'),
    ('metadata_operations', 'revision_id'),
    ('metadata_audit', 'revision_id'),
    ('metadata_pending', 'revision_id'),
    ('metadata_revisions', 'parent_revision_id'),
    ('metadata_audit', 'parent_revision_id'),
    ('metadata_pending', 'expected_head'),
)


def _job_owner(principal, user_id):
    # Validate supplied values even when principal takes precedence.
    _require(user_id is None or (type(user_id) is int and user_id > 0))
    _require(principal is None or (type(principal) is str and
             (principal in ('', 'legacy:api-key') or _owner(principal))))
    return principal or ('user:' + str(user_id) if user_id is not None else None)


@dataclass(frozen=True, slots=True)
class NamespaceState:
    job: JobRecord
    cached_job: JobRecord
    owner: str | None
    purged_at: datetime | None
    has_ledger: bool
    confirmed: tuple[ConfirmedExpectation, ...]
    pending: MetadataPending | None

    @property
    def binding(self):
        """Bounded authority comparison; compare cached_job identity separately.

        The JobRecord is a detached internal copy, not an immutable nested value.
        Callers must not mutate it or serialize either record.
        """
        removed = self.job.artifacts_removed_at
        return (
            self.job.job_id, self.job.status, self.owner,
            self.job.parent_job_id, self.job.working_dir,
            None if removed is None else removed.astimezone(timezone.utc),
            self.purged_at, self.has_ledger, self.confirmed, self.pending,
        )


@dataclass(frozen=True, slots=True)
class NamespacePlan:
    state: NamespaceState
    orphans: tuple[str, ...]
    protected_names: tuple[str, ...]


class MetadataNamespaceAuthority:
    @_guard
    def __init__(self, recovery: MetadataRecovery):
        _require(isinstance(recovery, MetadataRecovery))
        _require(isinstance(recovery.store, MetadataStore)
                 and recovery.jobs is recovery.store.jobs)
        _require(_absolute_path(recovery.root) ==
                 _absolute_path(recovery.jobs.root_dir))
        self.recovery = recovery
        self.store = recovery.store
        self.jobs = recovery.jobs
        self.root = recovery.root

    @_guard
    def validate_global_locked(self, conn) -> None:
        """Read global integrity under the caller's startup authority.

        Caller owns RuntimeLease, the shared lock and transaction before writers.
        Success is provisional until healthy commit; it authorizes no I/O itself.
        """
        self.store._connection_locked(conn)
        # None dimensions request no invented owner/job; integrity remains global.
        self.store._quota(conn, None, None, _check_limits=False)
        referenced = ' OR '.join(
            'EXISTS (SELECT 1 FROM ' + table + ' m WHERE m.job_id=j.job_id)'
            for table in _TABLES)
        cursor = conn.execute('SELECT j.job_id FROM jobs j WHERE ' + referenced)
        try:
            # Stream jobs; each individual ledger is bounded before materializing.
            for row in cursor:
                _require(len(row) == 1 and _hex(row[0], 32))
                self._load_state_locked(conn, row[0])
        finally:
            cursor.close()

    @_guard
    def load_locked(self, conn, job_id) -> NamespaceState:
        """Reload durable/cache authority; wait for healthy commit before use.

        The future caller supplies RuntimeLease before writers and owns the shared
        lock/transaction. No maintenance claim may precede this diagnosis.
        """
        self.store._connection_locked(conn)
        _require(_hex(job_id, 32))
        self.validate_global_locked(conn)
        return self._load_state_locked(conn, job_id)

    def _load_state_locked(self, conn, job_id):
        """Local diagnosis after global quota integrity, within the same transaction."""
        self.store._connection_locked(conn)
        _require(_hex(job_id, 32))
        has_ledger = False
        for table in _TABLES:
            maximum = 1 if table in ('metadata_heads', 'metadata_pending') else MAX_REVISIONS_PER_JOB
            count = conn.execute('SELECT COUNT(*) FROM ' + table +
                                 ' WHERE job_id=?', (job_id,)).fetchone()[0]
            _require(type(count) is int and 0 <= count <= maximum)
            has_ledger = has_ledger or count > 0
        row = conn.execute(
            'SELECT job_id,status,owner_principal_id,owner_user_id,parent_job_id,'
            'working_dir,artifacts_removed_at,artifacts_purged_at '
            'FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        _require(row is not None and len(row) == 8)
        stored_id, status, principal, user_id, parent, working, removed, purged = row
        _require(_hex(stored_id, 32) and stored_id == job_id
                 and type(status) is str and status in _STATUSES)
        owner = _job_owner(principal, user_id)
        _require(parent is None or (_hex(parent, 32) and parent != job_id))
        _require(type(working) is str)
        directory = _absolute_path(Path(working))
        _require(directory in (self.root / job_id,
                               self.root / 'admitted-jobs' / job_id))
        removed_at = None if removed is None else _stored_time(removed)
        purged_at = self.store._purge_marker(removed, purged, datetime.now(timezone.utc))
        cached = self.jobs.jobs.get(job_id)
        _require(isinstance(cached, JobRecord))
        _require(_hex(cached.job_id, 32) and cached.job_id == job_id
                 and type(cached.status) is str and cached.status == status
                 and cached.parent_job_id == parent
                 and _absolute_path(cached.working_dir) == directory
                 and _job_owner(cached.owner_principal_id, cached.owner_user_id) == owner
                 and cached.effective_owner == owner
                 and _same_time(cached.artifacts_removed_at, removed_at)
                 and _same_time(cached.artifacts_purged_at, purged_at))
        confirmed = []
        pending = None
        if has_ledger:
            _require(_owner(owner) and status == 'completed')
            # Complete bounded mixed SQL ledger validation, including active
            # claim rejection; this establishes no physical file authority.
            self.store._validate_purged_ledger_locked(conn, owner, job_id)
            columns = ','.join('r.' + column for column in _REVISION_COLUMNS.split(','))
            cursor = conn.execute(
                'SELECT ' + columns + ',a.actor,a.auth_source,a.created_at '
                'FROM metadata_revisions r JOIN metadata_audit a '
                'ON a.revision_id=r.revision_id WHERE r.job_id=? '
                'ORDER BY r.revision_id', (job_id,))
            try:
                for values in cursor:
                    _require(len(values) == 11)
                    revision = self.store._revision_row(values[:8])
                    actor, auth, created = values[8:]
                    _require(revision.owner == owner and revision.job_id == job_id
                             and actor == owner and _stored_time(created) == revision.created_at)
                    reviewed_sha256 = None
                    if revision.state == 'approved':
                        parent_revision = self.store._revision(conn, revision.parent_revision_id)
                        _require(parent_revision is not None
                                 and parent_revision.owner == owner
                                 and parent_revision.job_id == job_id
                                 and parent_revision.state == 'draft')
                        # The complete ledger has checked this parent's hash
                        # against the strict persisted approval receipt.
                        reviewed_sha256 = parent_revision.sha256
                    confirmed.append(ConfirmedExpectation(
                        revision.revision_id, revision.parent_revision_id,
                        revision.sha256, revision.size_bytes, revision.state,
                        revision.created_at, actor, auth,
                        reviewed_sha256=reviewed_sha256))
            finally:
                cursor.close()
            head = self.store._head(conn, owner, job_id)
            if confirmed:
                revisions = {value.revision_id: value for value in confirmed}
                _require(head is not None and head.revision_id in revisions)
                visited = set()
                current = head.revision_id
                while current is not None:
                    _require(len(visited) < MAX_REVISIONS_PER_JOB
                             and current in revisions and current not in visited)
                    visited.add(current)
                    current = revisions[current].parent_revision_id
                _require(visited == set(revisions))
            row = conn.execute('SELECT pending_id FROM metadata_pending WHERE job_id=?',
                               (job_id,)).fetchone()
            if row is not None:
                pending = self.store._pending(conn, row[0])
                _require(pending is not None and pending.owner == owner
                         and pending.job_id == job_id
                         and pending.pending_id not in self.store._active_writes)
                self.recovery._uncommitted_locked(conn, pending)
        return NamespaceState(replace(cached), cached, owner, purged_at,
                              has_ledger, tuple(confirmed), pending)

    def _references_locked(self, conn, state, stem, *, temporary):
        self.store._connection_locked(conn)
        pending = state.pending
        if temporary:
            references = (('metadata_pending', 'pending_id'),)
            justified = pending is not None and pending.pending_id == stem
        else:
            references = _FINAL_REFERENCES
            justified = (any(value.revision_id == stem for value in state.confirmed)
                         or (pending is not None and pending.revision_id == stem))
        found = False
        for table, column in references:
            cursor = conn.execute('SELECT owner,job_id FROM ' + table +
                                  ' WHERE ' + column + '=?', (stem,))
            try:
                for owner, job_id in cursor:
                    # A global reference is a contradiction, not mere protection,
                    # unless validated local authority explains this exact stem.
                    _require(owner == state.owner and job_id == state.job.job_id
                             and justified)
                    found = True
            finally:
                cursor.close()
        return found

    @_guard
    def plan_locked(self, conn, job_id, names: tuple[str, ...]) -> NamespacePlan:
        """Reload SQL and classify bounded names; never mint authority from files.

        Caller holds RuntimeLease before writers, the shared lock and transaction;
        wait for healthy commit before any physical use. Physical inventory and
        hardlink/symlink validation belong to the separate pinned recovery session.
        Purged jobs must be skipped by the future coordinator, without this plan.
        """
        self.store._connection_locked(conn)
        _require(_hex(job_id, 32) and type(names) is tuple
                 and len(names) <= MAX_INVENTORY_ENTRIES)
        for name in names:
            _require(type(name) is str and len(name) == 37)
            _require((name.endswith('.json') and _hex(name[:-5], 32))
                     or (name.startswith('.') and name.endswith('.tmp')
                         and _hex(name[1:-4], 32)))
        _require(len(set(names)) == len(names))
        state = self.load_locked(conn, job_id)
        _require(state.purged_at is None)
        protected = {value.revision_id + '.json' for value in state.confirmed}
        if state.pending is not None:
            protected.add('.' + state.pending.pending_id + '.tmp')
            protected.add(state.pending.revision_id + '.json')
        orphans = []
        for name in sorted(names):
            temporary = name.startswith('.')
            stem = name[1:-4] if temporary else name[:-5]
            referenced = self._references_locked(conn, state, stem, temporary=temporary)
            if referenced:
                _require(name in protected)
            elif name not in protected:
                orphans.append(name)
        _require(not set(orphans).intersection(protected)
                 and len(protected) <= MAX_INVENTORY_ENTRIES)
        return NamespacePlan(state, tuple(orphans), tuple(sorted(protected)))
