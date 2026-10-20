import json
from dataclasses import asdict
import pytest
from app.application import ApplicationError
from app.services.rag_runtime import configured_runtime
from test_rag_outbox import outbox
from test_rag_delivery import source


def config(outbox):
    queue, _, dest = outbox
    return dict(policy=asdict(queue.policy), destinations=[dict(key="normas", **asdict(dest))],
                interval_seconds=1, batch_size=1, warning_seconds=30, shutdown_seconds=2)


def test_optional_runtime_does_not_start_until_requested(outbox, tmp_path):
    queue, _, _ = outbox
    assert configured_runtime(queue.store, None) is None
    path = tmp_path / "operator.json"
    path.write_text(json.dumps(config(outbox)))
    runtime = configured_runtime(queue.store, path)
    assert runtime.service.outbox.store is queue.store
    assert runtime.worker._thread is None
    runtime.start()
    assert runtime.worker._thread.is_alive()
    runtime.stop()
    assert runtime.worker._thread is None


@pytest.mark.parametrize("invalid", ["secret", "oversize", "duplicate", "url", "policy", "relative"])
def test_bad_config_fails_closed_without_secret_disclosure(outbox, tmp_path, invalid):
    queue, _, dest = outbox
    body = config(outbox)
    if invalid == "duplicate":
        body["destinations"] *= 2
    if invalid == "url":
        body["destinations"][0]["url"] = "http://untrusted.invalid"
    if invalid == "policy":
        body["policy"]["hold_seconds"] = True
    path = tmp_path / "operator.json"
    path.write_text(dest.token if invalid == "secret" else "x" * 65537 if invalid == "oversize" else json.dumps(body))
    with pytest.raises(ApplicationError) as exc:
        configured_runtime(queue.store, "relative.json" if invalid == "relative" else path)
    assert exc.value.code == "rag_configuration_invalid"
    assert dest.token not in str(exc.value)


def test_shutdown_reports_live_writer(outbox, tmp_path, monkeypatch):
    queue, _, _ = outbox
    path = tmp_path / "operator.json"
    path.write_text(json.dumps(config(outbox)))
    runtime = configured_runtime(queue.store, path)
    monkeypatch.setattr(runtime.worker, "stop", lambda timeout: False)
    with pytest.raises(ApplicationError) as exc:
        runtime.stop()
    assert exc.value.code == "rag_shutdown_pending"


@pytest.mark.parametrize("stop_fails", [False, True])
def test_application_lifespan_pins_store_and_retains_lease_on_failed_stop(outbox, tmp_path, monkeypatch, stop_fails):
    import asyncio
    from unittest.mock import Mock
    import app.main as main
    from app.application import JobApplication
    from app.config import settings
    from app.services.upload_service import get_upload_service
    from app.services.batch_service import get_batch_service
    from app.services.control_service import get_control_service
    from app.services.export_service import get_export_service
    from app.services.job_queue import get_job_queue_processor
    queue, _, _ = outbox
    path = tmp_path / "operator.json"
    path.write_text(json.dumps(config(outbox)))
    monkeypatch.setattr(settings, "rag_config_file", path)
    monkeypatch.setattr(settings, "ocr_tmp_dir", queue.store.root_dir)
    app = main.create_app(lambda: JobApplication(queue.store))
    backend = Mock(jobs=queue.store, job_store=queue.store)
    backend.uploads = backend
    for getter in (get_upload_service, get_batch_service, get_control_service,
                   get_export_service, get_job_queue_processor):
        app.dependency_overrides[getter] = lambda: backend
    monkeypatch.setattr(main, "RetentionWorker", lambda *a, **kw: Mock())
    runtime = None
    async def scenario():
        nonlocal runtime
        async with app.router.lifespan_context(app):
            runtime = app.state.rag_runtime
            assert runtime.service.outbox.store is queue.store
            assert app.state.rag_service is runtime.service
            assert runtime.worker._thread.is_alive()
            if stop_fails:
                runtime.stop = lambda: (_ for _ in ()).throw(ApplicationError(503, "rag_shutdown_pending", "pending"))
    try:
        if stop_fails:
            with pytest.raises(ApplicationError):
                asyncio.run(scenario())
            assert app.state.runtime_lease._file is not None
            assert app.state.rag_runtime is runtime
            with pytest.raises(ApplicationError):
                asyncio.run(scenario())
        else:
            asyncio.run(scenario())
            assert app.state.runtime_lease._file is None
            assert app.state.rag_runtime is None
        assert app.state.rag_service is None
    finally:
        if runtime is not None:
            assert runtime.worker.stop(5)
        if app.state.runtime_lease._file is not None:
            app.state.runtime_lease.release()
