"""Behavioral coverage for lazy metadata wiring and observational health checks."""
from collections import Counter
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from hashlib import sha256
from types import SimpleNamespace
import builtins
import os
from pathlib import Path
import sqlite3

import pytest

from app.application import ApplicationError
from app.metadata_models import SaveMetadataReceipt
from app.services import metadata_runtime as runtime
from app.services.metadata_files import MetadataFiles
from app.services.metadata_recovery import MetadataRecovery
from app.services.metadata_storage import MetadataStorage
from app.services.metadata_store import MetadataStore
from app.services.storage_service import JobStore


SECRET = "synthetic-private-runtime-detail"
_UNSET = object()


class ConnectionProbe:
    """Observe real SQLite operations and inject one health observation."""

    def __init__(self, actual):
        self.actual = actual
        self.calls = Counter()
        self.observation = _UNSET

    def __getattr__(self, name):
        return getattr(self.actual, name)

    @property
    def in_transaction(self):
        self.calls["health"] += 1
        value, self.observation = self.observation, _UNSET
        if isinstance(value, BaseException):
            raise value
        return self.actual.in_transaction if value is _UNSET else value

    def execute(self, *args, **kwargs):
        self.calls["execute"] += 1
        return self.actual.execute(*args, **kwargs)

    def commit(self):
        self.calls["commit"] += 1
        return self.actual.commit()

    def rollback(self):
        self.calls["rollback"] += 1
        return self.actual.rollback()

    def close(self):
        self.calls["close"] += 1
        return self.actual.close()


@pytest.fixture
def make_store(tmp_path):
    connections = []

    def create(name):
        jobs = JobStore(root_dir=tmp_path / name)
        connections.append(jobs._conn)
        return jobs

    try:
        yield create
    finally:
        # Isolation only; health checks must never perform this repair.
        for actual in reversed(connections):
            try:
                actual.rollback()
            finally:
                actual.close()


def attach_probe(jobs):
    actual = jobs._conn
    assert actual is not None
    probe = ConnectionProbe(actual)
    jobs._conn = probe
    return probe, actual


def unavailable(error):
    assert isinstance(error, ApplicationError)
    assert (error.status, error.code, error.message) == (
        503, "metadata_unavailable", "Metadata is unavailable.")
    assert SECRET not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def assert_observation_only(probe):
    assert {key: value for key, value in probe.calls.items() if key != "health"} == {}


def assert_lock_free(jobs):
    acquired = jobs._lock.acquire(blocking=False)
    if acquired:
        jobs._lock.release()
    assert acquired


