"""Startup namespace method flows using real Linux files, SQLite and leases.

These tests do not establish provider integration or namespace readiness.
"""
import sys
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.metadata_models import MAX_SNAPSHOT_BYTES
from app.services.metadata_namespace_recovery import (
    MetadataNamespaceRecovery, NamespaceOccurrence, NamespaceRecoveryCounters,
)
from app.services.metadata_recovery import MetadataRecovery, RecoveryCounters
from app.services.metadata_recovery_files import MetadataRecoveryFiles
from app.services.metadata_store import MetadataStore
from app.services.runtime_lease import RuntimeLease
from app.services.storage_service import JobStore
from test_metadata_files import case, snapshot
from test_metadata_pending_recovery import (
    absent, collector, markers, memory, new_attempt, runtime,
)
from test_metadata_storage import (
    assert_error, coordination, make_write, sql_state, write,
)


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux namespace recovery')
MARKER = 'SENSITIVE_METADATA_MARKER'


@pytest.fixture
def empty_namespace(tmp_path):
    jobs = JobStore(tmp_path.absolute() / 'namespace-jobs')
    try:
        yield SimpleNamespace(jobs=jobs, store=MetadataStore(jobs))
    finally:
        if jobs._conn is not None:
            jobs._conn.close()


def new_job(c, owner=1):
    job = c.jobs.create('synthetic.pdf', b'%PDF-1.4\nsynthetic\n', owner_user_id=owner)
    c.jobs.update(job.job_id, status='completed', progress_percent=100)
    assert job.effective_owner == (None if owner is None else 'user:' + str(owner))
    return job


def committed(c):
    job = new_job(c)
    with c.jobs.artifact_lease(job.job_id, 'user:1') as leased:
        w = make_write(c, job=leased, model=snapshot(leased))
        assert write(c, w) == w.receipt
    w.directory = job.working_dir / 'results' / 'metadata'
    w.final = w.directory / (w.pending.revision_id + '.json')
    assert w.final.read_bytes() == w.data
    return w


def charges(c):
    with c.jobs._lock:
        conn = c.jobs._get_conn()
        return tuple(conn.execute(
            'SELECT (SELECT COUNT(*) FROM metadata_revisions), '
            '(SELECT COALESCE(SUM(size_bytes),0) FROM metadata_revisions), '
            '(SELECT COALESCE(SUM(approval_slots),0) FROM metadata_heads), '
            '(SELECT COALESCE(SUM(approval_bytes),0) FROM metadata_heads), '
            '(SELECT COALESCE(SUM(charged_slots),0) FROM metadata_pending), '
            '(SELECT COALESCE(SUM(charged_bytes),0) FROM metadata_pending)'
        ).fetchone())


def orphan(directory, temporary=False):
    directory.mkdir(parents=True, exist_ok=True)
    name = '.' + uuid4().hex + '.tmp' if temporary else uuid4().hex + '.json'
    path = directory / name
    path.write_bytes(MARKER.encode())
    return path


def test_empty_and_metadata_free_jobs_cross_real_keyset_boundary(empty_namespace, monkeypatch):
    c = empty_namespace
    recovery = collector(c)
    pages = []
    original = MetadataNamespaceRecovery._page

    def page(self, lease, after, ceiling):
        ids = original(self, lease, after, ceiling)
        pages.append((after, ceiling, ids))
        return ids

    monkeypatch.setattr(MetadataNamespaceRecovery, '_page', page)
    with runtime(c.jobs.root_dir) as held:
        result = recovery.recover_startup(held)
    assert result.pending == RecoveryCounters()
    assert result.counters == NamespaceRecoveryCounters()
    assert result.occurrences == () and result.truncated_occurrences == 0
    assert pages == [] and memory(c) == (set(), {})

    jobs = [new_job(c, owner=None if i % 2 else 1) for i in range(35)]
    with c.jobs._lock:
        durable = tuple(row[0] for row in c.jobs._get_conn().execute(
            'SELECT job_id FROM jobs ORDER BY job_id'))
    assert len(durable) == len(set(durable)) == 35
    before, baseline = sql_state(c), memory(c)
    with runtime(c.jobs.root_dir) as held:
        result = recovery.recover_startup(held)
    assert [len(p[2]) for p in pages] == [32, 3]
    assert pages[0][:2] == ('', durable[-1])
    assert pages[1][:2] == (durable[31], durable[-1])
    assert tuple(job_id for p in pages for job_id in p[2]) == durable
    assert result.counters == NamespaceRecoveryCounters(jobs_scanned=35, jobs_verified=35)
    assert result.pending == RecoveryCounters() and result.occurrences == ()
    assert sql_state(c) == before and memory(c) == baseline
    assert all(not (job.working_dir / 'results').exists() for job in jobs)


