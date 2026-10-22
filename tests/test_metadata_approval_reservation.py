"""SQLite approval prerequisites only; no approval publication or commit.

Reuse the store harness. Recovery checks exercise its real SQL validators;
physical recovery fixtures are not part of this proposal's supplied sources.
"""
from dataclasses import replace
from datetime import datetime
from uuid import uuid4

import pytest

import app.services.metadata_store as store_module
from app.metadata_approval_contract import ApproveMetadataReceipt
from app.services.metadata_recovery import MetadataRecovery
from app.services.metadata_storage import MetadataStorage
from test_metadata_store import (
    MIB, args, commit, error, expire, h, principal, receipt_for, reserve,
    restart, save, sql, state, totals,
)


def approver(principal, **updates):
    values = dict(scopes=(principal.scopes | {'metadata:approve'}) - {'metadata:write'})
    values.update(updates)
    return replace(principal, **values)


def draft(h, principal, count=1):
    head = None
    for _ in range(count):
        save(h, principal, args(head))
        head = h.svc.get_head(principal, h.job.job_id)
    return head


def approve(h, principal, values):
    return h.svc.reserve_approval(principal, h.job.job_id, **values)


def test_owner_approval_needs_no_write_scope_and_preserves_capacity(h, principal):
    head = draft(h, principal)
    before, charged = state(h), totals(h)
    timestamps = h.job.updated_at, h.job.artifacts_expires_at
    admission = approve(h, approver(principal), args(head, size_bytes=MIB))
    pending = admission.pending
    assert admission.head == head and admission.receipt is None
    assert pending.operation == 'approve-v1'
    assert (pending.owner, pending.actor, pending.auth_source) == (
        principal.subject, principal.subject, 'local_jwt')
    assert (pending.charged_slots, pending.charged_bytes,
            pending.approval_slots, pending.approval_bytes) == (0, 0, 0, 0)
    assert totals(h) == charged == (2, 123 + MIB)
    assert h.svc.get_head(principal, h.job.job_id) == head
    after = state(h)
    assert len(after['metadata_pending']) == 1
    assert all(after[t] == before[t] for t in before if t != 'metadata_pending')
    assert (h.job.updated_at, h.job.artifacts_expires_at) == timestamps
    with h.svc._transaction() as conn:
        assert h.svc._reserved_disk_bytes_locked(conn, principal.subject, h.job.job_id) == MIB


@pytest.mark.parametrize('source', ['local_jwt', 'oauth'])
def test_supported_human_authentication(h, principal, source):
    head = draft(h, principal)
    pending = approve(h, approver(principal, auth_source=source), args(head)).pending
    assert pending.auth_source == source


@pytest.mark.parametrize('updates', [
    {'kind': 'service', 'subject': 'service:' + 'a' * 32, 'user_id': None,
     'auth_source': 'service'},
    {'auth_source': 'legacy'},
    {'auth_source': 'unknown'},
    {'user_id': True},
    {'user_id': 0},
])
def test_nonhuman_or_invalid_identity_is_forbidden(h, principal, updates):
    head = draft(h, principal)
    before = state(h)
    with error(403, 'metadata_approval_forbidden'):
        approve(h, approver(principal, **updates), args(head))
    assert state(h) == before


@pytest.mark.parametrize('admin', [False, True])
def test_foreign_owner_is_hidden_even_for_admin(h, principal, admin):
    head = draft(h, principal)
    before = state(h)
    foreign = approver(principal, subject='user:2', user_id=2, is_admin=admin)
    with error(404, 'job_not_found'):
        approve(h, foreign, args(head))
    assert state(h) == before


@pytest.mark.parametrize('missing', [
    'metadata:approve', 'metadata:read', 'jobs:read', 'artifacts:read',
])
def test_every_approval_scope_is_required_even_for_admin(h, principal, missing):
    head = draft(h, principal)
    p = approver(principal, is_admin=True)
    p = replace(p, scopes=p.scopes - {missing})
    before = state(h)
    with error(403, 'insufficient_scope'):
        approve(h, p, args(head))
    assert state(h) == before


def test_non_principal_is_rejected_without_attribute_access(h, principal):
    head = draft(h, principal)
    before = state(h)
    with error(403, 'metadata_approval_forbidden'):
        approve(h, object(), args(head))
    assert state(h) == before


