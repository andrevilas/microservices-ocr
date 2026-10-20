"""Cooperative control with real SQLite, canonical pages and POSIX artifacts."""
from datetime import datetime, timedelta, timezone

import pytest

from app.application import ApplicationError
from app.services import metadata_files
from app.services.metadata_canonical import MetadataCanonicalSource
from test_document_metadata import (
    body, env, failure, idle, persisted, pytestmark, rows, save, who,
)
from test_document_metadata_approval import approve, assert_original, ready

TABLES = ("metadata_heads", "metadata_revisions", "metadata_operations",
          "metadata_audit", "metadata_pending")


def timeout():
    return ApplicationError(408, "operation_timeout", "Operation timed out.")


def ledger(e):
    with e.jobs._lock:
        return {table: e.jobs._conn.execute(
            "SELECT * FROM " + table + " ORDER BY rowid").fetchall()
                for table in TABLES}


def artifacts(e):
    directory = e.job.working_dir / "results" / "metadata"
    return {path.name: path.read_bytes() for path in directory.iterdir()}


def operation(e, name, checkpoint):
    if name == "approve":
        return e.service.approve(
            e.approver, e.job.job_id, e.approval_body,
            key="control-approval-0001", checkpoint=checkpoint)
    return e.service.save(
        e.owner, e.job.job_id, body(e, e.draft),
        key="control-save-0001", checkpoint=checkpoint)


def test_scoped_snapshot_and_get_keep_one_lease_and_never_write(env, monkeypatch):
    e = env
    receipt, _ = save(e)
    expected = persisted(e, receipt)
    before, disk = ledger(e), artifacts(e)
    checks, reads = [], []
    read = e.bundle.files.read

    def checkpoint():
        assert e.jobs._artifact_leases == {e.job.job_id: 1}
        assert not e.jobs._lock.locked()
        checks.append(True)

    def checked_read(*args, **kwargs):
        assert e.jobs._artifact_leases == {e.job.job_id: 1}
        assert not e.jobs._lock.locked()
        result = read(*args, **kwargs)
        reads.append(result)
        return result

    monkeypatch.setattr(e.bundle.files, "read", checked_read)
    with e.service.open_snapshot(
            e.owner, e.job.job_id, receipt.revision_id,
            sha256=receipt.sha256, checkpoint=checkpoint) as snapshot:
        assert snapshot == expected and snapshot is not reads[-1]
        assert e.jobs._artifact_leases == {e.job.job_id: 1}
        assert not e.jobs._lock.locked() and not e.jobs._conn.in_transaction
        snapshot.segments.clear()
    assert not e.jobs._artifact_leases
    assert e.service.get(e.owner, e.job.job_id, receipt.revision_id,
                         sha256=receipt.sha256, checkpoint=checkpoint) == expected
    assert len(checks) == 6 and len(reads) == 2
    assert ledger(e) == before and artifacts(e) == disk
    idle(e, 1)


@pytest.mark.parametrize("error", [RuntimeError("caller"), KeyboardInterrupt("caller"),
                                  SystemExit("caller")])
def test_snapshot_caller_errors_propagate_unchanged(env, error):
    e = env
    receipt, _ = save(e)
    with pytest.raises(type(error)) as caught:
        with e.service.open_snapshot(e.owner, e.job.job_id, receipt.revision_id,
                                     sha256=receipt.sha256):
            raise error
    assert caught.value is error
    idle(e, 1)


@pytest.mark.parametrize("change,status,code", [
    ("invalid", 422, "invalid_metadata_input"),
    ("missing", 404, "metadata_revision_not_found"),
    ("hash", 409, "metadata_revision_conflict"),
    ("foreign", 404, "job_not_found"),
    ("read", 503, "metadata_unavailable"),
    ("header", 503, "metadata_unavailable"),
])
def test_snapshot_setup_is_sanitized_and_releases_lease(env, monkeypatch, change, status, code):
    e = env
    receipt, _ = save(e)
    revision_id, digest, principal = receipt.revision_id, receipt.sha256, e.owner
    if change == "invalid":
        digest = "invalid"
    elif change == "missing":
        revision_id = "f" * 32
    elif change == "hash":
        digest = "f" * 64
    elif change == "foreign":
        principal = who("user:2")
    else:
        read = e.bundle.files.read

        def broken_read(*args, **kwargs):
            if change == "read":
                raise RuntimeError("PRIVATE_READ_DETAIL")
            return read(*args, **kwargs).model_copy(update={"job_id": "f" * 32})

        monkeypatch.setattr(e.bundle.files, "read", broken_read)

    def enter():
        with e.service.open_snapshot(principal, e.job.job_id, revision_id, sha256=digest):
            pytest.fail("invalid snapshot reached caller")

    error = failure(enter, status, code)
    assert "PRIVATE_READ_DETAIL" not in str(error) + repr(vars(error))
    idle(e, 1)


