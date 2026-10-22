"""Physical approval coordination; policy/evidence validation belongs to callers.

Reuse real SQLite/POSIX fixtures and their single existing artifact lease. Every
approval starts with a draft physically committed through the save writer.
"""
import hashlib
import threading
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.application import ApplicationError
from app.config import settings
from app.metadata_approval_contract import ApproveMetadataReceipt, approval_receipt_for
from app.metadata_models import (
    MAX_SNAPSHOT_BYTES, ApprovalRecord, MetadataSnapshot, SaveMetadataReceipt,
    snapshot_bytes,
)
from app.services import metadata_files as files
from test_metadata_storage import (
    MARKER, TABLES, assert_error, case, coordination, make_write, principal,
    sql_state, write,
)


def make_approval(c, *, header=None):
    draft = make_write(c)
    write(c, draft)
    base = sql_state(c)
    actor = principal(scopes=(c.principal.scopes - {"metadata:write"})
                      | {"metadata:approve"})
    now = draft.model.created_at + timedelta(seconds=1)
    approval = ApprovalRecord(
        reviewed_revision_id=draft.receipt.revision_id,
        reviewed_sha256=draft.receipt.sha256,
        actor_subject=actor.subject, auth_source=actor.auth_source,
        approved_at=now,
    )
    document = draft.model.model_dump(mode="python", by_alias=True)
    document.update(
        revision_id=uuid4().hex, parent_revision_id=draft.receipt.revision_id,
        created_at=now, state="approved",
        author=dict(actor_subject=actor.subject, auth_source=actor.auth_source,
                    recorded_at=now, execution_id=None),
        approval=approval.model_dump(mode="python"),
    )
    if header == "reviewed_hash":
        document["approval"]["reviewed_sha256"] = "e" * 64
    elif header == "parent":
        other = uuid4().hex
        document["parent_revision_id"] = other
        document["approval"]["reviewed_revision_id"] = other
    elif header == "actor":
        document["author"]["actor_subject"] = "user:2"
        document["approval"]["actor_subject"] = "user:2"
    elif header == "auth_source":
        document["author"]["auth_source"] = "oauth"
        document["approval"]["auth_source"] = "oauth"
    elif header == "time":
        later = now + timedelta(seconds=1)
        document["created_at"] = later
        document["author"]["recorded_at"] = later
        document["approval"]["approved_at"] = later
    model = MetadataSnapshot.model_validate(document, strict=True)
    data = snapshot_bytes(model)
    digest = hashlib.sha256(data).hexdigest()
    # Bind the handoff to the actual reserved draft even in bad-header tests.
    receipt = ApproveMetadataReceipt(
        operation="approve-v1", state="approved", revision_id=model.revision_id,
        sha256=digest, reviewed_revision_id=draft.receipt.revision_id,
        reviewed_sha256=draft.receipt.sha256,
    )
    if header is None:
        assert approval_receipt_for(model) == receipt
    admission = c.store.reserve_approval(
        actor, c.job.job_id, key_hash=draft.key, request_hash=draft.request,
        expected_head=draft.receipt.revision_id,
        expected_sha256=draft.receipt.sha256,
        revision_id=model.revision_id, sha256=digest, size_bytes=len(data),
        created_at=now,
    )
    assert admission.pending is not None and admission.receipt is None
    return SimpleNamespace(
        job=c.job, principal=actor, model=model, data=data, receipt=receipt,
        pending=admission.pending, draft=draft, base=base,
        key=draft.key, request=draft.request,
    )


def approve(c, w, callback=lambda conn: None):
    return c.storage.write_approval_reserved(
        w.principal, w.job, w.pending, w.receipt, w.data,
        check_bindings_locked=callback,
    )


def path(c, w):
    return c.directory / (w.pending.revision_id + ".json")


def assert_draft(c, w, *, pending=None, residue=False):
    state = sql_state(c)
    for table in TABLES[:-1]:
        assert state[table] == w.base[table]
    rows = state["metadata_pending"]
    if pending is None:
        assert rows == ()
    else:
        assert len(rows) == 1 and rows[0][0] == w.pending.pending_id
        assert rows[0][14:18] == (0, 0, 0, 0)
        assert rows[0][-1] == pending
    assert path(c, w).exists() is residue
    assert path(c, w.draft).read_bytes() == w.draft.data
    assert state["metadata_heads"][0][-2:] == (1, MAX_SNAPSHOT_BYTES)
    assert c.store._active_writes == set()