def test_committed_valid_preserved_and_canonical_orphans_removed(coordination):
    c = coordination
    w = committed(c)
    garbage = [orphan(w.directory), orphan(w.directory, temporary=True)]
    before, baseline, accounting = sql_state(c), memory(c), charges(c)
    assert baseline[1][c.job.job_id] == 1
    assert w.job.job_id not in baseline[1]
    assert accounting == (1, len(w.data), 1, MAX_SNAPSHOT_BYTES, 0, 0)
    with runtime(c.jobs.root_dir) as held:
        result = collector(c).recover_startup(held)
    assert result.pending == RecoveryCounters()
    assert result.counters == NamespaceRecoveryCounters(
        jobs_scanned=2, jobs_verified=2, orphan_files_removed=2, confirmed_valid=1)
    assert result.occurrences == () and result.truncated_occurrences == 0
    assert w.final.read_bytes() == w.data
    assert all(not path.exists() for path in garbage)
    assert sql_state(c) == before and charges(c) == accounting
    assert memory(c) == baseline


@pytest.mark.parametrize('damage', ['missing', 'content_invalid'])
def test_confirmed_damage_reports_only_fixed_fields_and_remains_charged(coordination, damage):
    c = coordination
    w = committed(c)
    if damage == 'missing':
        w.final.unlink()
    else:
        w.final.write_bytes(MARKER.encode())
    garbage = orphan(w.directory)
    before, baseline, accounting = sql_state(c), memory(c), charges(c)
    with runtime(c.jobs.root_dir) as held:
        result = collector(c).recover_startup(held)
    expected = dict(jobs_scanned=2, jobs_verified=2, orphan_files_removed=1)
    expected['confirmed_' + damage] = 1
    assert result.counters == NamespaceRecoveryCounters(**expected)
    assert result.occurrences == (NamespaceOccurrence(
        w.job.job_id, w.pending.revision_id, damage),)
    assert result.truncated_occurrences == 0 and result.pending == RecoveryCounters()
    assert MARKER not in repr(result.occurrences)
    assert '.json' not in repr(result.occurrences) and '.tmp' not in repr(result.occurrences)
    assert not garbage.exists()
    if damage == 'missing':
        assert not w.final.exists()
    else:
        assert w.final.read_bytes() == MARKER.encode()
    assert sql_state(c) == before and charges(c) == accounting
    assert accounting == (1, len(w.data), 1, MAX_SNAPSHOT_BYTES, 0, 0)
    assert memory(c) == baseline


def test_active_and_cleanup_pending_finish_before_namespace_inventory(coordination, monkeypatch):
    c = coordination
    attempts = [new_attempt(c, state=state, payload='both')
                for state in ('active', 'cleanup')]
    recovery = collector(c)
    baseline, before = memory(c), sql_state(c)
    reserved = charges(c)
    assert reserved == (0, 0, 0, 0, 4,
                        sum(len(w.data) for w in attempts) + 2 * MAX_SNAPSHOT_BYTES)
    phase, opens, completions = ['pending'], [], []
    original_pending, original_open = recovery.recover_pending_startup, recovery.physical.open

    def recover_pending(held):
        result = original_pending(held)
        completions.append(result)
        for w in attempts:
            absent(c, w)
        assert charges(c) == (0, 0, 0, 0, 0, 0)
        phase[0] = 'namespace'
        return result

    @contextmanager
    def opened(job):
        opens.append((phase[0], job.job_id))
        with original_open(job) as directory:
            yield directory

    monkeypatch.setattr(recovery, 'recover_pending_startup', recover_pending)
    monkeypatch.setattr(recovery.physical, 'open', opened)
    with runtime(c.jobs.root_dir) as held:
        result = recovery.recover_startup(held)
    assert completions == [RecoveryCounters(2, 2)] and result.pending == completions[0]
    assert [p for p, _ in opens] == ['pending', 'pending'] + ['namespace'] * 3
    assert {j for p, j in opens if p == 'pending'} == {w.job.job_id for w in attempts}
    assert len({j for p, j in opens if p == 'namespace'}) == 3
    assert result.counters == NamespaceRecoveryCounters(jobs_scanned=3, jobs_verified=3)
    after = sql_state(c)
    assert len(before['metadata_pending']) == 2 and after['metadata_pending'] == ()
    assert all(after[t] == before[t] for t in before if t != 'metadata_pending')
    assert memory(c) == baseline and result.occurrences == ()


