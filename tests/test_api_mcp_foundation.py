"""Contract/isolation checks with synthetic records and the official MCP HTTP client."""
import asyncio
import os
import subprocess
import sys
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.routing import Mount

from app.application import ApplicationError, JobApplication, Principal, get_job_application
from app.config import settings
from app.main import app
from app.mcp_server import build_mcp_server
from catalog_tool_expectations import assert_catalog_annotations, assert_tool_inventory
from app.services.auth_service import create_token
from app.services.storage_service import JobStore
from app.services.user_store import get_user_store


@pytest.fixture
def fixture(tmp_path):
    users = [get_user_store().create_user(f"User {n}", f"{uuid4().hex}@test.invalid", "synthetic-test-password") for n in (1, 2)]
    tokens = [create_token(u["id"], u["email"], u["role"], settings.jwt_secret) for u in users]
    store = JobStore(root_dir=tmp_path)
    jobs = [store.create(f"user-{u['id']}.pdf", b"synthetic", owner_user_id=u["id"]) for u in users]
    orphan = store.create("legacy.pdf", b"synthetic")
    artifact = jobs[0].working_dir / "result.pdf"
    artifact.write_bytes(b"%PDF-synthetic-artifact")
    store.update(jobs[0].job_id, status="completed", output_pdf_path=artifact)
    service = JobApplication(store)
    app.dependency_overrides[get_job_application] = lambda: service
    try:
        yield service, users, tokens, jobs, orphan
    finally:
        app.dependency_overrides.pop(get_job_application, None)
        store._conn.close()


def test_import_does_not_create_state_or_workers(tmp_path):
    state = tmp_path / "must-not-exist"
    env = {**os.environ, "OCR_TMP_DIR": str(state)}
    result = subprocess.run([sys.executable, "-c", "import threading; import app.main; "
                             "assert not any(t.name.startswith('ocr-worker-') for t in threading.enumerate())"],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert not state.exists()


def test_concurrent_singleton_initialization(tmp_path):
    env = {**os.environ, "OCR_TMP_DIR": str(tmp_path / "state")}
    code = """
from concurrent.futures import ThreadPoolExecutor
from app.services.storage_service import get_job_store
from app.services.user_store import get_user_store
from app.services.job_queue import get_job_queue_processor
for provider in (get_job_store, get_user_store, get_job_queue_processor):
    with ThreadPoolExecutor(max_workers=16) as pool:
        objects = list(pool.map(lambda _: provider(), range(64)))
    assert len({id(x) for x in objects}) == 1
assert not get_job_queue_processor()._started
assert get_job_queue_processor().job_store is get_job_store()
"""
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_http_owner_isolation_and_artifacts(fixture):
    _, users, tokens, jobs, orphan = fixture
    client = TestClient(app)
    first = {"Authorization": f"Bearer {tokens[0]}"}
    second = {"Authorization": f"Bearer {tokens[1]}"}
    assert client.get("/api/v1/me", headers=first).json()["subject"] == f"user:{users[0]['id']}"
    assert client.get("/api/v1/jobs", headers=first).json()["items"][0]["job_id"] == jobs[0].job_id
    assert client.get("/api/v1/metrics", headers=first).json()["total_jobs"] == 1
    for inaccessible in (jobs[1].job_id, orphan.job_id, "missing"):
        for suffix in ("", "/artifact", "/artifact/content"):
            response = client.get(f"/api/v1/jobs/{inaccessible}{suffix}", headers=first)
            assert response.status_code == 404
            assert response.json()["error"]["code"] == "job_not_found"
    assert client.get(f"/api/v1/jobs/{jobs[0].job_id}/artifact/content", headers=second).status_code == 404
    metadata = client.get(f"/api/v1/jobs/{jobs[0].job_id}/artifact", headers=first).json()
    assert metadata["available"] is True
    assert client.get(metadata["download_url"], headers=first).content == b"%PDF-synthetic-artifact"


def test_no_cookie_or_global_key_authentication(fixture, monkeypatch):
    _, _, tokens, _, _ = fixture
    monkeypatch.setattr(settings, "api_key", "synthetic-legacy-key")
    client = TestClient(app)
    for path in ("/api/v1/jobs", "/mcp"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"X-API-Key": "synthetic-legacy-key"}).status_code == 401
        client.cookies.set("access_token", tokens[0])
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401
        client.cookies.clear()
    assert client.get("/unknown-route").status_code == 404


