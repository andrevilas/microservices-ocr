"""Streaming upload, resource limits, ownership and durable exactly-one admission."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
from io import BytesIO
import subprocess
import sys
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
import pytest
from reportlab.pdfgen import canvas
from starlette.requests import Request, ClientDisconnect

from app.application import ApplicationError, JobApplication, Principal
from app.config import settings
from app.main import create_app
from app.routes.uploads import receive_upload
from app.services.auth_service import create_token
from app.services.identity_store import IdentityStore
from app.services.job_queue import JobQueueProcessor, get_job_queue_processor
from app.services.storage_service import JobStore
from app.services.upload_service import CHUNK, MIB, UploadService, UploadSession, get_upload_service, validate_pdf


def pdf(pages=1, size=(595, 842)):
    buffer = BytesIO()
    page = canvas.Canvas(buffer, pagesize=size)
    for n in range(pages):
        page.drawString(40, 500, f"Synthetic upload page {n}")
        page.showPage()
    page.save()
    return buffer.getvalue()


def bearer(token):
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def system(tmp_path, monkeypatch):
    import app.security as security
    users = {n: {"id": n, "role": "user", "email": f"user{n}@test.invalid"} for n in (1, 2)}
    monkeypatch.setattr(security, "get_user_store", lambda: SimpleNamespace(get_user_by_id=users.get))
    identities = IdentityStore(tmp_path / "identities.sqlite3")
    monkeypatch.setattr(security, "get_identity_store", lambda: identities)
    human_tokens = [create_token(n, users[n]["email"], "user", settings.jwt_secret) for n in users]
    services = [identities.create_service(f"Service {n}", "user:1") for n in (1, 2)]
    service_tokens = [identities.issue_credential(s["id"], "Uploader", ["jobs:read", "jobs:submit", "artifacts:read"], 3600, "user:1")[1] for s in services]
    jobs = JobStore(tmp_path / "data")
    uploads = UploadService(jobs)
    queue = SimpleNamespace(enqueued=[], enqueue=lambda job_id: None)
    queue.enqueue = queue.enqueued.append
    app = create_app(lambda: JobApplication(jobs))
    app.dependency_overrides[get_upload_service] = lambda: uploads
    app.dependency_overrides[get_job_queue_processor] = lambda: queue
    client = TestClient(app)
    yield SimpleNamespace(app=app, client=client, jobs=jobs, uploads=uploads, queue=queue, tokens=human_tokens+service_tokens,
                          identities=identities, services=services, tmp=tmp_path)
    client.close()
    identities.close()
    if jobs._conn:
        jobs._conn.close()


def reserve(system, data, token=None, **extra):
    response = system.client.post("/api/v1/upload-sessions", json={"filename": "example.pdf", "size_bytes": len(data), **extra}, headers=bearer(token or system.tokens[0]))
    assert response.status_code == 201, response.text
    return response.json()


def upload(system, data=None, token=None):
    data = data or pdf()
    token = token or system.tokens[0]
    receipt = reserve(system, data, token)
    response = system.client.put(receipt["content_url"], content=data, headers={**bearer(token), "Content-Type": "application/pdf"})
    assert response.status_code == 200, response.text
    return response.json()


def submit(system, upload_id, key="same-key-0001", token=None):
    return system.client.post("/api/v1/jobs", json={"upload_id": upload_id}, headers={**bearer(token or system.tokens[0]), "Idempotency-Key": key})


def test_four_identities_upload_and_submit_isolated(system):
    receipts = [upload(system, token=token) for token in system.tokens]
    ids = []
    for n, token in enumerate(system.tokens):
        for m, receipt in enumerate(receipts):
            response = system.client.get(f"/api/v1/uploads/{receipt['upload_id']}", headers=bearer(token))
            assert response.status_code == (200 if n == m else 404)
            if n != m:
                assert submit(system, receipt["upload_id"], token=token).status_code == 404
        response = submit(system, receipts[n]["upload_id"], token=token)
        assert response.status_code == 202, response.text
        ids.append(response.json()["job_id"])
        for m, other in enumerate(system.tokens):
            assert system.client.get(f"/api/v1/jobs/{ids[-1]}", headers=bearer(other)).status_code == (200 if n == m else 404)
    assert len(set(ids)) == 4  # Same idempotency key belongs independently to each owner.


def test_raw_and_multipart_receipts_are_durable(system):
    data = pdf(2)
    receipt = reserve(system, data, sha256=hashlib.sha256(data).hexdigest())
    response = system.client.put(receipt["content_url"], content=(data[n:n+97] for n in range(0, len(data), 97)), headers={**bearer(system.tokens[0]), "Content-Type": "application/pdf"})
    assert response.status_code == 200, response.text
    assert response.json()["page_count"] == 2
    assert response.json()["sha256"] == hashlib.sha256(data).hexdigest()
    direct = system.client.post("/api/v1/uploads", files={"file": ("direct.pdf", data, "application/pdf")}, headers=bearer(system.tokens[0]))
    assert direct.status_code == 201, direct.text
    assert direct.json()["sha256"] == response.json()["sha256"]
    assert direct.json()["filename"] == "direct.pdf"
    assert not list(system.uploads.root.glob("*.part"))
    system.jobs._conn.close()
    reopened = JobStore(system.jobs.root_dir)
    try:
        service = UploadService(reopened)
        saved = service.get(Principal("user:1", 1, frozenset({"jobs:submit"})), receipt["upload_id"])
        assert saved.sha256 == response.json()["sha256"]
        assert saved.state == "ready"
    finally:
        reopened._conn.close()


def test_idempotency_same_response_conflict_and_no_reprocessing(system):
    receipt = upload(system)
    first = submit(system, receipt["upload_id"])
    assert first.status_code == 202
    assert first.headers["idempotency-replayed"] == "false"
    system.jobs.update(first.json()["job_id"], status="completed")
    replay = submit(system, receipt["upload_id"])
    assert replay.json() == first.json()
    assert replay.headers["idempotency-replayed"] == "true"
    assert len(system.queue.enqueued) == 1
    other = upload(system)
    assert submit(system, other["upload_id"]).status_code == 409
    assert len(system.jobs.snapshot()) == 1
    # The receipt remains replayable after upload expiry; no new job can use it.
    system.jobs._conn.execute("UPDATE uploads SET expires_at=0")
    system.jobs._conn.commit()
    assert submit(system, receipt["upload_id"]).json() == first.json()
    assert submit(system, receipt["upload_id"], key="another-key").status_code == 410


def test_concurrent_duplicate_admission_creates_one_job(system):
    receipt = upload(system)
    principal = Principal("user:1", 1, frozenset({"jobs:submit"}))
    with ThreadPoolExecutor(max_workers=16) as pool:
        responses = list(pool.map(lambda _: system.uploads.submit(principal, receipt["upload_id"], "concurrent-key"), range(32)))
    assert len({r.job_id for r, _ in responses}) == 1
    assert sum(not replay for _, replay in responses) == 1
    assert len(system.jobs.snapshot()) == 1
    assert system.jobs._conn.execute("SELECT COUNT(*) FROM job_admissions").fetchone()[0] == 1


def test_dispatch_failure_retries_same_persisted_job(system):
    receipt = upload(system)
    def unavailable(_):
        raise RuntimeError("PRIVATE queue details")
    system.queue.enqueue = unavailable
    first = submit(system, receipt["upload_id"])
    assert first.status_code == 503
    assert "PRIVATE" not in first.text
    job_id = system.jobs.snapshot()[0].job_id
    system.queue.enqueue = system.queue.enqueued.append
    replay = submit(system, receipt["upload_id"])
    assert replay.status_code == 202 and replay.json()["job_id"] == job_id
    assert replay.headers["idempotency-replayed"] == "true"


def test_atomic_rollback_and_crash_recovery(system, monkeypatch):
    receipt = upload(system)
    principal = Principal("user:1", 1, frozenset({"jobs:submit"}))
    original = system.jobs._persist
    def fail_after_insert(job, *, commit=True):
        original(job, commit=commit)
        raise RuntimeError("simulated failure before receipt commit")
    with monkeypatch.context() as m:
        m.setattr(system.jobs, "_persist", fail_after_insert)
        with pytest.raises(RuntimeError):
            system.uploads.submit(principal, receipt["upload_id"], "crash-key-01")
    assert not system.jobs.snapshot()
    assert system.jobs._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
    assert not list(system.uploads.admissions.iterdir())
    system.jobs._conn.close()
    code = """