def test_purged_owned_ledger_and_unowned_empty_skip_physical(coordination, monkeypatch):
    c = coordination
    w = committed(c)
    c.jobs.remove_artifacts(w.job.job_id, 'user:1', 'user:1')
    owned_markers = markers(c, w.job.job_id)
    assert owned_markers[1] is not None and owned_markers[2] is not None
    assert not w.job.working_dir.exists() and charges(c)[0] == 1
    unowned = new_job(c, owner=None)
    # Authority fixture only: these markers do not prove physical deletion.
    now = datetime.now(timezone.utc)
    with c.jobs._lock:
        cached = c.jobs.jobs[unowned.job_id]
        cached.artifacts_removed_at = cached.artifacts_purged_at = now
        c.jobs._persist(cached)
    unowned_markers = markers(c, unowned.job_id)
    input_path = unowned.working_dir / 'original.pdf'
    retained_input = input_path.read_bytes()
    baseline = memory(c)
    recovery = collector(c)
    reconciled, opened_ids = [], []
    original_reconcile, original_open = c.store.reconcile_purged_job, recovery.physical.open

    def reconcile(job_id):
        reconciled.append(job_id)
        return original_reconcile(job_id)

    @contextmanager
    def opened(job):
        opened_ids.append(job.job_id)
        with original_open(job) as directory:
            yield directory

    monkeypatch.setattr(c.store, 'reconcile_purged_job', reconcile)
    monkeypatch.setattr(recovery.physical, 'open', opened)
    with runtime(c.jobs.root_dir) as held:
        result = recovery.recover_startup(held)
    assert reconciled == [w.job.job_id] and opened_ids == [c.job.job_id]
    assert result.counters == NamespaceRecoveryCounters(jobs_scanned=3, jobs_verified=1, purged_jobs=2)
    assert result.pending == RecoveryCounters() and result.occurrences == ()
    assert all(not rows for rows in sql_state(c).values())
    assert charges(c) == (0, 0, 0, 0, 0, 0) and memory(c) == baseline
    assert markers(c, w.job.job_id) == owned_markers
    assert markers(c, unowned.job_id) == unowned_markers
    assert c.jobs.jobs[unowned.job_id].effective_owner is None
    assert input_path.read_bytes() == retained_input


def test_real_129_missing_confirmed_jobs_bound_occurrences_and_freeze_results(coordination):
    c = coordination
    attempts = [committed(c) for _ in range(129)]
    for w in attempts:
        w.final.unlink()
    before, baseline, accounting = sql_state(c), memory(c), charges(c)
    with runtime(c.jobs.root_dir) as held:
        result = collector(c).recover_startup(held)
    assert result.counters == NamespaceRecoveryCounters(
        jobs_scanned=130, jobs_verified=130, confirmed_missing=129)
    ordered = sorted(attempts, key=lambda w: w.job.job_id)
    assert type(result.occurrences) is tuple
    assert result.occurrences == tuple(NamespaceOccurrence(
        w.job.job_id, w.pending.revision_id, 'missing') for w in ordered[:128])
    assert result.truncated_occurrences == 1 and result.pending == RecoveryCounters()
    for obj, field in ((result, 'truncated_occurrences'), (result.counters, 'jobs_scanned'),
                       (result.pending, 'scanned'), (result.occurrences[0], 'code')):
        with pytest.raises(FrozenInstanceError):
            setattr(obj, field, None)
    assert sql_state(c) == before and charges(c) == accounting
    assert accounting[:3] == (129, sum(len(w.data) for w in attempts), 129)
    assert memory(c) == baseline


def test_invalid_runtime_authority_rejects_before_sql_or_namespace_io(coordination, monkeypatch, tmp_path):
    c = coordination
    recovery = collector(c)
    before, baseline = sql_state(c), memory(c)
    released = RuntimeLease(c.jobs.root_dir)
    released.acquire()
    released.release()
    transactions, opened_ids = [], []
    original_transaction, original_open = c.store._transaction, recovery.physical.open

    @contextmanager
    def transaction():
        transactions.append(True)
        with original_transaction() as conn:
            yield conn

    @contextmanager
    def opened(job):
        opened_ids.append(job.job_id)
        with original_open(job) as directory:
            yield directory

    with runtime(tmp_path.absolute() / 'wrong-root') as wrong:
        with monkeypatch.context() as patch:
            patch.setattr(c.store, '_transaction', transaction)
            patch.setattr(recovery.physical, 'open', opened)
            for invalid in (None, released, wrong):
                assert_error(lambda: recovery.recover_startup(invalid))
                assert transactions == [] and opened_ids == []
            with runtime(c.jobs.root_dir) as held:
                result = recovery.recover_startup(held)
    assert transactions and opened_ids == [c.job.job_id]
    assert result.counters.jobs_verified == 1
    assert sql_state(c) == before and memory(c) == baseline


def test_ordinary_physical_exit_failure_releases_only_own_maintenance_count(coordination, monkeypatch):
    c = coordination
    recovery = collector(c)
    before, baseline = sql_state(c), memory(c)
    hits = []
    original_open = recovery.physical.open

    @contextmanager
    def failing_open(job):
        with original_open(job) as directory:
            hits.append((job.job_id, memory(c)))
            yield directory
            raise OSError(MARKER)

    with runtime(c.jobs.root_dir) as held:
        with monkeypatch.context() as patch:
            patch.setattr(recovery.physical, 'open', failing_open)
            assert_error(lambda: recovery.recover_startup(held))
        assert len(hits) == 1 and hits[0][0] == c.job.job_id
        assert hits[0][1] == (baseline[0], {c.job.job_id: baseline[1][c.job.job_id] + 1})
        assert memory(c) == baseline and sql_state(c) == before
        result = recovery.recover_startup(held)
    assert result.counters == NamespaceRecoveryCounters(jobs_scanned=1, jobs_verified=1)
    assert memory(c) == baseline and sql_state(c) == before