def test_cursor_filter_limits_and_scopes(fixture):
    service, users, tokens, jobs, _ = fixture
    principal = Principal(f"user:{users[0]['id']}", users[0]["id"], frozenset({"jobs:read"}))
    extra = [service.store.create(f"{n}.pdf", b"synthetic", owner_user_id=users[0]["id"]) for n in range(3)]
    first = service.list_jobs(principal, limit=2)
    assert first.next_cursor
    second = service.list_jobs(principal, limit=2, cursor=first.next_cursor)
    assert second.next_cursor is None
    assert {j.job_id for j in first.items + second.items} == {jobs[0].job_id, *(j.job_id for j in extra)}
    assert len(service.list_jobs(principal, status="queued").items) == 3
    with pytest.raises(ApplicationError, match="permission"):
        service.artifact(principal, jobs[0].job_id)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {tokens[0]}"}
    for query in ("limit=0", "limit=101", "cursor=bad", "status=invalid"):
        response = client.get(f"/api/v1/jobs?{query}", headers=headers)
        assert response.status_code == 422
        assert "error" in response.json()


def test_openapi_declares_identity_and_unique_operations():
    schema = app.openapi()
    assert schema["components"]["securitySchemes"]["PrincipalBearer"]["scheme"] == "bearer"
    ids = []
    for path, methods in schema["paths"].items():
        if not path.startswith("/api/v1/"):
            continue
        for operation in methods.values():
            assert operation["security"] == [{"PrincipalBearer": []}]
            ids.append(operation["operationId"])
    assert len(ids) == len(set(ids)) == 42


def test_read_snapshot_and_sanitized_error(fixture):
    service, users, _, jobs, _ = fixture
    principal = Principal(f"user:{users[0]['id']}", users[0]["id"], frozenset({"jobs:read"}))
    snapshot = service.store.get_snapshot(jobs[0].job_id)
    service.store.update(jobs[0].job_id, status="failed", error="Sensitive engine output and internal path")
    assert snapshot.status == "completed"
    response = service.get_job(principal, jobs[0].job_id)
    assert response.status == "failed"
    assert "Sensitive" not in response.error
    assert response.download_url is None


def test_one_runtime_owner_between_processes(tmp_path):
    from app.services.runtime_lease import RuntimeLease
    lease = RuntimeLease(tmp_path)
    lease.acquire()
    code = "from pathlib import Path; from app.services.runtime_lease import RuntimeLease; import sys; " \
           "lease=RuntimeLease(Path(sys.argv[1])); lease.acquire(); lease.release()"
    try:
        denied = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=15)
        assert denied.returncode != 0
        assert "already has an active server" in denied.stderr
    finally:
        lease.release()
    allowed = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, timeout=15)
    assert allowed.returncode == 0, allowed.stderr


def test_combined_app_lifecycle_and_real_mcp(fixture, monkeypatch, tmp_path):
    from unittest.mock import Mock
    import app.main as main
    service, _, tokens, _, _ = fixture
    processor = Mock()
    monkeypatch.setattr(main, "get_job_queue_processor", lambda: processor)
    monkeypatch.setattr(settings, "ocr_tmp_dir", tmp_path / "lifecycle")
    combined = main.create_app(lambda: service)
    async def scenario():
        async with combined.router.lifespan_context(combined):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=combined),
                                          headers={"Authorization": f"Bearer {tokens[0]}"}) as http:
                async with Client(streamable_http_client("http://localhost/mcp", http_client=http)) as client:
                    actual = await client.call_tool("ocr_get_metrics", {})
                    expected = await http.get("http://localhost/api/v1/metrics")
                    assert actual.structured_content == expected.json()
                assert (await http.get("http://localhost/health")).status_code == 200
        processor.start.assert_called_once()
        processor.stop.assert_called_once()
        assert combined.state.runtime_lease._file is None
    asyncio.run(scenario())


