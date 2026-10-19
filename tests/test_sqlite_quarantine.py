"""Shared quarantine tests using real SQLite effects and bounded thread barriers."""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Thread
import sqlite3

import pytest

from app.application import ApplicationError
from app.services.metadata_store import MetadataStore
from app.services.storage_service import JobStore


SECRET = "synthetic-sensitive-sql-marker"
WAIT = 10


class Principal:
    subject = "user:1"
    auth_source = "local_jwt"

    def require(self, scope):
        assert scope in {"jobs:read", "artifacts:read", "metadata:read", "metadata:write"}


class ConnectionProbe:
    """Forward real effects; each configured fault is consumed exactly once."""

    def __init__(self, actual):
        self.actual = actual
        self.calls = Counter()
        self.faults = {}

    def __getattr__(self, name):
        return getattr(self.actual, name)

    def fault(self, phase):
        value = self.faults.pop(phase, None)
        if isinstance(value, BaseException):
            raise value
        return value

    def health(self, offset, value):
        self.faults[f"health:{self.calls['health'] + offset}"] = value

    @property
    def in_transaction(self):
        self.calls["health"] += 1
        phase = f"health:{self.calls['health']}"
        if phase in self.faults:
            return self.fault(phase)
        return self.actual.in_transaction

    def execute(self, sql, *args, **kwargs):
        self.calls["sql"] += 1
        begin = sql.strip().upper() == "BEGIN IMMEDIATE"
        if begin:
            self.calls["begin"] += 1
            self.fault("begin_before")
        result = self.actual.execute(sql, *args, **kwargs)
        if begin:
            self.fault("begin_after")
        return result

    def commit(self):
        self.calls["commit"] += 1
        self.fault("commit_before")
        result = self.actual.commit()
        self.fault("commit_after")
        return result

    def rollback(self):
        self.calls["rollback"] += 1
        self.fault("rollback_before")
        result = self.actual.rollback()
        self.fault("rollback_after")
        return result


@pytest.fixture
def stores(tmp_path):
    jobs = JobStore(root_dir=tmp_path)
    actual = jobs._get_conn()
    try:
        metadata = MetadataStore(jobs)
        probe = ConnectionProbe(actual)
        jobs._conn = probe
        yield jobs, metadata, probe, actual
    finally:
        # Test isolation only: never use this repair as a product assertion.
        try:
            actual.rollback()
        finally:
            actual.close()


def make_job(jobs, *, completed=False):
    job = jobs.create("baseline.pdf", b"%PDF-1.4\n", owner_user_id=1)
    if completed:
        jobs.update(job.job_id, status="completed", progress_percent=100)
    return job


def unavailable(error):
    assert isinstance(error, ApplicationError)
    assert error.status == 503
    assert error.code == "metadata_unavailable"
    assert error.message == "Metadata is unavailable."
    assert SECRET not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def lock_free(jobs):
    acquired = jobs._lock.acquire(blocking=False)
    if acquired:
        jobs._lock.release()
    assert acquired


def launch(action):
    started, done, results = Event(), Event(), []

    def run():
        started.set()
        try:
            results.append(action())
        except BaseException as error:
            results.append(error)
        finally:
            done.set()

    thread = Thread(target=run, daemon=True)
    thread.start()
    return thread, started, done, results


def join_all(threads):
    for thread in threads:
        thread.join(timeout=WAIT)
    assert all(not thread.is_alive() for thread in threads)