import os, sys
from pathlib import Path
from app.application import Principal
from app.services.storage_service import JobStore
from app.services.upload_service import UploadService
store=JobStore(Path(sys.argv[1])); service=UploadService(store)
original=store._persist
def crash(job, **kwargs):
    original(job, **kwargs)
    os._exit(23)
store._persist=crash
service.submit(Principal('user:1',1,frozenset({'jobs:submit'})),sys.argv[2],'crash-key-01')
"""
    result = subprocess.run([sys.executable, "-c", code, str(system.jobs.root_dir), receipt["upload_id"]], timeout=30, capture_output=True)
    assert result.returncode == 23
    reopened = JobStore(system.jobs.root_dir)
    try:
        service = UploadService(reopened)
        assert not list(service.admissions.iterdir())
        accepted, replay = service.submit(principal, receipt["upload_id"], "crash-key-01")
        assert not replay and len(reopened.snapshot()) == 1
        assert accepted.job_id == reopened.snapshot()[0].job_id
    finally:
        reopened._conn.close()


def test_committed_job_survives_restart_and_queue_recovers(system):
    receipt = upload(system)
    principal = Principal("user:1", 1, frozenset({"jobs:submit"}))
    original, _ = system.uploads.submit(principal, receipt["upload_id"], "restart-key")
    system.jobs._conn.close()
    reopened = JobStore(system.jobs.root_dir)
    processor = JobQueueProcessor(reopened, worker_count=1)
    event = threading.Event()
    seen = []
    def process(job_id):
        seen.append(job_id)
        reopened.update(job_id, status="completed")
        event.set()
    processor.orchestrator.process_job = process
    try:
        service = UploadService(reopened)
        again, replay = service.submit(principal, receipt["upload_id"], "restart-key")
        assert replay and again == original
        processor.start()
        assert event.wait(5)
        assert seen == [original.job_id]
    finally:
        processor.stop()
        reopened._conn.close()


@pytest.mark.parametrize("data,status", [(b"not-pdf", 422), (b"%PDF-broken", 422), (pdf(2), 422), (pdf(size=(20000, 20000)), 422)])
def test_rejects_invalid_or_resource_heavy_pdf(system, monkeypatch, data, status):
    monkeypatch.setattr(settings, "upload_max_pages", 1)
    receipt = reserve(system, data)
    response = system.client.put(receipt["content_url"], content=data, headers={**bearer(system.tokens[0]), "Content-Type": "application/pdf"})
    assert response.status_code == status, response.text
    assert system.client.get(f"/api/v1/uploads/{receipt['upload_id']}", headers=bearer(system.tokens[0])).json()["state"] == "reserved"
    assert not list(system.uploads.root.glob("*.part"))


def test_content_retry_and_conflicts(system):
    data = pdf()
    receipt = reserve(system, data, sha256=hashlib.sha256(data).hexdigest())
    headers = {**bearer(system.tokens[0]), "Content-Type": "application/pdf"}
    assert system.client.put(receipt["content_url"], content=data[:-1], headers=headers).status_code == 422
    assert system.client.put(receipt["content_url"], content=data + b"extra", headers=headers).status_code == 413
    first = system.client.put(receipt["content_url"], content=data, headers=headers)
    again = system.client.put(receipt["content_url"], content=data, headers=headers)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    changed = data.replace(b"PDF", b"BAD", 1)
    assert system.client.put(receipt["content_url"], content=changed, headers=headers).status_code == 409
    assert system.client.put(receipt["content_url"], content=data[:-1], headers=headers).status_code == 409
    assert system.client.put(receipt["content_url"], content=data + b"different", headers=headers).status_code == 409
    assert (system.uploads.root / (receipt["upload_id"] + ".pdf")).read_bytes() == data


def test_disconnect_releases_slot_and_reservation_can_restart(system):
    data = pdf()
    receipt = reserve(system, data)
    principal = Principal("user:1", 1, frozenset({"jobs:submit"}))
    async def scenario():
        events = iter([{"type": "http.request", "body": data[:50], "more_body": True}, {"type": "http.disconnect"}])
        async def receive():
            return next(events)
        request = Request({"type": "http", "method": "PUT", "path": receipt["content_url"], "headers": [(b"content-type", b"application/pdf")]}, receive)
        with pytest.raises(ClientDisconnect):
            await receive_upload(request, principal, system.uploads, receipt["upload_id"])
    asyncio.run(scenario())
    assert not list(system.uploads.root.glob("*.part"))
    assert system.uploads.get(principal, receipt["upload_id"]).state == "reserved"
    response = system.client.put(receipt["content_url"], content=data, headers={**bearer(system.tokens[0]), "Content-Type": "application/pdf"})
    assert response.status_code == 200


def test_disk_full_and_quota_rejection_leave_no_partial_file(system, monkeypatch):
    data = pdf()
    receipt = reserve(system, data)
    original = UploadSession.write
    def full(self, chunk):
        original(self, chunk[:10])
        raise OSError("disk full")
    with monkeypatch.context() as m:
        m.setattr(UploadSession, "write", full)
        response = system.client.put(receipt["content_url"], content=data, headers={**bearer(system.tokens[0]), "Content-Type": "application/pdf"})
        assert response.status_code == 503
    assert not list(system.uploads.root.glob("*.part"))
    monkeypatch.setattr(settings, "upload_owner_limit_mb", 1)
    response = system.client.post("/api/v1/upload-sessions", json={"filename": "big.pdf", "size_bytes": 2*MIB}, headers=bearer(system.tokens[0]))
    assert response.status_code == 429
    assert system.jobs._conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 1


def test_concurrency_and_pending_limits(system, monkeypatch):
    data = pdf()
    receipts = [reserve(system, data) for _ in range(settings.upload_concurrency+1)]
    principal = Principal("user:1", 1, frozenset({"jobs:submit"}))
    sessions = [system.uploads.begin(principal, r["upload_id"]) for r in receipts[:-1]]
    try:
        with pytest.raises(ApplicationError) as error:
            system.uploads.begin(principal, receipts[-1]["upload_id"])
        assert error.value.status == 429
    finally:
        for session in sessions:
            session.close()
    ready = upload(system)
    monkeypatch.setattr(settings, "job_owner_max_pending", 1)
    assert submit(system, ready["upload_id"]).status_code == 202
    assert submit(system, ready["upload_id"], key="second-key").status_code == 429
    assert submit(system, ready["upload_id"]).status_code == 202


def test_expiry_cleans_upload_not_job_copy(system):
    ready = upload(system)
    admitted = submit(system, ready["upload_id"]).json()
    job = system.jobs.get_snapshot(admitted["job_id"])
    system.jobs._conn.execute("UPDATE uploads SET expires_at=0")
    system.jobs._conn.commit()
    response = system.client.get(f"/api/v1/uploads/{ready['upload_id']}", headers=bearer(system.tokens[0]))
    assert response.status_code == 410
    assert not (system.uploads.root / (ready["upload_id"] + ".pdf")).exists()
    assert job.input_pdf_path.is_file()


@pytest.mark.parametrize("filename", ["../escape.pdf", "C:\\private.pdf", "bad.txt", "a\x00.pdf"])
def test_filename_cannot_select_paths(system, filename):
    response = system.client.post("/api/v1/upload-sessions", json={"filename": filename, "size_bytes": 100}, headers=bearer(system.tokens[0]))
    assert response.status_code == 422
    assert not system.jobs._conn.execute("SELECT 1 FROM uploads").fetchone()


def test_upload_scopes_and_unsupported_body_contract(system):
    _, token = system.identities.issue_credential(system.services[0]["id"], "Read only", ["jobs:read"], 3600, "user:1")
    for path in ("/api/v1/uploads", "/api/v1/upload-sessions", "/api/v1/jobs"):
        response = system.client.post(path, json={}, headers={**bearer(token), "Idempotency-Key": "test-key-001"})
        assert response.status_code == 403
    assert system.client.post("/api/v1/uploads", files={"file": ("x.pdf", pdf())}).status_code == 401
    assert not system.jobs._conn.execute("SELECT 1 FROM uploads").fetchone()
    response = system.client.post("/api/v1/upload-sessions", content=b"x" * 65537, headers={**bearer(system.tokens[0]), "Content-Type": "application/json"})
    assert response.status_code == 413
    response = system.client.post("/api/v1/upload-sessions", json={"filename": "x.pdf", "size_bytes": True}, headers=bearer(system.tokens[0]))
    assert response.status_code == 422


def test_multipart_rejects_extra_parts_and_truncation(system):
    headers = bearer(system.tokens[0])
    response = system.client.post("/api/v1/uploads", files=[("file", ("a.pdf", pdf())), ("file", ("b.pdf", pdf()))], headers=headers)
    assert response.status_code == 422
    response = system.client.post("/api/v1/uploads", content=b"--boundary\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x.pdf\"\r\n\r\n%PDF-", headers={**headers, "Content-Type": "multipart/form-data; boundary=boundary"})
    assert response.status_code == 422
    assert not list(system.uploads.root.iterdir())
    assert not system.jobs._conn.execute("SELECT 1 FROM uploads").fetchone()


def test_validator_timeout_and_memory_bound(system, monkeypatch):
    import app.services.upload_service as module
    path = system.tmp / "document.pdf"
    path.write_bytes(pdf())
    assert validate_pdf(path) == 1  # Real child installs the platform memory limit.
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(module.subprocess, "run", timeout)
    with pytest.raises(ApplicationError) as error:
        validate_pdf(path)
    assert error.value.code == "pdf_validation_limit"


def test_os_rejects_allocation_above_validator_budget():
    code = """
