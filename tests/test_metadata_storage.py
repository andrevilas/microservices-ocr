"""C3b T1 coordination with real SQLite, POSIX files and one artifact lease."""
import hashlib
import threading
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.application import ApplicationError, Principal
from app.config import settings
from app.metadata_models import (
    MAX_SNAPSHOT_BYTES, MetadataSnapshot, SaveMetadataReceipt, snapshot_bytes,
)
from app.services import metadata_files as files
from app.services.metadata_storage import MetadataStorage
from app.services.metadata_store import MetadataStore
from test_metadata_files import case, snapshot


MARKER = "SENSITIVE_METADATA_MARKER"
TABLES = ("metadata_heads", "metadata_revisions", "metadata_operations",
          "metadata_audit", "metadata_pending")


def principal(user=1, **changes):
    values = dict(subject=f"user:{user}", user_id=user,
                  scopes=frozenset(("jobs:read", "artifacts:read",
                                    "metadata:read", "metadata:write")),
                  auth_source="local_jwt")
    values.update(changes)
    return Principal(**values)


@pytest.fixture
def coordination(case):
    # The imported fixture already owns the only lease for this job.
    case.store = MetadataStore(case.jobs)
    case.storage = MetadataStorage(case.store, case.service)
    case.principal = principal()
    return case


def make_write(c, *, model=None, job=None, actor=None, created_at=None):
    job = c.job if job is None else job
    actor = c.principal if actor is None else actor
    model = c.model if model is None else model
    data = snapshot_bytes(model)
    digest = hashlib.sha256(data).hexdigest()
    key, request = "1" * 64, "2" * 64
    admission = c.store.reserve(
        actor, job.job_id, key_hash=key, request_hash=request,
        expected_head=None, expected_sha256=None, revision_id=model.revision_id,
        sha256=digest, size_bytes=len(data),
        created_at=model.created_at if created_at is None else created_at,
    )
    receipt = SaveMetadataReceipt(revision_id=model.revision_id, sha256=digest,
                                  segment_ids={"row": model.segments[0].segment_id})
    assert admission.pending is not None and admission.receipt is None
    return SimpleNamespace(job=job, principal=actor, model=model, data=data,
                           pending=admission.pending, receipt=receipt,
                           key=key, request=request)


def write(c, w, callback=lambda conn: None):
    return c.storage.write_reserved(
        w.principal, w.job, w.pending, w.receipt, w.data,
        check_bindings_locked=callback,
    )


def sql_state(c):
    # Read-only assertions: no transaction context and no implicit commit.
    with c.jobs._lock:
        conn = c.jobs._get_conn()
        return {table: tuple(tuple(row) for row in conn.execute(
            "SELECT * FROM " + table + " ORDER BY rowid").fetchall())
                for table in TABLES}


def assert_error(operation, status=503, code="metadata_unavailable"):
    with pytest.raises(ApplicationError) as caught:
        operation()
    error = caught.value
    assert (error.status, error.code) == (status, code)
    assert error.__context__ is None and error.__cause__ is None
    assert MARKER not in str(error) + repr(error) + repr(vars(error))
    if code == "metadata_unavailable":
        assert error.message in ("Metadata is unavailable.",
                                 "Stored metadata is unavailable.")
    return error


def assert_unindexed(c):
    state = sql_state(c)
    assert all(not state[table] for table in TABLES)
    assert c.store._active_writes == set()