def test_retention_completion_and_cas_are_rechecked(h, principal):
    head = draft(h, principal)
    p = approver(principal)
    before = state(h)
    with error(409, 'metadata_revision_conflict'):
        approve(h, p, args(head, expected_sha256='f' * 64
                           if head.sha256 != 'f' * 64 else 'e' * 64))
    with error(409, 'metadata_revision_conflict'):
        approve(h, p, args(head, expected_head=uuid4().hex))
    assert state(h) == before
    h.jobs.update(h.job.job_id, status='queued')
    with error(409, 'job_not_completed'):
        approve(h, p, args(head))
    assert state(h) == before
    h.jobs.update(h.job.job_id, status='completed', progress_percent=100)
    code = expire(h)
    before = state(h)
    with error(410, code):
        approve(h, p, args(head))
    assert state(h) == before


def test_missing_and_approved_heads_conflict(h, principal):
    before = state(h)
    with error(409, 'metadata_revision_conflict'):
        approve(h, approver(principal), args(
            expected_head=uuid4().hex, expected_sha256='a' * 64))
    assert state(h) == before
    head = draft(h, principal)
    p = approver(principal)
    pending = approve(h, p, args(head)).pending
    receipt = ApproveMetadataReceipt.model_validate({
        'operation': 'approve-v1',
        'state': 'approved',
        'revision_id': pending.revision_id,
        'sha256': pending.sha256,
        'reviewed_revision_id': head.revision_id,
        'reviewed_sha256': head.sha256,
    }, strict=True)
    # SQL-only fixture: this commit does not prove physical durability.
    with h.svc._transaction() as conn:
        assert h.svc._commit_approval_index_locked(
            conn, p, pending.pending_id, receipt) == receipt
    approved_head = h.svc.get_head(principal, h.job.job_id)
    assert approved_head.state == 'approved'
    assert approved_head.revision_id == pending.revision_id
    before = state(h)
    with error(409, 'metadata_revision_conflict'):
        approve(h, p, args(approved_head))
    assert state(h) == before


def test_tampered_approved_head_without_approval_ledger_fails_closed(h, principal):
    head = draft(h, principal)
    # Leave the draft's save receipt and audit inconsistent with its new state.
    sql(h, "UPDATE metadata_revisions SET state='approved'")
    sql(h, 'UPDATE metadata_heads SET approval_slots=0, approval_bytes=0')
    before, charged = state(h), totals(h)
    with error(503, 'metadata_unavailable'):
        approve(h, approver(principal), args(head))
    assert state(h) == before and totals(h) == charged


@pytest.mark.parametrize('updates', [
    {'size_bytes': True}, {'size_bytes': False}, {'size_bytes': 0},
    {'size_bytes': -1}, {'size_bytes': MIB + 1}, {'size_bytes': 1.0},
    {'expected_head': None, 'expected_sha256': None},
    {'expected_head': None}, {'expected_sha256': None},
    {'revision_id': True}, {'sha256': 'SENSITIVE_MARKER'},
    {'key_hash': True}, {'request_hash': 'SENSITIVE_MARKER'},
    {'created_at': datetime(2026, 1, 1)},
])
def test_strict_reservation_inputs(h, principal, updates):
    head = draft(h, principal)
    before = state(h)
    with error(422, 'metadata_invalid_input'):
        approve(h, approver(principal), args(head, **updates))
    assert state(h) == before


def test_smallest_snapshot_is_accepted(h, principal):
    head = draft(h, principal)
    assert approve(h, approver(principal), args(head, size_bytes=1)).pending.size_bytes == 1


def test_thirty_one_drafts_leave_reserved_approval_slot_available(h, principal):
    head = draft(h, principal, count=31)
    charged = totals(h)
    assert charged == (32, 31 * 123 + MIB)
    pending = approve(h, approver(principal), args(head, size_bytes=MIB)).pending
    assert pending.charged_slots == pending.charged_bytes == 0
    assert totals(h) == charged
    with h.svc._transaction() as conn:
        h.svc._quota(conn, principal.subject, h.job.job_id)