from app.pdf_validation import limit_memory
handle=limit_memory(64*1024*1024)
try:
    memory=bytearray(128*1024*1024)
except MemoryError:
    print('limited')
else:
    raise SystemExit(2)
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "limited"


def test_large_upload_uses_bounded_writes(system, monkeypatch):
    buffer = BytesIO()
    page = canvas.Canvas(buffer, pageCompression=0)
    page.drawString(10, 10, "A" * (2 * CHUNK))
    page.save()
    data = buffer.getvalue()
    lengths = []
    original = UploadSession.write
    def capture(self, chunk):
        lengths.append(len(chunk))
        original(self, chunk)
    monkeypatch.setattr(UploadSession, "write", capture)
    ready = upload(system, data)
    assert ready["size_bytes"] == len(data)
    assert len(lengths) >= 3 and max(lengths) <= CHUNK


def test_cancel_during_write_waits_for_io_then_cleans(system, monkeypatch):
    data = pdf()
    receipt = reserve(system, data)
    started, release = threading.Event(), threading.Event()
    original = UploadSession.write
    def delayed(self, chunk):
        started.set()
        assert release.wait(5)
        original(self, chunk)
    monkeypatch.setattr(UploadSession, "write", delayed)
    async def scenario():
        async def receive():
            return {"type": "http.request", "body": data, "more_body": False}
        request = Request({"type": "http", "method": "PUT", "path": receipt["content_url"], "headers": [(b"content-type", b"application/pdf")]}, receive)
        task = asyncio.create_task(receive_upload(request, Principal("user:1", 1, frozenset({"jobs:submit"})), system.uploads, receipt["upload_id"]))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert not list(system.uploads.root.glob("*.part"))
    assert system.uploads.get(Principal("user:1", 1, frozenset({"jobs:submit"})), receipt["upload_id"]).state == "reserved"


