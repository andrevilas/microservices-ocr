"""Resolution paths: retry, resubmission after cancel, operator access, per-owner admission."""
from dataclasses import replace

import httpx2
import pytest

from app.application import ApplicationError, Principal
from app.services.rag_delivery import Destination, prepare_delivery, send_delivery
from app.services.rag_outbox import DeliveryPolicy, RagOutbox
from app.services.rag_application import RagApplication
from test_rag_delivery import source, response, receipt_for
from test_rag_outbox import DELETE_OWNER, outbox, set_fields
from test_results import OWNER, orchestrator, pdf

SUBMIT_OWNER = replace(OWNER, scopes=OWNER.scopes | {"jobs:submit"})
ADMIN = Principal("user:9", 9, frozenset({"jobs:read", "jobs:submit", "artifacts:read", "artifacts:delete"}),
                  kind="user", is_admin=True)
OTHER = Principal("user:2", 2, frozenset({"jobs:read", "jobs:submit", "artifacts:read", "artifacts:delete"}))


def receive_ok(request):
    return response(receipt_for(request.content))


def to_resolution(queue, delivery_id, dest):
    set_fields(queue, delivery_id, attempts=queue.policy.max_attempts)
    assert queue.run_one(delivery_id, dest) == "resolution"


def test_retry_from_resolution_resets_budget_and_delivers(outbox):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    to_resolution(queue, delivery_id, dest)
    with pytest.raises(ApplicationError) as exc:
        queue.retry(OWNER, delivery_id)  # jobs:submit is required
    assert exc.value.status == 403
    assert queue.retry(SUBMIT_OWNER, delivery_id) == "retry"
    status = queue.status(OWNER, delivery_id)
    assert status["attempts"] == 0 and status["error"] is None
    assert queue.run_one(delivery_id, dest, transport=httpx2.MockTransport(receive_ok)) == "indexed"
    with pytest.raises(ApplicationError) as exc:
        queue.retry(SUBMIT_OWNER, delivery_id)
    assert exc.value.code == "rag_delivery_not_in_resolution"


def test_resubmission_after_cancel_requeues_same_delivery(outbox):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    assert queue.cancel(DELETE_OWNER, delivery_id) == "canceled"
    assert queue.enqueue(OWNER, job.job_id, dest) == delivery_id
    status = queue.status(OWNER, delivery_id)
    assert status["state"] == "queued" and status["attempts"] == 0
    assert queue.run_one(delivery_id, dest, transport=httpx2.MockTransport(receive_ok)) == "indexed"
    with queue.transaction() as db:
        events = [row[0] for row in db.execute(
            "SELECT event FROM rag_delivery_audit WHERE delivery_id=? ORDER BY at", (delivery_id,))]
    assert events[0] == "queued" and any(e.startswith("requeued@") for e in events)


def test_operator_can_list_retry_and_cancel_other_owners(outbox):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    assert queue.list(OTHER) == []
    assert [d["id"] for d in queue.list(ADMIN)] == [delivery_id]
    with pytest.raises(ApplicationError) as exc:
        queue.retry(OTHER, delivery_id)
    assert exc.value.status == 404
    to_resolution(queue, delivery_id, dest)
    assert [d["id"] for d in queue.list(ADMIN, "resolution")] == [delivery_id]
    assert queue.retry(ADMIN, delivery_id) == "retry"
    assert queue.cancel(ADMIN, delivery_id) == "canceled"
    with queue.transaction() as db:
        events = {row[0].split("@")[0] for row in db.execute(
            "SELECT event FROM rag_delivery_audit WHERE delivery_id=?", (delivery_id,))}
    assert {"operator-retry", "operator-canceled"} <= events
    with pytest.raises(ApplicationError):
        queue.list(OWNER, "bogus")