def deny_connect(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("health gate attempted to reconnect")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    return calls


def exercise_health(jobs, provider, cached):
    if cached:
        return provider()
    return runtime.assert_metadata_health(jobs)


def assert_quarantine_persists(jobs, provider, probe):
    assert jobs._sqlite_quarantined
    # Restoring an observable connection is deliberately insufficient to recover.
    jobs._conn = probe
    probe.observation = _UNSET
    counts = probe.calls.copy()
    for action in (lambda: runtime.assert_metadata_health(jobs), provider):
        with pytest.raises(ApplicationError) as failure:
            action()
        unavailable(failure.value)
    assert not jobs.check_db()
    assert jobs._sqlite_quarantined
    assert probe.calls == counts
    assert_lock_free(jobs)


def test_provider_construction_is_lazy_and_has_no_filesystem_io(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("provider construction performed work")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        for name in ("mkdir", "open", "stat", "write_bytes", "write_text"):
            patch.setattr(Path, name, forbidden)
        for name in ("open", "mkdir", "listdir", "scandir"):
            patch.setattr(os, name, forbidden)
        patch.setattr(sqlite3, "connect", forbidden)
        provider = runtime.MetadataProvider(forbidden)
    assert calls == []
    assert provider._services is None
    assert provider._store is None


def test_shared_store_reuses_frozen_bundle_with_real_service_identities(make_store):
    jobs = make_store("shared")
    applications = []

    def application():
        result = SimpleNamespace(store=jobs)
        applications.append(result)
        return result

    provider = runtime.MetadataProvider(application)
    first = provider()
    second = provider()
    assert len(applications) == 2
    assert applications[0] is not applications[1]
    assert first is second
    assert isinstance(first.index, MetadataStore)
    assert isinstance(first.files, MetadataFiles)
    assert isinstance(first.storage, MetadataStorage)
    assert isinstance(first.recovery, MetadataRecovery)
    assert first.jobs is first.index.jobs is jobs
    assert first.storage.store is first.recovery.store is first.index
    assert first.storage.files is first.recovery.physical._files is first.files
    assert first.recovery.jobs is jobs
    with pytest.raises(FrozenInstanceError):
        first.files = object()


def test_changed_store_builds_a_new_bound_bundle(make_store):
    original, replacement = make_store("original"), make_store("replacement")
    current = SimpleNamespace(store=original)
    provider = runtime.MetadataProvider(lambda: current)
    first = provider()
    current.store = replacement
    second = provider()
    assert second is provider()
    assert second is not first
    assert second.jobs is replacement
    assert first.jobs is original
    for name in ("index", "files", "storage", "recovery"):
        assert getattr(second, name) is not getattr(first, name)
    assert second.storage.store is second.recovery.store is second.index
    assert second.storage.files is second.recovery.physical._files is second.files
    current.store = original
    assert provider() is first
    assert provider._store is original
    assert provider._services is first
    current.store = replacement
    assert provider() is second


@pytest.mark.parametrize("phase", ["constructor", "final_health"])
def test_failed_switch_preserves_previous_cache_without_publishing_partial_bundle(
        make_store, monkeypatch, phase):
    original, replacement = make_store("original"), make_store("replacement")
    current = SimpleNamespace(store=original)
    provider = runtime.MetadataProvider(lambda: current)
    first = provider()
    probe, _ = attach_probe(replacement)
    constructed = []
    real_recovery = runtime.MetadataRecovery

    def recovery(store, physical):
        result = real_recovery(store, physical)
        constructed.append(result)
        if phase == "constructor":
            raise RuntimeError(SECRET)
        probe.observation = RuntimeError(SECRET)
        return result

    monkeypatch.setattr(runtime, "MetadataRecovery", recovery)
    current.store = replacement
    with pytest.raises(ApplicationError) as failure:
        provider()
    unavailable(failure.value)
    assert len(constructed) == 1
    assert constructed[0].store.jobs is replacement
    assert provider._services is first
    assert provider._store is original
    assert provider._services_by_store == {id(original): first}
    if phase == "final_health":
        assert replacement._sqlite_quarantined
    current.store = original
    assert provider() is first
    assert not original._sqlite_quarantined
    assert_lock_free(original)
    assert_lock_free(replacement)
    if phase == "constructor":
        def successful_recovery(store, physical):
            result = real_recovery(store, physical)
            constructed.append(result)
            return result

        monkeypatch.setattr(runtime, "MetadataRecovery", successful_recovery)
        current.store = replacement
        retried = provider()
        assert len(constructed) == 2
        assert retried.recovery is constructed[1]
        assert retried.recovery is not constructed[0]
        assert retried is provider()
        current.store = original
        assert provider() is first


def test_switch_back_preserves_real_writer_claim_and_rejects_duplicate(
        make_store, monkeypatch):
    original, replacement = make_store("writer"), make_store("other")
    current = SimpleNamespace(store=original)
    provider = runtime.MetadataProvider(lambda: current)
    bundle = provider()
    job = original.create("source.pdf", b"%PDF-1.4\n", owner_user_id=1)
    job = original.update(job.job_id, status="completed")
    principal = SimpleNamespace(
        subject="user:1", auth_source="unknown", require=lambda scope: None)
    canonical = b"{}"
    digest = sha256(canonical).hexdigest()
    reservation = bundle.index.reserve(
        principal, job.job_id, key_hash="1" * 64, request_hash="2" * 64,
        expected_head=None, expected_sha256=None, revision_id="3" * 32,
        sha256=digest, size_bytes=len(canonical),
        created_at=datetime.now(timezone.utc),
    ).pending
    assert reservation is not None
    receipt = SaveMetadataReceipt(
        revision_id=reservation.revision_id, sha256=digest, segment_ids={})
    claims = bundle.index._active_writes
    prepared, published = [], []
    stop = KeyboardInterrupt("stop writer at the publication boundary")

    def durable_state():
        # A separate connection observes committed state, including while the
        # original writer owns its claim outside the shared SQL lock.
        reader = sqlite3.connect(str(original.db_path), timeout=1)
        try:
            return tuple(
                reader.execute("SELECT * FROM " + table).fetchall()
                for table in ("metadata_pending", "metadata_heads",
                              "metadata_revisions", "metadata_operations",
                              "metadata_audit")
            )
        finally:
            reader.close()

    baseline = durable_state()
    assert len(baseline[0]) == 1
    assert baseline[1:] == ([], [], [], [])

    def publish(data):
        published.append(data)
        assert claims == {reservation.pending_id}
        current.store = replacement
        assert provider().jobs is replacement
        current.store = original
        reused = provider()
        assert reused is bundle
        assert reused.index is bundle.index
        assert reused.index._active_writes is claims
        with pytest.raises(ApplicationError) as failure:
            reused.storage.write_reserved(
                principal, job, reservation, receipt, canonical,
                check_bindings_locked=lambda conn: None,
            )
        assert (failure.value.status, failure.value.code) == (
            409, "metadata_write_pending")
        assert claims == {reservation.pending_id}
        assert durable_state() == baseline
        assert prepared == [reservation.pending_id]
        assert published == [canonical]
        assert_lock_free(original)
        raise stop

    def prepare(job, *, pending_id, revision_id, sha256, size_bytes):
        prepared.append(pending_id)
        # Instrument the publication boundary; reservation and writer admission
        # use the real store and write_reserved implementation throughout.
        return SimpleNamespace(available_bytes=lambda: 2**100, publish=publish)

    monkeypatch.setattr(bundle.files, "prepare", prepare)
    with pytest.raises(KeyboardInterrupt) as failure:
        bundle.storage.write_reserved(
            principal, job, reservation, receipt, canonical,
            check_bindings_locked=lambda conn: None,
        )
    assert failure.value is stop
    assert prepared == [reservation.pending_id]
    assert published == [canonical]
    assert durable_state() == baseline
    # Only unwinding the original writer releases its in-memory claim.
    assert bundle.index._active_writes is claims
    assert claims == set()
    assert provider() is bundle
    assert_lock_free(original)
    assert_lock_free(replacement)


def test_request_accessor_uses_request_application_provider():
    sentinel = object()
    calls = []

    def provider():
        calls.append(True)
        return sentinel

    request = SimpleNamespace(app=SimpleNamespace(
        state=SimpleNamespace(metadata_provider=provider)))
    assert runtime.get_metadata_services(request) is sentinel
    assert calls == [True]


@pytest.mark.parametrize("cached", [False, True], ids=["direct", "cached"])
def test_successful_health_check_preserves_live_sql_state(make_store, monkeypatch, cached):
    jobs = make_store("healthy")
    provider = runtime.MetadataProvider(lambda: SimpleNamespace(store=jobs))
    bundle = provider()
    actual = jobs._conn
    actual.execute("CREATE TEMP TABLE health_marker (value TEXT)")
    actual.execute("INSERT INTO health_marker VALUES (?)", (SECRET,))
    actual.commit()
    changes = actual.total_changes
    probe, actual = attach_probe(jobs)
    connects = deny_connect(monkeypatch)
    result = exercise_health(jobs, provider, cached)
    assert result is (bundle if cached else None)
    assert probe.calls["health"] == 1
    assert_observation_only(probe)
    assert connects == []
    assert actual.total_changes == changes
    assert actual.in_transaction is False
    assert actual.execute("SELECT value FROM health_marker").fetchall() == [(SECRET,)]
    assert not jobs._sqlite_quarantined
    assert_lock_free(jobs)


@pytest.mark.parametrize("cached", [False, True], ids=["direct", "cached"])
@pytest.mark.parametrize("observation", [True, 0, None, RuntimeError(SECRET)],
                         ids=["transaction", "integer-zero", "none", "exception"])
def test_unprovable_health_rejects_cached_and_direct_access(
        make_store, monkeypatch, cached, observation):
    jobs = make_store("unhealthy")
    provider = runtime.MetadataProvider(lambda: SimpleNamespace(store=jobs))
    bundle = provider()
    probe, actual = attach_probe(jobs)
    probe.observation = observation
    connects = deny_connect(monkeypatch)
    with pytest.raises(ApplicationError) as failure:
        exercise_health(jobs, provider, cached)
    unavailable(failure.value)
    assert probe.calls["health"] == 1
    assert_observation_only(probe)
    assert actual.in_transaction is False
    assert provider._services is bundle
    assert_quarantine_persists(jobs, provider, probe)
    assert connects == []


@pytest.mark.parametrize("cached", [False, True], ids=["direct", "cached"])
def test_missing_connection_never_reconnects(make_store, monkeypatch, cached):
    jobs = make_store("missing")
    provider = runtime.MetadataProvider(lambda: SimpleNamespace(store=jobs))
    provider()
    probe, actual = attach_probe(jobs)
    jobs._conn = None
    connects = deny_connect(monkeypatch)
    with pytest.raises(ApplicationError) as failure:
        exercise_health(jobs, provider, cached)
    unavailable(failure.value)
    assert jobs._conn is None
    assert probe.calls == {}
    assert actual.in_transaction is False
    assert_quarantine_persists(jobs, provider, probe)
    assert connects == []


@pytest.mark.parametrize("cached", [False, True], ids=["direct", "cached"])
def test_foreign_transaction_is_neither_committed_nor_rolled_back(
        make_store, monkeypatch, cached):
    jobs = make_store("foreign")
    provider = runtime.MetadataProvider(lambda: SimpleNamespace(store=jobs))
    provider()
    actual = jobs._conn
    actual.execute("CREATE TABLE health_marker (value TEXT)")
    actual.execute("INSERT INTO health_marker VALUES ('baseline')")
    actual.commit()
    actual.execute("BEGIN IMMEDIATE")
    actual.execute("UPDATE health_marker SET value=?", (SECRET,))
    probe, actual = attach_probe(jobs)
    connects = deny_connect(monkeypatch)
    with pytest.raises(ApplicationError) as failure:
        exercise_health(jobs, provider, cached)
    unavailable(failure.value)
    assert_observation_only(probe)
    assert actual.in_transaction is True
    assert actual.execute("SELECT value FROM health_marker").fetchall() == [(SECRET,)]
    assert_quarantine_persists(jobs, provider, probe)
    assert actual.in_transaction is True
    assert connects == []
    # Use the original function through a separate scoped patch solely to read
    # durable state; product health checks above were forbidden to reconnect.
    monkeypatch.undo()
    reader = sqlite3.connect(str(jobs.db_path), timeout=1)
    try:
        assert reader.execute("SELECT value FROM health_marker").fetchall() == [("baseline",)]
    finally:
        reader.close()


@pytest.mark.parametrize("cached", [False, True], ids=["direct", "cached"])
@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
def test_health_control_exception_preserves_identity_and_quarantines(
        make_store, monkeypatch, cached, kind):
    jobs = make_store("control")
    provider = runtime.MetadataProvider(lambda: SimpleNamespace(store=jobs))
    provider()
    probe, actual = attach_probe(jobs)
    original = kind(SECRET)
    probe.observation = original
    connects = deny_connect(monkeypatch)
    with pytest.raises(kind) as failure:
        exercise_health(jobs, provider, cached)
    assert failure.value is original
    assert jobs._sqlite_quarantined
    assert probe.calls["health"] == 1
    assert_observation_only(probe)
    assert actual.in_transaction is False
    assert_quarantine_persists(jobs, provider, probe)
    assert connects == []


@pytest.mark.parametrize("kind", [RuntimeError, ApplicationError])
def test_provider_callback_failure_is_sanitized_and_cached_bundle_survives(make_store, kind):
    jobs = make_store("callback")
    fault = None

    def application():
        if fault is not None:
            raise fault
        return SimpleNamespace(store=jobs)

    provider = runtime.MetadataProvider(application)
    bundle = provider()
    probe, _ = attach_probe(jobs)
    fault = (ApplicationError(418, SECRET, SECRET)
             if kind is ApplicationError else RuntimeError(SECRET))
    with pytest.raises(ApplicationError) as failure:
        provider()
    unavailable(failure.value)
    assert probe.calls == {}
    assert provider._services is bundle
    assert not jobs._sqlite_quarantined
    fault = None
    assert provider() is bundle


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
def test_provider_callback_control_exception_preserves_identity(make_store, kind):
    jobs = make_store("callback-control")
    fault = None

    def application():
        if fault is not None:
            raise fault
        return SimpleNamespace(store=jobs)

    provider = runtime.MetadataProvider(application)
    bundle = provider()
    probe, _ = attach_probe(jobs)
    fault = kind(SECRET)
    with pytest.raises(kind) as failure:
        provider()
    assert failure.value is fault
    assert probe.calls == {}
    assert not jobs._sqlite_quarantined
    assert provider._services is bundle
    fault = None
    assert provider() is bundle
    assert_lock_free(jobs)
