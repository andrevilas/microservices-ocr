"""SQL authority tests only: fixture commits do not prove physical durability.

No startup readiness, file-content validity or deletion is inferred here.
Global corruption may be rejected before name classification; only the valid
foreign-ledger cases specifically exercise contradictory namespace references.
"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.application import ApplicationError
from app.services.metadata_namespace_authority import MetadataNamespaceAuthority
from app.services.metadata_recovery import MetadataRecovery
from app.services.metadata_recovery_files import MetadataRecoveryFiles
from test_metadata_store import (
    h, principal, args, reserve, commit, save, sql, state, totals,
)


@pytest.fixture
def authority(h):
    return MetadataNamespaceAuthority(MetadataRecovery(
        h.svc, MetadataRecoveryFiles(h.jobs.root_dir)))


def _load(h, authority, job_id=None):
    with h.svc._transaction() as conn:
        return authority.load_locked(conn, job_id or h.job.job_id)


def _plan(h, authority, names=(), job_id=None):
    with h.svc._transaction() as conn:
        return authority.plan_locked(conn, job_id or h.job.job_id, names)


def _snapshot(h):
    # These helpers acquire the shared lock: call only outside a transaction.
    return (state(h), sql(h, 'SELECT * FROM jobs ORDER BY job_id'), totals(h),
            dict(h.jobs._artifact_leases), set(h.svc._active_writes))


@contextmanager
def _unavailable():
    with pytest.raises(ApplicationError) as caught:
        yield
    exc = caught.value
    assert (exc.status, exc.code, exc.message) == (
        503, 'metadata_unavailable', 'Metadata is unavailable.')
    assert exc.__cause__ is None and exc.__context__ is None
    assert 'SENSITIVE_MARKER' not in str(exc) + repr(exc) + exc.message


def _reject(h, authority, names=()):
    before = _snapshot(h)
    with _unavailable():
        _plan(h, authority, names)
    assert _snapshot(h) == before


def _change(h, table, column, value):
    # Mutate exactly one existing row, then independently read back the fault.
    rowid = sql(h, 'SELECT rowid FROM ' + table +
                ' ORDER BY rowid DESC LIMIT 1')[0][0]
    sql(h, 'UPDATE ' + table + ' SET ' + column + '=? WHERE rowid=?',
        (value, rowid))
    assert sql(h, 'SELECT ' + column + ' FROM ' + table +
               ' WHERE rowid=?', (rowid,)) == [(value,)]


def _history(h, principal):
    first = save(h, principal, args())
    head = h.svc.get_head(principal, h.job.job_id)
    second = save(h, principal, args(head))
    head = h.svc.get_head(principal, h.job.job_id)
    pending = reserve(h, principal, args(head)).pending
    return first, second, pending


def _other(h, principal, owner):
    job = h.jobs.create('other.pdf', b'synthetic', owner_user_id=owner)
    h.jobs.update(job.job_id, status='completed', progress_percent=100)
    other = SimpleNamespace(jobs=h.jobs, svc=h.svc, job=job, path=h.path)
    return other, replace(principal, subject=f'user:{owner}', user_id=owner)


def test_empty_owned_state_is_detached_and_bound_to_cache(h, authority):
    before = _snapshot(h)
    loaded = _load(h, authority)
    binding = loaded.binding
    assert loaded.cached_job is h.job and loaded.job is not h.job
    assert loaded.owner == 'user:1' and loaded.purged_at is None
    assert not loaded.has_ledger and loaded.confirmed == () and loaded.pending is None
    loaded.job.status = 'failed'
    assert h.job.status == 'completed' and loaded.binding != binding
    assert _load(h, authority).binding == binding
    assert _plan(h, authority).protected_names == ()
    assert _snapshot(h) == before


@pytest.mark.parametrize('status', [
    'queued', 'processing', 'cancel_requested', 'failed', 'canceled',
])
def test_noncompleted_jobs_without_ledger(h, authority, status):
    h.jobs.update(h.job.job_id, status=status)
    before = _snapshot(h)
    assert _load(h, authority).job.status == status
    assert _plan(h, authority).orphans == ()
    assert _snapshot(h) == before


@pytest.mark.parametrize('owner', [None, 'legacy:api-key'])
def test_unowned_and_legacy_jobs_without_ledger(h, authority, owner):
    job = h.jobs.create('legacy.pdf', b'synthetic', owner_user_id=None,
                        owner_principal_id=owner)
    h.jobs.update(job.job_id, status='completed', progress_percent=100)
    before = _snapshot(h)
    loaded = _load(h, authority, job.job_id)
    assert loaded.owner == owner and not loaded.has_ledger
    assert _plan(h, authority, job_id=job.job_id).protected_names == ()
    assert _snapshot(h) == before


def test_admitted_path_is_accepted_when_durable_and_cached_paths_agree(h, authority):
    # This SQL helper does not require the syntactically valid directory to exist.
    path = h.jobs.root_dir / 'admitted-jobs' / h.job.job_id
    _change(h, 'jobs', 'working_dir', str(path))
    h.job.working_dir = path
    before = _snapshot(h)
    assert _load(h, authority).job.working_dir == path
    assert _plan(h, authority).orphans == ()
    assert _snapshot(h) == before


def test_confirmed_audit_expectations_pending_and_unobserved_protection(h, principal, authority):
    first, second, pending = _history(h, principal)
    before = _snapshot(h)
    protected = {first.revision_id + '.json', second.revision_id + '.json',
                 pending.revision_id + '.json', '.' + pending.pending_id + '.tmp'}
    orphans = (uuid4().hex + '.json', '.' + uuid4().hex + '.tmp')
    names = (orphans[0], second.revision_id + '.json', orphans[1])
    plan = _plan(h, authority, names)
    assert plan.state.pending == pending and plan.state.has_ledger
    assert plan.orphans == tuple(sorted(orphans))
    assert plan.protected_names == tuple(sorted(protected))
    assert not set(plan.orphans).intersection(plan.protected_names)
    assert protected - set(names)  # Protection does not require observing a file.
    with h.svc._transaction() as conn:
        rows = conn.execute(
            'SELECT r.revision_id,r.parent_revision_id,r.sha256,r.size_bytes,'
            'r.state,r.created_at,a.actor,a.auth_source FROM metadata_revisions r '
            'JOIN metadata_audit a ON a.revision_id=r.revision_id '
            'WHERE r.job_id=? ORDER BY r.revision_id', (h.job.job_id,)).fetchall()
    expected = tuple((*row[:5], datetime.fromisoformat(row[5]), *row[6:]) for row in rows)
    actual = tuple((v.revision_id, v.parent_revision_id, v.sha256, v.size_bytes,
                    v.state, v.created_at, v.actor, v.auth_source)
                   for v in plan.state.confirmed)
    assert actual == expected and len(actual) == 2
    assert all(v.state == 'draft' for v in plan.state.confirmed)
    assert next(v for v in plan.state.confirmed if v.revision_id == second.revision_id
                ).parent_revision_id == first.revision_id
    assert _plan(h, authority).protected_names == plan.protected_names
    assert _snapshot(h) == before  # Includes the pending charge and all five tables.
    assert authority.recovery._periodic_after == ''
    assert authority.recovery._periodic_ceiling is None


def test_inventory_bounds_and_sorted_orphans(h, authority):
    names = tuple(f'{i:032x}.json' for i in range(128))
    before = _snapshot(h)
    assert _plan(h, authority).orphans == ()
    assert _plan(h, authority, tuple(reversed(names))).orphans == names
    assert _snapshot(h) == before
    _reject(h, authority, names + (f'{128:032x}.json',))


@pytest.mark.parametrize('names', [
    [], (None,), (17,), ('A' * 32 + '.json',), ('a.json',),
    ('a' * 32 + '.json',) * 2, ('.' + 'A' * 32 + '.tmp',),
])
def test_invalid_inventory_fails_closed(h, authority, names):
    assert _plan(h, authority).orphans == ()
    _reject(h, authority, names)


@pytest.mark.parametrize('with_ledger', [False, True])
def test_durable_owned_purge_loads_without_collecting_sql(h, principal, authority, with_ledger):
    if with_ledger:
        _history(h, principal)
    assert _load(h, authority).purged_at is None
    h.jobs.remove_artifacts(h.job.job_id, owner=principal.subject, actor=principal.subject)
    assert h.job.artifacts_purged_at is not None
    before = _snapshot(h)
    loaded = _load(h, authority)
    assert loaded.purged_at == h.job.artifacts_purged_at
    assert loaded.has_ledger == with_ledger
    assert _snapshot(h) == before
    _reject(h, authority)


def test_unowned_empty_purge_marker_loads_without_collection(h, authority):
    job = h.jobs.create('unowned.pdf', b'synthetic', owner_user_id=None)
    h.jobs.update(job.job_id, status='completed', progress_percent=100)
    assert _load(h, authority, job.job_id).purged_at is None
    # No owner can authorize remove_artifacts: install a consistent synthetic
    # durable/cache marker solely to exercise SQL diagnosis, not physical purge.
    stamp = datetime.now(timezone.utc) - timedelta(seconds=1)
    sql(h, 'UPDATE jobs SET artifacts_removed_at=?,artifacts_purged_at=? WHERE job_id=?',
        (stamp.isoformat(), stamp.isoformat(), job.job_id))
    assert sql(h, 'SELECT artifacts_removed_at,artifacts_purged_at FROM jobs WHERE job_id=?',
               (job.job_id,)) == [(stamp.isoformat(), stamp.isoformat())]
    job.artifacts_removed_at = job.artifacts_purged_at = stamp
    before = _snapshot(h)
    assert _load(h, authority, job.job_id).purged_at == stamp
    with _unavailable():
        _plan(h, authority, job_id=job.job_id)
    assert _snapshot(h) == before


@pytest.mark.parametrize('owner', [1, 2])
@pytest.mark.parametrize('kind', ['confirmed', 'pending_temp', 'pending_final'])
def test_valid_foreign_references_are_contradictions(h, principal, authority, owner, kind):
    other, p = _other(h, principal, owner)
    confirmed = save(other, p, args())
    head = h.svc.get_head(p, other.job.job_id)
    pending = reserve(other, p, args(head)).pending
    name = {'confirmed': confirmed.revision_id + '.json',
            'pending_temp': '.' + pending.pending_id + '.tmp',
            'pending_final': pending.revision_id + '.json'}[kind]
    # Both ledgers pass global validation; failure below is not malformed setup.
    assert _plan(h, authority).state.job.job_id == h.job.job_id
    foreign = _plan(h, authority, (name,), other.job.job_id)
    assert name in foreign.protected_names and foreign.orphans == ()
    _reject(h, authority, (name,))


@pytest.mark.parametrize('fault', ['missing_job', 'job_id', 'owner', 'purge'])
def test_global_corruption_blocks_an_unrelated_empty_job(h, principal, authority, fault):
    other, p = _other(h, principal, 2)
    reserve(other, p, args())
    assert _plan(h, authority).state.pending is None
    table, column, value = {
        'missing_job': ('metadata_pending', 'job_id', uuid4().hex),
        'job_id': ('metadata_pending', 'job_id', 'g' * 32),
        'owner': ('metadata_pending', 'owner', 'user:01'),
        'purge': ('jobs', 'artifacts_purged_at', 'SENSITIVE_MARKER'),
    }[fault]
    if fault == 'purge':
        sql(h, 'UPDATE jobs SET artifacts_purged_at=? WHERE job_id=?', (value, other.job.job_id))
        assert sql(h, 'SELECT artifacts_purged_at FROM jobs WHERE job_id=?',
                   (other.job.job_id,)) == [(value,)]
    else:
        _change(h, table, column, value)
    _reject(h, authority)


def test_oversized_local_ledger_is_rejected_without_materialization_or_release(h, principal, authority):
    confirmed = save(h, principal, args())
    assert _plan(h, authority).state.has_ledger
    for _ in range(32):
        sql(h, 'INSERT INTO metadata_revisions '
            'SELECT ?,owner,job_id,revision_id,sha256,size_bytes,state,created_at '
            'FROM metadata_revisions WHERE revision_id=?', (uuid4().hex, confirmed.revision_id))
    assert sql(h, 'SELECT COUNT(*) FROM metadata_revisions') == [(33,)]
    _reject(h, authority)


def test_active_claim_is_rejected_and_never_removed(h, principal, authority):
    pending = reserve(h, principal, args()).pending
    assert _plan(h, authority).state.pending == pending
    h.svc._active_writes.add(pending.pending_id)
    try:
        assert pending.pending_id in h.svc._active_writes
        _reject(h, authority)
    finally:
        h.svc._active_writes.discard(pending.pending_id)


@pytest.mark.parametrize('field', [
    'owner_principal_id', 'status', 'working_dir', 'parent_job_id',
    'artifacts_removed_at', 'artifacts_purged_at', 'missing',
])
def test_cache_disagreement_fails_closed(h, authority, field):
    assert _load(h, authority).cached_job is h.job
    values = dict(owner_principal_id='user:2', status='failed',
                  working_dir=h.jobs.root_dir / 'elsewhere', parent_job_id=uuid4().hex,
                  artifacts_removed_at=datetime.now(timezone.utc),
                  artifacts_purged_at=datetime.now(timezone.utc))
    if field == 'missing':
        assert h.jobs.jobs.pop(h.job.job_id) is h.job
    else:
        setattr(h.job, field, values[field])
        assert getattr(h.job, field) == values[field]
    _reject(h, authority)


@pytest.mark.parametrize('fault', ['relative', 'escape', 'owner', 'status', 'noncompleted'])
def test_durable_bindings_and_ledger_status_fail_closed(h, principal, authority, fault):
    save(h, principal, args())
    assert _plan(h, authority).state.has_ledger
    column, value = {
        'relative': ('working_dir', 'relative/' + h.job.job_id),
        'escape': ('working_dir', str(h.jobs.root_dir / '..' / h.job.job_id)),
        'owner': ('owner_principal_id', 'user:01'),
        'status': ('status', 'SENSITIVE_MARKER'),
        'noncompleted': ('status', 'failed'),
    }[fault]
    _change(h, 'jobs', column, value)
    setattr(h.job, column, Path(value) if column == 'working_dir' else value)
    _reject(h, authority)  # Syntactic paths only: no stat or reparse assertion.


@pytest.mark.parametrize('table,column,value', [
    ('metadata_operations', 'receipt_json', '{SENSITIVE_MARKER'),
    ('metadata_operations', 'revision_id', 'e' * 32),
    ('metadata_audit', 'actor', 'user:2'),
    ('metadata_audit', 'sha256', 'SENSITIVE_MARKER'),
    ('metadata_audit', 'parent_revision_id', 'e' * 32),
    ('metadata_revisions', 'parent_revision_id', 'e' * 32),
    ('metadata_heads', 'revision_id', 'e' * 32),
    ('metadata_pending', 'expected_head', 'e' * 32),
])
def test_individual_ledger_corruption_preserves_all_rows_and_charges(
        h, principal, authority, table, column, value):
    _history(h, principal)
    assert _plan(h, authority).state.pending is not None
    if value == 'e' * 32:
        value = uuid4().hex  # A new dangling reference, never a duplicate PK.
    _change(h, table, column, value)
    _reject(h, authority)


def test_constructor_and_foreign_connection_are_denied(h, authority):
    assert _load(h, authority).job.job_id == h.job.job_id
    before = _snapshot(h)
    with _unavailable():
        MetadataNamespaceAuthority(object())
    foreign = sqlite3.connect(':memory:')
    try:
        foreign.execute('BEGIN IMMEDIATE')
        with _unavailable():
            with h.svc._transaction():
                authority.load_locked(foreign, h.job.job_id)
    finally:
        foreign.close()
    assert _snapshot(h) == before


def test_helpers_remain_read_only_under_sql_and_filesystem_tripwires(
        h, principal, authority, monkeypatch):
    other, p = _other(h, principal, 2)
    confirmed = save(other, p, args())
    assert _plan(h, authority).orphans == ()
    forbidden = {getattr(sqlite3, 'SQLITE_' + op) for op in (
        'INSERT UPDATE DELETE CREATE_INDEX CREATE_TABLE CREATE_TRIGGER CREATE_VIEW '
        'CREATE_TEMP_INDEX CREATE_TEMP_TABLE CREATE_TEMP_TRIGGER CREATE_TEMP_VIEW '
        'DROP_INDEX DROP_TABLE DROP_TRIGGER DROP_VIEW DROP_TEMP_INDEX DROP_TEMP_TABLE '
        'DROP_TEMP_TRIGGER DROP_TEMP_VIEW ALTER_TABLE CREATE_VTABLE DROP_VTABLE '
        'REINDEX ANALYZE ATTACH DETACH').split()}
    for failing in (False, True):
        before = _snapshot(h)
        hits = dict(read=0, write=0, io=0)
        def authorize(action, *unused):
            if action == sqlite3.SQLITE_READ:
                hits['read'] += 1
            if action in forbidden:
                hits['write'] += 1
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        def deny_io(*unused, **kwargs):
            hits['io'] += 1
            raise RuntimeError('SENSITIVE_MARKER')
        with h.svc._transaction() as conn:
            conn.set_authorizer(authorize)
            try:
                with monkeypatch.context() as patch:
                    patch.setattr(authority.recovery.physical, 'open', deny_io)
                    for method in ('open read_bytes read_text write_bytes write_text '
                                   'stat lstat resolve iterdir glob rglob mkdir unlink '
                                   'rename replace rmdir exists is_file is_dir touch '
                                   'chmod readlink symlink_to hardlink_to').split():
                        patch.setattr(Path, method, deny_io)
                    if failing:
                        with _unavailable():
                            authority.plan_locked(conn, h.job.job_id,
                                                  (confirmed.revision_id + '.json',))
                    else:
                        result = authority.plan_locked(conn, h.job.job_id, ())
                        assert result.orphans == () and result.protected_names == ()
            finally:
                conn.set_authorizer(None)
        assert hits['read'] > 0 and hits['write'] == hits['io'] == 0
        assert _snapshot(h) == before