def sql_job(actual, job):
    return actual.execute("SELECT * FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()


@pytest.mark.parametrize("rollback_phase", ["rollback_before", "rollback_after"])
def test_failed_rollback_quarantines_before_waiting_update(stores, monkeypatch, rollback_phase):
    jobs, metadata, probe, actual = stores
    baseline = make_job(jobs)
    snapshot = jobs.get_snapshot(baseline.job_id)
    persisted = sql_job(actual, baseline)
    pending_job = make_job(jobs, completed=True)
    pending = metadata.reserve(
        Principal(), pending_job.job_id, key_hash="a" * 64, request_hash="b" * 64,
        expected_head=None, expected_sha256=None, revision_id="d" * 32,
        sha256="c" * 64, size_bytes=17, created_at=datetime.now(timezone.utc),
    ).pending
    assert pending is not None
    query = ("SELECT state,charged_slots,charged_bytes,approval_slots,approval_bytes "
             "FROM metadata_pending WHERE pending_id=?")
    original = actual.execute(query, (pending.pending_id,)).fetchone()
    assert original == ("active", 2, 17 + 1048576, 1, 1048576)
    observed = []
    quarantine = jobs._quarantine_locked

    def observe_quarantine():
        acquired = jobs._lock.acquire(blocking=False)
        if acquired:
            jobs._lock.release()
        assert not acquired
        quarantine()
        observed.append(jobs._sqlite_quarantined)

    monkeypatch.setattr(jobs, "_quarantine_locked", observe_quarantine)
    probe.calls.clear()
    probe.faults["commit_before"] = RuntimeError(SECRET)
    probe.faults[rollback_phase] = RuntimeError(SECRET)
    threads = []
    try:
        with pytest.raises(ApplicationError) as failure:
            with metadata._transaction() as conn:
                conn.execute("UPDATE metadata_pending SET state='cleanup' WHERE pending_id=?",
                             (pending.pending_id,))
                thread, started, done, results = launch(
                    lambda: jobs.update(baseline.job_id, filename="must-not-publish.pdf"))
                threads.append(thread)
                assert started.wait(WAIT)
                assert not done.is_set()
    finally:
        join_all(threads)
    unavailable(failure.value)
    assert observed and all(observed)
    assert len(results) == 1
    unavailable(results[0])
    assert probe.calls["commit"] == probe.calls["rollback"] == 1
    assert jobs.get_snapshot(baseline.job_id) == snapshot
    assert sql_job(actual, baseline) == persisted
    current = actual.execute(query, (pending.pending_id,)).fetchone()
    assert current[1:] == original[1:]
    assert current[0] == ("cleanup" if rollback_phase == "rollback_before" else "active")
    reader = sqlite3.connect(str(jobs.db_path), timeout=1)
    try:
        assert reader.execute(query, (pending.pending_id,)).fetchone() == original
        assert reader.execute("SELECT COUNT(*) FROM metadata_revisions WHERE job_id=?",
                              (pending_job.job_id,)).fetchone() == (0,)
    finally:
        reader.close()
    lock_free(jobs)


@pytest.mark.parametrize("phase", ["commit_before", "commit_after"])
def test_commit_failure_with_healthy_rollback_remains_usable(stores, phase):
    jobs, metadata, probe, actual = stores
    job = make_job(jobs, completed=True)
    probe.calls.clear()
    probe.faults[phase] = RuntimeError(SECRET)
    with pytest.raises(ApplicationError) as failure:
        with metadata._transaction() as conn:
            conn.execute("UPDATE jobs SET filename='sql-effect.pdf' WHERE job_id=?", (job.job_id,))
    unavailable(failure.value)
    assert not jobs._sqlite_quarantined
    assert actual.in_transaction is False
    assert probe.calls["commit"] == probe.calls["rollback"] == 1
    assert actual.execute("SELECT filename FROM jobs WHERE job_id=?", (job.job_id,)).fetchone() == (
        "baseline.pdf" if phase == "commit_before" else "sql-effect.pdf",)
    assert metadata.get_head(Principal(), job.job_id) is None
    with metadata._transaction() as conn:
        assert metadata._reserved_disk_bytes_locked(conn, "user:1", job.job_id) == 0
    assert jobs.check_db()
    lock_free(jobs)


def test_foreign_transaction_is_never_rolled_back(stores):
    jobs, metadata, probe, actual = stores
    job = make_job(jobs, completed=True)
    actual.execute("BEGIN IMMEDIATE")
    actual.execute("UPDATE jobs SET filename='foreign.pdf' WHERE job_id=?", (job.job_id,))
    probe.calls.clear()
    with pytest.raises(ApplicationError) as failure:
        with metadata._transaction():
            pytest.fail("foreign transaction admitted")
    unavailable(failure.value)
    assert jobs._sqlite_quarantined
    assert actual.in_transaction is True
    assert probe.calls["rollback"] == probe.calls["commit"] == probe.calls["begin"] == 0
    assert actual.execute("SELECT filename FROM jobs WHERE job_id=?", (job.job_id,)).fetchone() == ("foreign.pdf",)
    assert not jobs.check_db()
    lock_free(jobs)


@pytest.mark.parametrize("offset,value", [
    (1, RuntimeError(SECRET)), (1, True), (1, 0), (1, None),
    (2, True), (2, RuntimeError(SECRET)),
])
def test_unprovable_health_is_monotonic(stores, offset, value):
    jobs, metadata, probe, actual = stores
    probe.calls.clear()
    probe.health(offset, value)
    with pytest.raises(ApplicationError) as failure:
        with metadata._transaction() as conn:
            metadata._connection_locked(conn) if offset == 1 else conn.execute("SELECT 1")
    unavailable(failure.value)
    assert jobs._sqlite_quarantined
    assert actual.in_transaction is False
    assert probe.in_transaction is False  # Fault consumed; underlying health restored.
    assert probe.calls["rollback"] == 0
    assert probe.calls["commit"] == (1 if offset == 2 else 0)
    counts = probe.calls.copy()
    with pytest.raises(ApplicationError) as repeated:
        with metadata._transaction():
            pytest.fail("quarantine reset")
    unavailable(repeated.value)
    assert not jobs.check_db()
    assert jobs._sqlite_quarantined
    assert probe.calls == counts
    lock_free(jobs)


def test_quarantined_missing_connection_never_connects(stores, monkeypatch):
    jobs, _, _, _ = stores
    with jobs._lock:
        jobs._quarantine_locked()
        jobs._conn = None

    def forbidden(*args, **kwargs):
        pytest.fail("quarantine attempted a connection")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    with pytest.raises(ApplicationError) as failure:
        jobs._get_conn()
    unavailable(failure.value)
    assert jobs._conn is None
    assert not jobs.check_db()


def test_last_lease_release_preserves_durable_tombstone_and_files(stores, monkeypatch):
    jobs, _, probe, actual = stores
    job = make_job(jobs, completed=True)
    jobs.acquire_artifacts(job.job_id, "user:1")
    jobs.remove_artifacts(job.job_id, "user:1", "user:1")
    snapshot = jobs.get_snapshot(job.job_id)
    persisted = sql_job(actual, job)
    assert snapshot.artifacts_removed_at is not None
    assert snapshot.artifacts_purged_at is None
    assert job.input_pdf_path.exists()
    with pytest.raises(ApplicationError) as retained:
        jobs.check_retained(snapshot)
    assert (retained.value.status, retained.value.code) == (410, "artifacts_deleted")

    def forbidden(*args, **kwargs):
        pytest.fail("quarantined lease release attempted physical purge")

    monkeypatch.setattr("app.services.artifact_retention.safe_rmtree", forbidden)
    with jobs._lock:
        jobs._quarantine_locked()
    counts = probe.calls.copy()
    with pytest.raises(ApplicationError) as failure:
        jobs.release_artifacts(job.job_id)
    unavailable(failure.value)
    assert job.job_id not in jobs._artifact_leases
    assert job.input_pdf_path.read_bytes() == b"%PDF-1.4\n"
    assert jobs.get_snapshot(job.job_id) == snapshot
    assert sql_job(actual, job) == persisted
    assert probe.calls == counts
    with pytest.raises(ApplicationError) as retained:
        jobs.check_retained(job)
    assert (retained.value.status, retained.value.code) == (410, "artifacts_deleted")


def test_upload_write_does_not_hold_lock_and_rechecks_quarantine(stores, monkeypatch):
    jobs, _, probe, actual = stores
    baseline = make_job(jobs)
    paused, resume = Event(), Event()
    write_bytes = Path.write_bytes

    def pause_upload(path, payload):
        if path.name == "original.pdf" and path.parent != baseline.working_dir:
            paused.set()
            assert resume.wait(WAIT)
        return write_bytes(path, payload)

    monkeypatch.setattr(Path, "write_bytes", pause_upload)
    threads = []
    try:
        upload, _, _, uploaded = launch(lambda: jobs.create("new.pdf", b"new", owner_user_id=1))
        threads.append(upload)
        assert paused.wait(WAIT)

        def control():
            assert jobs.get(baseline.job_id) is baseline
            return jobs.update(baseline.job_id, filename="control.pdf")

        controller, _, done, controlled = launch(control)
        threads.append(controller)
        assert done.wait(WAIT), "get/update blocked behind upload filesystem write"
        assert controlled == [baseline]
        snapshot = jobs.get_snapshot(baseline.job_id)
        persisted = sql_job(actual, baseline)
        counts = probe.calls.copy()
        with jobs._lock:
            jobs._quarantine_locked()
    finally:
        resume.set()
        join_all(threads)
    assert len(uploaded) == 1
    unavailable(uploaded[0])
    assert list(jobs.jobs) == [baseline.job_id]
    assert actual.execute("SELECT COUNT(*) FROM jobs").fetchone() == (1,)
    assert jobs.get_snapshot(baseline.job_id) == snapshot
    assert sql_job(actual, baseline) == persisted
    assert probe.calls == counts


def test_already_quarantined_create_and_update_have_no_effects(stores, monkeypatch):
    jobs, _, probe, actual = stores
    job = make_job(jobs)
    snapshot, persisted = jobs.get_snapshot(job.job_id), sql_job(actual, job)
    with jobs._lock:
        jobs._quarantine_locked()

    def forbidden(*args, **kwargs):
        pytest.fail("already quarantined create touched filesystem")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    counts = probe.calls.copy()
    for action in (lambda: jobs.create("new.pdf", b"new", owner_user_id=1),
                   lambda: jobs.update(job.job_id, filename="changed.pdf")):
        with pytest.raises(ApplicationError) as failure:
            action()
        unavailable(failure.value)
    assert jobs.get_snapshot(job.job_id) == snapshot
    assert sql_job(actual, job) == persisted
    assert actual.execute("SELECT COUNT(*) FROM jobs").fetchone() == (1,)
    assert probe.calls == counts


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("phase,quarantined", [
    ("entry_health", True), ("begin_before", False), ("begin_after", True),
    ("body", False), ("commit_before", False), ("commit_after", False),
    ("rollback_after", True), ("post_health", True), ("secondary_control", True),
])
def test_control_exception_identity_and_transaction_boundaries(stores, kind, phase, quarantined):
    jobs, metadata, probe, actual = stores
    job = make_job(jobs, completed=True)
    probe.calls.clear()
    original = kind(SECRET)
    if phase == "entry_health":
        probe.health(1, original)
    elif phase == "post_health":
        probe.health(2, original)
    elif phase == "secondary_control":
        secondary = SystemExit("secondary") if kind is KeyboardInterrupt else KeyboardInterrupt("secondary")
        probe.faults["rollback_after"] = secondary
    elif phase != "body":
        probe.faults[phase] = original
    caught = None
    try:
        with metadata._transaction() as conn:
            conn.execute("SELECT 1")
            if phase in ("body", "secondary_control"):
                raise original
            if phase == "rollback_after":
                raise ValueError(SECRET)
    except (KeyboardInterrupt, SystemExit) as error:
        caught = error
    assert caught is original
    assert not probe.faults
    assert jobs._sqlite_quarantined is quarantined
    assert actual.in_transaction is (phase == "begin_after")
    expected_rollback = phase in {"body", "commit_before", "commit_after", "rollback_after", "secondary_control"}
    assert probe.calls["rollback"] == int(expected_rollback)
    assert probe.calls["commit"] == int(phase in {"commit_before", "commit_after", "post_health"})
    lock_free(jobs)
    if quarantined:
        counts = probe.calls.copy()
        assert not jobs.check_db()
        with pytest.raises(ApplicationError) as failure:
            jobs._get_conn()
        unavailable(failure.value)
        assert probe.calls == counts
    else:
        assert metadata.get_head(Principal(), job.job_id) is None
        with metadata._transaction() as conn:
            assert metadata._reserved_disk_bytes_locked(conn, "user:1", job.job_id) == 0
        assert jobs.check_db()
