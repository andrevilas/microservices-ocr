"""Real SQLite fail-stop boundaries for retention and queue startup."""
from collections import Counter
from types import SimpleNamespace
import sqlite3

import pytest

from app.application import ApplicationError
from app.services import job_queue, metadata_runtime, retention_worker
from app.services.storage_service import JobStore


SECRET = "synthetic-private-worker-detail"
_UNSET = object()


@pytest.fixture
def make_store(tmp_path):
    connections = []

    def create(name="jobs"):
        jobs = JobStore(tmp_path / name)
        connections.append(jobs._conn)
        return jobs

    try:
        yield create
    finally:
        # Test teardown alone owns these raw connections. Product code must not
        # repair a foreign transaction or reconnect a quarantined store.
        for raw in reversed(connections):
            try:
                raw.rollback()
            finally:
                raw.close()


class ConnectionProbe:
    def __init__(self, raw):
        self.raw = raw
        self.calls = Counter()
        self.observation = _UNSET

    def __getattr__(self, name):
        return getattr(self.raw, name)

    @property
    def in_transaction(self):
        self.calls["health"] += 1
        value = self.observation
        if isinstance(value, BaseException):
            raise value
        return self.raw.in_transaction if value is _UNSET else value

    def execute(self, *args, **kwargs):
        self.calls["execute"] += 1
        return self.raw.execute(*args, **kwargs)

    def commit(self):
        self.calls["commit"] += 1
        return self.raw.commit()

    def rollback(self):
        self.calls["rollback"] += 1
        return self.raw.rollback()

    def close(self):
        self.calls["close"] += 1
        return self.raw.close()

    def __enter__(self):
        self.calls["enter"] += 1
        self.raw.__enter__()
        return self

    def __exit__(self, *args):
        self.calls["exit"] += 1
        return self.raw.__exit__(*args)


def watch_connection(monkeypatch, jobs):
    raw = jobs._conn
    probe = ConnectionProbe(raw)
    jobs._conn = probe
    connects = []

    def forbidden(*args, **kwargs):
        connects.append(True)
        raise AssertionError("unexpected reconnect")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    return probe, raw, connects


def assert_observation_only(probe, connects):
    # Explicit hit counters also catch assertions accidentally swallowed by a
    # category's ordinary-failure handling.
    assert {k: v for k, v in probe.calls.items() if k != "health"} == {}
    assert connects == []


def unavailable(error):
    assert isinstance(error, ApplicationError)
    assert (error.status, error.code, error.message) == (
        503, "metadata_unavailable", "Metadata is unavailable.")
    assert error.__cause__ is None
    assert error.__context__ is None
    assert SECRET not in str(error)


def collaborators(monkeypatch, jobs):
    events, paths = [], {}

    def action(name):
        path = jobs.root_dir / (name + "-marker")
        path.write_text("keep until cleanup")
        paths[name] = path

        def cleanup():
            events.append(name)
            path.unlink()

        return cleanup

    monkeypatch.setattr(jobs, "cleanup_expired", action("jobs"))
    uploads = SimpleNamespace(jobs=jobs, _expire=action("uploads"))
    exports = SimpleNamespace(jobs=jobs, _expire=action("exports"))
    return uploads, exports, events, paths


def thread_spy(monkeypatch, module):
    instances = []

    class ControlledThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.starts = 0
            self.joins = 0
            instances.append(self)

        def start(self):
            self.starts += 1

        def join(self, *args, **kwargs):
            self.joins += 1

        def is_alive(self):
            return False

    monkeypatch.setattr(module, "Thread", ControlledThread)
    return instances


@pytest.mark.parametrize("value", [False, True, 0, None, RuntimeError(SECRET)])
def test_locked_health_is_strict_and_observational(make_store, monkeypatch, value):
    jobs = make_store()
    probe, raw, connects = watch_connection(monkeypatch, jobs)
    probe.observation = value
    with jobs._lock:
        if value is False:
            metadata_runtime._assert_metadata_health_locked(jobs)
        else:
            with pytest.raises(ApplicationError) as failure:
                metadata_runtime._assert_metadata_health_locked(jobs)
            unavailable(failure.value)
    assert jobs._sqlite_quarantined is (value is not False)
    assert raw.in_transaction is False
    assert probe.calls["health"] == 1
    assert_observation_only(probe, connects)
    assert jobs._lock.acquire(blocking=False)
    jobs._lock.release()