@pytest.mark.parametrize('limit,other_owner', [
    ('MAX_JOB_BYTES', None),
    ('MAX_OWNER_BYTES', 1),
    ('MAX_GLOBAL_BYTES', 2),
])
def test_zero_charge_still_checks_all_existing_byte_limits(h, principal, monkeypatch,
                                                          limit, other_owner):
    head = draft(h, principal)
    used = 123 + MIB
    if other_owner is not None:
        other = h.jobs.create('synthetic.pdf', b'synthetic', owner_user_id=other_owner)
        h.jobs.update(other.job_id, status='completed', progress_percent=100)
        p = replace(principal, subject=f'user:{other_owner}', user_id=other_owner)
        pending = h.svc.reserve(p, other.job_id, **args()).pending
        commit(h, p, pending)
        used *= 2
    monkeypatch.setattr(store_module, limit, used - 1)
    before, charged = state(h), totals(h)
    with error(413, 'metadata_quota_exceeded'):
        approve(h, approver(principal), args(head))
    assert state(h) == before and totals(h) == charged


def test_existing_slot_limit_is_still_checked(h, principal, monkeypatch):
    head = draft(h, principal)
    monkeypatch.setattr(store_module, 'MAX_REVISIONS_PER_JOB', 1)
    before = state(h)
    with error(413, 'metadata_quota_exceeded'):
        approve(h, approver(principal), args(head))
    assert state(h) == before


def test_retries_and_competing_writes_remain_blocked_after_restart(h, principal):
    head = draft(h, principal)
    values = args(head)
    approve(h, approver(principal), values)
    before, charged = state(h), totals(h)
    restart(h)
    for attempt in (
        lambda: approve(h, approver(principal), values),
        lambda: approve(h, approver(principal), dict(values, request_hash=uuid4().hex * 2)),
        lambda: approve(h, approver(principal), args(head)),
        lambda: reserve(h, principal, args(head)),
    ):
        with error(409, 'metadata_write_pending'):
            attempt()
        assert state(h) == before and totals(h) == charged


def test_save_pending_also_blocks_approval(h, principal):
    head = draft(h, principal)
    reserve(h, principal, args(head))
    before = state(h)
    with error(409, 'metadata_write_pending'):
        approve(h, approver(principal), args(head))
    assert state(h) == before


def test_unsupported_committed_approval_key_fails_closed(h, principal):
    head = draft(h, principal)
    key = uuid4().hex * 2
    sql(h, '''INSERT INTO metadata_operations
        (owner,job_id,operation,key_hash,request_hash,revision_id,receipt_json,created_at)
        SELECT owner,job_id,'approve-v1',?,request_hash,revision_id,receipt_json,created_at
        FROM metadata_operations''', (key,))
    before = state(h)
    with error(503, 'metadata_unavailable'):
        approve(h, approver(principal), args(head, key_hash=key))
    assert state(h) == before


@pytest.mark.parametrize('collision', ['revision', 'pending'])
def test_revision_collisions_are_not_overwritten(h, principal, collision):
    head = draft(h, principal)
    revision_id = head.revision_id
    if collision == 'pending':
        other = h.jobs.create('synthetic.pdf', b'synthetic', owner_user_id=1)
        h.jobs.update(other.job_id, status='completed', progress_percent=100)
        revision_id = h.svc.reserve(principal, other.job_id, **args()).pending.revision_id
    before = state(h)
    with error(409, 'metadata_revision_conflict'):
        approve(h, approver(principal), args(head, revision_id=revision_id))
    assert state(h) == before


@pytest.mark.parametrize('damage', [
    'charged_slots=1', 'charged_bytes=1', 'approval_slots=1',
    'approval_bytes=1048576', "auth_source='service'", "auth_source='legacy'",
    "auth_source='unknown'", "actor='user:2'", 'expected_head=NULL',
    "expected_sha256='" + 'a' * 64 + "'",
])
def test_malformed_approval_pending_fails_closed_in_reader_and_global_quota(h, principal, damage):
    head = draft(h, principal)
    pending = approve(h, approver(principal), args(head)).pending
    sql(h, 'UPDATE metadata_pending SET ' + damage)
    before, charged = state(h), totals(h)
    with error(503, 'metadata_unavailable'):
        with h.svc._transaction() as conn:
            h.svc._pending(conn, pending.pending_id)
    with error(503, 'metadata_unavailable'):
        with h.svc._transaction() as conn:
            h.svc._quota(conn, principal.subject, h.job.job_id)
    with error(503, 'metadata_unavailable'):
        approve(h, approver(principal), args(head))
    assert state(h) == before and totals(h) == charged


