"""Document lifespan ownership, retained leases and the actual download deadline."""
import asyncio
import os
from threading import Event
from types import SimpleNamespace

import pytest

import app.services.document_execution as document_execution
from app.application import ApplicationError
from app.config import settings
from app.services.document_runtime import DocumentProvider, get_document_services
from app.services.runtime_lease import RuntimeLease
from test_document_metadata import env, who
from test_document_streaming import idle, streams_for, until
from test_metadata_startup import runtime, startup_failure, unavailable


def rejected(execution, code="document_operations_unavailable"):
    with pytest.raises(ApplicationError) as caught:
        execution.acquire(who())
    assert caught.value.status == 503
    assert caught.value.code == code


async def rejected_lifespan(app):
    with pytest.raises(ApplicationError) as caught:
        async with app.router.lifespan_context(app):
            pytest.fail("Retained application ownership allowed another lifespan")
    assert caught.value.status == 503
    assert caught.value.code == "metadata_unavailable"


def test_provider_shutdown_is_lazy_and_keeps_execution_identity():
    calls = []

    def forbidden():
        calls.append(True)
        pytest.fail("Shutdown resolved a document dependency")

    provider = DocumentProvider(forbidden, forbidden)
    execution = provider._execution
    assert provider._services is None and provider._cache == {}

    async def scenario():
        await provider.shutdown()
        await provider.shutdown(timeout_seconds=0)

    asyncio.run(scenario())
    assert calls == []
    assert provider._execution is execution
    assert provider._services is None and provider._cache == {}
    rejected(execution)


def test_app_construction_and_early_startup_cleanup_leave_documents_lazy(runtime):
    r = runtime
    app = r.make()
    original = app.state.document_provider
    request = SimpleNamespace(app=app)
    assert isinstance(original, DocumentProvider)
    assert original._services is None and original._cache == {}
    assert r.events == [] and r.lookups == 0
    assert r.provider._services is None

    del app.dependency_overrides[r.getters["uploads"]]
    assert startup_failure(app).code == "metadata_unavailable"
    assert r.events == []
    assert r.provider._services is None
    assert original._services is None and original._cache == {}
    rejected(original._execution)
    replacement = app.state.document_provider
    assert replacement is not original
    assert replacement._services is None and replacement._cache == {}
    assert app.state.runtime_lease._file is None

    # The request dependency resolves current state rather than retaining the old
    # coordinator. No real document bundle is needed to check that indirection.
    sentinel = object()
    app.state.document_provider = lambda: sentinel
    assert get_document_services(request) is sentinel
    app.state.document_provider = replacement


def test_app_documents_share_store_execution_and_current_provider_callbacks(runtime):
    r = runtime
    app = r.make()
    provider = app.state.document_provider

    async def scenario():
        async with app.router.lifespan_context(app):
            metadata = app.state.metadata_provider()
            catalog = app.state.catalog_provider()
            calls = []

            def current_metadata():
                calls.append("metadata")
                return metadata

            def current_catalog():
                calls.append("catalog")
                return catalog

            app.state.metadata_provider = current_metadata
            app.state.catalog_provider = current_catalog
            services = get_document_services(SimpleNamespace(app=app))
            assert calls == ["metadata", "catalog"]
            assert services.jobs is metadata.jobs is r.jobs
            assert catalog.contexts.jobs is catalog.types.jobs is catalog.templates.jobs is r.jobs
            assert services.document.bundle is metadata
            assert services.document.types is catalog.types
            assert services.canonical.store is r.jobs
            assert services.execution is provider._execution is services.streams.execution
            assert services.queries.document is services.document
            assert provider() is services
            assert calls == ["metadata", "catalog", "metadata", "catalog"]
            permit = services.execution.acquire(who())
            try:
                with pytest.raises(ApplicationError) as caught:
                    services.streams.execution.acquire(who())
                assert caught.value.code == "document_operations_busy"
            finally:
                permit.close()
        assert app.state.document_provider is not provider
        assert app.state.document_provider._services is None
        assert calls == ["metadata", "catalog", "metadata", "catalog"]
        rejected(provider._execution)

    asyncio.run(scenario())


def test_two_successful_lifespans_rotate_admission_without_rebuilding_bundles(runtime):
    r = runtime
    app = r.make()
    providers, bundles = [], []

    async def scenario():
        for index in range(2):
            provider = app.state.document_provider
            assert provider._services is None and provider._cache == {}
            providers.append(provider)
            async with app.router.lifespan_context(app):
                services = provider()
                bundles.append(services.document.bundle)
                assert await services.execution.run(who(), lambda control: index) == index
            rejected(provider._execution)
            assert app.state.document_provider is not provider
            assert app.state.document_provider._services is None
            assert app.state.document_provider._cache == {}
            assert app.state.runtime_lease._file is None
        assert providers[0]._execution is not providers[1]._execution
        assert bundles[0] is bundles[1]
        assert r.events.count("recover") == 2
        other = r.foreign()
        r.current = other
        assert app.state.document_provider().jobs is other

    asyncio.run(scenario())