def test_normal_write_and_idempotent_preflight(coordination):
    c = coordination
    w = make_write(c)
    callbacks = []

    def bindings(conn):
        assert conn is c.jobs._get_conn() and conn.in_transaction
        path = c.directory / (w.model.revision_id + ".json")
        assert path.read_bytes() == w.data
        parsed = c.service.read(c.job, revision_id=w.pending.revision_id,
                                sha256=w.pending.sha256, size_bytes=len(w.data))
        assert snapshot_bytes(parsed) == w.data
        assert conn.execute("SELECT COUNT(*) FROM metadata_revisions").fetchone()[0] == 0
        callbacks.append(conn)

    result = write(c, w, bindings)
    assert result == w.receipt and result is not w.receipt
    assert result.segment_ids is not w.receipt.segment_ids
    assert len(callbacks) == 1
    state = sql_state(c)
    assert [len(state[t]) for t in TABLES] == [1, 1, 1, 1, 0]
    assert c.store._active_writes == set()
    with c.jobs._lock:
        conn = c.jobs._get_conn()
        digest, size = conn.execute(
            "SELECT sha256,size_bytes FROM metadata_revisions").fetchone()
        slots, approval = conn.execute(
            "SELECT approval_slots,approval_bytes FROM metadata_heads").fetchone()
    disk = (c.directory / (w.model.revision_id + ".json")).read_bytes()
    assert digest == hashlib.sha256(disk).hexdigest() == w.receipt.sha256
    assert size == len(disk) and (slots, approval) == (1, MAX_SNAPSHOT_BYTES)
    assert size + approval == len(w.data) + 1024 * 1024
    replay = c.store.preflight(c.principal, c.job.job_id,
                               key_hash=w.key, request_hash=w.request)
    assert replay.receipt == result and replay.receipt is not result
    assert sql_state(c) == state
    assert sorted(p.name for p in c.directory.iterdir()) == [w.model.revision_id + ".json"]


@pytest.mark.parametrize("mismatch", [
    "actor", "auth_source", "recorded_at", "created_at", "execution_id",
    "parent_revision_id", "receipt_segments",
])
def test_valid_snapshot_header_mismatches_cleanup(coordination, monkeypatch, mismatch):
    c = coordination
    document = c.model.model_dump(mode="python", by_alias=True)
    later = c.model.created_at + timedelta(seconds=1)
    if mismatch in ("actor", "auth_source", "recorded_at", "execution_id"):
        key, value = {
            "actor": ("actor_subject", "user:2"),
            "auth_source": ("auth_source", "oauth"),
            "recorded_at": ("recorded_at", later),
            "execution_id": ("execution_id", "d" * 32),
        }[mismatch]
        document["author"][key] = value
    elif mismatch == "created_at":
        document["created_at"] = later
    elif mismatch == "parent_revision_id":
        document["parent_revision_id"] = "d" * 32
    try:
        model = MetadataSnapshot.model_validate(document, strict=True)
    except ValidationError:
        if mismatch in ("execution_id", "parent_revision_id"):
            pytest.skip("C1 rejects this header variant")
        raise
    w = make_write(c, model=model, created_at=c.model.created_at)
    if mismatch == "receipt_segments":
        segment = "f" * 32 if model.segments[0].segment_id != "f" * 32 else "e" * 32
        w.receipt = SaveMetadataReceipt(revision_id=model.revision_id,
                                       sha256=w.pending.sha256, segment_ids={"row": segment})
    observed = []
    original_read = files.MetadataFiles.read
    original_cleanup = files.WriteAttempt.cleanup
    original_absent = files.WriteAttempt.verify_absent
    original_release = c.store._release_pending_after_cleanup_locked

    def read(self, *args, **kwargs):
        result = original_read(self, *args, **kwargs)
        assert snapshot_bytes(result) == w.data
        observed.append("read")
        return result

    def cleanup(self):
        original_cleanup(self)
        observed.append("cleanup")

    def absent(self):
        original_absent(self)
        observed.append("absent")

    def release(conn, pending_id):
        assert observed == ["read", "cleanup", "absent"]
        original_release(conn, pending_id)
        observed.append("release")

    monkeypatch.setattr(files.MetadataFiles, "read", read)
    monkeypatch.setattr(files.WriteAttempt, "cleanup", cleanup)
    monkeypatch.setattr(files.WriteAttempt, "verify_absent", absent)
    monkeypatch.setattr(c.store, "_release_pending_after_cleanup_locked", release)
    callbacks = []
    assert_error(lambda: write(c, w, lambda conn: callbacks.append(conn)))
    assert callbacks == []
    assert observed == ["read", "cleanup", "absent", "release"]
    assert_unindexed(c)
    assert not list(c.directory.glob("*.json"))
    assert not list(c.directory.glob(".*.tmp"))


