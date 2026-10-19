"""Real SQLite approval index tests; SQL commits are not physical durability proof.

No snapshots are published and no literal evidence verification is simulated.
The private commit assumes those obligations were met by a future leased writer.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.metadata_approval_contract import ApproveMetadataReceipt
from app.services.metadata_namespace_authority import MetadataNamespaceAuthority
from app.services.metadata_recovery import MetadataRecovery
from app.services.metadata_recovery_files import MetadataRecoveryFiles
from test_metadata_store import (
    MIB, args, commit, error, expire, h, principal, receipt_for,
    reserve, restart, save, sql, state, totals,
)


@pytest.fixture
def approver(principal):
    return replace(principal, scopes=(principal.scopes | {'metadata:approve'}) - {'metadata:write'})


def approval_receipt(pending):
    return ApproveMetadataReceipt(
        operation='approve-v1', state='approved', revision_id=pending.revision_id,
        sha256=pending.sha256, reviewed_revision_id=pending.expected_head,
        reviewed_sha256=pending.expected_sha256)


def reserve_approval(h, approver, **updates):
    head = h.svc.get_head(approver, h.job.job_id)
    values = args(head, **updates)
    admission = h.svc.reserve_approval(approver, h.job.job_id, **values)
    return values, admission.pending


def commit_approval(h, approver, pending, receipt=None):
    if receipt is None:
        receipt = approval_receipt(pending)
    with h.svc._transaction() as conn:
        return h.svc._commit_approval_index_locked(
            conn, approver, pending.pending_id, receipt)


def approved(h, principal, approver):
    save(h, principal, args())
    values, pending = reserve_approval(h, approver, size_bytes=456)
    receipt = commit_approval(h, approver, pending)
    return values, pending, receipt


def preflight(h, approver, values):
    return h.svc.preflight_approval(
        approver, h.job.job_id, key_hash=values['key_hash'],
        request_hash=values['request_hash'])


def namespace(h):
    authority = MetadataNamespaceAuthority(
        MetadataRecovery(h.svc, MetadataRecoveryFiles(h.jobs.root_dir)))
    with h.svc._transaction() as conn:
        result = authority.load_locked(conn, h.job.job_id)
    return result


def outcome(h, pending, receipt):
    with h.svc._transaction() as conn:
        return h.svc._index_outcome_locked(conn, pending, receipt)


def mark_purged(h, when):
    # Trusted test-only SQL marker: no physical purge is performed by these tests.
    sql(h, 'UPDATE jobs SET artifacts_removed_at=?,artifacts_purged_at=? WHERE job_id=?',
        (when.isoformat(), when.isoformat(), h.job.job_id))


def test_approval_without_write_consumes_existing_reserve_and_namespace_hash(h, principal, approver):
    timestamps = h.job.updated_at, h.job.artifacts_expires_at
    draft = save(h, principal, args())
    values, pending = reserve_approval(h, approver, size_bytes=456)
    assert (pending.charged_slots, pending.charged_bytes,
            pending.approval_slots, pending.approval_bytes) == (0, 0, 0, 0)
    before = totals(h)
    receipt = approval_receipt(pending)
    assert outcome(h, pending, receipt) == 'uncommitted'
    # Online diagnosis may inspect the live claim; startup must reject it.
    h.svc._active_writes.add(pending.pending_id)
    try:
        assert outcome(h, pending, receipt) == 'uncommitted'
        with error(503, 'metadata_unavailable'):
            namespace(h)
        assert commit_approval(h, approver, pending, receipt) == receipt
    finally:
        h.svc._active_writes.discard(pending.pending_id)
    assert totals(h) == (before[0], before[1] - MIB + pending.size_bytes)
    head = h.svc.get_head(approver, h.job.job_id)
    assert (head.revision_id, head.state, head.approval_slots, head.approval_bytes) == (
        pending.revision_id, 'approved', 0, 0)
    assert preflight(h, approver, values).receipt == receipt
    assert outcome(h, pending, receipt) == 'confirmed'
    ledger = namespace(h)
    confirmed = {item.revision_id: item for item in ledger.confirmed}
    assert confirmed[draft.revision_id].reviewed_sha256 is None
    assert confirmed[receipt.revision_id].reviewed_sha256 == draft.sha256
    assert confirmed[receipt.revision_id].parent_revision_id == draft.revision_id
    assert (h.job.updated_at, h.job.artifacts_expires_at) == timestamps
    assert sql(h, 'SELECT operation FROM metadata_operations ORDER BY rowid') == [
        ('save-v1',), ('approve-v1',)]
    assert sql(h, 'SELECT action,actor,auth_source FROM metadata_audit ORDER BY rowid') == [
        ('save', 'user:1', 'local_jwt'), ('approve', 'user:1', 'local_jwt')]


def test_thirty_one_drafts_and_reserve_become_thirty_two_revisions(h, principal, approver):
    head = None
    for _ in range(31):
        save(h, principal, args(head))
        head = h.svc.get_head(principal, h.job.job_id)
    assert totals(h) == (32, 31 * 123 + MIB)
    _, pending = reserve_approval(h, approver, size_bytes=456)
    assert totals(h) == (32, 31 * 123 + MIB)
    commit_approval(h, approver, pending)
    assert totals(h) == (32, 31 * 123 + 456)
    revisions = h.svc.list_revisions(approver, h.job.job_id)
    assert len(revisions) == 32
    assert sum(item.state == 'approved' for item in revisions) == 1
    before = state(h), totals(h)
    with error(413, 'metadata_quota_exceeded'):
        reserve(h, principal, args(h.svc.get_head(principal, h.job.job_id)))
    assert (state(h), totals(h)) == before
    assert len(namespace(h).confirmed) == 32


def test_save_after_approval_reserves_again_and_original_replay_survives_later_pending(h, principal, approver):
    values, first_pending, original = approved(h, principal, approver)
    first_head = h.svc.get_head(approver, h.job.job_id)
    edit = reserve(h, principal, args(first_head, size_bytes=234)).pending
    assert (edit.charged_slots, edit.charged_bytes,
            edit.approval_slots, edit.approval_bytes) == (2, 234 + MIB, 1, MIB)
    # A valid later reservation must not obstruct replay of the older operation.
    h.svc._active_writes.add(edit.pending_id)
    try:
        before = state(h), totals(h)
        replay = h.svc.reserve_approval(approver, h.job.job_id, **values)
        assert replay.receipt == original and replay.receipt is not original
        assert replay.head == first_head and replay.pending is None
        assert outcome(h, first_pending, original) == 'confirmed'
        assert (state(h), totals(h)) == before
    finally:
        h.svc._active_writes.discard(edit.pending_id)
    commit(h, principal, edit)
    second_values, second_pending = reserve_approval(h, approver, size_bytes=345)
    assert second_pending.charged_slots == second_pending.charged_bytes == 0
    later_head = h.svc.get_head(approver, h.job.job_id)
    before = state(h), totals(h)
    assert preflight(h, approver, values).receipt == original
    replay = h.svc.reserve_approval(approver, h.job.job_id, **values)
    assert replay.receipt == original and replay.head == later_head and replay.pending is None
    with error(409, 'idempotency_conflict'):
        h.svc.reserve_approval(approver, h.job.job_id,
                               **dict(values, request_hash=uuid4().hex * 2))
    assert (state(h), totals(h)) == before
    assert outcome(h, first_pending, original) == 'confirmed'
    second_receipt = commit_approval(h, approver, second_pending)
    assert preflight(h, approver, second_values).receipt == second_receipt
    assert totals(h) == (4, 123 + 456 + 234 + 345)
    assert len(namespace(h).confirmed) == 4
    restart(h)
    current = h.svc.get_head(approver, h.job.job_id)
    replay = h.svc.reserve_approval(approver, h.job.job_id, **values)
    assert replay.receipt == original and replay.head == current
    assert outcome(h, first_pending, original) == 'confirmed'


def test_save_and_approval_keys_are_separate(h, principal, approver):
    draft_values = args()
    draft_receipt = save(h, principal, draft_values)
    values, pending = reserve_approval(
        h, approver, key_hash=draft_values['key_hash'])
    receipt = commit_approval(h, approver, pending)
    assert preflight(h, approver, values).receipt == receipt
    assert h.svc.preflight(principal, h.job.job_id,
                           key_hash=draft_values['key_hash'],
                           request_hash=draft_values['request_hash']).receipt == draft_receipt


@pytest.mark.parametrize('missing', ['metadata:approve', 'metadata:read', 'jobs:read', 'artifacts:read'])
@pytest.mark.parametrize('entry', ['preflight', 'reserve'])
def test_replay_rechecks_every_approval_grant(h, principal, approver, missing, entry):
    values, _, _ = approved(h, principal, approver)
    revoked = replace(approver, scopes=approver.scopes - {missing}, is_admin=True)
    before = state(h), totals(h)
    with error(403, 'insufficient_scope'):
        if entry == 'preflight':
            preflight(h, revoked, values)
        else:
            h.svc.reserve_approval(revoked, h.job.job_id, **values)
    assert (state(h), totals(h)) == before


@pytest.mark.parametrize('entry', ['preflight', 'reserve'])
@pytest.mark.parametrize('condition', ['expired', 'not_completed', 'foreign', 'service', 'legacy', 'fake'])
def test_replay_rechecks_identity_ownership_and_lifecycle(h, principal, approver, entry, condition):
    values, _, _ = approved(h, principal, approver)
    caller = approver
    if condition == 'expired':
        expected = (410, expire(h))
    elif condition == 'not_completed':
        h.jobs.update(h.job.job_id, status='failed')
        expected = (409, 'job_not_completed')
    elif condition == 'foreign':
        caller = replace(approver, subject='user:2', user_id=2, is_admin=True)
        expected = (404, 'job_not_found')
    elif condition == 'service':
        caller = replace(approver, subject='service:' + 'a' * 32,
                         user_id=None, auth_source='service', is_admin=True)
        expected = (403, 'metadata_approval_forbidden')
    elif condition == 'legacy':
        caller = replace(approver, auth_source='legacy')
        expected = (403, 'metadata_approval_forbidden')
    else:
        caller = SimpleNamespace(user_id=1, subject='user:1', scopes=approver.scopes,
                                 auth_source='local_jwt', kind='user')
        expected = (403, 'metadata_approval_forbidden')
    before = state(h), totals(h)
    with error(*expected):
        if entry == 'preflight':
            preflight(h, caller, values)
        else:
            h.svc.reserve_approval(caller, h.job.job_id, **values)
    assert (state(h), totals(h)) == before


@pytest.mark.parametrize('condition', ['scope', 'auth', 'cleanup', 'expired', 'not_completed', 'foreign'])
def test_commit_reauthorizes_and_requires_same_active_actor_auth(h, principal, approver, condition):
    save(h, principal, args())
    _, pending = reserve_approval(h, approver)
    caller = approver
    if condition == 'scope':
        caller = replace(approver, scopes=approver.scopes - {'metadata:approve'})
        expected = (403, 'insufficient_scope')
    elif condition == 'auth':
        caller = replace(approver, auth_source='oauth')
        expected = (409, 'metadata_write_pending')
    elif condition == 'cleanup':
        with h.svc._transaction() as conn:
            h.svc._mark_cleanup_locked(conn, pending.pending_id)
        expected = (409, 'metadata_write_pending')
    elif condition == 'expired':
        expected = (410, expire(h))
    elif condition == 'not_completed':
        h.jobs.update(h.job.job_id, status='failed')
        expected = (409, 'job_not_completed')
    else:
        caller = replace(approver, subject='user:2', user_id=2, is_admin=True)
        expected = (404, 'job_not_found')
    before = state(h), totals(h)
    with error(*expected):
        commit_approval(h, caller, pending)
    assert (state(h), totals(h)) == before


def test_oauth_approval_and_strict_receipt_bindings(h, principal, approver):
    approver = replace(approver, auth_source='oauth')
    save(h, principal, args())
    _, pending = reserve_approval(h, approver)
    valid = approval_receipt(pending)
    cases = [
        (valid.model_copy(update={'revision_id': True}), 422, 'metadata_invalid_input'),
        (valid.model_copy(update={'reviewed_sha256': 123}), 422, 'metadata_invalid_input'),
        (valid.model_copy(update={'revision_id': uuid4().hex}), 409, 'metadata_revision_conflict'),
        (valid.model_copy(update={'sha256': 'e' * 64 if valid.sha256 != 'e' * 64 else 'f' * 64}),
         409, 'metadata_revision_conflict'),
        (valid.model_copy(update={'reviewed_revision_id': uuid4().hex}), 409, 'metadata_revision_conflict'),
        (valid.model_copy(update={'reviewed_sha256': 'e' * 64 if valid.reviewed_sha256 != 'e' * 64 else 'f' * 64}),
         409, 'metadata_revision_conflict'),
    ]
    before = state(h), totals(h)
    for receipt, status, code in cases:
        with error(status, code):
            commit_approval(h, approver, pending, receipt)
        assert (state(h), totals(h)) == before
    assert commit_approval(h, approver, pending, valid.model_dump(mode='python')) == valid
    assert namespace(h).confirmed[-1:] or namespace(h).confirmed


@pytest.mark.parametrize('mutation', ['revision', 'head', 'operation', 'audit', 'pending'])
def test_sql_failure_after_each_mutation_restores_ledger_and_charges(h, principal, approver, mutation):
    save(h, principal, args())
    _, pending = reserve_approval(h, approver, size_bytes=456)
    events = {
        'revision': ('INSERT', 'metadata_revisions'),
        'head': ('UPDATE', 'metadata_heads'),
        'operation': ('INSERT', 'metadata_operations'),
        'audit': ('INSERT', 'metadata_audit'),
        'pending': ('DELETE', 'metadata_pending'),
    }
    event, table = events[mutation]
    sql(h, 'CREATE TRIGGER fail_approval AFTER ' + event + ' ON ' + table +
        " BEGIN SELECT RAISE(ABORT,'SENSITIVE_MARKER'); END")
    before = state(h), totals(h)
    with error(503, 'metadata_unavailable'):
        commit_approval(h, approver, pending)
    assert (state(h), totals(h)) == before
    sql(h, 'DROP TRIGGER fail_approval')
    commit_approval(h, approver, pending)
    assert totals(h) == (before[1][0], before[1][1] - MIB + 456)


@pytest.mark.parametrize('damage', ['head', 'audit_auth', 'receipt_json'])
def test_post_mutation_quota_validation_rolls_back_trigger_corruption(h, principal, approver, damage):
    save(h, principal, args())
    _, pending = reserve_approval(h, approver)
    mutations = {
        'head': 'UPDATE metadata_heads SET approval_slots=1,approval_bytes=1048576;',
        'audit_auth': "UPDATE metadata_audit SET auth_source='service' WHERE action='approve';",
        'receipt_json': "UPDATE metadata_operations SET receipt_json='{}' WHERE operation='approve-v1';",
    }
    sql(h, 'CREATE TRIGGER corrupt_after_delete AFTER DELETE ON metadata_pending BEGIN ' +
        mutations[damage] + ' END')
    before = state(h), totals(h)
    with error(503, 'metadata_unavailable'):
        commit_approval(h, approver, pending)
    assert (state(h), totals(h)) == before
    sql(h, 'DROP TRIGGER corrupt_after_delete')
    receipt = approval_receipt(pending)
    assert commit_approval(h, approver, pending, receipt) == receipt
    assert totals(h) == (before[1][0], before[1][1] - MIB + pending.size_bytes)
    assert outcome(h, pending, receipt) == 'confirmed'


@pytest.mark.parametrize('signal', [None, KeyboardInterrupt('SENSITIVE_MARKER'), SystemExit('SENSITIVE_MARKER')])
def test_caller_owns_commit_and_control_exception_identity(h, principal, approver, signal):
    save(h, principal, args())
    _, pending = reserve_approval(h, approver, size_bytes=456)
    receipt = approval_receipt(pending)
    before = state(h), totals(h)
    if signal is None:
        with error(503, 'metadata_unavailable'):
            with h.svc._transaction() as conn:
                assert h.svc._commit_approval_index_locked(
                    conn, approver, pending.pending_id, receipt) == receipt
                assert conn.in_transaction
                raise RuntimeError('SENSITIVE_MARKER')
    else:
        with pytest.raises(type(signal)) as caught:
            with h.svc._transaction() as conn:
                h.svc._commit_approval_index_locked(conn, approver, pending.pending_id, receipt)
                assert conn.in_transaction
                raise signal
        assert caught.value is signal
    assert (state(h), totals(h)) == before
    with h.jobs._lock:
        with error(503, 'metadata_unavailable'):
            h.svc._commit_approval_index_locked(
                h.jobs._get_conn(), approver, pending.pending_id, receipt)
    assert (state(h), totals(h)) == before


@pytest.mark.parametrize('damage', [
    'duplicate_json', 'oversized_json', 'receipt_type', 'parent_hash', 'parent_state',
    'audit_action', 'audit_auth', 'audit_actor', 'operation', 'operation_time', 'missing_operation',
])
def test_corrupt_mixed_ledger_replay_and_namespace_fail_without_mutation(h, principal, approver, damage):
    values, pending, receipt = approved(h, principal, approver)
    if damage == 'duplicate_json':
        encoded = json.dumps(receipt.model_dump(mode='json'))
        encoded = encoded[:-1] + ',"state":"approved"}'
        sql(h, "UPDATE metadata_operations SET receipt_json=? WHERE operation='approve-v1'", (encoded,))
    elif damage == 'oversized_json':
        sql(h, "UPDATE metadata_operations SET receipt_json=? WHERE operation='approve-v1'", (' ' * 8193,))
    elif damage == 'receipt_type':
        damaged = receipt.model_dump(mode='json')
        damaged['reviewed_sha256'] = True
        sql(h, "UPDATE metadata_operations SET receipt_json=? WHERE operation='approve-v1'",
            (json.dumps(damaged),))
    elif damage == 'parent_hash':
        wrong = 'f' * 64 if pending.expected_sha256 != 'f' * 64 else 'e' * 64
        sql(h, 'UPDATE metadata_revisions SET sha256=? WHERE revision_id=?',
            (wrong, pending.expected_head))
    elif damage == 'parent_state':
        sql(h, "UPDATE metadata_revisions SET state='approved' WHERE revision_id=?", (pending.expected_head,))
    elif damage == 'audit_action':
        sql(h, "UPDATE metadata_audit SET action='save' WHERE action='approve'")
    elif damage == 'audit_auth':
        sql(h, "UPDATE metadata_audit SET auth_source='service' WHERE action='approve'")
    elif damage == 'audit_actor':
        sql(h, "UPDATE metadata_audit SET actor='user:2' WHERE action='approve'")
    elif damage == 'operation':
        sql(h, "UPDATE metadata_operations SET operation='save-v1' WHERE operation='approve-v1'")
    elif damage == 'operation_time':
        sql(h, "UPDATE metadata_operations SET created_at=? WHERE operation='approve-v1'",
            ((pending.created_at + timedelta(seconds=1)).isoformat(),))
    else:
        sql(h, "DELETE FROM metadata_operations WHERE operation='approve-v1'")
    before = state(h), totals(h)
    with error(503, 'metadata_unavailable'):
        preflight(h, approver, values)
    with error(503, 'metadata_unavailable'):
        h.svc.reserve_approval(approver, h.job.job_id, **values)
    with error(503, 'metadata_unavailable'):
        namespace(h)
    assert outcome(h, pending, receipt) == 'unknown'
    assert (state(h), totals(h)) == before


def test_bogus_approval_operation_does_not_authorize_replay(h, principal, approver):
    draft = save(h, principal, args())
    values = args(h.svc.get_head(principal, h.job.job_id))
    sql(h, '''INSERT INTO metadata_operations
        (owner,job_id,operation,key_hash,request_hash,revision_id,receipt_json,created_at)
        VALUES (?,?,?,?,?,?,?,?)''',
        ('user:1', h.job.job_id, 'approve-v1', values['key_hash'], values['request_hash'],
         draft.revision_id, '{}', values['created_at'].isoformat()))
    before = state(h), totals(h)
    with error(503, 'metadata_unavailable'):
        h.svc.reserve_approval(approver, h.job.job_id, **values)
    assert (state(h), totals(h)) == before


def test_approval_pending_must_still_reference_exact_draft_head(h, principal, approver):
    save(h, principal, args())
    _, pending = reserve_approval(h, approver)
    wrong = 'f' * 64 if pending.expected_sha256 != 'f' * 64 else 'e' * 64
    sql(h, 'UPDATE metadata_pending SET expected_sha256=?', (wrong,))
    before = state(h), totals(h)
    with error(503, 'metadata_unavailable'):
        commit_approval(h, approver, pending)
    assert (state(h), totals(h)) == before


def test_save_outcome_remains_confirmed_after_approval_and_later_pending(h, principal, approver):
    first = reserve(h, principal, args()).pending
    first_receipt = receipt_for(first)
    assert outcome(h, first, first_receipt) == 'uncommitted'
    commit(h, principal, first, first_receipt)
    assert outcome(h, first, first_receipt) == 'confirmed'
    _, approval = reserve_approval(h, approver)
    assert outcome(h, first, first_receipt) == 'confirmed'
    commit_approval(h, approver, approval)
    assert outcome(h, first, first_receipt) == 'confirmed'
    reserve(h, principal, args(h.svc.get_head(principal, h.job.job_id)))
    assert outcome(h, first, first_receipt) == 'confirmed'


@pytest.mark.parametrize('committed', [False, True])
def test_outcome_rejects_unrelated_cross_job_references(h, principal, approver, committed):
    save(h, principal, args())
    _, pending = reserve_approval(h, approver)
    receipt = approval_receipt(pending)
    if committed:
        commit_approval(h, approver, pending, receipt)
    other = h.jobs.create('synthetic.pdf', b'synthetic', owner_user_id=2)
    h.jobs.update(other.job_id, status='completed', progress_percent=100)
    foreign = replace(principal, subject='user:2', user_id=2)
    other_pending = h.svc.reserve(foreign, other.job_id, **args()).pending
    commit(h, foreign, other_pending)
    sql(h, 'UPDATE metadata_audit SET parent_revision_id=? WHERE job_id=?',
        (pending.revision_id, other.job_id))
    before = state(h), totals(h)
    assert outcome(h, pending, receipt) == 'unknown'
    assert (state(h), totals(h)) == before


def test_mixed_purged_history_survives_thirty_days_and_only_pending_is_released(h, principal, approver):
    approved(h, principal, approver)
    save(h, principal, args(h.svc.get_head(principal, h.job.job_id)))
    _, second = reserve_approval(h, approver)
    commit_approval(h, approver, second)
    cleanup = reserve(h, principal, args(h.svc.get_head(principal, h.job.job_id))).pending
    with h.svc._transaction() as conn:
        h.svc._mark_cleanup_locked(conn, cleanup.pending_id)
    logical = datetime.now(timezone.utc) - timedelta(days=1)
    sql(h, 'UPDATE jobs SET artifacts_removed_at=? WHERE job_id=?',
        (logical.isoformat(), h.job.job_id))
    before = state(h), totals(h)
    assert h.svc.reconcile_purged_job(h.job.job_id) is False
    assert (state(h), totals(h)) == before
    mark_purged(h, logical)
    assert h.svc.reconcile_purged_job(h.job.job_id) is True
    after = state(h)
    assert after['metadata_pending'] == []
    for table in after:
        if table != 'metadata_pending':
            assert after[table] == before[0][table]
    assert h.svc.reconcile_purged_job(
        h.job.job_id, now=logical + timedelta(days=30) - timedelta(microseconds=1)) is True
    assert state(h) == after
    assert h.svc.reconcile_purged_job(
        h.job.job_id, now=logical + timedelta(days=30)) is True
    assert all(not rows for rows in state(h).values())


@pytest.mark.parametrize('damage', ['parent_hash', 'audit', 'operation'])
def test_corrupt_mixed_purged_ledger_never_collects(h, principal, approver, damage):
    _, pending, _ = approved(h, principal, approver)
    reserve(h, principal, args(h.svc.get_head(principal, h.job.job_id)))
    if damage == 'parent_hash':
        wrong = 'e' * 64 if pending.expected_sha256 != 'e' * 64 else 'f' * 64
        sql(h, 'UPDATE metadata_revisions SET sha256=? WHERE revision_id=?',
            (wrong, pending.expected_head))
    elif damage == 'audit':
        sql(h, "UPDATE metadata_audit SET auth_source='legacy' WHERE action='approve'")
    else:
        sql(h, "DELETE FROM metadata_operations WHERE operation='approve-v1'")
    mark_purged(h, datetime.now(timezone.utc) - timedelta(days=31))
    before = state(h), totals(h)
    with error(503, 'metadata_unavailable'):
        h.svc.reconcile_purged_job(h.job.job_id)
    assert (state(h), totals(h)) == before
