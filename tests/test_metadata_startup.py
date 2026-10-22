"""Runtime wiring with real SQLite/recovery and thread-free backend observers."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette

import app.main as main
import app.services.metadata_runtime as metadata_runtime
import app.services.storage_service as storage_service
from app.application import ApplicationError, JobApplication
from app.config import settings
from app.services.metadata_recovery import MetadataRecovery
from app.services.runtime_lease import RuntimeLease
from app.services.storage_service import JobStore


def lifecycle(app, inside=lambda: None):
    async def scenario():
        async with app.router.lifespan_context(app):
            inside()
    asyncio.run(scenario())


def startup_failure(app, kind=ApplicationError):
    async def scenario():
        with pytest.raises(kind) as caught:
            async with app.router.lifespan_context(app):
                pytest.fail("Startup must not yield")
        return caught.value
    return asyncio.run(scenario())


def unavailable(callback):
    with pytest.raises(ApplicationError) as caught:
        callback()
    assert caught.value.code == "metadata_unavailable"


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    jobs = JobStore(root_dir=(tmp_path / "jobs").resolve())
    r = SimpleNamespace(jobs=jobs, current=jobs, stores=[jobs], apps=[], events=[],
                        instances={}, factory_hooks={}, start_hooks={}, stop_errors={},
                        recovery_error=None, lookups=0, getters={})
    monkeypatch.setattr(settings, "ocr_tmp_dir", jobs.root_dir)

    def application():
        r.lookups += 1
        return JobApplication(r.current)
    r.application = application

    class Backend:
        def __init__(self, name):
            self.name, self.jobs, self.job_store = name, jobs, jobs
            if name in ("batches", "controls"):
                self.uploads = r.instances["uploads"]

        def start(self):
            r.events.append("start:" + self.name)
            hook = r.start_hooks.get(self.name)
            if hook is not None:
                hook(self)

        def stop(self):
            r.events.append("stop:" + self.name)
            error = r.stop_errors.get(self.name)
            if error is not None:
                raise error

    def factory(name):
        r.events.append("factory:" + name)
        candidate = Backend(name)
        r.instances[name] = candidate
        hook = r.factory_hooks.get(name)
        if hook is not None:
            hook(candidate)
        return candidate
    r.factory = factory

    names = {"uploads": "get_upload_service", "batches": "get_batch_service",
             "controls": "get_control_service", "exports": "get_export_service",
             "queue": "get_job_queue_processor"}
    for name, attribute in names.items():
        def global_factory(name=name):
            r.events.append("global:" + name)
            return factory(name)
        monkeypatch.setattr(main, attribute, global_factory)
        r.getters[name] = global_factory

    class Retention:
        def __init__(self, uploads, exports, metadata=None):
            r.events.append("retention.construct")
            assert uploads.jobs is jobs and exports.jobs is jobs
            self.metadata = metadata
            r.retention = self

        def start(self):
            r.events.extend(["retention.cleanup", "retention.start"])

        def stop(self):
            r.events.append("retention.stop")
    monkeypatch.setattr(main, "RetentionWorker", Retention)

    def admin(**kwargs):
        r.events.append("admin")
    monkeypatch.setattr(main, "get_user_store", lambda: SimpleNamespace(ensure_admin=admin))

    def build(callback, *args, **kwargs):
        r.mcp = SimpleNamespace(application=callback, catalog=kwargs["catalog_provider"])

        @asynccontextmanager
        async def run():
            r.events.append("mcp.enter")
            try:
                yield
            finally:
                r.events.append("mcp.exit")
        return SimpleNamespace(session_manager=SimpleNamespace(run=run)), Starlette()
    monkeypatch.setattr(main, "build_mcp_server", build)

    real_catalog = main.CatalogProvider

    def catalog(callback):
        r.catalog_application = callback
        return real_catalog(callback)
    monkeypatch.setattr(main, "CatalogProvider", catalog)

    real_index = metadata_runtime.MetadataStore

    def index(*args, **kwargs):
        r.events.append("metadata.construct")
        return real_index(*args, **kwargs)
    monkeypatch.setattr(metadata_runtime, "MetadataStore", index)

    real_recover = MetadataRecovery.recover_startup

    def recover(self, lease):
        r.events.append("recover")
        r.recovery, r.recovery_lease = self, lease
        assert lease.path == jobs.root_dir / ".ocr-runtime.lock"
        assert lease._file is not None and not lease._file.closed
        contender = RuntimeLease(jobs.root_dir)
        try:
            with pytest.raises(RuntimeError, match="already has an active server"):
                contender.acquire()
        finally:
            contender.release()
        if r.recovery_error is not None:
            raise r.recovery_error
        return real_recover(self, lease)
    monkeypatch.setattr(MetadataRecovery, "recover_startup", recover)

    def make(*, custom=True):
        app = main.create_app(application) if custom else main.create_app()
        r.apps.append(app)
        r.provider = app.state.metadata_provider

        def metadata():
            r.events.append("metadata.provider")
            return r.provider()
        app.state.metadata_provider = metadata
        if custom:
            app.dependency_overrides.update({getter: (lambda name=name: factory(name))
                                             for name, getter in r.getters.items()})
        return app
    r.make = make

    def foreign():
        store = JobStore(root_dir=(tmp_path / "foreign").resolve())
        r.stores.append(store)
        return store
    r.foreign = foreign

    try:
        yield r
    finally:
        # All observers are thread-free, including deliberately failed stops.
        for app in r.apps:
            lease = getattr(app.state, "runtime_lease", None)
            if lease is not None:
                lease.release()
        for store in r.stores:
            if store._conn is not None:
                store._conn.close()


def test_construction_is_lazy(runtime, monkeypatch, tmp_path):
    r = runtime
    unused = tmp_path / "uncreated"
    monkeypatch.setattr(settings, "ocr_tmp_dir", unused)
    initialized = []
    real_init = JobStore._init_db

    def init(store):
        initialized.append(store)
        return real_init(store)
    monkeypatch.setattr(JobStore, "_init_db", init)
    r.make()
    assert r.events == []
    assert r.lookups == 0
    assert initialized == []
    assert r.provider._services is None
    assert not unused.exists()


def test_healthy_recovery_precedes_all_backend_effects(runtime):
    r = runtime
    app = r.make()

    def inside():
        assert r.events == [
            "metadata.provider", "metadata.construct", "recover", "admin",
            "factory:uploads", "factory:batches", "factory:controls",
            "factory:exports", "factory:queue", "retention.construct",
            "retention.cleanup", "retention.start", "start:queue", "start:exports", "mcp.enter",
        ]
        bundle = app.state.metadata_provider()
        assert bundle is r.provider._services
        assert bundle.recovery is r.recovery
        assert r.retention.metadata is bundle
        assert bundle.jobs is r.jobs
        assert app.state.runtime_lease is r.recovery_lease
        assert app.dependency_overrides[main.get_job_application]().store is r.jobs
        assert r.mcp.application().store is r.jobs
        assert r.catalog_application().store is r.jobs
        r.mcp.catalog()
        assert r.instances["batches"].uploads is r.instances["uploads"]
        assert r.instances["controls"].uploads is r.instances["uploads"]
    lifecycle(app, inside)
    assert r.events.count("recover") == 1
    assert r.events[-4:] == ["mcp.exit", "retention.stop", "stop:exports", "stop:queue"]
    assert app.state.runtime_lease._file is None


def test_default_provider_uses_global_factories_after_recovery(runtime, monkeypatch):
    r = runtime
    monkeypatch.setattr(storage_service, "_job_store", r.jobs)
    app = r.make(custom=False)
    assert main.get_job_application not in app.dependency_overrides
    assert all(getter not in app.dependency_overrides for getter in r.getters.values())
    lifecycle(app)
    globals_called = [event for event in r.events if event.startswith("global:")]
    assert globals_called == ["global:uploads", "global:batches", "global:controls", "global:exports", "global:queue"]
    assert r.events.index("recover") < r.events.index("admin") < r.events.index("global:uploads")
    assert r.events.count("recover") == 1
    assert app.state.runtime_lease._file is None


@pytest.mark.parametrize("kind", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_recovery_failure_preserves_identity_and_has_no_backend_effects(runtime, kind):
    r = runtime
    marker = kind("recovery interrupted")
    r.recovery_error = marker
    app = r.make()
    assert startup_failure(app, kind) is marker
    assert r.events == ["metadata.provider", "metadata.construct", "recover"]
    assert app.state.runtime_lease._file is None


@pytest.mark.parametrize("missing", ["uploads", "batches", "controls", "exports", "queue"])
def test_custom_runtime_requires_every_override_before_metadata_construction(runtime, missing):
    r = runtime
    app = r.make()
    del app.dependency_overrides[r.getters[missing]]
    error = startup_failure(app)
    assert error.code == "metadata_unavailable"
    assert r.events == []
    assert app.state.runtime_lease._file is None


def test_root_mismatch_precedes_metadata_lookup(runtime, monkeypatch, tmp_path):
    r = runtime
    app = r.make()
    monkeypatch.setattr(settings, "ocr_tmp_dir", (tmp_path / "other-root").resolve())
    assert startup_failure(app).code == "metadata_unavailable"
    assert r.events == []
    assert app.state.runtime_lease._file is None


def test_foreign_backend_is_rejected_without_stopping_it(runtime):
    r = runtime
    other = r.foreign()
    r.factory_hooks["exports"] = lambda candidate: setattr(candidate, "jobs", other)
    app = r.make()
    assert startup_failure(app).code == "metadata_unavailable"
    assert "factory:exports" in r.events
    assert "factory:queue" not in r.events
    assert not any(event.startswith(("start:", "stop:", "retention.", "mcp.")) for event in r.events)
    assert r.instances["exports"].jobs is other
    assert app.state.runtime_lease._file is None


@pytest.mark.parametrize("poison", ["quarantine", "transaction"])
def test_backend_factory_poison_blocks_later_factories_and_starts(runtime, poison):
    r = runtime

    def contaminate(candidate):
        with r.jobs._lock:
            if poison == "quarantine":
                r.jobs._quarantine_locked()
            else:
                r.jobs._conn.execute("BEGIN IMMEDIATE")
    r.factory_hooks["batches"] = contaminate
    app = r.make()
    assert startup_failure(app).code == "metadata_unavailable"
    assert r.jobs._sqlite_quarantined
    assert "factory:batches" in r.events
    assert "factory:controls" not in r.events
    assert not any(event.startswith(("start:", "retention.", "mcp.")) for event in r.events)
    assert app.state.runtime_lease._file is None


def test_late_factory_failure_stops_only_validated_targets(runtime):
    r = runtime

    def fail(candidate):
        raise RuntimeError("queue construction failed")
    r.factory_hooks["queue"] = fail
    app = r.make()
    assert startup_failure(app).code == "metadata_unavailable"
    assert [event for event in r.events if event.startswith("stop:")] == ["stop:exports"]
    assert not any(event.startswith(("start:", "retention.", "mcp.")) for event in r.events)
    assert app.state.runtime_lease._file is None


def test_start_failure_stops_validated_workers_before_releasing_lease(runtime):
    r = runtime
    marker = RuntimeError("queue start failed")

    def fail(candidate):
        assert app.state.runtime_lease._file is not None
        raise marker
    r.start_hooks["queue"] = fail
    app = r.make()
    original_stop = None

    def inside_factory(candidate):
        nonlocal original_stop
        original_stop = candidate.stop

        def stop():
            assert app.state.runtime_lease._file is not None
            original_stop()
        candidate.stop = stop
    r.factory_hooks["queue"] = inside_factory
    assert startup_failure(app, RuntimeError) is marker
    assert r.events[-3:] == ["retention.stop", "stop:exports", "stop:queue"]
    assert "mcp.enter" not in r.events
    assert app.state.runtime_lease._file is None


def test_stop_failure_retains_lease_and_store_pin(runtime):
    r = runtime
    other = r.foreign()
    marker = RuntimeError("export stop failed")
    r.stop_errors["exports"] = marker
    app = r.make()
    try:
        with pytest.raises(RuntimeError) as caught:
            lifecycle(app)
        assert caught.value is marker
        assert app.state.runtime_lease._file is not None
        assert r.events[-2:] == ["retention.stop", "stop:exports"]
        assert "stop:queue" not in r.events
        r.current = other
        unavailable(r.mcp.application)
        unavailable(app.state.metadata_provider)
        unavailable(r.mcp.catalog)
    finally:
        r.stop_errors.clear()
        for name in ("exports", "queue"):
            r.instances[name].stop()
        app.state.runtime_lease.release()


def test_active_store_switch_is_rejected_and_safe_shutdown_removes_pin(runtime):
    r = runtime
    other = r.foreign()
    app = r.make()

    def inside():
        r.current = other
        unavailable(r.mcp.application)
        unavailable(app.state.metadata_provider)
        unavailable(r.mcp.catalog)
        unavailable(r.catalog_application)
    lifecycle(app, inside)
    assert app.state.runtime_lease._file is None
    assert r.mcp.application().store is other
    assert app.state.metadata_provider().jobs is other
    assert r.catalog_application().store is other
    r.mcp.catalog()


@pytest.mark.parametrize("poison", ["quarantine", "transaction"])
def test_last_worker_start_must_be_health_checked_before_mcp_or_yield(runtime, poison):
    # Regression: lifespan currently has no health gate after export.start().
    r = runtime

    def contaminate(candidate):
        with r.jobs._lock:
            if poison == "quarantine":
                r.jobs._quarantine_locked()
            else:
                r.jobs._conn.execute("BEGIN IMMEDIATE")
    r.start_hooks["exports"] = contaminate
    app = r.make()
    assert startup_failure(app).code == "metadata_unavailable"
    assert "mcp.enter" not in r.events
    assert r.events[-3:] == ["retention.stop", "stop:exports", "stop:queue"]
    assert app.state.runtime_lease._file is None