@pytest.mark.parametrize("with_metadata", [False, True])
@pytest.mark.parametrize("filesystem_failure", [False, True])
def test_clean_collection_order_and_healthy_failure_isolation(
        make_store, monkeypatch, caplog, with_metadata, filesystem_failure):
    jobs = make_store()
    uploads, exports, events, paths = collaborators(monkeypatch, jobs)
    metadata = None
    limits = []
    if with_metadata:
        provider = metadata_runtime.MetadataProvider(lambda: SimpleNamespace(store=jobs))
        metadata = provider()

        def collect_cleanup(limit=128):
            events.append("metadata")
            limits.append(limit)
            return {"removed": 0}

        monkeypatch.setattr(metadata.recovery, "collect_cleanup", collect_cleanup)
    if filesystem_failure:
        def blocked():
            events.append("jobs")
            raise OSError(SECRET)

        monkeypatch.setattr(jobs, "cleanup_expired", blocked)
    worker = (retention_worker.RetentionWorker(uploads, exports, metadata)
              if with_metadata else retention_worker.RetentionWorker(uploads, exports))
    worker.collect()
    assert events == (["metadata"] if with_metadata else []) + ["jobs", "uploads", "exports"]
    assert limits == ([128] if with_metadata else [])
    assert paths["jobs"].exists() is filesystem_failure
    assert not paths["uploads"].exists()
    assert not paths["exports"].exists()
    assert jobs._conn.in_transaction is False
    assert not jobs._sqlite_quarantined
    assert SECRET not in caplog.text
    assert "Traceback" not in caplog.text


@pytest.mark.parametrize("mismatch", ["type", "metadata_store", "export_store"])
def test_metadata_validation_precedes_work(make_store, monkeypatch, mismatch):
    jobs, other = make_store(), make_store("other")
    uploads, exports, events, paths = collaborators(monkeypatch, jobs)
    bound = other if mismatch == "metadata_store" else jobs
    metadata = metadata_runtime.MetadataProvider(lambda: SimpleNamespace(store=bound))()
    if mismatch == "type":
        metadata = SimpleNamespace(jobs=jobs)
    if mismatch == "export_store":
        exports.jobs = other
    probe, raw, connects = watch_connection(monkeypatch, jobs)
    with pytest.raises(TypeError if mismatch == "type" else ValueError):
        retention_worker.RetentionWorker(uploads, exports, metadata)
    assert events == []
    assert all(path.exists() for path in paths.values())
    assert probe.calls == {}
    assert_observation_only(probe, connects)


@pytest.mark.parametrize("entry", ["collect", "reconcile_startup", "start"])
@pytest.mark.parametrize("state", ["dirty", "quarantined", "missing"])
def test_unsafe_retention_preserves_categories_and_legacy_staging(
        make_store, monkeypatch, entry, state):
    jobs = make_store()
    uploads, exports, events, paths = collaborators(monkeypatch, jobs)
    legacy = jobs.root_dir / "legacy-batch-ab12_cd3"
    legacy.mkdir()
    sentinel = legacy / "private.pdf"
    sentinel.write_bytes(b"preserve")
    worker = retention_worker.RetentionWorker(uploads, exports)
    threads = thread_spy(monkeypatch, retention_worker)
    probe, raw, connects = watch_connection(monkeypatch, jobs)
    if state == "dirty":
        raw.execute("BEGIN IMMEDIATE")
    elif state == "quarantined":
        with jobs._lock:
            jobs._quarantine_locked()
    else:
        jobs._conn = None
    with pytest.raises(ApplicationError) as failure:
        getattr(worker, entry)()
    unavailable(failure.value)
    assert events == []
    assert threads == []
    assert worker._thread is None
    assert sentinel.read_bytes() == b"preserve"
    assert all(path.exists() for path in paths.values())
    assert jobs._sqlite_quarantined
    assert raw.in_transaction is (state == "dirty")
    assert_observation_only(probe, connects)