def capture_attempt(c, monkeypatch):
    attempts = []
    original = files.MetadataFiles.prepare

    def prepare(self, *args, **kwargs):
        attempt = original(self, *args, **kwargs)
        attempts.append(attempt)
        return attempt

    monkeypatch.setattr(files.MetadataFiles, "prepare", prepare)
    return attempts


def test_approval_without_write_scope_publishes_before_final_callback(coordination, monkeypatch):
    c = coordination
    w = make_approval(c)
    assert "metadata:write" not in w.principal.scopes
    observed = []
    publish, read = files.WriteAttempt.publish, files.MetadataFiles.read
    commit = c.store._commit_approval_index_locked

    def unlocked():
        assert c.jobs._lock.acquire(blocking=False)
        c.jobs._lock.release()
        assert c.store._active_writes == {w.pending.pending_id}

    def published(self, data):
        unlocked()
        result = publish(self, data)
        observed.append("published")
        return result

    def verified(self, *args, **kwargs):
        unlocked()
        result = read(self, *args, **kwargs)
        assert snapshot_bytes(result) == w.data
        observed.append("verified")
        return result

    def bindings(conn):
        assert observed == ["published", "verified"]
        assert conn is c.jobs._get_conn() and conn.in_transaction
        assert path(c, w).read_bytes() == w.data
        assert conn.execute("SELECT COUNT(*) FROM metadata_revisions").fetchone()[0] == 1
        observed.append("bindings")

    def indexed(conn, *args):
        assert observed == ["published", "verified", "bindings"]
        result = commit(conn, *args)
        observed.append("indexed")
        return result

    monkeypatch.setattr(files.WriteAttempt, "publish", published)
    monkeypatch.setattr(files.MetadataFiles, "read", verified)
    monkeypatch.setattr(c.store, "_commit_approval_index_locked", indexed)
    result = approve(c, w, bindings)
    assert result == w.receipt and result is not w.receipt
    assert observed == ["published", "verified", "bindings", "indexed"]
    assert c.store._active_writes == set()
    disk = path(c, w).read_bytes()
    assert disk == w.data and hashlib.sha256(disk).hexdigest() == result.sha256
    assert path(c, w.draft).read_bytes() == w.draft.data
    state = sql_state(c)
    assert [len(state[t]) for t in TABLES] == [1, 2, 2, 2, 0]
    with c.jobs._lock:
        conn = c.jobs._get_conn()
        assert conn.in_transaction is False
        assert tuple(conn.execute(
            "SELECT sha256,size_bytes,state FROM metadata_revisions WHERE revision_id=?",
            (result.revision_id,)).fetchone()) == (result.sha256, len(disk), "approved")
        assert tuple(conn.execute(
            "SELECT revision_id,sha256,approval_slots,approval_bytes FROM metadata_heads"
        ).fetchone()) == (result.revision_id, result.sha256, 0, 0)
        assert tuple(conn.execute(
            "SELECT parent_revision_id,action,actor,auth_source,created_at "
            "FROM metadata_audit WHERE revision_id=?", (result.revision_id,)
        ).fetchone()) == (
            w.draft.receipt.revision_id, "approve", w.principal.subject,
            w.principal.auth_source, w.pending.created_at.isoformat(),
        )
    replay = c.store.preflight_approval(
        w.principal, c.job.job_id, key_hash=w.key, request_hash=w.request)
    assert replay.receipt == result and replay.receipt is not result
    assert sql_state(c) == state