def test_shutdown_does_not_release_live_worker_state(tmp_path, monkeypatch):
    from threading import Event, Thread
    from app.services.job_queue import JobQueueProcessor
    store = JobStore(root_dir=tmp_path)
    processor = JobQueueProcessor(store)
    release = Event()
    worker = Thread(target=lambda: release.wait(5))
    processor._threads = [worker]
    processor._started = True
    monkeypatch.setattr(settings, "worker_shutdown_timeout_seconds", 0)
    worker.start()
    try:
        with pytest.raises(RuntimeError, match="shutdown deadline"):
            processor.stop()
        assert processor._started
        assert processor._threads == [worker]
    finally:
        release.set()
        worker.join(5)
        processor.stop()
        store._conn.close()


@pytest.mark.parametrize("claims", [None, [], {"sub": "bad", "exp": 9999999999},
                                   {"sub": True, "exp": 9999999999}, {"sub": 1},
                                   {"sub": 1, "exp": float("nan")}, {"sub": 1, "exp": 0}])
def test_invalid_claims_return_auth_error(monkeypatch, claims):
    import app.security as security
    monkeypatch.setattr(security, "decode_token", lambda *_: claims)
    with pytest.raises(ApplicationError) as error:
        security.authenticate_bearer("synthetic-token")
    assert error.value.status == 401


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_real_sdk_http_parity_and_cross_user_denial(fixture, mode):
    service, _, tokens, jobs, orphan = fixture
    server, transport_app = build_mcp_server(lambda: service)
    asgi_app = Starlette(routes=[Mount("/", app=transport_app)])

    async def scenario():
        async with server.session_manager.run():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=asgi_app),
                                          headers={"Authorization": f"Bearer {tokens[0]}"}) as http:
                async with Client(streamable_http_client("http://localhost/mcp", http_client=http), mode=mode) as client:
                    tools = await client.list_tools()
                    assert_tool_inventory(tools.tools)
                    assert_catalog_annotations(tools.tools)
                    for name, args, path in [
                        ("ocr_whoami", {}, "/api/v1/me"),
                        ("ocr_get_capabilities", {}, "/api/v1/capabilities"),
                        ("ocr_list_jobs", {}, "/api/v1/jobs"),
                        ("ocr_get_job", {"job_id": jobs[0].job_id}, f"/api/v1/jobs/{jobs[0].job_id}"),
                        ("ocr_get_metrics", {}, "/api/v1/metrics"),
                        ("ocr_get_artifact", {"job_id": jobs[0].job_id}, f"/api/v1/jobs/{jobs[0].job_id}/artifact"),
                    ]:
                        actual = await client.call_tool(name, args)
                        expected = TestClient(app).get(path, headers={"Authorization": f"Bearer {tokens[0]}"}).json()
                        assert not actual.is_error
                        assert actual.structured_content == expected
                    for job in (jobs[1], orphan):
                        denied = await client.call_tool("ocr_get_job", {"job_id": job.job_id})
                        assert denied.is_error
                        assert "job_not_found" in denied.content[0].text
                    http.headers["Authorization"] = f"Bearer {tokens[1]}"
                    denied = await client.call_tool("ocr_get_artifact", {"job_id": jobs[0].job_id})
                    assert denied.is_error
    asyncio.run(scenario())


def test_mcp_rejects_untrusted_origin(fixture):
    service, _, tokens, _, _ = fixture
    server, transport = build_mcp_server(lambda: service)
    asgi_app = Starlette(routes=[Mount("/", app=transport)])
    async def scenario():
        async with server.session_manager.run():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=asgi_app)) as client:
                response = await client.post("http://localhost/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                             headers={"Authorization": f"Bearer {tokens[0]}", "Origin": "https://untrusted.invalid"})
                assert response.status_code in (403, 421)
    asyncio.run(scenario())