def test_healthy_startup_removes_only_stale_legacy_staging(make_store, monkeypatch):
    jobs = make_store()
    uploads, exports, events, paths = collaborators(monkeypatch, jobs)
    stale = jobs.root_dir / "legacy-batch-ab12_cd3"
    unknown = jobs.root_dir / "user-documents"
    stale.mkdir()
    unknown.mkdir()
    (stale / "source.pdf").write_bytes(b"stale")
    (unknown / "source.pdf").write_bytes(b"keep")
    retention_worker.RetentionWorker(uploads, exports).reconcile_startup()
    assert not stale.exists()
    assert (unknown / "source.pdf").read_bytes() == b"keep"
    assert events == ["jobs", "uploads", "exports"]


@pytest.mark.parametrize("category", ["metadata", "jobs"])
@pytest.mark.parametrize("raises", [False, True])
def test_category_poison_aborts_before_later_deletion(
        make_store, monkeypatch, category, raises):
    jobs = make_store()
    uploads, exports, events, paths = collaborators(monkeypatch, jobs)
    metadata = metadata_runtime.MetadataProvider(lambda: SimpleNamespace(store=jobs))()
    raw = jobs._conn

    def poison(*args, **kwargs):
        events.append(category)
        raw.execute("BEGIN IMMEDIATE")
        if raises:
            raise OSError(SECRET)

    def clean_metadata(limit=128):
        events.append("metadata")
        return {}

    monkeypatch.setattr(metadata.recovery, "collect_cleanup",
                        poison if category == "metadata" else clean_metadata)
    if category == "jobs":
        monkeypatch.setattr(jobs, "cleanup_expired", poison)
    worker = retention_worker.RetentionWorker(uploads, exports, metadata)
    probe, raw, connects = watch_connection(monkeypatch, jobs)
    with pytest.raises(ApplicationError) as failure:
        worker.collect()
    unavailable(failure.value)
    assert events == (["metadata"] if category == "metadata" else ["metadata", "jobs"])
    assert all(path.exists() for path in paths.values())
    assert raw.in_transaction is True
    assert jobs._sqlite_quarantined
    assert_observation_only(probe, connects)


@pytest.mark.parametrize("entry", ["_uploads", "_exports"])
def test_direct_expiry_checks_health_inside_its_lock_before_work(
        make_store, monkeypatch, entry):
    jobs = make_store()
    uploads, exports, events, paths = collaborators(monkeypatch, jobs)
    worker = retention_worker.RetentionWorker(uploads, exports)
    probe, raw, connects = watch_connection(monkeypatch, jobs)
    actual_lock = jobs._lock

    class PoisonOnAcquire:
        hits = 0

        def __enter__(self):
            actual_lock.acquire()
            self.hits += 1
            # Simulate SQL becoming dirty between an earlier observation and
            # acquisition of the lock protecting physical expiry/context entry.
            raw.execute("BEGIN IMMEDIATE")
            return self

        def __exit__(self, *args):
            actual_lock.release()

    lock = PoisonOnAcquire()
    jobs._lock = lock
    with pytest.raises(ApplicationError) as failure:
        getattr(worker, entry)()
    unavailable(failure.value)
    assert lock.hits == 1
    assert events == []
    assert all(path.exists() for path in paths.values())
    assert raw.in_transaction is True
    assert jobs._sqlite_quarantined
    assert_observation_only(probe, connects)
    assert actual_lock.acquire(blocking=False)
    actual_lock.release()