@pytest.mark.parametrize("boundary", [1, 2, 3])
@pytest.mark.parametrize("control", [False, True])
def test_snapshot_checkpoints_release_lease_at_each_boundary(env, boundary, control):
    e = env
    receipt, _ = save(e)
    error = KeyboardInterrupt("control") if control else timeout()
    calls = []

    def checkpoint():
        assert not e.jobs._lock.locked()
        calls.append(True)
        if len(calls) == boundary:
            raise error

    with pytest.raises(type(error)) as caught:
        with e.service.open_snapshot(e.owner, e.job.job_id, receipt.revision_id,
                                     sha256=receipt.sha256, checkpoint=checkpoint):
            pytest.fail("cancelled read reached caller")
    if control:
        assert caught.value is error
    else:
        assert (caught.value.status, caught.value.code, caught.value.message) == (
            408, "operation_timeout", "Operation timed out.")
        assert caught.value.__context__ is None
    idle(e, 1)


def test_admitted_snapshot_survives_expiry_during_io_but_new_read_does_not(env, monkeypatch):
    e = env
    receipt, _ = save(e)
    read = e.bundle.files.read
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)

    def expire_during_read(*args, **kwargs):
        result = read(*args, **kwargs)
        with e.jobs._lock:
            e.job.artifacts_expires_at = expired
            e.jobs._persist(e.job)
        return result

    monkeypatch.setattr(e.bundle.files, "read", expire_during_read)
    with e.service.open_snapshot(e.owner, e.job.job_id, receipt.revision_id,
                                 sha256=receipt.sha256) as snapshot:
        assert snapshot.revision_id == receipt.revision_id
        assert e.jobs._artifact_leases == {e.job.job_id: 1}
        assert e.job.artifacts_expires_at == expired
    failure(lambda: e.service.get(e.owner, e.job.job_id, receipt.revision_id,
                                  sha256=receipt.sha256), 410, "artifacts_expired")
    idle(e, 1)


@pytest.mark.parametrize("name", ["save", "approve", "get", "open_snapshot"])
def test_invalid_checkpoint_fails_before_health_or_io(ready, monkeypatch, name):
    e = ready

    def forbidden(*args, **kwargs):
        pytest.fail("invalid callback reached runtime work")

    monkeypatch.setattr(type(e.bundle), "ensure_healthy", forbidden)
    if name in {"save", "approve"}:
        action = lambda: operation(e, name, False)
    elif name == "get":
        action = lambda: e.service.get(e.owner, e.job.job_id, e.draft.revision_id,
                                       sha256=e.draft.sha256, checkpoint=False)
    else:
        def action():
            with e.service.open_snapshot(e.owner, e.job.job_id, e.draft.revision_id,
                                         sha256=e.draft.sha256, checkpoint=False):
                pytest.fail("invalid callback reached caller")
    failure(action, 422, "invalid_metadata_input")
    idle(e, 1)


@pytest.mark.parametrize("name", ["save", "approve"])
@pytest.mark.parametrize("stage", ["preparation", "head", "page", "reservation",
                                   "handoff", "prepared", "published", "final"])