def test_active_document_worker_drains_before_backend_stops_and_lease_release(runtime, monkeypatch):
    r = runtime
    app = r.make()
    provider = app.state.document_provider
    execution = provider._execution
    shutdown = provider.shutdown
    release = RuntimeLease.release
    started, exited = Event(), Event()

    async def observed_shutdown(timeout_seconds=30):
        r.events.append("documents.drain")
        await shutdown(timeout_seconds=timeout_seconds)
        r.events.append("documents.drained")

    def observed_release(lease):
        if lease is getattr(app.state, "runtime_lease", None):
            assert exited.is_set()
            assert execution.active_count == 0
            assert app.state.document_provider is provider
            assert provider._execution is execution
            r.events.append("lease.release")
        return release(lease)

    monkeypatch.setattr(provider, "shutdown", observed_shutdown)
    monkeypatch.setattr(RuntimeLease, "release", observed_release)

    def work(control):
        started.set()
        try:
            assert control._cancelled.wait(3), "Shutdown did not cancel document work"
            control.checkpoint()
        finally:
            exited.set()

    async def scenario():
        task = None
        try:
            async with app.router.lifespan_context(app):
                task = asyncio.create_task(execution.run(who(), work))
                await until(started.is_set)
                assert execution.active_count == 1
            with pytest.raises(ApplicationError) as caught:
                await task
            assert caught.value.code == "operation_cancelled"
            order = [r.events.index(event) for event in (
                "documents.drain", "documents.drained", "retention.stop",
                "stop:exports", "stop:queue", "lease.release")]
            assert order == sorted(order)
            assert app.state.document_provider is not provider
            assert app.state.runtime_lease._file is None
        finally:
            # Also signals cancellation if an assertion failed before normal exit.
            await shutdown(timeout_seconds=3)
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_cancelled_awaiter_and_failed_drain_retain_exact_runtime_ownership(runtime, monkeypatch, tmp_path):
    r = runtime
    app = r.make()
    provider = app.state.document_provider
    execution = provider._execution
    shutdown = provider.shutdown
    started, release_worker, exited = Event(), Event(), Event()

    async def immediate_shutdown(timeout_seconds=30):
        await shutdown(timeout_seconds=0)

    monkeypatch.setattr(provider, "shutdown", immediate_shutdown)

    def work(control):
        started.set()
        try:
            assert release_worker.wait(5), "Test did not release blocked document work"
            control.checkpoint()
        finally:
            exited.set()

    async def scenario():
        task = None
        lease = None
        try:
            with pytest.raises(ApplicationError) as caught:
                async with app.router.lifespan_context(app):
                    lease = app.state.runtime_lease
                    task = asyncio.create_task(execution.run(who(), work))
                    await until(started.is_set)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert execution.active_count == 1
                    assert not exited.is_set()
            assert caught.value.status == 503
            assert caught.value.code == "document_operations_active"
            assert execution.active_count == 1
            assert app.state.document_provider is provider
            assert provider._execution is execution
            assert app.state.runtime_lease is lease
            assert lease._file is not None and not lease._file.closed
            assert not any(event.startswith(("retention.stop", "stop:")) for event in r.events)
            rejected(execution)

            r.current = r.foreign()
            unavailable(r.mcp.application)
            unavailable(app.state.metadata_provider)
            unavailable(r.mcp.catalog)
            unavailable(provider)
            changed_root = (tmp_path / "changed-root").resolve()
            monkeypatch.setattr(settings, "ocr_tmp_dir", changed_root)
            await rejected_lifespan(app)
            assert not changed_root.exists()
            assert app.state.runtime_lease is lease
            assert app.state.document_provider is provider

            release_worker.set()
            await until(lambda: exited.is_set() and execution.active_count == 0)
            await shutdown(timeout_seconds=0)
            # Draining the coordinator later does not implicitly release the
            # application's retained lease, clear its pin or reopen admission.
            await rejected_lifespan(app)
            unavailable(r.mcp.application)
            assert app.state.runtime_lease is lease
            assert lease._file is not None
            assert app.state.document_provider is provider
            rejected(execution)
        finally:
            release_worker.set()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await until(lambda: execution.active_count == 0)
            await shutdown(timeout_seconds=0)
            # Explicit safe test teardown; application failure never releases it.
            if lease is not None:
                for name in ("exports", "queue"):
                    r.instances[name].stop()
                r.retention.stop()
                lease.release()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", [RuntimeError, KeyboardInterrupt, SystemExit, asyncio.CancelledError])