@pytest.mark.parametrize("entry", ["_uploads", "_exports"])
@pytest.mark.parametrize("raises", [False, True])
def test_direct_expiry_checks_health_after_work(make_store, monkeypatch, entry, raises):
    jobs = make_store()
    calls = []

    def poison():
        calls.append(entry)
        jobs._quarantine_locked()
        if raises:
            raise OSError(SECRET)

    uploads = SimpleNamespace(jobs=jobs, _expire=poison)
    exports = SimpleNamespace(jobs=jobs, _expire=poison)
    worker = retention_worker.RetentionWorker(uploads, exports)
    with pytest.raises(ApplicationError) as failure:
        getattr(worker, entry)()
    unavailable(failure.value)
    assert calls == [entry]
    assert jobs._sqlite_quarantined


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("source", ["health", "category"])
def test_retention_control_exception_identity(make_store, monkeypatch, kind, source):
    jobs = make_store()
    uploads, exports, events, paths = collaborators(monkeypatch, jobs)
    worker = retention_worker.RetentionWorker(uploads, exports)
    original = kind(SECRET)
    probe, raw, connects = watch_connection(monkeypatch, jobs)
    if source == "health":
        probe.observation = original
    else:
        def interrupt():
            events.append("jobs")
            raise original

        monkeypatch.setattr(jobs, "cleanup_expired", interrupt)
    with pytest.raises(kind) as failure:
        worker.collect()
    assert failure.value is original
    assert events == ([] if source == "health" else ["jobs"])
    assert all(path.exists() for path in paths.values())
    assert jobs._sqlite_quarantined is (source == "health")
    assert_observation_only(probe, connects)


def test_background_retention_stops_with_fixed_diagnostic(make_store, monkeypatch, caplog):
    jobs = make_store()
    uploads, exports, events, paths = collaborators(monkeypatch, jobs)
    worker = retention_worker.RetentionWorker(uploads, exports)
    waits = iter([False, True])
    worker._stop = SimpleNamespace(wait=lambda interval: next(waits, True))
    probe, raw, connects = watch_connection(monkeypatch, jobs)
    probe.observation = RuntimeError(SECRET)
    worker._run()
    assert events == []
    assert all(path.exists() for path in paths.values())
    assert [record.getMessage() for record in caplog.records] == [
        "Retention worker stopped: metadata unavailable"]
    assert all(record.exc_info is None for record in caplog.records)
    assert SECRET not in caplog.text
    assert_observation_only(probe, connects)


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("entry", ["jobs", "exports", "queue", "legacy"])
def test_dirty_cleanup_control_quarantines_without_finalization(
        make_store, monkeypatch, kind, entry):
    jobs = make_store()
    record = jobs.create("source.pdf", b"%PDF-1.4\n", owner_user_id=1)
    jobs._conn.execute("CREATE TEMP TABLE control_probe (value TEXT)")
    uploads, exports, events, paths = collaborators(monkeypatch, jobs)
    legacy = jobs.root_dir / "legacy-batch-ab12_cd3"
    legacy.mkdir()
    sentinel = legacy / "source.pdf"
    sentinel.write_bytes(b"keep")
    worker = retention_worker.RetentionWorker(uploads, exports)
    queue = processor(monkeypatch, jobs) if entry == "queue" else None
    threads = thread_spy(monkeypatch, job_queue if queue else retention_worker)
    probe, raw, connects = watch_connection(monkeypatch, jobs)
    original = kind(SECRET)

    def interrupt(*args):
        events.append(entry)
        probe.execute("BEGIN IMMEDIATE")
        probe.execute("INSERT INTO control_probe VALUES (?)", (SECRET,))
        raise original

    if entry in ("jobs", "queue"):
        monkeypatch.setattr(jobs, "cleanup_expired", interrupt)
    elif entry == "exports":
        exports._expire = interrupt
    else:
        monkeypatch.setattr(retention_worker, "safe_rmtree", interrupt)
    if queue:
        for name in ("snapshot", "get_processing_jobs", "get_queued_jobs", "update"):
            monkeypatch.setattr(jobs, name,
                                lambda *args, name=name, **kwargs: events.append(name) or [])
    invoke = (queue.start if queue else worker._exports if entry == "exports"
              else worker.reconcile_startup if entry == "legacy" else worker.collect)
    with pytest.raises(kind) as failure:
        invoke()
    assert failure.value is original
    assert jobs._sqlite_quarantined
    assert raw.in_transaction is True
    assert raw.execute("SELECT value FROM control_probe").fetchall() == [(SECRET,)]
    assert threads == []
    assert worker._thread is None
    if queue:
        assert not queue._started
        assert queue._threads == []
        assert queue._queue.empty()
        assert queue._queued_ids == set()
    for operation in (jobs._get_conn, lambda: jobs._persist(record),
                      queue.start if queue else worker.collect):
        with pytest.raises(ApplicationError) as rejected:
            operation()
        unavailable(rejected.value)
    assert events == [entry]
    assert all(path.exists() for path in paths.values())
    assert sentinel.read_bytes() == b"keep"
    assert probe.calls["health"] > 0
    assert {k: v for k, v in probe.calls.items() if k != "health"} == {"execute": 2}
    assert connects == []


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("entry", ["exports", "legacy"])
def test_locked_cleanup_healthy_control_preserves_availability(
        make_store, monkeypatch, kind, entry):
    jobs = make_store()
    uploads, exports, events, paths = collaborators(monkeypatch, jobs)
    worker = retention_worker.RetentionWorker(uploads, exports)
    original = kind(SECRET)
    if entry == "legacy":
        (jobs.root_dir / "legacy-batch-ab12_cd3").mkdir()
    probe, raw, connects = watch_connection(monkeypatch, jobs)

    def interrupt(*args):
        events.append(entry)
        raise original

    if entry == "exports":
        exports._expire = interrupt
    else:
        monkeypatch.setattr(retention_worker, "safe_rmtree", interrupt)
    with pytest.raises(kind) as failure:
        (worker._exports if entry == "exports" else worker.reconcile_startup)()
    assert failure.value is original
    assert events == [entry]
    assert all(path.exists() for path in paths.values())
    assert not jobs._sqlite_quarantined
    assert raw.in_transaction is False
    assert probe.calls["health"] >= 2
    assert_observation_only(probe, connects)