def test_inflight_conflict_and_reservation_count(system, monkeypatch):
    data = pdf()
    receipt = reserve(system, data)
    principal = Principal("user:1", 1, frozenset({"jobs:submit"}))
    session = system.uploads.begin(principal, receipt["upload_id"])
    try:
        with pytest.raises(ApplicationError) as error:
            system.uploads.begin(principal, receipt["upload_id"])
        assert error.value.code == "upload_in_progress"
    finally:
        session.close()
    monkeypatch.setattr(settings, "upload_owner_max_reservations", 1)
    response = system.client.post("/api/v1/upload-sessions", json={"filename": "a.pdf", "size_bytes": 100}, headers=bearer(system.tokens[0]))
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"


def test_low_disk_respects_outstanding_reservations(system, monkeypatch):
    import app.services.upload_service as module
    monkeypatch.setattr(settings, "upload_min_free_mb", 0)
    monkeypatch.setattr(module.shutil, "disk_usage", lambda _: SimpleNamespace(free=2500))
    headers = bearer(system.tokens[0])
    assert system.client.post("/api/v1/upload-sessions", json={"filename": "a.pdf", "size_bytes": 2000}, headers=headers).status_code == 201
    response = system.client.post("/api/v1/upload-sessions", json={"filename": "b.pdf", "size_bytes": 1000}, headers=headers)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"


