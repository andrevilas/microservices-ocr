"""Internal, bounded startup namespace coordination before writers start.

The caller holds RuntimeLease throughout. Results describe completed recovery,
not namespace readiness. No provider, queue or retention integration is installed.
Public authority load/plan APIs repeat global validation and can make this sweep
quadratic. Preserve those checks pending integration optimization and measurements.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace

from app.services.metadata_namespace_authority import MetadataNamespaceAuthority
from app.services.metadata_recovery import MetadataRecovery, RecoveryCounters, _Ownership
from app.services.metadata_recovery_files import ConfirmedCheck
from app.services.metadata_store import _error, _guard, _hex, _require


_MAX_OCCURRENCES = 128


@dataclass(frozen=True, slots=True)
class NamespaceRecoveryCounters:
    jobs_scanned: int = 0
    jobs_verified: int = 0
    purged_jobs: int = 0
    orphan_files_removed: int = 0
    confirmed_valid: int = 0
    confirmed_missing: int = 0
    confirmed_content_invalid: int = 0


@dataclass(frozen=True, slots=True)
class NamespaceOccurrence:
    job_id: str
    revision_id: str
    code: str


@dataclass(frozen=True, slots=True)
class NamespaceRecoveryResult:
    pending: RecoveryCounters
    counters: NamespaceRecoveryCounters
    occurrences: tuple[NamespaceOccurrence, ...]
    truncated_occurrences: int


class MetadataNamespaceRecovery:
    @_guard
    def __init__(self, recovery: MetadataRecovery):
        _require(isinstance(recovery, MetadataRecovery))
        self.recovery = recovery
        self.store = recovery.store
        self.jobs = recovery.jobs
        self.physical = recovery.physical
        self.root = recovery.root
        self.authority = MetadataNamespaceAuthority(recovery)

    @contextmanager
    def _transaction(self, runtime_lease):
        # Startup uses the existing connection only. In particular, do not enter
        # a transaction that could initialize a missing connection.
        _require(self.jobs._conn is not None)
        with self.recovery._transaction() as conn:
            self.recovery._runtime_authority(runtime_lease)
            _require(conn is self.jobs._conn)
            self.store._connection_locked(conn)
            yield conn

    def _validate_and_ceiling(self, runtime_lease):
        with self._transaction(runtime_lease) as conn:
            self.authority.validate_global_locked(conn)
            previous = ''
            ceiling = None
            cursor = conn.execute('SELECT job_id FROM jobs ORDER BY job_id')
            try:
                # Validate every durable ID, including jobs without metadata.
                # Retain only the last ID, never a materialized all-jobs list.
                for row in cursor:
                    _require(len(row) == 1 and _hex(row[0], 32)
                             and previous < row[0])
                    previous = row[0]
                    ceiling = row[0]
            finally:
                cursor.close()
        return ceiling

    def _page(self, runtime_lease, after, ceiling):
        with self._transaction(runtime_lease) as conn:
            cursor = conn.execute(
                'SELECT job_id FROM jobs WHERE job_id>? AND job_id<=? '
                'ORDER BY job_id LIMIT 32', (after, ceiling))
            try:
                rows = tuple(cursor)
            finally:
                cursor.close()
            _require(len(rows) <= 32)
            previous = after
            for row in rows:
                _require(len(row) == 1 and _hex(row[0], 32)
                         and previous < row[0] <= ceiling)
                previous = row[0]
        return tuple(row[0] for row in rows)

    def _check_owned_locked(self, owned, state):
        _require(owned.pending_id is None and not owned.claimed
                 and owned.leased and owned.job_id == state.job.job_id
                 and owned.cached_job is state.cached_job
                 and self.jobs.jobs.get(owned.job_id) is owned.cached_job)
        count = self.jobs._artifact_leases.get(owned.job_id)
        _require(type(count) is int and count >= 1)

    def _release_maintenance(self, owned):
        """Always release our token, preserving the first control exception."""
        may_purge = False
        control = None
        failed = False
        try:
            if owned.leased:
                with self.jobs._lock:
                    count = self.jobs._artifact_leases.get(owned.job_id)
                    _require(type(count) is int and count >= 1
                             and self.jobs.jobs.get(owned.job_id) is owned.cached_job)
                    may_purge = (count == 1 and
                                 owned.cached_job.artifacts_removed_at is not None)
        except (KeyboardInterrupt, SystemExit) as exc:
            control = exc
        except Exception:
            failed = True
        finally:
            try:
                self.recovery._release_owned(owned)
            except (KeyboardInterrupt, SystemExit) as exc:
                if control is None:
                    control = exc
            except Exception:
                failed = True
        if control is not None:
            raise control
        _require(not failed)
        return may_purge

    @staticmethod
    def _validate_checks(checks, expectations):
        _require(type(checks) is tuple and len(checks) == len(expectations))
        for check, expected in zip(checks, expectations):
            _require(type(check) is ConfirmedCheck
                     and _hex(check.revision_id, 32)
                     and check.revision_id == expected.revision_id
                     and type(check.status) is str
                     and check.status in ('valid', 'missing', 'content_invalid'))

    def _recover_job(self, runtime_lease, job_id):
        # This memory-only token must never become an SQL identity or claim.
        owned = _Ownership(pending_id=None)
        control = None
        checks = ()
        orphan_count = 0
        may_purge = False
        try:
            with self._transaction(runtime_lease) as conn:
                state = self.authority.load_locked(conn, job_id)
                if state.purged_at is None:
                    count = self.jobs._artifact_leases.get(job_id, 0)
                    _require(type(count) is int and count >= 0
                             and self.jobs.jobs.get(job_id) is state.cached_job)
                    owned.job_id = job_id
                    owned.cached_job = state.cached_job
                    self.jobs._artifact_leases[job_id] = count + 1
                    owned.leased = True
            # No physical action or purged collection precedes healthy commit.
            if state.purged_at is not None:
                if state.has_ledger:
                    _require(self.store.reconcile_purged_job(job_id) is True)
            else:
                with self.physical.open(state.job) as directory:
                    inventory = directory.inventory()
                    names = tuple(entry.name for entry in inventory.entries)
                    with self._transaction(runtime_lease) as conn:
                        plan = self.authority.plan_locked(conn, job_id, names)
                        _require(plan.state.cached_job is state.cached_job
                                 and plan.state.binding == state.binding)
                        self._check_owned_locked(owned, plan.state)
                    checks = directory.verify_confirmed(inventory, plan.state.confirmed)
                    self._validate_checks(checks, plan.state.confirmed)
                    # Missing/invalid confirmed contents remain protected and
                    # charged. Even empty selections execute physical checks.
                    directory.remove_orphans(
                        inventory, plan.orphans,
                        protected_names=plan.protected_names)
                    orphan_count = len(plan.orphans)
                # Physical sync and every close must have succeeded here.
        except (KeyboardInterrupt, SystemExit) as exc:
            control = exc
            raise
        finally:
            try:
                may_purge = self._release_maintenance(owned)
            except BaseException:
                if control is None:
                    raise

        # Release may legitimately finish retention for an already tombstoned
        # job. Revalidate durable/cache authority after that operation as well.
        with self._transaction(runtime_lease) as conn:
            current = self.authority.load_locked(conn, job_id)
            _require(current.cached_job is state.cached_job)
            if state.purged_at is not None:
                if state.has_ledger:
                    # C3cA may collect pending/history rows; job identity and
                    # both retention markers must still match the diagnosis.
                    comparable = replace(
                        current, has_ledger=state.has_ledger,
                        confirmed=state.confirmed, pending=state.pending)
                    _require(comparable.binding == state.binding)
                else:
                    _require(current.binding == state.binding)
            elif current.purged_at is not None:
                _require(may_purge and state.job.artifacts_removed_at is not None
                         and self.jobs._artifact_leases.get(job_id, 0) == 0)
                _require(replace(current, purged_at=None).binding == state.binding)
            else:
                _require(current.binding == state.binding)
        return state.purged_at is not None, checks, orphan_count

    @_guard
    def recover_startup(self, runtime_lease) -> NamespaceRecoveryResult:
        """Run before writers under RuntimeLease; return bounded diagnostics."""
        try:
            return self._run(runtime_lease)
        except Exception:
            # All ordinary startup failures have one sanitized public outcome.
            raise _error('metadata_unavailable') from None

    def _run(self, runtime_lease):
        self.recovery._runtime_authority(runtime_lease)
        # Diagnose globally and validate the entire durable ID domain before
        # existing pending recovery can perform any physical cleanup.
        ceiling = self._validate_and_ceiling(runtime_lease)
        pending = self.recovery.recover_pending_startup(runtime_lease)
        _require(type(pending) is RecoveryCounters
                 and all(type(value) is int and value >= 0 for value in (
                     pending.scanned, pending.recovered, pending.skipped, pending.failed))
                 and pending.scanned == pending.recovered + pending.skipped + pending.failed
                 and pending.failed == 0)
        # Remaining active claims fail authority validation. Valid remaining
        # pending rows stay protected and retain their existing reservations.
        _require(self._validate_and_ceiling(runtime_lease) == ceiling)

        counters = NamespaceRecoveryCounters()
        occurrences = []
        truncated = 0
        after = ''
        while ceiling is not None:
            job_ids = self._page(runtime_lease, after, ceiling)
            if not job_ids:
                break
            for job_id in job_ids:
                purged, checks, removed = self._recover_job(runtime_lease, job_id)
                # Publish diagnostics only after physical exit, maintenance
                # release and the final healthy authority transaction.
                counters = replace(
                    counters,
                    jobs_scanned=counters.jobs_scanned + 1,
                    jobs_verified=counters.jobs_verified + int(not purged),
                    purged_jobs=counters.purged_jobs + int(purged),
                    orphan_files_removed=counters.orphan_files_removed + removed,
                    confirmed_valid=counters.confirmed_valid + sum(
                        check.status == 'valid' for check in checks),
                    confirmed_missing=counters.confirmed_missing + sum(
                        check.status == 'missing' for check in checks),
                    confirmed_content_invalid=counters.confirmed_content_invalid + sum(
                        check.status == 'content_invalid' for check in checks))
                for check in checks:
                    if check.status != 'valid':
                        if len(occurrences) < _MAX_OCCURRENCES:
                            occurrences.append(NamespaceOccurrence(
                                job_id, check.revision_id, check.status))
                        else:
                            truncated += 1
                after = job_id
            if after == ceiling:
                break
        return NamespaceRecoveryResult(pending, counters, tuple(occurrences), truncated)