def test_timeout_boundaries_preserve_head_and_release_verified_cleanup(ready, monkeypatch, name, stage):
    e = ready
    before, disk = ledger(e), artifacts(e)
    armed = [stage == "preparation"]
    observed, reservations = [], []
    index = e.bundle.index
    reserve_name = "reserve_approval" if name == "approve" else "reserve"
    reserve = getattr(index, reserve_name)
    read = e.bundle.files.read
    source = MetadataCanonicalSource.__call__
    prepare = e.bundle.files.prepare

    def checkpoint():
        observed.append((e.jobs._lock.locked(), bool(index._active_writes)))
        if armed[0] or (stage == "final" and e.jobs._lock.locked()):
            raise timeout()

    def checked_reserve(*args, **kwargs):
        if stage == "reservation":
            pytest.fail("pre-reservation checkpoint was skipped")
        result = reserve(*args, **kwargs)
        reservations.append(result.pending)
        if stage == "handoff":
            armed[0] = True
        return result

    def checked_read(*args, **kwargs):
        result = read(*args, **kwargs)
        if ((stage == "head" and result.revision_id == e.draft.revision_id)
                or (stage == "published" and result.revision_id != e.draft.revision_id)):
            armed[0] = True
        return result

    def checked_source(self, number):
        result = source(self, number)
        if stage == "reservation":
            armed[0] = True
        return result

    def checked_prepare(*args, **kwargs):
        result = prepare(*args, **kwargs)
        if stage == "prepared":
            armed[0] = True
        return result

    monkeypatch.setattr(index, reserve_name, checked_reserve)
    monkeypatch.setattr(e.bundle.files, "read", checked_read)
    monkeypatch.setattr(e.bundle.files, "prepare", checked_prepare)
    monkeypatch.setattr(MetadataCanonicalSource, "__call__", checked_source)
    if stage == "page":
        # Arm after the physical HEAD checkpoint. The next check must run
        # before the evidence source is called, independently of canonical setup.
        import app.services.document_service as documents
        policy_name = "prepare_approval" if name == "approve" else "assess_snapshot"
        policy = getattr(documents, policy_name)

        def checked_policy(*args, **kwargs):
            armed[0] = True
            return policy(*args, **kwargs)

        def forbidden_source(*args, **kwargs):
            pytest.fail("evidence read occurred before its checkpoint")

        monkeypatch.setattr(documents, policy_name, checked_policy)
        monkeypatch.setattr(MetadataCanonicalSource, "__call__", forbidden_source)
    failure(lambda: operation(e, name, checkpoint), 408, "operation_timeout")
    assert observed
    assert ledger(e) == before and artifacts(e) == disk
    assert_original(e)
    idle(e, 1)
    if stage in {"handoff", "prepared", "published", "final"}:
        assert len(reservations) == 1 and reservations[0] is not None
    else:
        assert not reservations
    if stage == "final":
        assert observed[-1] == (True, True)
    elif stage in {"prepared", "published"}:
        assert observed[-1] == (False, True)
    else:
        assert observed[-1] == (False, False)