@pytest.mark.parametrize("secondary_kind", [RuntimeError, SystemExit])
@pytest.mark.parametrize("entry", ["jobs", "exports"])
def test_secondary_health_failure_preserves_cleanup_control(
        make_store, monkeypatch, secondary_kind, entry):
    jobs = make_store()
    uploads, exports, events, paths = collaborators(monkeypatch, jobs)
    worker = retention_worker.RetentionWorker(uploads, exports)
    probe, raw, connects = watch_connection(monkeypatch, jobs)
    original = KeyboardInterrupt(SECRET)

    def interrupt():
        events.append(entry)
        probe.observation = secondary_kind(SECRET)
        raise original

    if entry == "jobs":
        monkeypatch.setattr(jobs, "cleanup_expired", interrupt)
    else:
        exports._expire = interrupt
    with pytest.raises(KeyboardInterrupt) as failure:
        (worker.collect if entry == "jobs" else worker._exports)()
    assert failure.value is original
    assert events == [entry]
    assert jobs._sqlite_quarantined
    assert raw.in_transaction is False
    assert all(path.exists() for path in paths.values())
    assert probe.calls["health"] >= 2
    assert_observation_only(probe, connects)


def processor(monkeypatch, jobs):
    monkeypatch.setattr(job_queue, "OcrOrchestrator",
                        lambda job_store: SimpleNamespace())
    return job_queue.JobQueueProcessor(jobs, worker_count=1)