@pytest.mark.parametrize("failure,status,code", [
    ("foreign", 404, "job_not_found"), ("admin", 404, "job_not_found"),
    ("scope", 403, "insufficient_scope"),
    ("unknown_pending", 409, "metadata_pending_not_found"),
    ("altered_reservation", 503, "metadata_unavailable"),
])
def test_initial_admission_never_claims_or_cleans(coordination, monkeypatch, failure, status, code):
    c = coordination
    w = make_write(c)
    before = sql_state(c)
    if failure in ("foreign", "admin"):
        w.principal = principal(2, is_admin=failure == "admin")
    elif failure == "scope":
        w.principal = principal(scopes=c.principal.scopes - {"metadata:write"})
    elif failure == "unknown_pending":
        unknown = "0" * 32 if w.pending.pending_id != "0" * 32 else "f" * 32
        w.pending = replace(w.pending, pending_id=unknown)
    else:
        w.pending = replace(w.pending, actor="user:2")
    cleanups = []
    original = files.WriteAttempt.cleanup

    def cleanup(self):
        cleanups.append(self)
        return original(self)

    monkeypatch.setattr(files.WriteAttempt, "cleanup", cleanup)
    assert_error(lambda: write(c, w), status, code)
    assert sql_state(c) == before and cleanups == []
    assert c.store._active_writes == set()
    assert not (c.job.working_dir / "results").exists()


def capacity_background(c):
    other = c.jobs.create("other.pdf", b"%PDF-1.4\nother\n", owner_user_id=2)
    c.jobs.update(other.job_id, status="completed", progress_percent=100)
    pending = make_write(c, job=other, actor=principal(2), model=snapshot(other))
    with c.store._transaction() as conn:
        c.store._mark_cleanup_locked(conn, pending.pending.pending_id)
    head_job = c.jobs.create("head.pdf", b"%PDF-1.4\nhead\n", owner_user_id=2)
    c.jobs.update(head_job.job_id, status="completed", progress_percent=100)
    model = snapshot(head_job)
    data = snapshot_bytes(model)
    digest = hashlib.sha256(data).hexdigest()
    # Explicit preexisting index fixture only; this SQL does not prove durability
    # or implement approval. The head is a draft with its future approval reserve.
    with c.store._transaction() as conn:
        conn.execute(
            "INSERT INTO metadata_revisions VALUES (?,?,?,?,?,?,?,?)",
            (model.revision_id, "user:2", head_job.job_id, None, digest,
             len(data), "draft", model.created_at.isoformat()))
        conn.execute("INSERT INTO metadata_heads VALUES (?,?,?,?,?,?)",
                     ("user:2", head_job.job_id, model.revision_id, digest,
                      1, MAX_SNAPSHOT_BYTES))
    return pending


@pytest.mark.parametrize("shortfall", [0, 1])
def test_capacity_counts_all_reserves_at_exact_boundary(coordination, monkeypatch, shortfall):
    c = coordination
    other = capacity_background(c)
    w = make_write(c)
    before = sql_state(c)
    # Independent arithmetic: own first pending, foreign cleanup pending, draft head.
    reserved = (len(w.data) + MAX_SNAPSHOT_BYTES
                + len(other.data) + MAX_SNAPSHOT_BYTES + MAX_SNAPSHOT_BYTES)
    with c.jobs._lock:
        conn = c.jobs._get_conn()
        actual = conn.execute(
            "SELECT (SELECT SUM(charged_bytes) FROM metadata_pending) + "
            "(SELECT SUM(approval_bytes) FROM metadata_heads)").fetchone()[0]
    assert actual == reserved
    available = reserved + settings.upload_min_free_mb * 1024 * 1024 - shortfall
    monkeypatch.setattr(files.os, "fstatvfs", lambda fd: SimpleNamespace(
        f_bavail=available, f_frsize=1))
    publications = []
    original = files.WriteAttempt.publish

    def publish(self, data):
        publications.append(self)
        return original(self, data)

    monkeypatch.setattr(files.WriteAttempt, "publish", publish)
    if shortfall:
        error = assert_error(lambda: write(c, w), 503, "storage_unavailable")
        assert error.message == "Insufficient free disk space."
        assert publications == []
        assert not (c.job.working_dir / "results").exists()
    else:
        assert write(c, w) == w.receipt
        assert len(publications) == 1
        assert (c.directory / (w.model.revision_id + ".json")).read_bytes() == w.data
    after = sql_state(c)
    assert after["metadata_pending"] == tuple(
        row for row in before["metadata_pending"] if row[0] != w.pending.pending_id)
    for table in TABLES[:-1]:
        assert all(row in after[table] for row in before[table])
        assert len(after[table]) == len(before[table]) + (not shortfall)
    assert c.store._active_writes == set()


