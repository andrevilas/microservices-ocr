"""Real SQLite index tests; private commits do not prove physical file durability.

The two P2 regression tests intentionally assert contracts the current product
violates. They are neither skipped nor marked xfail.
"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.application import ApplicationError, Principal
from app.metadata_models import SaveMetadataReceipt
from app.services.metadata_store import MetadataStore
from app.services.storage_service import JobStore

MIB = 1024 * 1024
TABLES = ('metadata_heads', 'metadata_revisions', 'metadata_operations',
          'metadata_pending', 'metadata_audit')
SCOPES = frozenset(('jobs:read', 'artifacts:read', 'metadata:read', 'metadata:write'))


@pytest.fixture
def principal():
    return Principal(subject='user:1', user_id=1, scopes=SCOPES, auth_source='local_jwt')


@pytest.fixture
def h(tmp_path):
    jobs = JobStore(tmp_path)
    job = jobs.create('synthetic.pdf', b'synthetic', owner_user_id=1)
    jobs.update(job.job_id, status='completed', progress_percent=100)
    harness = SimpleNamespace(jobs=jobs, svc=MetadataStore(jobs),
                              job=jobs.jobs[job.job_id], path=tmp_path)
    try:
        yield harness
    finally:
        harness.jobs._conn.close()


def args(head=None, **updates):
    result = dict(key_hash=uuid4().hex * 2, request_hash=uuid4().hex * 2,
                  expected_head=head.revision_id if head else None,
                  expected_sha256=head.sha256 if head else None,
                  revision_id=uuid4().hex, sha256=uuid4().hex * 2,
                  size_bytes=123, created_at=datetime.now(timezone.utc))
    result.update(updates)
    return result


def reserve(h, p, values):
    return h.svc.reserve(p, h.job.job_id, **values)


def receipt_for(pending):
    return SaveMetadataReceipt(revision_id=pending.revision_id, sha256=pending.sha256,
                               segment_ids={'s': uuid4().hex})


def commit(h, p, pending, receipt=None):
    if receipt is None:
        receipt = receipt_for(pending)
    with h.svc._transaction() as conn:
        return h.svc._commit_index_locked(conn, p, pending.pending_id, receipt)


def save(h, p, values):
    pending = reserve(h, p, values).pending
    return commit(h, p, pending)


def sql(h, statement, parameters=()):
    with h.jobs._lock:
        conn = h.jobs._get_conn()
        result = conn.execute(statement, parameters).fetchall()
        conn.commit()
        return result


def state(h):
    with h.jobs._lock:
        conn = h.jobs._get_conn()
        return {t: conn.execute('SELECT * FROM ' + t + ' ORDER BY rowid').fetchall()
                for t in TABLES}


def totals(h):
    return sql(h, '''SELECT
        (SELECT COUNT(*) FROM metadata_revisions) +
        COALESCE((SELECT SUM(approval_slots) FROM metadata_heads),0) +
        COALESCE((SELECT SUM(charged_slots) FROM metadata_pending),0),
        COALESCE((SELECT SUM(size_bytes) FROM metadata_revisions),0) +
        COALESCE((SELECT SUM(approval_bytes) FROM metadata_heads),0) +
        COALESCE((SELECT SUM(charged_bytes) FROM metadata_pending),0)''')[0]


@contextmanager
def error(status, code):
    with pytest.raises(ApplicationError) as caught:
        yield
    exc = caught.value
    assert (exc.status, exc.code) == (status, code)
    assert exc.__cause__ is None and exc.__context__ is None
    assert 'SENSITIVE_MARKER' not in str(exc) + repr(exc) + exc.message


def restart(h):
    job_id = h.job.job_id
    h.jobs._conn.close()
    h.jobs = JobStore(h.path)
    h.svc = MetadataStore(h.jobs)
    h.job = h.jobs.jobs[job_id]


def expire(h):
    with h.jobs._lock:
        h.job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        h.jobs._persist(h.job)
    # Retention belongs to JobStore: preserve its actual public error code.
    with pytest.raises(ApplicationError) as caught:
        h.jobs.check_retained(h.job)
    assert caught.value.status == 410
    return caught.value.code


def test_reservations_commits_and_retention_timestamps(h, principal):
    timestamps = (h.job.updated_at, h.job.artifacts_expires_at)
    first = args()
    admission = reserve(h, principal, first)
    pending = admission.pending
    assert admission.head is None and admission.receipt is None
    assert (pending.charged_slots, pending.charged_bytes) == (2, 123 + MIB)
    assert (pending.approval_slots, pending.approval_bytes) == (1, MIB)
    assert {t: len(v) for t, v in state(h).items()} == dict(zip(TABLES, (0, 0, 0, 1, 0)))
    commit(h, principal, pending)
    head = h.svc.get_head(principal, h.job.job_id)
    assert (head.revision_id, head.state, head.approval_slots, head.approval_bytes) == (
        first['revision_id'], 'draft', 1, MIB)
    assert {t: len(v) for t, v in state(h).items()} == dict(zip(TABLES, (1, 1, 1, 0, 1)))
    assert totals(h) == (2, 123 + MIB)
    second = reserve(h, principal, args(head, size_bytes=456)).pending
    assert (second.charged_slots, second.charged_bytes) == (1, 456)
    assert (second.approval_slots, second.approval_bytes) == (0, 0)
    charged = totals(h)
    commit(h, principal, second)
    assert totals(h) == charged == (3, 579 + MIB)
    revisions = {r.revision_id: r for r in h.svc.list_revisions(principal, h.job.job_id)}
    assert len(revisions) == 2 and revisions[first['revision_id']].parent_revision_id is None
    assert revisions[second.revision_id].parent_revision_id == first['revision_id']
    assert (h.job.updated_at, h.job.artifacts_expires_at) == timestamps


def test_replay_is_detached_survives_new_head_and_restart(h, principal):
    first = args()
    original = save(h, principal, first)
    expected_map = dict(original.segment_ids)
    latest = save(h, principal, args(h.svc.get_head(principal, h.job.job_id)))
    before = state(h)
    replay = reserve(h, principal, first)  # Well-formed, stale (None, None) CAS.
    assert replay.receipt == original and replay.head.revision_id == latest.revision_id
    assert replay.pending is None
    replay.receipt.segment_ids['s'] = uuid4().hex
    assert reserve(h, principal, first).receipt.segment_ids == expected_map
    restart(h)
    admission = h.svc.preflight(principal, h.job.job_id,
                                key_hash=first['key_hash'], request_hash=first['request_hash'])
    assert admission.receipt == original and admission.head.revision_id == latest.revision_id
    assert reserve(h, principal, first).receipt.segment_ids == expected_map
    with error(409, 'idempotency_conflict'):
        reserve(h, principal, dict(first, request_hash=uuid4().hex * 2))
    assert state(h) == before


def test_same_key_is_scoped_to_job_and_owner(h, principal):
    first = args()
    save(h, principal, first)
    for owner in (1, 2):
        job = h.jobs.create('synthetic.pdf', b'synthetic', owner_user_id=owner)
        h.jobs.update(job.job_id, status='completed', progress_percent=100)
        p = replace(principal, subject=f'user:{owner}', user_id=owner)
        admission = h.svc.reserve(p, job.job_id, **args(
            key_hash=first['key_hash'], request_hash=first['request_hash']))
        assert admission.receipt is None and admission.head is None
        assert admission.pending.owner == p.subject


def test_foreign_owner_including_admin_is_hidden(h, principal):
    before = state(h)
    for admin in (False, True):
        foreign = replace(principal, subject='user:2', user_id=2, is_admin=admin)
        with error(404, 'job_not_found'):
            reserve(h, foreign, args())
        with error(404, 'job_not_found'):
            h.svc.get_head(foreign, h.job.job_id)
    assert state(h) == before


@pytest.mark.parametrize('missing', sorted(SCOPES))
def test_every_write_scope_is_required(h, principal, missing):
    before = state(h)
    with error(403, 'insufficient_scope'):
        reserve(h, replace(principal, scopes=SCOPES - {missing}), args())
    assert state(h) == before


def test_reads_do_not_require_write_scope(h, principal):
    saved = save(h, principal, args())
    reader = replace(principal, scopes=SCOPES - {'metadata:write'})
    assert h.svc.get_head(reader, h.job.job_id).revision_id == saved.revision_id
    assert [r.revision_id for r in h.svc.list_revisions(reader, h.job.job_id)] == [saved.revision_id]


def test_queued_job_and_expired_replay_are_rejected(h, principal):
    queued = h.jobs.create('synthetic.pdf', b'synthetic', owner_user_id=1)
    with error(409, 'job_not_completed'):
        h.svc.reserve(principal, queued.job_id, **args())
    values = args()
    save(h, principal, values)
    code = expire(h)
    before = state(h)
    with error(410, code):
        reserve(h, principal, values)
    with error(410, code):
        h.svc.preflight(principal, h.job.job_id,
                        key_hash=values['key_hash'], request_hash=values['request_hash'])
    assert state(h) == before


def test_expiration_after_reservation_preserves_pending_and_old_head(h, principal):
    save(h, principal, args())
    pending = reserve(h, principal, args(h.svc.get_head(principal, h.job.job_id))).pending
    code = expire(h)
    before = state(h)
    with error(410, code):
        commit(h, principal, pending)
    assert state(h) == before


def test_invalid_reservation_inputs_and_wrong_cas_do_not_write(h, principal):
    before = state(h)
    invalid = [{'size_bytes': v} for v in (True, 0, MIB + 1)]
    invalid += [{'expected_head': uuid4().hex}, {'created_at': datetime(2026, 1, 1)}]
    for updates in invalid:
        with error(422, 'metadata_invalid_input'):
            reserve(h, principal, args(**updates))
        assert state(h) == before
    with error(409, 'metadata_revision_conflict'):
        reserve(h, principal, args(expected_head=uuid4().hex, expected_sha256='a' * 64))
    assert state(h) == before


def test_invalid_copied_receipt_and_mismatched_bindings_are_atomic(h, principal):
    pending = reserve(h, principal, args()).pending
    valid = receipt_for(pending)
    cases = [(valid.model_copy(update={'segment_ids': {'s': True}}), 422, 'metadata_invalid_input'),
             (valid.model_copy(update={'revision_id': uuid4().hex}), 409, 'metadata_revision_conflict'),
             (valid.model_copy(update={'sha256': 'f' * 64 if valid.sha256 != 'f' * 64 else 'e' * 64}),
              409, 'metadata_revision_conflict')]
    before = state(h)
    for receipt, status, code in cases:
        with error(status, code):
            commit(h, principal, pending, receipt)
        assert state(h) == before


def test_audit_failure_rolls_back_every_index_write_and_can_retry(h, principal):
    pending = reserve(h, principal, args()).pending
    receipt = receipt_for(pending)
    sql(h, "CREATE TRIGGER fail_audit BEFORE INSERT ON metadata_audit BEGIN SELECT RAISE(ABORT,'SENSITIVE_MARKER'); END")
    before = state(h)
    with error(503, 'metadata_unavailable'):
        commit(h, principal, pending, receipt)
    assert state(h) == before
    sql(h, 'DROP TRIGGER fail_audit')
    assert commit(h, principal, pending, receipt) == receipt
    assert {t: len(v) for t, v in state(h).items()} == dict(zip(TABLES, (1, 1, 1, 0, 1)))


def test_private_helpers_require_shared_connection_and_active_transaction(h, principal):
    pending = reserve(h, principal, args()).pending
    receipt = receipt_for(pending)
    before = state(h)
    foreign = sqlite3.connect(':memory:')
    try:
        foreign.execute('BEGIN IMMEDIATE')
        with h.jobs._lock:
            for conn in (foreign, h.jobs._get_conn()):
                for invoke in (
                    lambda: h.svc._commit_index_locked(conn, principal, pending.pending_id, receipt),
                    lambda: h.svc._mark_cleanup_locked(conn, pending.pending_id),
                    lambda: h.svc._release_pending_after_cleanup_locked(conn, pending.pending_id),
                ):
                    with error(503, 'metadata_unavailable'):
                        invoke()
    finally:
        foreign.close()
    assert state(h) == before


def test_private_commit_leaves_transaction_open_and_caller_failure_rolls_back(h, principal):
    pending = reserve(h, principal, args()).pending
    receipt = receipt_for(pending)
    before = state(h)
    with error(503, 'metadata_unavailable'):
        with h.svc._transaction() as conn:
            assert h.svc._commit_index_locked(conn, principal, pending.pending_id, receipt) == receipt
            assert conn.in_transaction
            raise RuntimeError('SENSITIVE_MARKER')
    assert state(h) == before


def test_cleanup_survives_restart_and_releases_only_pending_charge(h, principal):
    save(h, principal, args())
    head = h.svc.get_head(principal, h.job.job_id)
    original, charged = state(h), totals(h)
    pending = reserve(h, principal, args(head)).pending
    before = state(h)
    with error(503, 'metadata_unavailable'):
        with h.svc._transaction() as conn:
            h.svc._release_pending_after_cleanup_locked(conn, pending.pending_id)
    assert state(h) == before
    reserved = totals(h)
    with h.svc._transaction() as conn:
        h.svc._mark_cleanup_locked(conn, pending.pending_id)
        assert h.svc._pending(conn, pending.pending_id).state == 'cleanup'
    assert totals(h) == reserved
    cleanup = state(h)
    with error(409, 'metadata_write_pending'):
        reserve(h, principal, args(head))
    restart(h)
    assert state(h) == cleanup and totals(h) == reserved
    # Index-only precondition: assume ALL pending files have been verified absent.
    with h.svc._transaction() as conn:
        h.svc._release_pending_after_cleanup_locked(conn, pending.pending_id)
    assert state(h) == original and totals(h) == charged
    assert h.svc.get_head(principal, h.job.job_id) == head


@pytest.mark.parametrize('damage', ['receipt_json', 'missing_head'])
def test_corrupt_committed_index_is_unavailable(h, principal, damage):
    values = args()
    save(h, principal, values)
    if damage == 'receipt_json':
        sql(h, 'UPDATE metadata_operations SET receipt_json=?', ('{SENSITIVE_MARKER',))
    else:
        sql(h, 'DELETE FROM metadata_heads')
    before = state(h)
    with error(503, 'metadata_unavailable'):
        h.svc.preflight(principal, h.job.job_id,
                        key_hash=values['key_hash'], request_hash=values['request_hash'])
    assert state(h) == before


@pytest.mark.parametrize('damage', ['owner', 'charged_bytes'])
def test_p2_corrupt_pending_must_fail_closed_before_pending_conflict(h, principal, damage):
    reserve(h, principal, args())
    if damage == 'owner':
        sql(h, 'UPDATE metadata_pending SET owner=?', ('user:2',))
    else:
        sql(h, 'UPDATE metadata_pending SET charged_bytes=charged_bytes+1')
    before = state(h)
    with error(503, 'metadata_unavailable'):
        reserve(h, principal, args())
    assert state(h) == before


def test_p2_old_revision_owner_cannot_shift_accounting_to_another_owner(h, principal):
    first = save(h, principal, args())
    save(h, principal, args(h.svc.get_head(principal, h.job.job_id)))
    head = h.svc.get_head(principal, h.job.job_id)
    sql(h, 'UPDATE metadata_revisions SET owner=? WHERE revision_id=?',
        ('user:2', first.revision_id))
    before = state(h)
    with error(503, 'metadata_unavailable'):
        reserve(h, principal, args(head))
    assert state(h) == before
