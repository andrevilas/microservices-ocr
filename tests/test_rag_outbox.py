import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event

import httpx2
import pytest

from app.application import ApplicationError
from app.services.rag_outbox import DeliveryPolicy, RagOutbox
from app.services.storage_service import JobStore
from test_rag_delivery import source, response, receipt_for
from test_results import OWNER

DELETE_OWNER = replace(OWNER, scopes=OWNER.scopes | {"artifacts:delete"})


@pytest.fixture
def outbox(source):
    store, job, destination = source
    # Synthetic test values only; no default production policy.
    policy = DeliveryPolicy(4, 32 * 1024 * 1024, 3600, 300, 3, 10, 60)
    return RagOutbox(store, policy), job, destination


def expire_source(store, job):
    with store._lock:
        job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        store._persist(job)


def set_fields(outbox, delivery_id, **values):
    with outbox.transaction() as db:
        db.execute("UPDATE rag_outbox SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                   (*values.values(), delivery_id))


def test_recovery_preserves_expired_hold_and_exact_package_after_restart(outbox):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    expected = queue.recovery_package(OWNER, delivery_id)
    assert dest.token.encode() not in expected
    expire_source(queue.store, job)
    set_fields(queue, delivery_id, deadline=time.time() - 1)
    assert queue.status(OWNER, delivery_id)["state"] == "resolution"
    restarted = JobStore(root_dir=queue.store.root_dir)
    try:
        restored = RagOutbox(restarted, queue.policy)
        assert restored.recovery_package(OWNER, delivery_id) == expected
        assert restored.status(OWNER, delivery_id)["state"] == "resolution"
        assert restarted.cleanup_expired() == []
    finally:
        restarted._conn.close()
    queue.cancel(DELETE_OWNER, delivery_id)
    with pytest.raises(ApplicationError) as exc:
        queue.recovery_package(OWNER, delivery_id)
    assert exc.value.code == "rag_package_unavailable"


@pytest.mark.parametrize("method", ["receipt", "recovery_package"])
def test_delivery_reads_require_owner_and_scope(outbox, method):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    read = getattr(queue, method)
    with pytest.raises(ApplicationError) as exc:
        read(replace(OWNER, subject="user:2", user_id=2), delivery_id)
    assert exc.value.status == 404
    with pytest.raises(ApplicationError) as exc:
        read(replace(OWNER, scopes=frozenset()), delivery_id)
    assert exc.value.status == 403
    if method == "recovery_package":
        with pytest.raises(ApplicationError) as exc:
            read(replace(OWNER, scopes=frozenset({"jobs:read"})), delivery_id)
        assert exc.value.status == 403


def test_verified_receipt_remains_available_after_package_release(outbox):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    with pytest.raises(ApplicationError) as exc:
        queue.receipt(OWNER, delivery_id)
    assert exc.value.code == "rag_receipt_unavailable"
    expected = receipt_for(queue.recovery_package(OWNER, delivery_id))
    queue.run_one(delivery_id, dest, transport=httpx2.MockTransport(lambda r: response(expected)))
    assert queue.receipt(OWNER, delivery_id) == expected
    with pytest.raises(ApplicationError) as exc:
        queue.recovery_package(OWNER, delivery_id)
    assert exc.value.code == "rag_package_unavailable"
    restarted = JobStore(root_dir=queue.store.root_dir)
    try:
        assert RagOutbox(restarted, queue.policy).receipt(OWNER, delivery_id) == expected
    finally:
        restarted._conn.close()


def test_hold_survives_restart_and_all_cleanup_paths(outbox):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    assert queue.enqueue(OWNER, job.job_id, dest) == delivery_id
    expire_source(queue.store, job)
    assert queue.store.cleanup_expired() == []
    for call in (lambda: queue.store.remove_artifacts(job.job_id, OWNER.subject, OWNER.subject),
                 lambda: queue.store.cleanup(job.job_id)):
        with pytest.raises(ApplicationError) as exc:
            call()
        assert exc.value.code == "rag_delivery_hold"
    restarted = JobStore(root_dir=queue.store.root_dir)
    try:
        assert restarted.cleanup_expired() == []
        assert job.output_pdf_path.is_file()
        assert RagOutbox(restarted, queue.policy).status(OWNER, delivery_id)["state"] == "queued"
    finally:
        restarted._conn.close()
    with queue.store._lock:
        row = queue.store._get_conn().execute("SELECT destination,policy FROM rag_outbox").fetchone()
        assert dest.token not in " ".join(row)


def test_indexed_receipt_releases_hold_and_extends_ttl_atomically(outbox):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    expire_source(queue.store, job)
    before = datetime.now(timezone.utc)
    status = queue.run_one(delivery_id, dest, transport=httpx2.MockTransport(
        lambda r: response(receipt_for(r.content))))
    assert status == "indexed"
    assert job.artifacts_expires_at >= before + timedelta(seconds=queue.policy.post_receipt_seconds)
    assert queue.store.cleanup_expired() == []
    with queue.store._lock:
        assert not queue.store._delivery_hold_locked(job.job_id)
        row = queue.store._get_conn().execute("SELECT payload,receipt FROM rag_outbox").fetchone()
        assert row[0] is None and json.loads(row[1])["indexing_state"] == "indexed"
    restarted = JobStore(root_dir=queue.store.root_dir)
    try:
        assert restarted.jobs[job.job_id].artifacts_expires_at == job.artifacts_expires_at
    finally:
        restarted._conn.close()
    assert queue.run_one(delivery_id, dest) == "indexed"


def test_timeout_retry_and_deadline_never_release_hold(outbox):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    def timeout(_):
        raise httpx2.ReadTimeout("not logged")
    assert queue.run_one(delivery_id, dest, transport=httpx2.MockTransport(timeout)) == "retry"
    assert queue.run_one(delivery_id, dest) == "busy"
    expire_source(queue.store, job)
    assert queue.store.cleanup_expired() == []
    set_fields(queue, delivery_id, deadline=time.time() - 1)
    assert queue.status(OWNER, delivery_id)["state"] == "resolution"
    assert queue.run_one(delivery_id, dest) == "resolution"
    assert queue.store.cleanup_expired() == []
    assert job.output_pdf_path.is_file()
    # Expiry does not replace explicit cancellation.
    assert queue.cancel(DELETE_OWNER, delivery_id) == "canceled"
    assert queue.store.cleanup_expired() == [job.job_id]


def test_exhaustion_and_permanent_failure(outbox):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    set_fields(queue, delivery_id, attempts=queue.policy.max_attempts)
    assert queue.run_one(delivery_id, dest) == "resolution"
    assert queue.status(OWNER, delivery_id)["error"] == "attempt_limit"
    with queue.store._lock:
        assert queue.store._delivery_hold_locked(job.job_id)


def test_concurrent_claim_cleanup_and_cancellation(outbox):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    entered, release = Event(), Event()
    def receive(request):
        entered.set()
        assert release.wait(5)
        return response(receipt_for(request.content))
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(queue.run_one, delivery_id, dest, transport=httpx2.MockTransport(receive))
        assert entered.wait(5)
        try:
            assert queue.run_one(delivery_id, dest) == "busy"
            with pytest.raises(ApplicationError) as exc:
                queue.cancel(DELETE_OWNER, delivery_id)
            assert exc.value.code == "rag_delivery_busy"
            expire_source(queue.store, job)
            assert queue.store.cleanup_expired() == []
        finally:
            release.set()
        assert future.result() == "indexed"


def test_crash_claim_and_late_response_are_fenced(outbox):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    def crashed(_):
        raise RuntimeError("worker died")
    with pytest.raises(RuntimeError):
        queue.run_one(delivery_id, dest, transport=httpx2.MockTransport(crashed))
    assert queue.run_one(delivery_id, dest) == "busy"
    set_fields(queue, delivery_id, claim_until=0)
    def late(request):
        set_fields(queue, delivery_id, claim="replacement", claim_until=time.time() + 60)
        return response(receipt_for(request.content))
    assert queue.run_one(delivery_id, dest, transport=httpx2.MockTransport(late)) == "superseded"
    with queue.store._lock:
        assert queue.store._delivery_hold_locked(job.job_id)


@pytest.mark.parametrize("kind", ["owner", "destination", "quota", "policy"])
def test_admission_boundaries(outbox, kind):
    queue, job, dest = outbox
    with pytest.raises((ApplicationError, ValueError)) as exc:
        if kind == "owner":
            queue.enqueue(replace(OWNER, subject="user:2"), job.job_id, dest)
        elif kind == "destination":
            delivery_id = queue.enqueue(OWNER, job.job_id, dest)
            queue.run_one(delivery_id, replace(dest, url="https://other.example"))
        elif kind == "quota":
            small = RagOutbox(queue.store, replace(queue.policy, max_payload_bytes=1))
            small.enqueue(OWNER, job.job_id, dest)
        else:
            replace(queue.policy, max_attempts=0)
    assert queue.store._artifact_leases == {}


def test_unauthorized_status_and_cancel(outbox):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    with pytest.raises(ApplicationError) as exc:
        queue.status(replace(OWNER, subject="user:2"), delivery_id)
    assert exc.value.status == 404
    with pytest.raises(ApplicationError):
        queue.cancel(OWNER, delivery_id)
    with pytest.raises(ApplicationError):
        queue.cancel(replace(DELETE_OWNER, subject="user:2"), delivery_id)
    assert queue.status(OWNER, delivery_id)["state"] == "queued"


@pytest.mark.parametrize("mode", ["pending", "forged"])
def test_nonfinal_or_invalid_ack_keeps_hold(outbox, mode):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    def receive(request):
        receipt = receipt_for(request.content)
        if mode == "pending":
            receipt["indexing_state"] = "pending"
        else:
            receipt["manifest_sha256"] = "0" * 64
        return response(receipt)
    assert queue.run_one(delivery_id, dest, transport=httpx2.MockTransport(receive)) == (
        "retry" if mode == "pending" else "resolution")
    expire_source(queue.store, job)
    assert queue.store.cleanup_expired() == []
    assert job.output_pdf_path.is_file()


@pytest.mark.parametrize("commit_first", [False, True])
def test_uncertain_receipt_commit_quarantines_store(outbox, commit_first):
    queue, job, dest = outbox
    delivery_id = queue.enqueue(OWNER, job.job_id, dest)
    expire_source(queue.store, job)
    connection = queue.store._conn
    class UncertainCommit:
        def __getattr__(self, name):
            return getattr(connection, name)
        def commit(self):
            state = connection.execute("SELECT state FROM rag_outbox WHERE id=?", (delivery_id,)).fetchone()[0]
            if state == "indexed":
                if commit_first:
                    connection.commit()
                raise RuntimeError("injected uncertain commit")
            connection.commit()
    queue.store._conn = UncertainCommit()
    with pytest.raises(RuntimeError):
        queue.run_one(delivery_id, dest, transport=httpx2.MockTransport(
            lambda r: response(receipt_for(r.content))))
    with pytest.raises(ApplicationError) as exc:
        queue.store.cleanup_expired()
    assert exc.value.status == 503
    assert job.output_pdf_path.is_file()
    # A real restart closes the quarantined connection before opening another.
    queue.store._conn.close()
    restarted = JobStore(root_dir=queue.store.root_dir)
    try:
        assert restarted.cleanup_expired() == []
        state = RagOutbox(restarted, queue.policy).status(OWNER, delivery_id)["state"]
        assert state == ("indexed" if commit_first else "sending")
    finally:
        restarted._conn.close()


def test_cleanup_wins_before_admission_without_enqueuing_missing_files(outbox, monkeypatch):
    import app.services.rag_outbox as module
    queue, job, dest = outbox
    original = module.prepare_delivery
    def race(*args, **kwargs):
        prepared = original(*args, **kwargs)
        expire_source(queue.store, job)
        assert queue.store.cleanup_expired() == [job.job_id]
        assert job.working_dir.exists()  # Outer runtime lease prevents physical purge.
        return prepared
    monkeypatch.setattr(module, "prepare_delivery", race)
    with pytest.raises(ApplicationError):
        queue.enqueue(OWNER, job.job_id, dest)
    with queue.store._lock:
        assert queue.store._get_conn().execute("SELECT count(*) FROM rag_outbox").fetchone()[0] == 0
    assert not job.working_dir.exists()