def test_receiving_state_recovers_to_reserved_after_restart(system):
    receipt = reserve(system, pdf())
    system.jobs._conn.execute("UPDATE uploads SET state='receiving'")
    system.jobs._conn.commit()
    (system.uploads.root / (receipt["upload_id"] + ".part")).write_bytes(b"partial")
    system.jobs._conn.close()
    reopened = JobStore(system.jobs.root_dir)
    try:
        service = UploadService(reopened)
        assert not list(service.root.glob("*.part"))
        assert service.get(Principal("user:1", 1, frozenset({"jobs:submit"})), receipt["upload_id"]).state == "reserved"
    finally:
        reopened._conn.close()


def test_all_five_http_exchanges_match_published_schema(system):
    schema = system.app.openapi()
    headers = bearer(system.tokens[0])
    def check(response, template, method):
        contract = schema["paths"][template][method]["responses"][str(response.status_code)]["content"]["application/json"]["schema"]
        Draft202012Validator({"components": schema["components"], **contract}).validate(response.json())
        assert response.headers["cache-control"] == "no-store"
        return response.json()
    data = pdf()
    response = system.client.post("/api/v1/upload-sessions", json={"filename": "schema.pdf", "size_bytes": len(data)}, headers=headers)
    receipt = check(response, "/api/v1/upload-sessions", "post")
    response = system.client.put(receipt["content_url"], content=data, headers={**headers, "Content-Type": "application/pdf"})
    check(response, "/api/v1/uploads/{upload_id}/content", "put")
    response = system.client.get(f"/api/v1/uploads/{receipt['upload_id']}", headers=headers)
    check(response, "/api/v1/uploads/{upload_id}", "get")
    response = system.client.post("/api/v1/uploads", files={"file": ("schema.pdf", data, "application/pdf")}, headers=headers)
    check(response, "/api/v1/uploads", "post")
    response = submit(system, receipt["upload_id"])
    check(response, "/api/v1/jobs", "post")
    assert response.headers["idempotency-replayed"] == "false"


