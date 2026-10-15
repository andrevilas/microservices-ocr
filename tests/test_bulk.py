"""Batch atomicity, stable manifests and bounded ZIP export over HTTP/MCP/stdio."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
from io import BytesIO
import json
import time
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import httpx2
import pytest
from starlette.requests import ClientDisconnect

from app.application import ApplicationError, Principal
from app.bulk_models import BatchInput, ExportInput
from app.config import settings
from app.routes.api import get_orchestrator
from app.routes.bulk import ExportResponse
from app.services.batch_service import BatchService, get_batch_service
from app.services.export_service import ExportService, get_export_service
from app.services.ocr_orchestrator import OcrOrchestrator
from app.services.storage_service import JobStore, get_job_store
from app.services.upload_service import UploadService
from test_http_contract import validate_response
from test_mcp_vertical import runtime, connected, call
from test_upload_admission import bearer, pdf, system, upload, reserve

OWNER = Principal("user:1", 1, frozenset({"jobs:submit", "jobs:read", "artifacts:read"}))


@pytest.fixture
def bulk(system):
    system.batches = BatchService(system.jobs, system.uploads)
    system.exports = ExportService(system.jobs)
    system.app.dependency_overrides[get_batch_service] = lambda: system.batches
    system.app.dependency_overrides[get_export_service] = lambda: system.exports
    system.app.dependency_overrides[get_orchestrator] = lambda: SimpleNamespace(job_store=system.jobs)
    system.app.dependency_overrides[get_job_store] = lambda: system.jobs
    system.exports.start()
    try:
        yield system
    finally:
        system.exports.stop()


def batch_body(ids):
    return {"items": [{"client_item_id": f"item-{n}", "upload_id": upload_id,
                       "options": {"processing_mode": "extract_text"}} for n, upload_id in enumerate(ids)]}


def create_batch(bulk, body, key="batch-key-0001", token=None):
    return bulk.client.post("/api/v1/batches", json=body,
                            headers={**bearer(token or bulk.tokens[0]), "Idempotency-Key": key})


def create_export(bulk, ids, key="export-key-0001", token=None):
    return bulk.client.post("/api/v1/exports", json={"job_ids": ids},
                            headers={**bearer(token or bulk.tokens[0]), "Idempotency-Key": key})


def wait_export(bulk, admission):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = bulk.client.get("/api/v1/exports/" + admission["export_id"], headers=bearer(bulk.tokens[0]))
        assert response.status_code == 200, response.text
        if response.json()["status"] in ("ready", "failed"):
            return response.json()
        time.sleep(0.01)
    raise AssertionError("Export did not complete")


def job(bulk, name="same.pdf", status="completed", owner=1, data=None):
    record = bulk.jobs.create(name, pdf(), owner_user_id=owner)
    if status == "completed":
        path = record.working_dir / "result.pdf"
        path.write_bytes(data or pdf())
        bulk.jobs.update(record.job_id, status=status, output_pdf_path=path, progress_percent=100)
    else:
        bulk.jobs.update(record.job_id, status=status, error="PRIVATE" if status == "failed" else None)
    return record


@pytest.mark.parametrize("invalid", ["foreign", "missing", "not-ready", "expired", "options", "duplicate", "pending-quota", "size-quota"])
def test_mixed_batch_rejects_without_any_job(bulk, monkeypatch, invalid):
    good = upload(bulk)
    other = upload(bulk, token=bulk.tokens[1] if invalid == "foreign" else None)
    if invalid == "missing":
        other["upload_id"] = "f" * 32
    elif invalid == "not-ready":
        other = reserve(bulk, pdf())
    elif invalid == "expired":
        bulk.jobs._conn.execute("UPDATE uploads SET expires_at=0 WHERE upload_id=?", (other["upload_id"],))
        bulk.jobs._conn.commit()
    body = batch_body([good["upload_id"], other["upload_id"]])
    if invalid == "options":
        body["items"][1]["options"]["shell"] = "PRIVATE"
    elif invalid == "duplicate":
        body["items"][1]["client_item_id"] = body["items"][0]["client_item_id"]
    elif invalid == "pending-quota":
        monkeypatch.setattr(settings, "job_owner_max_pending", 1)
    elif invalid == "size-quota":
        monkeypatch.setattr(settings, "batch_max_total_mb", 0)
    response = create_batch(bulk, body)
    assert response.status_code in (404, 409, 410, 413, 422, 429), response.text
    validate_response(bulk.app, response, "/api/v1/batches", "post")
    assert "PRIVATE" not in response.text
    assert not bulk.jobs.snapshot()
    assert not bulk.queue.enqueued
    assert bulk.jobs._conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0


def test_batch_rollback_on_second_persist_failure(bulk, monkeypatch):
    ready = upload(bulk)
    original = bulk.jobs._persist
    calls = []
    def failing(record, **kwargs):
        calls.append(record.job_id)
        if len(calls) == 2:
            raise OSError("PRIVATE storage path")
        return original(record, **kwargs)
    monkeypatch.setattr(bulk.jobs, "_persist", failing)
    response = create_batch(bulk, batch_body([ready["upload_id"]] * 2))
    assert response.status_code == 503
    assert not bulk.jobs.snapshot() and not bulk.queue.enqueued
    assert bulk.jobs._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert not list(bulk.uploads.admissions.iterdir())


def test_batch_concurrency_replay_restart_manifest_and_independent_failure(bulk):
    ready = upload(bulk)
    body = BatchInput.model_validate(batch_body([ready["upload_id"]] * 2))
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(lambda _: bulk.batches.create(OWNER, body, "concurrent-batch"), range(16)))
    assert len({r.batch_id for r, replay in receipts}) == 1
    assert sum(not replay for _, replay in receipts) == 1
    receipt = receipts[0][0]
    assert len(bulk.jobs.snapshot()) == 2
    ids = [i.job_id for i in receipt.items]
    OcrOrchestrator(bulk.jobs).process_job(ids[0])
    bulk.jobs.update(ids[1], status="failed", error="PRIVATE", error_code="synthetic_failure", progress_percent=100)
    view = bulk.batches.get(OWNER, receipt.batch_id)
    assert view.terminal and view.counts == {"completed": 1, "failed": 1}
    assert view.progress_percent == 100 and view.items[1].error_code == "synthetic_failure"
    assert "PRIVATE" not in view.model_dump_json()
    replay = create_batch(bulk, body.model_dump(mode="json"), "concurrent-batch")
    assert replay.status_code == 202 and replay.json() == receipt.model_dump(mode="json")
    assert replay.headers["idempotency-replayed"] == "true"
    validate_response(bulk.app, replay, "/api/v1/batches", "post")
    bulk.jobs._conn.execute("UPDATE uploads SET expires_at=0")
    bulk.jobs._conn.commit()
    reopened = JobStore(bulk.jobs.root_dir)
    try:
        service = BatchService(reopened, UploadService(reopened))
        assert service.create(OWNER, body, "concurrent-batch")[0] == receipt
        assert service.get(OWNER, receipt.batch_id) == view
    finally:
        reopened._conn.close()
    body.items.reverse()
    assert create_batch(bulk, body.model_dump(mode="json"), "concurrent-batch").status_code == 409


def test_batch_list_cursor_isolation_and_queue_failure(bulk):
    ready = upload(bulk)
    def fail(_):
        raise RuntimeError("PRIVATE queue")
    bulk.queue.enqueue = fail
    body = batch_body([ready["upload_id"]])
    failed = create_batch(bulk, body)
    assert failed.status_code == 503 and "PRIVATE" not in failed.text
    bulk.queue.enqueue = bulk.queue.enqueued.append
    first = create_batch(bulk, body)
    second = create_batch(bulk, body, "second-batch")
    assert first.status_code == second.status_code == 202
    assert len(bulk.jobs.snapshot()) == 2
    response = bulk.client.get("/api/v1/batches?limit=1", headers=bearer(bulk.tokens[0]))
    validate_response(bulk.app, response, "/api/v1/batches")
    page = response.json()
    continuation = bulk.client.get("/api/v1/batches", params={"limit": 1, "cursor": page["next_cursor"]}, headers=bearer(bulk.tokens[0])).json()
    assert {page["items"][0]["batch_id"], continuation["items"][0]["batch_id"]} == {first.json()["batch_id"], second.json()["batch_id"]}
    assert continuation["next_cursor"] is None
    filtered = bulk.client.get("/api/v1/jobs", params={"batch_id": first.json()["batch_id"]}, headers=bearer(bulk.tokens[0]))
    assert [j["job_id"] for j in filtered.json()["items"]] == [first.json()["items"][0]["job_id"]]
    assert bulk.client.get("/api/v1/jobs", params={"batch_id": first.json()["batch_id"]}, headers=bearer(bulk.tokens[1])).status_code == 404
    assert bulk.client.get("/api/v1/batches", params={"cursor": page["next_cursor"]}, headers=bearer(bulk.tokens[1])).status_code == 422
    assert bulk.client.get("/api/v1/batches/" + first.json()["batch_id"], headers=bearer(bulk.tokens[1])).status_code == 404
    assert bulk.client.get("/api/v1/batches", headers=bearer(bulk.tokens[1])).json()["items"] == []


def test_zip_manifest_duplicates_exclusions_hashes_and_replay(bulk):
    records = [job(bulk), job(bulk), job(bulk, status="failed"), job(bulk)]
    bulk.jobs.cleanup(records[-1].job_id)
    ids = [j.job_id for j in records]
    response = create_export(bulk, ids)
    assert response.status_code == 202, response.text
    validate_response(bulk.app, response, "/api/v1/exports", "post")
    admission = response.json()
    receipt = wait_export(bulk, admission)
    assert receipt["status"] == "ready", receipt
    assert [i["included"] for i in receipt["items"]] == [True, True, False, False]
    assert [i["reason"] for i in receipt["items"][2:]] == ["job_not_completed", "artifact_unavailable"]
    download = bulk.client.get(receipt["download_url"], headers=bearer(bulk.tokens[0]))
    assert download.status_code == 200 and download.headers["content-type"] == "application/zip"
    assert len(download.content) == receipt["size_bytes"]
    assert hashlib.sha256(download.content).hexdigest() == receipt["sha256"]
    with ZipFile(BytesIO(download.content)) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)) == 3
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["items"] == receipt["items"]
        for item in receipt["items"][:2]:
            assert hashlib.sha256(archive.read(item["archive_name"])).hexdigest() == item["sha256"]
    assert not bulk.exports.leases
    replay = create_export(bulk, ids)
    assert replay.json() == admission and replay.headers["idempotency-replayed"] == "true"
    assert create_export(bulk, ids[::-1]).status_code == 409
    reopened = ExportService(bulk.jobs)
    assert reopened.get(OWNER, receipt["export_id"]).model_dump(mode="json") == receipt
    for path in (receipt["download_url"], "/api/v1/exports/" + receipt["export_id"]):
        assert bulk.client.get(path, headers=bearer(bulk.tokens[1])).status_code == 404


@pytest.mark.parametrize("invalid", ["foreign", "missing", "duplicate", "oversize", "quota"])
def test_export_rejected_atomically(bulk, monkeypatch, invalid):
    own = job(bulk)
    other = job(bulk, owner=2 if invalid == "foreign" else 1)
    ids = [own.job_id, other.job_id]
    if invalid == "missing":
        ids[1] = "f" * 32
    elif invalid == "duplicate":
        ids[1] = own.job_id
    elif invalid == "oversize":
        monkeypatch.setattr(settings, "export_max_total_mb", 0)
    elif invalid == "quota":
        monkeypatch.setattr(settings, "export_owner_limit_mb", 0)
    response = create_export(bulk, ids)
    assert response.status_code in (404, 413, 422, 429)
    assert not list(bulk.exports.root.iterdir())
    assert bulk.jobs._conn.execute("SELECT COUNT(*) FROM exports").fetchone()[0] == 0


def test_export_failure_releases_disk_and_does_not_commit(bulk, monkeypatch):
    record = job(bulk)
    def fail(*a, **kw):
        raise OSError("PRIVATE archive error")
    monkeypatch.setattr(ZipFile, "writestr", fail)
    response = create_export(bulk, [record.job_id])
    assert response.status_code == 202
    final = wait_export(bulk, response.json())
    assert final["status"] == "failed" and final["error_code"] == "export_failed"
    assert "PRIVATE" not in json.dumps(final)
    assert not list(bulk.exports.root.iterdir())
    assert bulk.jobs._conn.execute("SELECT COUNT(*) FROM exports").fetchone()[0] == 0


def test_export_all_excluded_still_has_explicit_manifest(bulk):
    record = job(bulk, status="queued")
    response = create_export(bulk, [record.job_id])
    assert response.status_code == 202
    final = wait_export(bulk, response.json())
    archive = bulk.client.get(final["download_url"], headers=bearer(bulk.tokens[0]))
    with ZipFile(BytesIO(archive.content)) as zipfile:
        assert zipfile.namelist() == ["manifest.json"]
        assert json.loads(zipfile.read("manifest.json"))["items"][0]["reason"] == "job_not_completed"


def test_expiry_lease_cleanup_and_disconnect(bulk):
    record = job(bulk)
    receipt = wait_export(bulk, create_export(bulk, [record.job_id]).json())
    export_id = receipt["export_id"]
    metadata, source = bulk.exports.open_download(OWNER, export_id)
    bulk.jobs._conn.execute("UPDATE exports SET expires_at=0")
    bulk.jobs._conn.commit()
    path = bulk.exports.root / f"{export_id}.zip"
    with bulk.jobs._lock:
        bulk.exports._expire()
    assert path.exists() and source.read(2) == b"PK"
    assert create_export(bulk, [record.job_id]).status_code == 410
    assert bulk.client.get(receipt["download_url"], headers=bearer(bulk.tokens[0])).status_code == 410
    async def disconnect():
        async def send(message):
            raise OSError("client disconnected before first byte")
        with pytest.raises(ClientDisconnect):
            await ExportResponse(bulk.exports, metadata, source)({"type": "http", "asgi": {"spec_version": "2.4"}}, None, send)
    asyncio.run(disconnect())
    assert source.closed and not bulk.exports.leases
    with bulk.jobs._lock:
        bulk.exports._expire()
    assert not path.exists()
    assert create_export(bulk, [record.job_id], "new-export-key").status_code == 202


def test_legacy_mixed_upload_creates_no_invisible_job(bulk):
    response = bulk.client.post("/api/jobs/batch", files=[("files", ("valid.pdf", pdf(), "application/pdf")),
                                                        ("files", ("bad.pdf", b"invalid", "application/pdf"))], headers=bearer(bulk.tokens[0]))
    assert response.status_code == 400
    assert not bulk.jobs.snapshot() and not bulk.queue.enqueued
    assert not list(bulk.jobs.root_dir.glob("legacy-batch-*"))


def test_legacy_dispatch_failure_returns_all_admitted_ids(bulk):
    def fail(_):
        raise RuntimeError("PRIVATE queue")
    bulk.queue.enqueue = fail
    response = bulk.client.post("/api/jobs/batch", files=[("files", ("same.pdf", pdf(), "application/pdf"))] * 2, headers=bearer(bulk.tokens[0]))
    assert response.status_code == 202
    assert response.json()["dispatch_pending"] and response.json()["batch_id"]
    assert {j["job_id"] for j in response.json()["jobs"]} == {j.job_id for j in bulk.jobs.snapshot()}
    assert all(j.options_requested is None for j in bulk.jobs.snapshot())


def test_legacy_zip_names_are_unique_and_manifest_only_contains_owned_jobs(bulk):
    records = [job(bulk), job(bulk), job(bulk, owner=2)]
    response = bulk.client.get("/api/jobs/download-batch", params=[("job_ids", j.job_id) for j in records], headers=bearer(bulk.tokens[0]))
    assert response.status_code == 200
    with ZipFile(BytesIO(response.content)) as archive:
        assert len(archive.namelist()) == len(set(archive.namelist())) == 3
        manifest = json.loads(archive.read("manifest.json"))
        assert {i["job_id"] for i in manifest["items"]} == {r.job_id for r in records[:2]}


def test_legacy_zip_generated_name_cannot_collide_with_client_filename(bulk):
    records = [job(bulk) for _ in range(4)]
    records[0].filename = f"004-{records[3].job_id}-1.pdf"
    records[1].filename = "Duplicate.pdf"
    records[2].filename = "duplicate.pdf"
    records[3].filename = "DUPLICATE.pdf"
    response = bulk.client.get("/api/jobs/download-batch", params=[("job_ids", j.job_id) for j in records], headers=bearer(bulk.tokens[0]))
    assert response.status_code == 200
    with ZipFile(BytesIO(response.content)) as archive:
        assert len(archive.namelist()) == len({n.casefold() for n in archive.namelist()}) == 5
        manifest = json.loads(archive.read("manifest.json"))
        for item in manifest["items"]:
            assert hashlib.sha256(archive.read(item["archive_name"])).hexdigest() == item["sha256"]


def test_legacy_zip_backpressure_releases_slots(bulk):
    from app.routes.api import _legacy_zip_slots
    record = job(bulk)
    _legacy_zip_slots.acquire()
    _legacy_zip_slots.acquire()
    try:
        response = bulk.client.get("/api/jobs/download-batch", params={"job_ids": record.job_id}, headers=bearer(bulk.tokens[0]))
        assert response.status_code == 429 and response.headers["retry-after"] == "5"
    finally:
        _legacy_zip_slots.release()
        _legacy_zip_slots.release()
    for _ in range(3):
        assert bulk.client.get("/api/jobs/download-batch", params={"job_ids": record.job_id}, headers=bearer(bulk.tokens[0])).status_code == 200


@pytest.mark.parametrize("scopes", [["jobs:read"], ["artifacts:read"], ["jobs:submit"]])
def test_export_scopes_do_not_grant_implicit_permissions(bulk, scopes):
    token = bulk.identities.issue_credential(bulk.services[0]["id"], "Restricted", scopes, 3600, "user:1")[1]
    record = job(bulk)
    response = create_export(bulk, [record.job_id], token=token)
    assert response.status_code == 403
    assert not list(bulk.exports.root.iterdir())


def test_batch_scopes_and_malformed_cursors(bulk):
    token = bulk.identities.issue_credential(bulk.services[0]["id"], "Reader", ["jobs:read"], 3600, "user:1")[1]
    ready = upload(bulk)
    assert create_batch(bulk, batch_body([ready["upload_id"]]), token=token).status_code == 403
    for cursor in ("bad", "W10=", "e30=", "0" * 513):
        response = bulk.client.get("/api/v1/batches", params={"cursor": cursor}, headers=bearer(bulk.tokens[0]))
        assert response.status_code == 422


def test_export_admission_is_async_concurrent_and_recovers_processing(bulk):
    bulk.exports.stop()
    record = job(bulk)
    body = ExportInput(job_ids=[record.job_id])
    with ThreadPoolExecutor(max_workers=8) as pool:
        admissions = list(pool.map(lambda _: bulk.exports.create(OWNER, body, "concurrent-export"), range(16)))
    assert len({a.export_id for a, replay in admissions}) == 1
    assert sum(not replay for _, replay in admissions) == 1
    admission = admissions[0][0]
    assert admission.status == "queued" and admission.download_url is None
    assert not list(bulk.exports.root.glob("*.zip"))
    path = "/api/v1/exports/" + admission.export_id + "/content"
    assert bulk.client.get(path, headers=bearer(bulk.tokens[0])).status_code == 409
    # Persisted processing with no worker alive models a crash before publication.
    bulk.jobs._conn.execute("UPDATE export_requests SET state='processing'")
    bulk.jobs._conn.commit()
    recovered = ExportService(bulk.jobs)
    assert recovered.get(OWNER, admission.export_id).status == "queued"
    recovered.start()
    try:
        deadline = time.monotonic() + 10
        while (view := recovered.get(OWNER, admission.export_id)).status not in ("ready", "failed") and time.monotonic() < deadline:
            time.sleep(0.01)
        assert view.status == "ready"
        assert recovered.create(OWNER, body, "concurrent-export")[0] == admission
        assert len(list(recovered.root.glob("*.zip"))) == 1
    finally:
        recovered.stop()


def test_pending_export_queue_is_bounded(bulk, monkeypatch):
    bulk.exports.stop()
    record = job(bulk)
    monkeypatch.setattr(settings, "export_owner_max_pending", 1)
    assert create_export(bulk, [record.job_id]).status_code == 202
    response = create_export(bulk, [record.job_id], "another-export")
    assert response.status_code == 429 and response.json()["error"]["code"] == "export_queue_full"


@pytest.mark.parametrize("transport,mode", [("http", "auto"), ("http", "legacy"), ("stdio", "auto"), ("stdio", "legacy")])
def test_real_mcp_batch_to_zip(runtime, transport, mode):
    runtime.start()
    async def scenario():
        async with connected(runtime, transport, mode) as client:
            data = pdf(2)
            receipt = await call(client, "ocr_prepare_upload", {"filename": "same.pdf", "size_bytes": len(data)})
            async with httpx2.AsyncClient(base_url=runtime.origin, headers=bearer(runtime.tokens[0]), trust_env=False) as http:
                assert (await http.put(receipt["content_url"], content=data, headers={"Content-Type": "application/pdf"})).status_code == 200
                args = {**batch_body([receipt["upload_id"]] * 2), "idempotency_key": "mcp-batch-key"}
                batch = await call(client, "ocr_create_batch", args)
                assert await call(client, "ocr_create_batch", args) == batch
                replay = await http.post("/api/v1/batches", json={"items": args["items"]}, headers={"Idempotency-Key": args["idempotency_key"]})
                assert replay.status_code == 202 and replay.json() == batch
                async with asyncio.timeout(30):
                    while not (view := await call(client, "ocr_get_batch", {"batch_id": batch["batch_id"]}))["terminal"]:
                        await asyncio.sleep(0.1)
                assert view["counts"] == {"completed": 2}
                assert len((await call(client, "ocr_list_batches"))["items"]) == 1
                assert len((await call(client, "ocr_list_jobs", {"batch_id": batch["batch_id"]}))["items"]) == 2
                export_args = {"job_ids": [i["job_id"] for i in batch["items"]], "idempotency_key": "mcp-export-key"}
                admission = await call(client, "ocr_create_export", export_args)
                assert admission["status"] == "queued"
                assert await call(client, "ocr_create_export", export_args) == admission
                async with asyncio.timeout(30):
                    while (exported := await call(client, "ocr_get_export", {"export_id": admission["export_id"]}))["status"] not in ("ready", "failed"):
                        await asyncio.sleep(0.1)
                assert exported["status"] == "ready", exported
                downloaded = await http.get(exported["download_url"])
                assert downloaded.status_code == 200 and hashlib.sha256(downloaded.content).hexdigest() == exported["sha256"]
                with ZipFile(BytesIO(downloaded.content)) as archive:
                    assert len(set(archive.namelist())) == 3
                    assert len(json.loads(archive.read("manifest.json"))["items"]) == 2
            async with connected(runtime, transport, mode, runtime.tokens[1]) as other:
                for name, fields in [("ocr_get_batch", {"batch_id": batch["batch_id"]}), ("ocr_get_export", {"export_id": exported["export_id"]}),
                                     ("ocr_create_export", export_args)]:
                    denied = await other.call_tool(name, fields)
                    assert denied.is_error and "not_found" in denied.content[0].text
    asyncio.run(scenario())


def test_real_export_crash_recovery_preserves_admission(runtime):
    runtime.start(export_pause=True)
    async def prepare():
        async with connected(runtime, "http", "auto") as client:
            data = pdf()
            ready = await call(client, "ocr_prepare_upload", {"filename": "restart.pdf", "size_bytes": len(data)})
            async with httpx2.AsyncClient(base_url=runtime.origin, headers=bearer(runtime.tokens[0]), trust_env=False) as http:
                assert (await http.put(ready["content_url"], content=data, headers={"Content-Type": "application/pdf"})).status_code == 200
            batch = await call(client, "ocr_create_batch", {**batch_body([ready["upload_id"]]), "idempotency_key": "restart-batch"})
            async with asyncio.timeout(30):
                while not (await call(client, "ocr_get_batch", {"batch_id": batch["batch_id"]}))["terminal"]:
                    await asyncio.sleep(0.1)
            args = {"job_ids": [batch["items"][0]["job_id"]], "idempotency_key": "restart-export"}
            admission = await call(client, "ocr_create_export", args)
            async with asyncio.timeout(10):
                while not (runtime.root / "processing-export").exists():
                    await asyncio.sleep(0.05)
            return args, admission
    args, admission = asyncio.run(prepare())
    runtime.stop()
    runtime.start()
    async def recover():
        async with connected(runtime, "stdio", "legacy") as client:
            async with asyncio.timeout(20):
                while (ready := await call(client, "ocr_get_export", {"export_id": admission["export_id"]}))["status"] not in ("ready", "failed"):
                    await asyncio.sleep(0.1)
            assert ready["status"] == "ready"
            assert await call(client, "ocr_create_export", args) == admission
            async with httpx2.AsyncClient(base_url=runtime.origin, headers=bearer(runtime.tokens[0]), trust_env=False) as http:
                output = await http.get(ready["download_url"])
                assert hashlib.sha256(output.content).hexdigest() == ready["sha256"]
    asyncio.run(recover())