def test_foreign_job_corrupt_approval_blocks_save_admission(h, principal):
    head = draft(h, principal)
    approve(h, approver(principal), args(head))
    sql(h, 'UPDATE metadata_pending SET charged_bytes=1')
    other = h.jobs.create('synthetic.pdf', b'synthetic', owner_user_id=2)
    h.jobs.update(other.job_id, status='completed', progress_percent=100)
    p = replace(principal, subject='user:2', user_id=2)
    before, charged = state(h), totals(h)
    with error(503, 'metadata_unavailable'):
        h.svc.reserve(p, other.job_id, **args())
    assert state(h) == before and totals(h) == charged


def test_insert_failure_rolls_back_without_consuming_head_reserve(h, principal):
    head = draft(h, principal)
    sql(h, "CREATE TRIGGER fail_approval BEFORE INSERT ON metadata_pending BEGIN SELECT RAISE(ABORT,'SENSITIVE_MARKER'); END")
    values = args(head)
    before, charged = state(h), totals(h)
    with error(503, 'metadata_unavailable'):
        approve(h, approver(principal), values)
    assert state(h) == before and totals(h) == charged
    sql(h, 'DROP TRIGGER fail_approval')
    assert approve(h, approver(principal), values).pending is not None


def test_save_commit_and_writer_refuse_approval_without_adopting_cleanup(h, principal):
    head = draft(h, principal)
    pending = approve(h, approver(principal), args(head)).pending
    receipt = receipt_for(pending)
    before, charged = state(h), totals(h)
    with error(503, 'metadata_unavailable'):
        commit(h, principal, pending, receipt)
    # No file capability is supplied: refusal must precede physical access,
    # and unclaimed save reconciliation must not adopt this approval pending.
    storage = object.__new__(MetadataStorage)
    storage.store = h.svc
    storage.files = object()
    with error(503, 'metadata_unavailable'):
        storage.write_reserved(approver(principal), h.job, pending, receipt, b'unused',
                               check_bindings_locked=lambda conn: None)
    assert state(h) == before and totals(h) == charged


def test_recovery_validators_and_cleanup_preserve_draft_head_reserve(h, principal):
    head = draft(h, principal)
    original, charged = state(h), totals(h)
    pending = approve(h, approver(principal), args(head)).pending
    recovery = object.__new__(MetadataRecovery)
    recovery.store = h.svc
    with h.svc._transaction() as conn:
        h.svc._quota(conn, principal.subject, h.job.job_id, _check_limits=False)
        h.svc._validate_purged_ledger_locked(conn, principal.subject, h.job.job_id)
        recovery._uncommitted_locked(conn, pending)
        h.svc._mark_cleanup_locked(conn, pending.pending_id)
    assert totals(h) == charged
    restart(h)
    recovery.store = h.svc
    with h.svc._transaction() as conn:
        actual = h.svc._pending(conn, pending.pending_id)
        assert actual.state == 'cleanup'
        recovery._uncommitted_locked(conn, actual)
        # SQL helper's trusted precondition: ALL pending files are absent.
        h.svc._release_pending_after_cleanup_locked(conn, pending.pending_id)
    assert state(h) == original and totals(h) == charged
    assert h.svc.get_head(principal, h.job.job_id) == head


def test_cleanup_operation_guard_uses_approval_namespace(h, principal):
    head = draft(h, principal)
    pending = approve(h, approver(principal), args(head)).pending
    sql(h, '''INSERT INTO metadata_operations
        (owner,job_id,operation,key_hash,request_hash,revision_id,receipt_json,created_at)
        SELECT owner,job_id,'approve-v1',?,?,revision_id,receipt_json,created_at
        FROM metadata_operations WHERE operation='save-v1' ''',
        (pending.key_hash, pending.request_hash))
    with h.svc._transaction() as conn:
        h.svc._mark_cleanup_locked(conn, pending.pending_id)
    before, charged = state(h), totals(h)
    with error(503, 'metadata_unavailable'):
        with h.svc._transaction() as conn:
            h.svc._release_pending_after_cleanup_locked(conn, pending.pending_id)
    assert state(h) == before and totals(h) == charged


def test_unsupported_charge_operation_fails_closed(h, principal):
    head = draft(h, principal)
    with error(503, 'metadata_unavailable'):
        h.svc._charges(head, 123, operation='unsupported')