def test_body_limit_and_header_limit_drop_convenience_upload(system, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_size_mb", 1)
    headers = bearer(system.tokens[0])
    response = system.client.post("/api/v1/uploads", files={"file": ("oversize.pdf", b"%PDF-" + b"X" * MIB)}, headers=headers)
    assert response.status_code == 413
    data = b'--limit\r\nContent-Disposition: form-data; name="file"; filename="x.pdf"\r\nX-Large: ' + b'A'*9000 + b'\r\n\r\n%PDF-\r\n--limit--\r\n'
    response = system.client.post("/api/v1/uploads", content=data, headers={**headers, "Content-Type": "multipart/form-data; boundary=limit"})
    assert response.status_code == 413
    assert not list(system.uploads.root.iterdir())
    assert system.jobs._conn.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 0


def test_delayed_replay_cannot_reenqueue_completed_job(system):
    ready = upload(system)
    principal = Principal("user:1", 1, frozenset({"jobs:submit"}))
    receipt, _ = system.uploads.submit(principal, ready["upload_id"], "delayed-replay")
    processor = JobQueueProcessor(system.jobs)
    processor._started = True  # Exercise admission without running engines.
    stale = system.jobs.get_snapshot(receipt.job_id)
    assert stale.status == "queued"
    system.jobs.update(receipt.job_id, status="completed")
    processor.enqueue(stale.job_id)
    assert processor._queue.empty() and not processor._queued_ids