def test_active_duplicate_threads_preserve_first_claim(coordination, monkeypatch):
    c = coordination
    w = make_write(c)
    entered, release = threading.Event(), threading.Event()
    publications, commits, results, errors = [], [], {}, {}
    original_publish = files.WriteAttempt.publish
    original_commit = c.store._commit_index_locked

    def publish(self, data):
        publications.append(self)
        entered.set()
        if not release.wait(10):
            raise RuntimeError(MARKER)
        return original_publish(self, data)

    def commit(*args):
        result = original_commit(*args)
        commits.append(result)
        return result

    def run(name):
        try:
            results[name] = write(c, w)
        except Exception as error:
            errors[name] = error

    monkeypatch.setattr(files.WriteAttempt, "publish", publish)
    monkeypatch.setattr(c.store, "_commit_index_locked", commit)
    first = threading.Thread(target=run, args=("first",), daemon=True)
    second = threading.Thread(target=run, args=("second",), daemon=True)
    try:
        first.start()
        assert entered.wait(10)
        second.start()
        second.join(10)
        assert not second.is_alive()
        assert "second" not in results
        error = errors["second"]
        assert isinstance(error, ApplicationError)
        assert (error.status, error.code) == (409, "metadata_write_pending")
        assert error.__context__ is None and error.__cause__ is None
        assert c.store._active_writes == {w.pending.pending_id}
        assert len(publications) == 1 and commits == []
    finally:
        release.set()
        for thread in (first, second):
            if thread.ident is not None:
                thread.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert "first" not in errors and results["first"] == w.receipt
    assert len(publications) == len(commits) == 1
    assert [len(sql_state(c)[t]) for t in TABLES] == [1, 1, 1, 1, 0]
    assert c.store._active_writes == set()


@pytest.mark.parametrize("bavail,frsize,fault", [
    (17, 1, False), (True, 1, False), (-1, 1, False),
    (17, True, False), (17, 0, False), (17, 1, True),
])
def test_available_bytes_is_pure_validated_and_attempt_stays_usable(case, monkeypatch, bavail, frsize, fault):
    c = case
    attempt = c.service.prepare(c.job, pending_id=c.pending,
                                revision_id=c.model.revision_id,
                                sha256=c.digest, size_bytes=len(c.data))
    mutations = []

    def capacity(fd):
        if fault:
            raise OSError(MARKER)
        return SimpleNamespace(f_bavail=bavail, f_frsize=frsize)

    def observe(original, name):
        def wrapped(*args, **kwargs):
            mutations.append(name)
            return original(*args, **kwargs)
        return wrapped

    with monkeypatch.context() as patch:
        patch.setattr(files.os, "fstatvfs", capacity)
        for name in ("mkdir", "fsync"):
            patch.setattr(files.os, name, observe(getattr(files.os, name), name))
        if type(bavail) is int and bavail >= 0 and type(frsize) is int and frsize > 0 and not fault:
            assert attempt.available_bytes() == bavail * frsize
        else:
            assert_error(attempt.available_bytes)
    assert mutations == [] and not (c.job.working_dir / "results").exists()
    attempt.publish(c.data)
    assert (c.directory / (c.model.revision_id + ".json")).read_bytes() == c.data
    attempt.cleanup()
    attempt.verify_absent()


@pytest.mark.parametrize("entry", ["missing_results", "missing_metadata", "temp", "final"])
def test_verify_absent_handles_safe_missing_children_and_unowned_collisions(case, entry):
    c = case
    attempt = c.service.prepare(c.job, pending_id=c.pending,
                                revision_id=c.model.revision_id,
                                sha256=c.digest, size_bytes=len(c.data))
    if entry == "missing_metadata":
        c.directory.parent.mkdir()
    if entry.startswith("missing"):
        attempt.verify_absent()
        assert not c.directory.exists()
        assert (c.directory.parent.exists()) == (entry == "missing_metadata")
        return
    c.directory.mkdir(parents=True)
    name = "." + c.pending + ".tmp" if entry == "temp" else c.model.revision_id + ".json"
    path = c.directory / name
    path.write_bytes(MARKER.encode())
    inode = path.stat().st_ino
    assert_error(attempt.verify_absent)
    assert path.read_bytes() == MARKER.encode() and path.stat().st_ino == inode
    assert list(c.directory.iterdir()) == [path]
