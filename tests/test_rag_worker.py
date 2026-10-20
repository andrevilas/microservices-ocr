import time
from dataclasses import replace
from threading import Event

import httpx2
import pytest

from app.services.rag_worker import RagDeliveryWorker
from test_rag_outbox import outbox, set_fields
from test_rag_delivery import source, receipt_for, response
from test_results import OWNER, pdf, orchestrator


def worker(queue, destinations, transport=None, batch_size=1):
    return RagDeliveryWorker(queue, destinations, interval_seconds=60,
                             batch_size=batch_size, warning_seconds=120, transport=transport)


def test_construct_does_not_start_and_snapshot_has_no_secrets(outbox):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    service = worker(queue, [dest])
    assert service._thread is None
    set_fields(queue, delivery_id, deadline=time.time() + 60)
    status = service.snapshot()
    assert status["due"] == status["near_deadline"] == 1
    assert status["requires_resolution"] == 0
    assert status["queued_payload_bytes"] > 0
    assert dest.token not in str(status) and job.job_id not in str(status)
    set_fields(queue, delivery_id, deadline=time.time() - 1)
    status = service.snapshot()
    assert status["requires_resolution"] == 1
    assert status["due"] == 0
    assert job.output_pdf_path.is_file()


def test_aggregate_alerts_only_on_transitions_and_no_secrets(outbox, caplog, monkeypatch):
    from types import SimpleNamespace
    import app.services.rag_worker as module
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    service = worker(queue, [dest])
    caplog.set_level("INFO", logger=module.__name__)
    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    set_fields(queue, delivery_id, deadline=time.time() + 60)
    assert service.observe()["storage_pressure"]
    service.observe()
    assert len(caplog.records) == 1
    set_fields(queue, delivery_id, deadline=time.time() - 1)
    assert service.observe()["requires_resolution"] == 1
    service.observe()
    assert len(caplog.records) == 2
    assert dest.token not in caplog.text and job.job_id not in caplog.text
    assert dest.url not in caplog.text and delivery_id not in caplog.text
    assert job.output_pdf_path.is_file()


def test_bounded_batch_and_replay(outbox):
    queue, job, dest = outbox
    queue.enqueue(OWNER, job.job_id, dest)
    other = queue.store.create("other.pdf", pdf(["first", "second"]), owner_user_id=1)
    orchestrator(queue.store).process_job(other.job_id)
    queue.enqueue(OWNER, other.job_id, dest)
    calls = []
    def receive(request):
        calls.append(request)
        return response(receipt_for(request.content))
    service = worker(queue, [dest], httpx2.MockTransport(receive))
    assert service.collect()["processed"] == 1
    assert service.snapshot()["due"] == 1
    assert service.collect()["processed"] == 1
    assert service.collect()["processed"] == 0
    assert len(calls) == 2
    assert service.snapshot()["states"] == {"indexed": 2}


def test_missing_destination_never_sends_and_retains_hold(outbox):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    def unexpected(_):
        pytest.fail("must not send with missing binding")
    service = worker(queue, [replace(dest, url="https://other.example")],
                     httpx2.MockTransport(unexpected))
    assert service.collect()["outcomes"] == {"resolution": 1}
    assert queue.status(OWNER, delivery_id)["error"] == "destination_missing"
    with queue.store._lock:
        assert queue.store._delivery_hold_locked(job.job_id)


def test_backoff_and_credential_rotation(outbox):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    calls = []
    def receive(request):
        calls.append(request.headers["authorization"])
        if len(calls) == 1:
            raise httpx2.ReadTimeout("do not log this")
        return response(receipt_for(request.content))
    service = worker(queue, [dest], httpx2.MockTransport(receive))
    assert service.collect()["outcomes"] == {"retry": 1}
    assert service.collect()["processed"] == 0
    service.replace_destinations([replace(dest, token="new-token-" * 4)])
    set_fields(queue, delivery_id, next_attempt=0)
    assert service.collect()["outcomes"] == {"indexed": 1}
    assert calls == ["Bearer " + dest.token, "Bearer " + "new-token-" * 4]


def test_concurrent_completion_is_not_reported_as_missing_configuration(outbox, monkeypatch):
    from contextlib import contextmanager
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    original = queue.transaction
    first = True
    @contextmanager
    def after_selection(*args, **kwargs):
        nonlocal first
        selected = first
        first = False
        with original(*args, **kwargs) as db:
            yield db
        if selected:
            # Another executor completes between selection and processing the row.
            assert queue.run_one(delivery_id, dest, transport=httpx2.MockTransport(
                lambda r: response(receipt_for(r.content)))) == "indexed"
    monkeypatch.setattr(queue, "transaction", after_selection)
    service = worker(queue, [])
    assert service.collect()["outcomes"] == {"indexed": 1}
    assert queue.status(OWNER, delivery_id)["state"] == "indexed"


def test_lifecycle_overlap_and_honest_stop(outbox):
    queue, job, dest = outbox
    queue.enqueue(OWNER, job.job_id, dest)
    entered, release = Event(), Event()
    def receive(request):
        entered.set()
        assert release.wait(5)
        return response(receipt_for(request.content))
    service = worker(queue, [dest], httpx2.MockTransport(receive))
    try:
        assert service.start()
        assert entered.wait(5)
        assert not service.start()
        assert service.collect() == {"busy": True, "processed": 0}
        assert not service.stop(0)
        assert not service.start()
    finally:
        release.set()
        assert service.stop(5)
    assert service.snapshot()["states"] == {"indexed": 1}


def test_unexpected_error_stops_worker_with_durable_claim(outbox):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    entered = Event()
    def crash(_):
        entered.set()
        raise RuntimeError("sensitive failure detail")
    service = worker(queue, [dest], httpx2.MockTransport(crash))
    assert service.start()
    assert entered.wait(5)
    assert service.stop(5)
    assert service.snapshot()["worker_failure"] == "worker_failed"
    assert queue.status(OWNER, delivery_id)["state"] == "sending"
    with queue.store._lock:
        assert queue.store._delivery_hold_locked(job.job_id)


@pytest.mark.parametrize("kwargs", [
    {"interval_seconds": 0}, {"interval_seconds": float("nan")},
    {"batch_size": True}, {"batch_size": 101}, {"warning_seconds": -1},
])
def test_policy_is_explicit_and_bounded(outbox, kwargs):
    queue, _, dest = outbox
    values = dict(interval_seconds=60, batch_size=1, warning_seconds=120)
    values.update(kwargs)
    with pytest.raises(ValueError):
        RagDeliveryWorker(queue, [dest], **values)