def test_resolution_blocks_only_its_owner(source, tmp_path):
    store, job, dest = source
    queue = RagOutbox(store, DeliveryPolicy(4, 32 * 1024 * 1024, 3600, 300, 3, 10, 60))
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    to_resolution(queue, delivery_id, dest)
    other_job = store.create("other.pdf", pdf(["texto de outro usuario"]), owner_user_id=2)
    orchestrator(store).process_job(other_job.job_id)
    other_dest = replace(dest, owner=OTHER.subject)
    assert queue.enqueue(OTHER, other_job.job_id, other_dest)
    second = store.create("second.pdf", pdf(["segundo documento"]), owner_user_id=1)
    orchestrator(store).process_job(second.job_id)
    with pytest.raises(ApplicationError) as exc:
        queue.enqueue(OWNER, second.job_id, dest)
    assert exc.value.code == "rag_queue_full"


def test_finished_entries_do_not_consume_capacity(source):
    store, job, dest = source
    queue = RagOutbox(store, DeliveryPolicy(1, 32 * 1024 * 1024, 3600, 300, 3, 10, 60))
    first = queue.enqueue(OWNER, job.job_id, dest)
    assert queue.run_one(first, dest, transport=httpx2.MockTransport(receive_ok)) == "indexed"
    second = store.create("second.pdf", pdf(["segundo documento"]), owner_user_id=1)
    orchestrator(store).process_job(second.job_id)
    assert queue.enqueue(OWNER, second.job_id, dest)


def test_per_owner_entry_limit(source):
    store, job, dest = source
    queue = RagOutbox(store, DeliveryPolicy(4, 32 * 1024 * 1024, 3600, 300, 3, 10, 60,
                                            max_entries_per_owner=1))
    queue.enqueue(OWNER, job.job_id, dest)
    second = store.create("second.pdf", pdf(["segundo documento"]), owner_user_id=1)
    orchestrator(store).process_job(second.job_id)
    with pytest.raises(ApplicationError) as exc:
        queue.enqueue(OWNER, second.job_id, dest)
    assert exc.value.code == "rag_queue_full"


@pytest.mark.parametrize("values", [
    dict(claim_seconds=59), dict(max_entries_per_owner=5), dict(max_entries_per_owner=0)])
def test_policy_rejects_unsafe_values(values):
    base = dict(max_entries=4, max_payload_bytes=1024, hold_seconds=3600, post_receipt_seconds=300,
                max_attempts=3, backoff_seconds=10, claim_seconds=60)
    with pytest.raises(ValueError):
        DeliveryPolicy(**{**base, **values})


@pytest.mark.parametrize("status", [401, 403, 404])
def test_unauthorized_destination_is_retryable(outbox, status):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    transport = httpx2.MockTransport(lambda request: response({}, status))
    assert queue.run_one(delivery_id, dest, transport=transport) == "retry"
    assert queue.status(OWNER, delivery_id)["error"] == "rag_destination_unauthorized"


@pytest.mark.parametrize("url,allowed", [
    ("http://document-rag.document-rag.svc.cluster.local:8092", True),
    ("http://svc.cluster.local", False),
    ("http://a.b.c.svc.cluster.local", False),
    ("http://evil.example/.svc.cluster.local", False),
    ("http://document-rag.document-rag.svc.cluster.local.evil.example", False),
    ("http://192.168.3.140:8092", False),
])
def test_cluster_service_http_only(source, url, allowed):
    store, job, dest = source
    candidate = replace(dest, url=url)
    if allowed:
        candidate.validate()
    else:
        with pytest.raises(ApplicationError):
            candidate.validate()


def test_operations_require_administrator(outbox):
    queue, job, dest = outbox

    class Worker:
        _thread = None
        def snapshot(self):
            return {"states": {}, "due": 0, "near_deadline": 0, "requires_resolution": 0,
                    "queued_payload_bytes": 0, "worker_failure": None}

    service = RagApplication(queue, {"normas": dest}, Worker())
    with pytest.raises(ApplicationError) as exc:
        service.get_rag_operations(OWNER)
    assert exc.value.status == 403
    result = service.get_rag_operations(ADMIN)
    assert result.enabled and not result.worker_running and result.worker_failure is None