@pytest.mark.parametrize("header", ["reviewed_hash", "parent", "actor", "auth_source", "time"])
def test_published_header_mismatch_cleans_only_approval(coordination, monkeypatch, header):
    c = coordination
    w = make_approval(c, header=header)
    verified = []
    original = files.MetadataFiles.read

    def read(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        verified.append(result)
        return result

    monkeypatch.setattr(files.MetadataFiles, "read", read)
    callbacks = []
    assert_error(lambda: approve(c, w, lambda conn: callbacks.append(conn)))
    assert len(verified) == 1 and callbacks == []
    assert_draft(c, w)
    assert not list(c.directory.glob(".*.tmp"))


@pytest.mark.parametrize("field,value", [
    ("revision_id", "e" * 32), ("sha256", "e" * 64),
    ("reviewed_revision_id", "e" * 32), ("reviewed_sha256", "e" * 64),
    ("reviewed_sha256", 123),
])
def test_receipt_must_strictly_match_before_claim_or_cleanup(coordination, monkeypatch, field, value):
    c = coordination
    w = make_approval(c)
    before = sql_state(c)
    # An original model instance must be revalidated, including mutated fields.
    object.__setattr__(w.receipt, field, value)

    def forbidden(*args, **kwargs):
        raise AssertionError("unmatched receipt authorized physical I/O")

    monkeypatch.setattr(files.MetadataFiles, "prepare", forbidden)
    assert_error(lambda: approve(c, w), 422, "metadata_invalid_input")
    assert sql_state(c) == before
    assert_draft(c, w, pending="active")


@pytest.mark.parametrize("invalid", ["bytes", "callback"])
def test_invalid_input_reconciles_exact_owned_unclaimed_handoff(coordination, monkeypatch, invalid):
    c = coordination
    w = make_approval(c)
    attempts = capture_attempt(c, monkeypatch)
    callback = None if invalid == "callback" else lambda conn: None
    if invalid == "bytes":
        w.data = bytearray(w.data)
    assert_error(lambda: approve(c, w, callback), 422, "metadata_invalid_input")
    assert len(attempts) == 1
    assert_draft(c, w)


@pytest.mark.parametrize("invalid", ["foreign", "altered", "unknown", "nonhuman"])
def test_unowned_or_unauthorized_handoff_never_cleans(coordination, monkeypatch, invalid):
    c = coordination
    w = make_approval(c)
    before = sql_state(c)
    status, code = 503, "metadata_unavailable"
    if invalid == "foreign":
        w.principal = principal(2, scopes=w.principal.scopes)
        status, code = 404, "job_not_found"
    elif invalid == "altered":
        w.pending = replace(w.pending, actor="user:2")
    elif invalid == "unknown":
        w.pending = replace(w.pending, pending_id=uuid4().hex)
        status, code = 409, "metadata_pending_not_found"
    else:
        w.principal = principal(scopes=w.principal.scopes, auth_source="service")

    def forbidden(*args, **kwargs):
        raise AssertionError("unowned handoff authorized physical I/O")

    monkeypatch.setattr(files.MetadataFiles, "prepare", forbidden)
    if invalid == "nonhuman":
        with pytest.raises(ApplicationError):
            approve(c, w)
    else:
        assert_error(lambda: approve(c, w), status, code)
    assert sql_state(c) == before and c.store._active_writes == set()
    assert path(c, w.draft).read_bytes() == w.draft.data


@pytest.mark.parametrize("approval_reservation", [False, True])
def test_cross_operation_never_claims_or_cleans(coordination, monkeypatch, approval_reservation):
    c = coordination
    w = make_approval(c) if approval_reservation else make_write(c)
    before = sql_state(c)

    def forbidden(*args, **kwargs):
        raise AssertionError("cross-operation physical I/O")

    monkeypatch.setattr(files.MetadataFiles, "prepare", forbidden)
    if approval_reservation:
        assert_error(lambda: write(c, w))
    else:
        assert_error(lambda: approve(c, w))
    assert sql_state(c) == before and c.store._active_writes == set()


@pytest.mark.parametrize("shortfall", [0, 1])
def test_approval_disk_boundary_uses_existing_head_reserve(coordination, monkeypatch, shortfall):
    c = coordination
    w = make_approval(c)
    available = MAX_SNAPSHOT_BYTES + settings.upload_min_free_mb * 1024 * 1024 - shortfall
    monkeypatch.setattr(files.os, "fstatvfs", lambda fd: SimpleNamespace(
        f_bavail=available, f_frsize=1))
    if shortfall:
        assert_error(lambda: approve(c, w), 503, "storage_unavailable")
        assert_draft(c, w)
    else:
        assert approve(c, w) == w.receipt
        assert path(c, w).read_bytes() == w.data


@pytest.mark.parametrize("fault", ["callback", "scope", "lifecycle", "hash", "read"])
def test_failures_preserve_original_draft_and_release_verified_cleanup(coordination, monkeypatch, fault):
    c = coordination
    w = make_approval(c)
    original = files.WriteAttempt.publish
    callbacks = []

    def publish(self, data):
        result = original(self, data)
        if fault == "scope":
            object.__setattr__(w.principal, "scopes",
                               w.principal.scopes - {"metadata:approve"})
        elif fault == "lifecycle":
            c.jobs.update(c.job.job_id, status="failed")
        elif fault == "read":
            # Preserve the owned inode while corrupting real published bytes.
            damaged = bytearray(path(c, w).read_bytes())
            damaged[-1] ^= 1
            path(c, w).write_bytes(damaged)
        return result

    def bindings(conn):
        callbacks.append(conn)
        if fault == "callback":
            raise RuntimeError(MARKER)

    monkeypatch.setattr(files.WriteAttempt, "publish", publish)
    if fault == "hash":
        damaged = bytearray(w.data)
        damaged[-1] ^= 1
        w.data = bytes(damaged)
    status, code = {
        "scope": (403, "insufficient_scope"),
        "lifecycle": (409, "job_not_completed"),
    }.get(fault, (503, "metadata_unavailable"))
    assert_error(lambda: approve(c, w, bindings), status, code)
    assert len(callbacks) == (1 if fault == "callback" else 0)
    assert_draft(c, w)


def test_duplicate_active_approval_preserves_first_claim(coordination, monkeypatch):
    c = coordination
    w = make_approval(c)
    entered, release = threading.Event(), threading.Event()
    results, errors, publications = {}, {}, []
    original = files.WriteAttempt.publish

    def publish(self, data):
        publications.append(self)
        entered.set()
        if not release.wait(10):
            raise RuntimeError(MARKER)
        return original(self, data)

    def run(name):
        try:
            results[name] = approve(c, w)
        except Exception as exc:
            errors[name] = exc

    monkeypatch.setattr(files.WriteAttempt, "publish", publish)
    first = threading.Thread(target=run, args=("first",), daemon=True)
    second = threading.Thread(target=run, args=("second",), daemon=True)
    try:
        first.start()
        assert entered.wait(10)
        second.start()
        second.join(10)
        assert not second.is_alive()
        error = errors["second"]
        assert isinstance(error, ApplicationError)
        assert (error.status, error.code) == (409, "metadata_write_pending")
        assert error.__context__ is None and error.__cause__ is None
        assert c.store._active_writes == {w.pending.pending_id}
        assert len(publications) == 1
    finally:
        release.set()
        for thread in (first, second):
            if thread.ident is not None:
                thread.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert "first" not in errors and results["first"] == w.receipt
    assert c.store._active_writes == set()
    assert path(c, w.draft).read_bytes() == w.draft.data


@pytest.mark.parametrize("outcome", ["rollback", "committed", "unknown"])
def test_sql_commit_failures_require_fresh_diagnosis_before_cleanup(coordination, monkeypatch, outcome):
    c = coordination
    w = make_approval(c)
    real = c.jobs._get_conn()

    class ConnectionFault:
        armed = False

        def __getattr__(self, name):
            return getattr(real, name)

        def commit(self):
            if self.armed:
                self.armed = False
                if outcome != "rollback":
                    real.commit()
                raise RuntimeError(MARKER)
            return real.commit()

    connection = ConnectionFault()
    original = c.store._commit_approval_index_locked
    cleanups = []
    cleanup = files.WriteAttempt.cleanup

    def index(*args):
        result = original(*args)
        connection.armed = True
        return result

    def cleaned(self):
        cleanups.append(self)
        return cleanup(self)

    monkeypatch.setattr(c.jobs, "_get_conn", lambda: connection)
    monkeypatch.setattr(c.store, "_commit_approval_index_locked", index)
    monkeypatch.setattr(files.WriteAttempt, "cleanup", cleaned)
    if outcome == "unknown":
        def uncertain(*args):
            raise RuntimeError(MARKER)
        monkeypatch.setattr(c.store, "_index_outcome_locked", uncertain)
    assert_error(lambda: approve(c, w))
    assert c.store._active_writes == set()
    if outcome == "rollback":
        assert len(cleanups) == 1
        assert_draft(c, w)
    else:
        assert cleanups == [] and path(c, w).read_bytes() == w.data
        state = sql_state(c)
        assert [len(state[t]) for t in TABLES] == [1, 2, 2, 2, 0]
        assert c.store.preflight_approval(
            w.principal, c.job.job_id, key_hash=w.key,
            request_hash=w.request).receipt == w.receipt
    assert path(c, w.draft).read_bytes() == w.draft.data


def test_cleanup_failure_retains_reserve_until_bounded_retry(coordination, monkeypatch):
    c = coordination
    w = make_approval(c)
    attempts = capture_attempt(c, monkeypatch)
    original = files.WriteAttempt.cleanup

    def failed_cleanup(self):
        raise OSError(MARKER)

    def failed_bindings(conn):
        raise RuntimeError(MARKER)

    with monkeypatch.context() as patch:
        patch.setattr(files.WriteAttempt, "cleanup", failed_cleanup)
        assert_error(lambda: approve(c, w, failed_bindings))
    assert len(attempts) == 1
    assert_draft(c, w, pending="cleanup", residue=True)
    assert files.WriteAttempt.cleanup is original
    # Trusted bounded reconciliation uses the retained attempt under the same lease.
    c.storage._reconcile(w.pending, w.receipt, attempts[0])
    assert_draft(c, w)


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("phase", ["publish", "callback"])
def test_control_exception_identity_and_abandoned_pending(coordination, monkeypatch, control_type, phase):
    c = coordination
    w = make_approval(c)
    attempts = capture_attempt(c, monkeypatch)
    control = control_type(MARKER)
    original = files.WriteAttempt.publish

    def publish(self, data):
        result = original(self, data)
        if phase == "publish":
            raise control
        return result

    def bindings(conn):
        raise control

    monkeypatch.setattr(files.WriteAttempt, "publish", publish)
    with pytest.raises(control_type) as caught:
        approve(c, w, bindings)
    assert caught.value is control
    assert_draft(c, w, pending="active", residue=True)
    assert len(attempts) == 1
    c.storage._reconcile(w.pending, w.receipt, attempts[0])
    assert_draft(c, w)


def test_confirmed_approval_diagnosis_allows_later_head_and_pending(coordination):
    c = coordination
    w = make_approval(c)
    approve(c, w)
    document = c.model.model_dump(mode="python", by_alias=True)
    now = w.pending.created_at + timedelta(seconds=1)
    document.update(revision_id=uuid4().hex,
                    parent_revision_id=w.pending.revision_id, created_at=now)
    document["author"]["recorded_at"] = now
    model = MetadataSnapshot.model_validate(document, strict=True)
    data = snapshot_bytes(model)
    digest = hashlib.sha256(data).hexdigest()
    admission = c.store.reserve(
        c.principal, c.job.job_id, key_hash="3" * 64, request_hash="4" * 64,
        expected_head=w.pending.revision_id, expected_sha256=w.pending.sha256,
        revision_id=model.revision_id, sha256=digest, size_bytes=len(data),
        created_at=now,
    )
    later = SimpleNamespace(
        job=c.job, principal=c.principal, pending=admission.pending, data=data,
        receipt=SaveMetadataReceipt(
            revision_id=model.revision_id, sha256=digest,
            segment_ids={"row": model.segments[0].segment_id}),
    )
    write(c, later)
    c.store.reserve(
        c.principal, c.job.job_id, key_hash="5" * 64, request_hash="6" * 64,
        expected_head=model.revision_id, expected_sha256=digest,
        revision_id=uuid4().hex, sha256="f" * 64, size_bytes=1,
        created_at=now + timedelta(seconds=1),
    )
    before = sql_state(c)
    with c.store._transaction() as conn:
        assert c.store._index_outcome_locked(conn, w.pending, w.receipt) == "confirmed"
    c.storage._reconcile(w.pending, w.receipt, None)
    assert sql_state(c) == before
    assert path(c, w).read_bytes() == w.data
    assert path(c, w.draft).read_bytes() == w.draft.data