@pytest.mark.parametrize("state", ["dirty", "quarantined", "cleanup_dirty", "cleanup_dirty_error"])
def test_queue_unsafe_start_does_not_recover_enqueue_or_construct_threads(
        make_store, monkeypatch, state):
    jobs = make_store()
    job = jobs.create("source.pdf", b"%PDF-1.4\n", owner_user_id=1)
    jobs.update(job.job_id, status="processing")
    queue = processor(monkeypatch, jobs)
    threads = thread_spy(monkeypatch, job_queue)
    events = []
    raw = jobs._conn

    def cleanup():
        events.append("cleanup")
        raw.execute("BEGIN IMMEDIATE")
        if state == "cleanup_dirty_error":
            raise OSError(SECRET)

    monkeypatch.setattr(jobs, "cleanup_expired", cleanup)
    for name in ("snapshot", "get_processing_jobs", "get_queued_jobs"):
        def observed(name=name):
            events.append(name)
            return []

        monkeypatch.setattr(jobs, name, observed)
    original_update = jobs.update

    def update(*args, **kwargs):
        events.append("update")
        return original_update(*args, **kwargs)

    monkeypatch.setattr(jobs, "update", update)
    probe, raw, connects = watch_connection(monkeypatch, jobs)
    if state == "dirty":
        raw.execute("BEGIN IMMEDIATE")
    elif state == "quarantined":
        with jobs._lock:
            jobs._quarantine_locked()
    with pytest.raises(ApplicationError) as failure:
        queue.start()
    unavailable(failure.value)
    assert events == (["cleanup"] if state.startswith("cleanup_") else [])
    assert threads == []
    assert queue._threads == []
    assert not queue._started
    assert queue._queue.empty()
    assert queue._queued_ids == set()
    assert jobs.jobs[job.job_id].status == "processing"
    assert raw.in_transaction is (state != "quarantined")
    assert_observation_only(probe, connects)


def test_queue_healthy_cleanup_failure_recovers_and_stops_controlled_threads(
        make_store, monkeypatch, caplog):
    jobs = make_store()
    job = jobs.create("source.pdf", b"%PDF-1.4\n", owner_user_id=1)
    jobs.update(job.job_id, status="processing")
    queue = processor(monkeypatch, jobs)
    threads = thread_spy(monkeypatch, job_queue)
    calls = []

    def blocked():
        calls.append("cleanup")
        raise OSError(SECRET)

    monkeypatch.setattr(jobs, "cleanup_expired", blocked)
    queue.start()
    try:
        assert calls == ["cleanup"]
        assert jobs.get_snapshot(job.job_id).status == "queued"
        assert queue._queued_ids == {job.job_id}
        assert queue._queue.qsize() == 1
        assert queue._started
        assert len(threads) == 1
        assert threads[0].starts == 1
        queue.start()
        assert len(threads) == 1
        assert calls == ["cleanup"]
        assert SECRET not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
    finally:
        queue.stop()
    assert threads[0].joins == 1
    assert queue._threads == []
    assert not queue._started


def test_already_started_queue_still_checks_health(make_store, monkeypatch):
    jobs = make_store()
    monkeypatch.setattr(jobs, "cleanup_expired", lambda: [])
    queue = processor(monkeypatch, jobs)
    threads = thread_spy(monkeypatch, job_queue)
    queue.start()
    try:
        probe, raw, connects = watch_connection(monkeypatch, jobs)
        raw.execute("BEGIN IMMEDIATE")
        with pytest.raises(ApplicationError) as failure:
            queue.start()
        unavailable(failure.value)
        assert len(threads) == 1
        assert threads[0].starts == 1
        assert raw.in_transaction is True
        assert_observation_only(probe, connects)
    finally:
        queue.stop()


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("source", ["health", "cleanup"])
def test_queue_start_preserves_control_identity(make_store, monkeypatch, kind, source):
    jobs = make_store()
    queue = processor(monkeypatch, jobs)
    threads = thread_spy(monkeypatch, job_queue)
    original = kind(SECRET)
    calls = []

    def interrupt():
        calls.append("cleanup")
        raise original

    monkeypatch.setattr(jobs, "cleanup_expired", interrupt)
    probe, raw, connects = watch_connection(monkeypatch, jobs)
    if source == "health":
        probe.observation = original
    with pytest.raises(kind) as failure:
        queue.start()
    assert failure.value is original
    assert calls == ([] if source == "health" else ["cleanup"])
    assert threads == []
    assert not queue._started
    assert_observation_only(probe, connects)