def test_shutdown_base_exception_retains_provider_lease_and_store_pin(runtime, monkeypatch, kind):
    r = runtime
    app = r.make()
    provider = app.state.document_provider
    shutdown = provider.shutdown
    marker = kind("Synthetic shutdown interruption")

    async def interrupted(timeout_seconds=30):
        raise marker

    monkeypatch.setattr(provider, "shutdown", interrupted)

    async def scenario():
        lease = None
        try:
            with pytest.raises(kind) as caught:
                async with app.router.lifespan_context(app):
                    lease = app.state.runtime_lease
            assert caught.value is marker
            assert app.state.document_provider is provider
            assert app.state.runtime_lease is lease
            assert lease._file is not None and not lease._file.closed
            assert not any(event.startswith(("retention.stop", "stop:")) for event in r.events)
            r.current = r.foreign()
            unavailable(r.mcp.application)
            unavailable(app.state.metadata_provider)
            unavailable(r.mcp.catalog)
            await rejected_lifespan(app)
            assert app.state.runtime_lease is lease
        finally:
            await shutdown(timeout_seconds=0)
            if lease is not None:
                r.retention.stop()
                r.instances["exports"].stop()
                r.instances["queue"].stop()
                lease.release()

    asyncio.run(scenario())


def test_actual_shutdown_task_cancellation_retains_runtime(runtime, monkeypatch):
    r = runtime
    app = r.make()
    provider = app.state.document_provider
    shutdown = provider.shutdown
    draining = asyncio.Event()

    async def blocked_shutdown(timeout_seconds=30):
        await shutdown(timeout_seconds=0)
        draining.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(provider, "shutdown", blocked_shutdown)

    async def lifespan():
        async with app.router.lifespan_context(app):
            pass

    async def scenario():
        task = asyncio.create_task(lifespan())
        try:
            await asyncio.wait_for(draining.wait(), 3)
            lease = app.state.runtime_lease
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert app.state.document_provider is provider
            assert app.state.runtime_lease is lease and lease._file is not None
            rejected(provider._execution)
            r.current = r.foreign()
            unavailable(r.mcp.application)
            await rejected_lifespan(app)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await shutdown(timeout_seconds=0)
            lease = getattr(app.state, "runtime_lease", None)
            if lease is not None and lease._file is not None:
                r.retention.stop()
                r.instances["exports"].stop()
                r.instances["queue"].stop()
                lease.release()

    asyncio.run(scenario())


def test_backend_stop_failure_keeps_closed_document_provider_and_pin(runtime):
    r = runtime
    app = r.make()
    provider = app.state.document_provider
    marker = RuntimeError("Synthetic export stop failure")
    r.stop_errors["exports"] = marker

    async def scenario():
        lease = None
        try:
            with pytest.raises(RuntimeError) as caught:
                async with app.router.lifespan_context(app):
                    lease = app.state.runtime_lease
            assert caught.value is marker
            assert app.state.document_provider is provider
            assert app.state.runtime_lease is lease and lease._file is not None
            rejected(provider._execution)
            assert r.events[-2:] == ["retention.stop", "stop:exports"]
            assert "stop:queue" not in r.events
            r.current = r.foreign()
            unavailable(r.mcp.application)
            unavailable(app.state.metadata_provider)
            unavailable(r.mcp.catalog)
            await rejected_lifespan(app)
        finally:
            r.stop_errors.clear()
            await provider.shutdown(timeout_seconds=0)
            if lease is not None:
                r.instances["exports"].stop()
                r.instances["queue"].stop()
                lease.release()

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")
def test_download_remaining_seconds_uses_existing_deadline_through_send_and_eof(env, monkeypatch):
    e = env
    clock = [100.0]
    monkeypatch.setattr(document_execution, "monotonic", lambda: clock[0])
    streams = streams_for(e, document_execution.DocumentExecution(timeout_seconds=10))

    async def scenario():
        download = None
        try:
            async with streams.open_canonical(e.owner, e.job.job_id) as download:
                control = download._transfer.permit.control
                deadline = control._deadline
                values = [download.remaining_seconds]
                assert values[0] == pytest.approx(deadline - clock[0])
                with pytest.raises(AttributeError):
                    download.remaining_seconds = 999
                chunks = []
                async for chunk in download.iter_bytes():
                    chunks.append(chunk)
                    before_send = download.remaining_seconds
                    # Model time spent in an awaited adapter send without making
                    # the real source or its worker depend on wall-clock sleeps.
                    clock[0] += 0.25
                    await asyncio.sleep(0)
                    after_send = download.remaining_seconds
                    assert after_send == pytest.approx(deadline - clock[0])
                    assert after_send < before_send
                    assert control._deadline == deadline
                    assert streams.execution.active_count == 1
                    values.extend((before_send, after_send))
                assert len(chunks) == 2 and all(chunks)
                await idle(e, streams)
                # Normal permit close cancels control authorization; the deadline
                # accessor must still work while its enclosing context is open.
                assert control._cancelled.is_set()
                assert download.remaining_seconds == pytest.approx(deadline - clock[0])
                values.append(download.remaining_seconds)
                assert values == sorted(values, reverse=True)
                clock[0] = deadline + 1
                assert download.remaining_seconds == 0
                assert control._deadline == deadline
            with pytest.raises(RuntimeError, match="context is closed"):
                _ = download.remaining_seconds
        finally:
            await idle(e, streams)

    asyncio.run(scenario())
