"""Durable expiration/removal, active read leases and real HTTP/MCP deletion."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import os
import sqlite3
from threading import Event
import time

import httpx2
import pytest

from app.application import ApplicationError, JobApplication, Principal
from app.config import settings
from app.control_models import RetryInput
from app.leased_response import LeasedFileResponse
from app.retention_models import retention_info
from app.services.artifact_retention import safe_rmtree
from app.services.control_service import ControlService
from app.services.export_service import ExportService
from app.services.retention_worker import RetentionWorker
from app.services.storage_service import JobStore, _CREATE_TABLE_SQL
from test_http_contract import validate_response
from test_mcp_vertical import runtime, connected, call, complete
from test_results import orchestrator
from test_upload_admission import system, bearer, pdf, upload
from test_bulk import bulk, job as bulk_job, create_export, wait_export

OWNER = Principal("user:1", 1, frozenset({"jobs:read", "jobs:submit", "artifacts:read", "artifacts:delete"}))


def finished(system, status="completed"):
    job = system.jobs.create("synthetic-private-name.pdf", pdf(), owner_user_id=1)
    if status == "completed":
        orchestrator(system.jobs).process_job(job.job_id)
        assert job.status == "completed"
    else:
        system.jobs.update(job.job_id, status=status)
    return job


def expire(store, job):
    with store._lock:
        job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        store._persist(job)


def delete(system, job, token=None):
    return system.client.delete(f"/api/v1/jobs/{job.job_id}/artifacts", headers=bearer(token or system.tokens[0]))


def assert_gone(action, code="artifacts_deleted"):
    with pytest.raises(ApplicationError) as error:
        action()
    assert (error.value.status, error.value.code) == (410, code)


@pytest.mark.parametrize("status", ["completed", "failed", "canceled"])
def test_delete_terminal_idempotent_audited_and_durable(system, status):
    job = finished(system, status)
    before = job.updated_at
    response = delete(system, job)
    assert response.status_code == 200, response.text
    validate_response(system.app, response, "/api/v1/jobs/{job_id}/artifacts", "delete")
    assert response.json()["retention"]["state"] == "deleted"
    assert not response.json()["retention"]["purge_pending"]
    assert not job.working_dir.exists()
    assert delete(system, job).json() == response.json()
    assert job.updated_at == before and job.status == status
    rows = system.jobs._conn.execute("SELECT event,actor,reason FROM artifact_audit WHERE job_id=? ORDER BY event", (job.job_id,)).fetchall()
    assert rows == [("purged", "system:retention", "deleted"), ("unavailable", "user:1", "deleted")]
    assert "synthetic-private-name" not in str(rows)
    other = JobStore(system.jobs.root_dir)
    try:
        assert JobApplication(other).delete_artifacts(OWNER, job.job_id).model_dump(mode="json") == response.json()
        assert other.get(job.job_id).updated_at == before
        assert other.get(job.job_id).status == status
    finally:
        other._conn.close()
    for suffix in ("text", "report", "artifact/content"):
        result = system.client.get(f"/api/v1/jobs/{job.job_id}/{suffix}", headers=bearer(system.tokens[0]))
        assert result.status_code == 410 and result.json()["error"]["code"] == "artifacts_deleted"
        validate_response(system.app, result, "/api/v1/jobs/{job_id}/" + suffix)
    for suffix in ("", "/artifact"):
        result = system.client.get(f"/api/v1/jobs/{job.job_id}{suffix}", headers=bearer(system.tokens[0]))
        assert result.status_code == 200
        assert result.json()["retention"]["state"] == "deleted"
        assert result.json()["download_url"] is None
    # Neither existence nor removal can be discovered by a different owner.
    assert delete(system, job, system.tokens[1]).status_code == 404


@pytest.mark.parametrize("status", ["queued", "processing", "cancel_requested"])
def test_active_jobs_cannot_be_removed_or_expired(system, status):
    job = finished(system, status)
    assert job.artifacts_expires_at is None
    assert delete(system, job).status_code == 409
    assert not system.jobs.cleanup_expired(retention_seconds=-1)
    assert job.input_pdf_path.is_file() and job.status == status


def test_scoped_service_deletion_does_not_require_read_or_submit(system):
    service_id = system.services[0]["id"]
    job = system.jobs.create("service.pdf", pdf(), owner_principal_id=service_id)
    system.jobs.update(job.job_id, status="failed")
    assert delete(system, job, system.tokens[2]).status_code == 403
    token = system.identities.issue_credential(service_id, "Delete only", ["artifacts:delete"], 3600, "user:1")[1]
    assert delete(system, job, token).status_code == 200
    assert system.client.delete("/api/v1/jobs/" + "f" * 32 + "/artifacts", headers=bearer(token)).status_code == 404
    assert system.client.get(f"/api/v1/jobs/{job.job_id}", headers=bearer(token)).status_code == 403


def test_deadline_fixed_at_terminal_transition_and_backfilled_once(system, monkeypatch):
    monkeypatch.setattr(settings, "job_retention_seconds", 1234)
    job = finished(system)
    deadline = job.artifacts_expires_at
    assert deadline == job.updated_at + timedelta(seconds=1234)
    system.jobs.update(job.job_id, progress_percent=100)
    assert job.artifacts_expires_at == deadline
    monkeypatch.setattr(settings, "job_retention_seconds", 9999)
    other = JobStore(system.jobs.root_dir)
    try:
        assert other.get(job.job_id).artifacts_expires_at == deadline
    finally:
        other._conn.close()
    # A pre-AM11 terminal row gets one migration deadline based on its old timestamp.
    system.jobs._conn.execute("UPDATE jobs SET artifacts_expires_at=NULL WHERE job_id=?", (job.job_id,))
    system.jobs._conn.commit()
    other = JobStore(system.jobs.root_dir)
    try:
        assert other.get(job.job_id).artifacts_expires_at == job.updated_at + timedelta(seconds=9999)
    finally:
        other._conn.close()


def test_original_schema_migration_preserves_job(tmp_path):
    root = tmp_path / "old"
    root.mkdir()
    conn = sqlite3.connect(root / "jobs.sqlite3")
    conn.execute(_CREATE_TABLE_SQL)
    now = datetime.now(timezone.utc)
    job_id = "a" * 32
    conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, "old.pdf", str(root / job_id), "completed", 1, 100,
                 now.isoformat(), now.isoformat(), str(root / job_id / "original.pdf"), None, None, None))
    conn.commit()
    conn.close()
    store = JobStore(root)
    try:
        job = store.get(job_id)
        assert job.effective_owner == "user:1" and job.status == "completed"
        assert job.artifacts_expires_at == now + timedelta(seconds=settings.job_retention_seconds)
    finally:
        store._conn.close()


def test_expiry_blocks_reads_before_collection_and_never_renews(system):
    job = finished(system)
    expire(system.jobs, job)
    service = JobApplication(system.jobs)
    for operation in (service.text, service.report, service.artifact_download):
        assert_gone(lambda: operation(OWNER, job.job_id), "artifacts_expired")
    assert job.working_dir.exists()
    assert service.get_job(OWNER, job.job_id).status == "completed"
    assert service.artifact(OWNER, job.job_id).retention.purge_pending
    first = delete(system, job).json()
    assert first["retention"]["state"] == "expired"
    assert delete(system, job).json() == first
    assert system.jobs.cleanup_expired() == []
    assert not job.working_dir.exists()


@pytest.mark.parametrize("headers,status,expected", [([], 200, None), ([(b"range", b"bytes=10-29")], 206, (10, 30)),
                                                   ([(b"range", b"bytes=999999999-")], 416, False)])
def test_inflight_full_range_and_invalid_range_release_lease(system, headers, status, expected):
    job = finished(system)
    data = job.output_pdf_path.read_bytes()
    response = LeasedFileResponse(system.jobs, job.job_id, "user:1", filename="result.pdf")
    messages = []

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.start":
            receipt = JobApplication(system.jobs).delete_artifacts(OWNER, job.job_id)
            assert receipt.retention.purge_pending and job.working_dir.exists()
            assert_gone(lambda: JobApplication(system.jobs).artifact_download(OWNER, job.job_id))

    asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}, "method": "GET", "headers": headers, "extensions": {"http.response.pathsend": {}}}, None, send))
    assert messages[0]["status"] == status
    assert not any(m["type"] == "http.response.pathsend" for m in messages)
    body = b"".join(m.get("body", b"") for m in messages)
    if expected is not False:
        assert body == (data if expected is None else data[slice(*expected)])
    assert not system.jobs._artifact_leases and not job.working_dir.exists()


@pytest.mark.parametrize("failure", [OSError, asyncio.CancelledError])
def test_send_failure_or_cancel_releases_last_download_lease(system, failure):
    job = finished(system)
    response = LeasedFileResponse(system.jobs, job.job_id, "user:1", filename="result.pdf")
    JobApplication(system.jobs).delete_artifacts(OWNER, job.job_id)

    async def send(_):
        raise failure()

    with pytest.raises(failure):
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}, "method": "GET", "headers": []}, None, send))
    assert not job.working_dir.exists() and not system.jobs._artifact_leases


def test_multiple_leases_last_reader_purges_and_restart_recovers_tombstone(system):
    job = finished(system)
    for _ in range(2):
        system.jobs.acquire_artifacts(job.job_id, "user:1")
    receipt = JobApplication(system.jobs).delete_artifacts(OWNER, job.job_id)
    assert receipt.retention.purge_pending
    system.jobs.release_artifacts(job.job_id)
    assert job.working_dir.exists()
    # Simulate process death: runtime leases are gone but the tombstone is durable.
    other = JobStore(system.jobs.root_dir)
    try:
        assert_gone(lambda: other.acquire_artifacts(job.job_id, "user:1"))
        other.cleanup_expired()
        assert other.get(job.job_id).artifacts_purged_at
        assert not job.working_dir.exists()
    finally:
        other._conn.close()
    system.jobs.release_artifacts(job.job_id)


@pytest.mark.parametrize("operation,reader", [("text", "read_page"), ("report", "read_report")])
def test_result_read_already_admitted_finishes_during_deletion(system, monkeypatch, operation, reader):
    import app.application as application
    job = finished(system)
    original = getattr(application, reader)
    entered, proceed = Event(), Event()

    def gated(*args):
        entered.set()
        assert proceed.wait(5)
        return original(*args)

    monkeypatch.setattr(application, reader, gated)
    service = JobApplication(system.jobs)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(getattr(service, operation), OWNER, job.job_id)
        try:
            assert entered.wait(5)
            assert service.delete_artifacts(OWNER, job.job_id).retention.purge_pending
            assert job.working_dir.exists()
        finally:
            proceed.set()
        assert future.result().job_id == job.job_id
    assert not job.working_dir.exists()


def test_filesystem_failure_keeps_tombstone_and_retries(system, monkeypatch):
    import app.services.artifact_retention as retention
    job = finished(system)
    original = retention.safe_rmtree
    monkeypatch.setattr(retention, "safe_rmtree", lambda *args: (_ for _ in ()).throw(PermissionError("PRIVATE path")))
    first = delete(system, job)
    assert first.status_code == 200 and first.json()["retention"]["purge_pending"]
    assert job.working_dir.exists()
    assert_gone(lambda: JobApplication(system.jobs).artifact_download(OWNER, job.job_id))
    assert delete(system, job).json() == first.json()
    monkeypatch.setattr(retention, "safe_rmtree", original)
    system.jobs.cleanup_expired()
    assert not job.working_dir.exists()
    assert system.jobs._conn.execute("SELECT COUNT(*) FROM artifact_audit").fetchone()[0] == 2


def test_failed_tombstone_commit_never_removes_files_or_mutates_cache(system, monkeypatch):
    job = finished(system)
    output = job.output_pdf_path
    original = system.jobs._persist

    def fail(record, **kwargs):
        original(record, **kwargs)
        raise sqlite3.OperationalError("synthetic write failure")

    monkeypatch.setattr(system.jobs, "_persist", fail)
    with pytest.raises(sqlite3.OperationalError):
        JobApplication(system.jobs).delete_artifacts(OWNER, job.job_id)
    assert job.artifacts_removed_at is None and job.output_pdf_path == output and output.is_file()
    assert system.jobs._conn.execute("SELECT artifacts_removed_at FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()[0] is None
    assert system.jobs._conn.execute("SELECT COUNT(*) FROM artifact_audit").fetchone()[0] == 0


def test_failed_purge_metadata_commit_is_retried_without_masking_transfer(system, monkeypatch):
    job = finished(system)
    system.jobs.acquire_artifacts(job.job_id, "user:1")
    JobApplication(system.jobs).delete_artifacts(OWNER, job.job_id)
    original = system.jobs._persist

    def fail(record, **kwargs):
        if record.artifacts_purged_at:
            raise sqlite3.OperationalError("synthetic write failure")
        return original(record, **kwargs)

    monkeypatch.setattr(system.jobs, "_persist", fail)
    system.jobs.release_artifacts(job.job_id)
    assert not job.working_dir.exists() and job.artifacts_purged_at is None
    monkeypatch.setattr(system.jobs, "_persist", original)
    system.jobs.cleanup_expired()
    assert job.artifacts_purged_at


def test_purge_rejects_outside_directory_and_linked_descendants(system, tmp_path):
    job = finished(system)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "preserve.txt"
    sentinel.write_text("keep")
    with pytest.raises(OSError):
        safe_rmtree(system.jobs.root_dir, outside, job.job_id)
    link = job.working_dir / "linked"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        assert delete(system, job).json()["retention"]["purge_pending"]
        assert sentinel.read_text() == "keep"
    finally:
        if os.name == "nt":
            link.rmdir()  # Remove only the junction itself, never traverse it.
        else:
            link.unlink()
    system.jobs.cleanup_expired()
    assert not job.working_dir.exists() and sentinel.read_text() == "keep"


def test_retry_cannot_read_logically_removed_input_but_prior_receipt_replays(system):
    job = finished(system)
    service = ControlService(system.uploads)
    body = RetryInput(options={"processing_mode": "extract_text"})
    first, replayed = service.retry(OWNER, job.job_id, body, "retention-retry-key")
    assert not replayed
    system.jobs.acquire_artifacts(job.job_id, "user:1")
    try:
        JobApplication(system.jobs).delete_artifacts(OWNER, job.job_id)
        assert job.input_pdf_path.exists()
        assert_gone(lambda: service.retry(OWNER, job.job_id, body, "new-retention-key"))
        replay, replayed = service.retry(OWNER, job.job_id, body, "retention-retry-key")
        assert replayed and replay == first
    finally:
        system.jobs.release_artifacts(job.job_id)


@pytest.mark.parametrize("reason", ["delete", "expire"])
def test_existing_export_invalidated_but_active_transfer_finishes(bulk, reason):
    job = bulk_job(bulk)
    admission = create_export(bulk, [job.job_id]).json()
    ready = wait_export(bulk, admission)
    receipt, source = bulk.exports.open_download(OWNER, ready["export_id"])
    path = bulk.exports.root / (receipt.export_id + ".zip")
    try:
        if reason == "delete":
            delete(bulk, job)
        else:
            expire(bulk.jobs, job)
        assert_gone(lambda: bulk.exports.open_download(OWNER, receipt.export_id), "export_source_removed")
        bulk.exports._expire()
        assert path.exists()
        assert hashlib.sha256(source.read()).hexdigest() == receipt.sha256
    finally:
        bulk.exports.close_download(receipt.export_id, source)
    bulk.exports._expire()
    assert not path.exists()
    assert create_export(bulk, [job.job_id]).status_code == 410
    fresh = wait_export(bulk, create_export(bulk, [job.job_id], key="new-snapshot-key").json())
    assert fresh["status"] == "ready" and not fresh["items"][0]["included"]
    assert fresh["items"][0]["reason"] == "artifact_unavailable"


def test_periodic_collector_jobs_uploads_exports_and_stop(system, monkeypatch):
    monkeypatch.setattr(settings, "retention_interval_seconds", 1)
    exports = ExportService(system.jobs)
    worker = RetentionWorker(system.uploads, exports)
    worker.start()
    try:
        job = finished(system)
        ready = upload(system)
        with system.jobs._lock, system.jobs._conn:
            system.jobs._conn.execute("UPDATE uploads SET expires_at=0 WHERE upload_id=?", (ready["upload_id"],))
        expire(system.jobs, job)
        deadline = time.monotonic() + 5
        while job.working_dir.exists() or (system.uploads.root / (ready["upload_id"] + ".pdf")).exists():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert retention_info(job).state == "expired" and job.artifacts_purged_at
        assert system.jobs._conn.execute("SELECT state FROM uploads WHERE upload_id=?", (ready["upload_id"],)).fetchone()[0] == "expired"
    finally:
        worker.stop()
    assert worker._thread is None


def test_startup_reconciles_only_own_staging_and_runtime_sweep_preserves_active(system):
    worker = RetentionWorker(system.uploads, ExportService(system.jobs))
    stale = system.jobs.root_dir / "legacy-batch-ab12_cd3"
    unknown = system.jobs.root_dir / "user-documents"
    for path in (stale, unknown):
        path.mkdir()
        (path / "keep.pdf").write_bytes(pdf())
    worker.reconcile_startup()
    assert not stale.exists() and unknown.exists()
    stale.mkdir()
    worker.collect()
    assert stale.exists()


def test_collector_failure_isolated_and_receiving_upload_not_expired(system, monkeypatch):
    ready = upload(system)
    session = system.uploads.begin(OWNER, ready["upload_id"])
    exports = ExportService(system.jobs)
    worker = RetentionWorker(system.uploads, exports)
    called = []
    monkeypatch.setattr(system.jobs, "cleanup_expired", lambda: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(exports, "_expire", lambda: called.append(True))
    with system.jobs._lock, system.jobs._conn:
        system.jobs._conn.execute("UPDATE uploads SET expires_at=0 WHERE upload_id=?", (ready["upload_id"],))
    try:
        worker.collect()
        assert called == [True]
        assert system.jobs._conn.execute("SELECT state FROM uploads WHERE upload_id=?", (ready["upload_id"],)).fetchone()[0] == "receiving"
    finally:
        session.close()


def test_asgi_disconnect_releases_download_lease(system):
    job = finished(system)
    response = LeasedFileResponse(system.jobs, job.job_id, "user:1", filename="result.pdf")

    async def scenario():
        started = asyncio.Event()

        async def send(message):
            if message["type"] == "http.response.start":
                assert JobApplication(system.jobs).delete_artifacts(OWNER, job.job_id).retention.purge_pending
                started.set()
                await asyncio.Event().wait()

        async def receive():
            await started.wait()
            return {"type": "http.disconnect"}

        await response({"type": "http", "method": "GET", "headers": []}, receive, send)

    asyncio.run(scenario())
    assert not job.working_dir.exists() and not system.jobs._artifact_leases


@pytest.mark.parametrize("transport,mode", [("http", "auto"), ("http", "legacy"), ("stdio", "auto"), ("stdio", "legacy")])
def test_real_mcp_delete_http_410_owner_isolation_and_restart(runtime, transport, mode):
    runtime.start()

    async def scenario():
        async with connected(runtime, transport, mode) as client:
            data = pdf()
            receipt = await call(client, "ocr_prepare_upload", {"filename": "synthetic.pdf", "size_bytes": len(data)})
            async with httpx2.AsyncClient(base_url=runtime.origin, headers=bearer(runtime.tokens[0]), trust_env=False) as http:
                assert (await http.put(receipt["content_url"], content=data, headers={"Content-Type": "application/pdf"})).status_code == 200
                job = await call(client, "ocr_submit_job", {"upload_id": receipt["upload_id"], "idempotency_key": "retention-real-key", "options": {"processing_mode": "extract_text"}})
                job_id = job["job_id"]
                await complete(client, job_id)
                removed = await call(client, "ocr_delete_job_artifacts", {"job_id": job_id})
                assert removed["retention"]["state"] == "deleted" and not removed["retention"]["purge_pending"]
                assert await call(client, "ocr_delete_job_artifacts", {"job_id": job_id}) == removed
                assert (await http.delete(f"/api/v1/jobs/{job_id}/artifacts")).json() == removed
                for name in ("ocr_get_text", "ocr_get_report"):
                    error = await client.call_tool(name, {"job_id": job_id})
                    assert error.is_error and "artifacts_deleted" in error.content[0].text
                assert (await http.get(f"/api/v1/jobs/{job_id}/artifact/content")).status_code == 410
                assert (await call(client, "ocr_get_job", {"job_id": job_id}))["status"] == "completed"
                assert not (await call(client, "ocr_get_artifact", {"job_id": job_id}))["available"]
            async with connected(runtime, transport, mode, runtime.tokens[1]) as other:
                error = await other.call_tool("ocr_delete_job_artifacts", {"job_id": job_id})
                assert error.is_error and "job_not_found" in error.content[0].text
        runtime.stop()
        runtime.start()
        async with connected(runtime, transport, mode) as client:
            assert await call(client, "ocr_delete_job_artifacts", {"job_id": job_id}) == removed
        assert not (runtime.root / "stdio-must-not-create").exists()

    asyncio.run(scenario())
