"""Internal provider identity and bounded execution, without transport wiring."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import os
from threading import Barrier, Event, Lock, Thread
from types import SimpleNamespace

import pytest

from app.application import ApplicationError, JobApplication, Principal
from app.services.catalog_runtime import CatalogProvider
import app.services.document_execution as execution_module
from app.services.document_execution import DocumentExecution, OperationControl
from app.services.document_runtime import DocumentProvider, get_document_services
from app.services.metadata_runtime import MetadataProvider
from app.services.storage_service import JobStore
from test_document_metadata import env, save


def principal(number=1):
    return Principal(subject=f"user:{number}", user_id=number, scopes=frozenset())


def failure(action, status, code):
    with pytest.raises(ApplicationError) as caught:
        action()
    assert (caught.value.status, caught.value.code) == (status, code)
    return caught.value


async def async_failure(awaitable, status, code):
    with pytest.raises(ApplicationError) as caught:
        await awaitable
    assert (caught.value.status, caught.value.code) == (status, code)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    return caught.value


async def eventually(predicate):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3
    while not predicate():
        assert loop.time() < deadline, "worker did not reach the expected state"
        await asyncio.sleep(0.001)


def blocked(started, release, observations):
    def work(control):
        observations.append(control)
        started.set()
        assert release.wait(3), "test did not release worker"
        return "completed"
    return work


def test_atomic_admission_subject_keys_and_owned_manual_permits():
    coordinator = DocumentExecution()
    first = coordinator.acquire(principal())
    failure(lambda: coordinator.acquire(principal()), 429, "document_operations_busy")
    service = Principal(subject="service:" + "a" * 32, user_id=None,
                        scopes=frozenset(), kind="service")
    second = coordinator.acquire(service)
    failure(lambda: coordinator.acquire(principal(3)), 429, "document_operations_busy")
    assert coordinator.active_count == 2
    first.close()
    first.close()
    failure(first.control.checkpoint, 408, "operation_cancelled")
    assert coordinator.active_count == 1
    # Closing an old permit must not close a newer permit for the same subject.
    replacement = coordinator.acquire(principal())
    first.close()
    assert coordinator.active_count == 2
    replacement.close()
    second.close()
    assert coordinator.active_count == 0


def test_concurrent_admission_is_atomic():
    coordinator = DocumentExecution()
    barrier, lock, results = Barrier(4, timeout=3), Lock(), []

    def enter(number):
        barrier.wait()
        try:
            result = coordinator.acquire(principal(number))
        except ApplicationError as error:
            result = error
        with lock:
            results.append(result)

    threads = [Thread(target=enter, args=(n,), daemon=True) for n in (1, 2, 3)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(3)
    assert all(not thread.is_alive() for thread in threads)
    permits = [value for value in results if not isinstance(value, ApplicationError)]
    errors = [value for value in results if isinstance(value, ApplicationError)]
    try:
        assert len(permits) == 2 and len(errors) == 1
        assert (errors[0].status, errors[0].code) == (429, "document_operations_busy")
    finally:
        for permit in permits:
            permit.close()


@pytest.mark.parametrize("subject", [None, "", "user:0", "user:-1", "user:01",
                                     "user:1\n", "service:abc", "admin:1"])
def test_invalid_subject(subject):
    coordinator = DocumentExecution()
    actor = Principal(subject=subject, user_id=None, scopes=frozenset())
    failure(lambda: coordinator.acquire(actor), 422, "invalid_document_principal")
    assert coordinator.active_count == 0


def test_exact_principal_required():
    class Derived(Principal):
        pass
    coordinator = DocumentExecution()
    for actor in (SimpleNamespace(subject="user:1"),
                  Derived(subject="user:1", user_id=1, scopes=frozenset())):
        failure(lambda: coordinator.acquire(actor), 422, "invalid_document_principal")


@pytest.mark.parametrize("timeout", [True, False, 0, -1, 121, float("inf"),
                                     float("nan"), "1"])
def test_timeout_constructor_validation(timeout):
    with pytest.raises(ValueError):
        OperationControl(timeout)
    with pytest.raises(ValueError):
        DocumentExecution(timeout)


def test_control_deadline_and_explicit_cancellation(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(execution_module, "monotonic", lambda: now[0])
    control = OperationControl(2)
    control.checkpoint()
    assert control.remaining_seconds == 2
    now[0] = 102
    failure(control.checkpoint, 408, "operation_timeout")
    cancelled = OperationControl()
    cancelled.cancel()
    cancelled.cancel()
    failure(cancelled.checkpoint, 408, "operation_cancelled")


def test_worker_results_errors_controls_and_submission_failure(monkeypatch):
    async def scenario():
        coordinator = DocumentExecution()
        assert await coordinator.run(principal(), lambda control: (control.checkpoint(), 7)[1]) == 7
        assert coordinator.active_count == 0

        def throw(error):
            def work(control):
                raise error
            return work

        original = ApplicationError(409, "metadata_head_conflict", "secret payload")
        sanitized = await async_failure(
            coordinator.run(principal(), throw(original)), 409, "metadata_head_conflict")
        assert sanitized is not original and "secret" not in sanitized.message
        unexpected = await async_failure(
            coordinator.run(principal(), throw(RuntimeError("secret payload"))),
            503, "document_operations_unavailable")
        assert "secret" not in unexpected.message
        # Catch controls in the awaiting coroutine, never in detached Tasks.
        for original_control in (KeyboardInterrupt(), SystemExit(), asyncio.CancelledError()):
            try:
                await coordinator.run(principal(), throw(original_control))
            except BaseException as caught:
                assert caught is original_control
            else:
                pytest.fail("worker control did not reach its awaiting caller")
            assert coordinator.active_count == 0

        loop = asyncio.get_running_loop()
        calls = []

        def reject(*args):
            calls.append(True)
            raise RuntimeError("private executor error")

        with monkeypatch.context() as patch:
            patch.setattr(loop, "run_in_executor", reject)
            await async_failure(coordinator.run(principal(), lambda control: None),
                                503, "document_operations_unavailable")
        assert calls == [True] and coordinator.active_count == 0
        await coordinator.shutdown()
        failure(lambda: coordinator.acquire(principal()), 503, "document_operations_unavailable")

    asyncio.run(scenario())


def test_checkpoint_timeout_prevents_later_publication(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(execution_module, "monotonic", lambda: now[0])

    async def scenario():
        coordinator, published = DocumentExecution(), []

        def work(control):
            now[0] = 221
            control.checkpoint()
            published.append(True)

        await async_failure(coordinator.run(principal(), work), 408, "operation_timeout")
        assert not published and coordinator.active_count == 0
        await coordinator.shutdown()

    asyncio.run(scenario())


def test_caller_cancellation_retains_worker_and_subject_until_completion():
    async def scenario():
        coordinator = DocumentExecution()
        started, release, controls, cancellations = Event(), Event(), [], []

        async def invoke():
            try:
                return await coordinator.run(principal(), blocked(started, release, controls))
            except asyncio.CancelledError as error:
                cancellations.append(error)
                raise

        task = asyncio.create_task(invoke())
        try:
            await eventually(started.is_set)
            task.cancel("synthetic cancellation")
            with pytest.raises(asyncio.CancelledError) as caught:
                await task
            assert cancellations == [caught.value]
            failure(controls[0].checkpoint, 408, "operation_cancelled")
            assert coordinator.active_count == 1
            failure(lambda: coordinator.acquire(principal()), 429, "document_operations_busy")
            other = coordinator.acquire(principal(2))
            failure(lambda: coordinator.acquire(principal(3)), 429, "document_operations_busy")
            other.close()
        finally:
            release.set()
            await eventually(lambda: coordinator.active_count == 0)
        await coordinator.shutdown()

    asyncio.run(scenario())


def test_client_deadline_retains_blocking_worker_and_consumes_late_error():
    async def scenario():
        coordinator = DocumentExecution(timeout_seconds=0.1)
        started, release, controls = Event(), Event(), []
        base = blocked(started, release, controls)

        def work(control):
            base(control)
            raise SystemExit("private late control")

        task = asyncio.create_task(coordinator.run(principal(), work))
        try:
            await eventually(started.is_set)
            await async_failure(task, 408, "operation_timeout")
            assert coordinator.active_count == 1
            failure(lambda: coordinator.acquire(principal()), 429, "document_operations_busy")
            failure(controls[0].checkpoint, 408, "operation_timeout")
        finally:
            release.set()
            await eventually(lambda: coordinator.active_count == 0)
        await coordinator.shutdown()

    asyncio.run(scenario())


def test_expired_executor_queue_never_calls_body():
    async def scenario():
        coordinator = DocumentExecution(timeout_seconds=0.05)
        started, release, called = Event(), Event(), Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            loop = asyncio.get_running_loop()
            loop.set_default_executor(executor)

            def occupy_executor():
                started.set()
                assert release.wait(3)

            blocker = executor.submit(occupy_executor)
            try:
                await eventually(started.is_set)
                await async_failure(coordinator.run(principal(), lambda control: called.set()),
                                    408, "operation_timeout")
                assert coordinator.active_count == 1 and not called.is_set()
                failure(lambda: coordinator.acquire(principal()), 429, "document_operations_busy")
            finally:
                release.set()
                await eventually(lambda: coordinator.active_count == 0)
            blocker.result(timeout=1)
            assert not called.is_set()
            await coordinator.shutdown()

    asyncio.run(scenario())


def test_shutdown_keeps_active_permits_and_never_reopens():
    async def scenario():
        coordinator = DocumentExecution()
        started, release, controls = Event(), Event(), []
        task = asyncio.create_task(coordinator.run(principal(), blocked(started, release, controls)))
        try:
            await eventually(started.is_set)
            await async_failure(coordinator.shutdown(timeout_seconds=0),
                                503, "document_operations_active")
            assert coordinator.active_count == 1
            failure(controls[0].checkpoint, 408, "operation_cancelled")
            failure(lambda: coordinator.acquire(principal(2)),
                    503, "document_operations_unavailable")
        finally:
            release.set()
            assert await task == "completed"
        assert coordinator.active_count == 0
        await coordinator.shutdown()
        failure(lambda: coordinator.acquire(principal()), 503, "document_operations_unavailable")

    asyncio.run(scenario())


def test_manual_stream_primitive_requires_explicit_cleanup_close():
    async def scenario():
        coordinator = DocumentExecution()
        permit = coordinator.acquire(principal())
        try:
            # Cancellation is not a release, including during stream cleanup.
            permit.control.cancel()
            await async_failure(coordinator.shutdown(0), 503, "document_operations_active")
            assert coordinator.active_count == 1
        finally:
            permit.close()
            permit.close()
        await coordinator.shutdown()
        assert coordinator.active_count == 0

    asyncio.run(scenario())


def test_provider_construction_is_lazy_and_controls_keep_identity():
    calls = []
    original = KeyboardInterrupt()

    def metadata():
        calls.append("metadata")
        raise original

    def catalog():
        calls.append("catalog")
        pytest.fail("catalog resolution followed a control exception")

    provider = DocumentProvider(metadata, catalog)
    assert calls == [] and provider._services is None
    try:
        provider()
    except KeyboardInterrupt as caught:
        assert caught is original
    else:
        pytest.fail("provider swallowed control")
    assert calls == ["metadata"] and provider._services is None
    bad = DocumentProvider(lambda: object(), lambda: object())
    error = failure(bad, 503, "metadata_unavailable")
    assert error.__cause__ is None and error.__context__ is None


@pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")
def test_real_provider_identity_switch_failure_cache_and_scoped_read(env, tmp_path):
    e = env
    other = JobStore(tmp_path.absolute() / "other-document-runtime")
    current = [JobApplication(e.jobs)]
    metadata = MetadataProvider(lambda: current[0])
    catalogs = CatalogProvider(lambda: current[0])
    override, calls = [None], []

    def resolve_metadata():
        calls.append("metadata")
        return metadata()

    def resolve_catalog():
        calls.append("catalog")
        return catalogs() if override[0] is None else override[0]

    provider = DocumentProvider(resolve_metadata, resolve_catalog)
    try:
        assert calls == []
        first = provider()
        first_catalog = catalogs()
        assert calls == ["metadata", "catalog"]
        assert provider() is first
        assert first.jobs is e.jobs
        assert first.canonical is first.document.canonical
        assert first.document.bundle is metadata()
        assert first.document.types is first_catalog.types
        with pytest.raises(FrozenInstanceError):
            first.execution = DocumentExecution()
        request = SimpleNamespace(app=SimpleNamespace(
            state=SimpleNamespace(document_provider=provider)))
        assert get_document_services(request) is first

        current[0] = JobApplication(other)
        second = provider()
        second_catalog = catalogs()
        assert second is not first and second.jobs is other
        assert second.execution is first.execution
        current[0] = JobApplication(e.jobs)
        override[0] = second_catalog
        error = failure(provider, 503, "metadata_unavailable")
        assert error.__cause__ is None and error.__context__ is None
        assert provider._services is second and len(provider._cache) == 2
        override[0] = first_catalog
        assert provider() is first  # Exact input bundles, not merely same store.
        override[0] = None
        rebuilt = provider()  # CatalogProvider rebuilt its bundle after switching.
        assert rebuilt is not first and rebuilt.jobs is first.jobs
        assert rebuilt.execution is first.execution
        assert provider() is rebuilt

        permits = [first.execution.acquire(principal()),
                   second.execution.acquire(principal(2))]
        try:
            failure(lambda: rebuilt.execution.acquire(principal(3)),
                    429, "document_operations_busy")
        finally:
            for permit in permits:
                permit.close()

        receipt, _ = save(e)
        snapshot = rebuilt.document.get(e.owner, e.job.job_id, receipt.revision_id,
                                        sha256=receipt.sha256)
        assert snapshot.job_id == e.job.job_id
        foreign = Principal(subject="user:2", user_id=2, scopes=e.owner.scopes)
        failure(lambda: rebuilt.document.get(foreign, e.job.job_id, receipt.revision_id,
                                             sha256=receipt.sha256),
                404, "job_not_found")
        restricted = Principal(subject=e.owner.subject, user_id=e.owner.user_id,
                               scopes=e.owner.scopes - {"metadata:read"})
        failure(lambda: rebuilt.document.get(restricted, e.job.job_id, receipt.revision_id,
                                             sha256=receipt.sha256),
                403, "insufficient_scope")
        assert not e.jobs._artifact_leases
    finally:
        with other._lock:
            other._conn.close()


@pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")
@pytest.mark.parametrize("component", ["contexts", "types", "templates"])
def test_each_real_catalog_component_must_share_jobstore(env, tmp_path, component):
    e = env
    other = JobStore(tmp_path.absolute() / ("foreign-" + component))
    try:
        metadata = MetadataProvider(lambda: JobApplication(e.jobs))
        catalog = CatalogProvider(lambda: JobApplication(e.jobs))()
        foreign = CatalogProvider(lambda: JobApplication(other))()
        provider = DocumentProvider(metadata, lambda: catalog)
        previous = provider()
        service = getattr(catalog, component)
        original = service.jobs
        service.jobs = getattr(foreign, component).jobs
        try:
            failure(provider, 503, "metadata_unavailable")
            assert provider._services is previous and len(provider._cache) == 1
        finally:
            service.jobs = original
        assert provider() is previous
    finally:
        with other._lock:
            other._conn.close()