@pytest.mark.parametrize("name", ["save", "approve"])
@pytest.mark.parametrize("control", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_postpublication_control_identity_and_cleanup_accounting(ready, monkeypatch, name, control, cleanup_fails):
    e = ready
    before, disk = ledger(e), artifacts(e)
    read = e.bundle.files.read
    published = []
    error = SystemExit("trusted control") if control else timeout()
    cleanup = metadata_files.WriteAttempt.cleanup
    cleanups = []

    def checked_read(*args, **kwargs):
        result = read(*args, **kwargs)
        if result.revision_id != e.draft.revision_id:
            published.append(result.revision_id)
        return result

    def checkpoint():
        if published:
            raise error

    def checked_cleanup(self):
        cleanups.append(self)
        if cleanup_fails:
            raise OSError("PRIVATE_CLEANUP_DETAIL")
        return cleanup(self)

    monkeypatch.setattr(e.bundle.files, "read", checked_read)
    monkeypatch.setattr(metadata_files.WriteAttempt, "cleanup", checked_cleanup)
    with pytest.raises(type(error) if control else ApplicationError) as caught:
        operation(e, name, checkpoint)
    if control:
        assert caught.value is error
    else:
        assert caught.value.code == ("metadata_unavailable" if cleanup_fails else "operation_timeout")
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        assert "PRIVATE_CLEANUP_DETAIL" not in str(caught.value) + repr(vars(caught.value))
    assert len(published) == 1
    assert len(cleanups) == (0 if control else 1)
    after = ledger(e)
    for table in TABLES[:-1]:
        assert after[table] == before[table]
    assert not e.bundle.index._active_writes and not e.jobs._artifact_leases
    assert_original(e)
    if control or cleanup_fails:
        assert len(rows(e, "metadata_pending")) == 1
        with e.jobs._lock:
            pending = e.jobs._conn.execute(
                "SELECT state,charged_slots,charged_bytes FROM metadata_pending WHERE job_id=?",
                (e.job.job_id,)).fetchone()
            head_reserve = tuple(e.jobs._conn.execute(
                "SELECT approval_slots,approval_bytes FROM metadata_heads WHERE job_id=?",
                (e.job.job_id,)).fetchone())
        assert pending[0] == ("active" if control else "cleanup")
        assert head_reserve == (1, 1048576)
        # Committed usage is unchanged above. Retained quota also includes
        # the draft HEAD reserve plus any additional pending charges.
        retained = (head_reserve[0] + pending[1], head_reserve[1] + pending[2])
        if name == "approve":
            assert tuple(pending[1:]) == (0, 0)
            assert retained == (1, 1048576)
        else:
            assert pending[1] >= 1 and pending[2] > 0
            assert retained[0] >= 2 and retained[1] > 1048576
        assert artifacts(e) == {**disk, published[0] + ".json":
                                (e.job.working_dir / "results" / "metadata" /
                                 (published[0] + ".json")).read_bytes()}
        assert not e.jobs._conn.in_transaction
    else:
        assert after == before and artifacts(e) == disk
        idle(e, 1)


@pytest.mark.parametrize("name", ["save", "approve"])
def test_success_checks_only_before_commit_and_duplicate_cannot_steal_claim(ready, monkeypatch, name):
    e = ready
    index = e.bundle.index
    committed, checks, duplicate = [], [], []
    commit_name = "_commit_approval_index_locked" if name == "approve" else "_commit_index_locked"
    write_name = "write_approval_reserved" if name == "approve" else "write_reserved"
    commit = getattr(index, commit_name)
    writer = getattr(e.bundle.storage, write_name)
    read = e.bundle.files.read

    def checkpoint():
        assert not committed, "checkpoint ran after successful index commit"
        checks.append((e.jobs._lock.locked(), bool(index._active_writes)))

    def checked_commit(*args, **kwargs):
        result = commit(*args, **kwargs)
        committed.append(True)
        return result

    def checked_write(principal, job, reservation, receipt, canonical_bytes, *,
                      check_bindings_locked, checkpoint=None):
        def checked_read(*args, **kwargs):
            result = read(*args, **kwargs)
            if result.revision_id == reservation.revision_id:
                path = job.working_dir / "results" / "metadata" / (reservation.revision_id + ".json")
                data, state = path.read_bytes(), ledger(e)
                failure(lambda: writer(
                    principal, job, reservation, receipt, canonical_bytes,
                    check_bindings_locked=check_bindings_locked,
                    checkpoint=checkpoint), 409, "metadata_write_pending")
                assert index._active_writes == {reservation.pending_id}
                assert ledger(e) == state and path.read_bytes() == data
                duplicate.append(True)
            return result

        with monkeypatch.context() as patch:
            patch.setattr(e.bundle.files, "read", checked_read)
            return writer(principal, job, reservation, receipt, canonical_bytes,
                          check_bindings_locked=check_bindings_locked,
                          checkpoint=checkpoint)

    monkeypatch.setattr(index, commit_name, checked_commit)
    monkeypatch.setattr(e.bundle.storage, write_name, checked_write)
    receipt, replayed = operation(e, name, checkpoint)
    assert not replayed and committed == duplicate == [True]
    assert checks[-1] == (True, True)
    assert (False, False) in checks and (False, True) in checks
    assert persisted(e, receipt).parent_revision_id == e.draft.revision_id
    assert_original(e)
    idle(e, 2)


@pytest.mark.parametrize("name", ["save", "approve"])
def test_control_before_initial_admission_leaves_active_handoff(ready, monkeypatch, name):
    e = ready
    before, disk = ledger(e), artifacts(e)
    index = e.bundle.index
    reserve_name = "reserve_approval" if name == "approve" else "reserve"
    reserve = getattr(index, reserve_name)
    armed = []
    error = KeyboardInterrupt("handoff control")

    def checked_reserve(*args, **kwargs):
        result = reserve(*args, **kwargs)
        armed.append(result.pending)
        return result

    def checkpoint():
        if armed:
            assert not index._active_writes and not e.jobs._lock.locked()
            raise error

    monkeypatch.setattr(index, reserve_name, checked_reserve)
    with pytest.raises(KeyboardInterrupt) as caught:
        operation(e, name, checkpoint)
    assert caught.value is error and len(armed) == 1
    after = ledger(e)
    for table in TABLES[:-1]:
        assert after[table] == before[table]
    assert len(rows(e, "metadata_pending")) == 1
    with index._transaction() as conn:
        assert index._pending(conn, armed[0].pending_id) == armed[0]
    assert armed[0].state == "active"
    assert artifacts(e) == disk
    assert not index._active_writes and not e.jobs._artifact_leases
    assert not e.jobs._conn.in_transaction
    assert_original(e)
