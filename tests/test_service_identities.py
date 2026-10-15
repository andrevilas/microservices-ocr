"""Individual credentials, four-principal isolation, legacy boundaries and migration."""
import asyncio
import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from reportlab.pdfgen import canvas

from app.application import JobApplication
from app.config import settings
from app.main import create_app
from app.security import authenticate_bearer
from app.services.auth_service import create_token
from app.services.identity_store import IdentityStore, get_identity_store
from app.services.job_queue import JobQueueProcessor, get_job_queue_processor
from app.services.storage_service import JobStore, get_job_store
from app.services.user_store import get_user_store


def bearer(token):
    return {"Authorization": "Bearer " + token}


def pdf():
    buffer = BytesIO()
    page = canvas.Canvas(buffer)
    page.drawString(80, 700, "Synthetic service identity test")
    page.save()
    return buffer.getvalue()


@pytest.fixture
def system(tmp_path, monkeypatch):
    import app.security as security
    identities = IdentityStore(tmp_path / "identities.sqlite3")
    jobs = JobStore(root_dir=tmp_path / "jobs")
    queue = JobQueueProcessor(jobs, worker_count=1)
    queue._started = True  # Test admission/ownership without executing OCR engines.
    monkeypatch.setattr(security, "get_identity_store", lambda: identities)
    app = create_app(lambda: JobApplication(jobs))
    app.dependency_overrides[get_identity_store] = lambda: identities
    app.dependency_overrides[get_job_store] = lambda: jobs
    app.dependency_overrides[get_job_queue_processor] = lambda: queue
    client = TestClient(app)
    users = [get_user_store().create_user(f"Synthetic {role}", f"{uuid4().hex}@test.invalid", "synthetic-password", role=role)
             for role in ("admin", "user", "user")]
    human_tokens = [create_token(u["id"], u["email"], u["role"], settings.jwt_secret) for u in users]
    admin_headers = bearer(human_tokens[0])
    services, credentials, service_tokens = [], [], []
    for n in (1, 2):
        response = client.post("/api/v1/service-principals", json={"name": f"Test service {n}"}, headers=admin_headers)
        assert response.status_code == 201
        service = response.json()
        response = client.post(f"/api/v1/service-principals/{service['id']}/credentials",
                               json={"name": "test credential", "scopes": ["jobs:read", "artifacts:read", "jobs:submit", "jobs:cancel"]},
                               headers=admin_headers)
        assert response.status_code == 201
        assert response.headers["cache-control"] == "no-store"
        services.append(service)
        credentials.append(response.json()["credential"])
        service_tokens.append(response.json()["token"])
    yield {"app": app, "client": client, "identities": identities, "jobs": jobs, "queue": queue,
           "users": users, "human_tokens": human_tokens, "admin": admin_headers, "services": services,
           "credentials": credentials, "service_tokens": service_tokens, "tmp_path": tmp_path}
    identities.close()
    jobs._conn.close()


def admit(system, token, name):
    response = system["client"].post("/api/jobs", files={"file": (name, pdf(), "application/pdf")}, headers=bearer(token))
    assert response.status_code == 202
    job = system["jobs"].get(response.json()["job_id"])
    output = job.working_dir / "result.pdf"
    output.write_bytes(pdf())
    system["jobs"].update(job.job_id, status="completed", output_pdf_path=output)
    return job


def test_four_principals_are_isolated_on_http_and_legacy(system, monkeypatch):
    client = system["client"]
    tokens = system["human_tokens"][1:] + system["service_tokens"]
    jobs = [admit(system, token, f"owner-{i}.pdf") for i, token in enumerate(tokens)]
    orphan = system["jobs"].create("unassigned.pdf", pdf())
    for index, token in enumerate(tokens):
        headers = bearer(token)
        listed = client.get("/api/v1/jobs", headers=headers).json()["items"]
        assert [j["job_id"] for j in listed] == [jobs[index].job_id]
        for path in ("/metrics", "/api/v1/metrics"):
            assert client.get(path, headers=headers).json()["total_jobs"] == 1
        for n, job in enumerate(jobs + [orphan]):
            for path in (f"/api/jobs/{job.job_id}", f"/api/jobs/{job.job_id}/download", f"/api/v1/jobs/{job.job_id}",
                         f"/api/v1/jobs/{job.job_id}/artifact", f"/api/v1/jobs/{job.job_id}/artifact/content"):
                assert client.get(path, headers=headers).status_code == (200 if n == index else 404)
        response = client.get("/api/jobs/download-batch", params=[("job_ids", j.job_id) for j in jobs], headers=headers)
        assert response.status_code == 200
        with ZipFile(BytesIO(response.content)) as archive:
            assert archive.namelist() == [f"owner-{index}-searchable.pdf"]
    # Administrating identities does not grant automatic document access.
    for job in jobs + [orphan]:
        assert client.get(f"/api/v1/jobs/{job.job_id}", headers=system["admin"]).status_code == 404
        assert client.get(f"/api/jobs/{job.job_id}", headers=system["admin"]).status_code == 404
    monkeypatch.setattr(settings, "api_key", "synthetic-legacy")
    legacy = {"X-API-Key": "synthetic-legacy"}
    for job in jobs + [orphan]:
        assert client.get(f"/api/jobs/{job.job_id}", headers=legacy).status_code == 404
    created = client.post("/api/jobs", files={"file": ("legacy.pdf", pdf(), "application/pdf")}, headers=legacy)
    legacy_id = created.json()["job_id"]
    assert system["jobs"].get(legacy_id).owner_principal_id == "legacy:api-key"
    assert client.get(f"/api/jobs/{legacy_id}", headers=legacy).status_code == 200
    for token in tokens:
        assert client.get(f"/api/jobs/{legacy_id}", headers=bearer(token)).status_code == 404
    assert client.get("/api/v1/jobs", headers=legacy).status_code == 401
    assert client.post("/mcp", headers=legacy).status_code == 401


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_service_mcp_isolation_scopes_and_revocation(system, mode, monkeypatch):
    import app.main as main
    from unittest.mock import Mock
    tokens = system["human_tokens"][1:] + system["service_tokens"]
    jobs = [admit(system, token, f"mcp-{i}.pdf") for i, token in enumerate(tokens)]
    monkeypatch.setattr(main, "get_job_queue_processor", Mock())
    monkeypatch.setattr(settings, "ocr_tmp_dir", system["tmp_path"] / "lifecycle")
    app = system["app"]

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=bearer(tokens[2])) as http:
                async with Client(streamable_http_client("http://localhost/mcp", http_client=http), mode=mode) as client:
                    catalog = await client.list_tools()
                    assert not any("credential" in t.name or "password" in t.name or "login" in t.name for t in catalog.tools)
                    for index, token in enumerate(tokens):
                        http.headers.update(bearer(token))
                        result = await client.call_tool("ocr_whoami", {})
                        assert result.structured_content["kind"] == ("user" if index < 2 else "service")
                        listed = await client.call_tool("ocr_list_jobs", {})
                        assert [j["job_id"] for j in listed.structured_content["items"]] == [jobs[index].job_id]
                        for n, job in enumerate(jobs):
                            for tool in ("ocr_get_job", "ocr_get_artifact"):
                                result = await client.call_tool(tool, {"job_id": job.job_id})
                                assert result.is_error == (n != index)
                    # Restrict a new credential to jobs:read: artifact tool must deny it.
                    _, limited = system["identities"].issue_credential(system["services"][0]["id"], "read only", ["jobs:read"], 600, "test")
                    http.headers.update(bearer(limited))
                    assert (await client.call_tool("ocr_get_artifact", {"job_id": jobs[2].job_id})).is_error
                    # Revoke during an existing MCP connection. The next HTTP request is rejected.
                    http.headers.update(bearer(tokens[2]))
                    system["identities"].revoke_credential(system["services"][0]["id"], system["credentials"][0]["id"], "test")
                    response = await http.post("http://localhost/mcp", json={"jsonrpc": "2.0", "id": 99, "method": "tools/list"})
                    assert response.status_code == 401
    asyncio.run(scenario())


def test_scopes_deny_upload_download_and_cancel(system):
    client, store, service = system["client"], system["identities"], system["services"][0]
    job = admit(system, system["service_tokens"][0], "limited.pdf")
    _, token = store.issue_credential(service["id"], "read", ["jobs:read"], 600, "test")
    headers = bearer(token)
    assert client.get(f"/api/jobs/{job.job_id}", headers=headers).status_code == 200
    for path in (f"/api/jobs/{job.job_id}/download", f"/api/v1/jobs/{job.job_id}/artifact/content", "/api/jobs/download-batch?job_ids=" + job.job_id):
        assert client.get(path, headers=headers).status_code == 403
    assert client.post("/api/jobs", files={"file": ("doc.pdf", pdf(), "application/pdf")}, headers=headers).status_code == 403
    assert client.post("/api/jobs/batch", files=[("files", ("doc.pdf", pdf(), "application/pdf"))], headers=headers).status_code == 403
    assert client.post("/api/jobs/clear-queue", headers=headers).status_code == 403
    _, submit_token = store.issue_credential(service["id"], "submit only", ["jobs:submit"], 600, "test")
    assert client.get("/api/v1/jobs", headers=bearer(submit_token)).status_code == 403
    assert client.get("/metrics", headers=bearer(submit_token)).status_code == 403


def test_cancel_only_own_queue_and_no_wildcard(system, monkeypatch):
    tokens = system["human_tokens"][1:] + system["service_tokens"]
    jobs = []
    for i, token in enumerate(tokens):
        response = system["client"].post("/api/jobs", files={"file": (f"{i}.pdf", pdf(), "application/pdf")}, headers=bearer(token))
        jobs.append(system["jobs"].get(response.json()["job_id"]))
    assert system["client"].post("/api/jobs/clear-queue", headers=bearer(tokens[2])).json()["cleared_count"] == 1
    assert [j.status for j in jobs] == ["queued", "queued", "canceled", "queued"]
    monkeypatch.setattr(settings, "api_key", "synthetic-legacy")
    assert system["client"].post("/api/jobs/clear-queue", headers={"X-API-Key": "synthetic-legacy"}).json()["cleared_count"] == 0
    with pytest.raises(ValueError, match="explicit owner"):
        system["queue"].clear_pending_jobs()


def test_secret_hash_persistence_expiry_rotation_and_audit(system):
    store = system["identities"]
    sid = system["services"][0]["id"]
    token = system["service_tokens"][0]
    credential = system["credentials"][0]
    assert authenticate_bearer(token).subject == sid
    metadata = store.list_credentials(sid)
    assert metadata[0]["last_used_at"] is not None
    assert token not in json.dumps(metadata)
    assert "token_hash" not in metadata[0]
    digest = store._conn.execute("SELECT token_hash FROM service_credentials WHERE id=?", (credential["id"],)).fetchone()[0]
    assert digest == hashlib.sha256(token.encode()).hexdigest()
    reopened = IdentityStore(store.path)
    try:
        assert reopened.authenticate(token)["subject"] == sid
        rotated, second_token = reopened.issue_credential(sid, "replacement", ["jobs:read"], 600, "test")
        assert second_token != token
        assert reopened.authenticate(second_token)["subject"] == sid
        store.revoke_credential(sid, credential["id"], "test")
        assert reopened.authenticate(token) is None
        assert reopened.authenticate(second_token) is not None
        store.disable_service(sid, "test")
        assert reopened.authenticate(second_token) is None
        with pytest.raises(ValueError, match="disabled"):
            reopened.issue_credential(sid, "invalid", ["jobs:read"], 600, "test")
    finally:
        reopened.close()
    audit = [dict(r) for r in store._conn.execute("SELECT * FROM identity_audit")]
    assert {"service.created", "credential.issued", "credential.revoked", "service.disabled"} <= {r["action"] for r in audit}
    assert token not in json.dumps(audit)
    for path in store.path.parent.glob("identities.sqlite3*"):
        assert token.encode() not in path.read_bytes()


def test_expired_and_tampered_credentials_fail(system):
    store, token = system["identities"], system["service_tokens"][0]
    assert store.authenticate(token[:-1] + ("A" if token[-1] != "A" else "B")) is None
    with store._conn:
        store._conn.execute("UPDATE service_credentials SET expires_at=? WHERE id=?", (time.time() - 1, system["credentials"][0]["id"]))
    assert system["client"].get("/api/v1/jobs", headers=bearer(token)).status_code == 401
    assert store.list_credentials(system["services"][0]["id"])[0]["last_used_at"] is None


def test_admin_only_no_escalation_no_cookie_fallback(system, monkeypatch):
    client = system["client"]
    for token in system["human_tokens"][1:] + system["service_tokens"]:
        assert client.post("/api/v1/service-principals", json={"name": "denied"}, headers=bearer(token)).status_code == 403
        assert client.get("/api/v1/service-principals", headers=bearer(token)).status_code == 403
    client.cookies.set("access_token", system["human_tokens"][0])
    assert client.post("/api/v1/service-principals", json={"name": "denied"}).status_code == 401
    monkeypatch.setattr(settings, "api_key", "synthetic-legacy")
    headers = {**bearer("invalid"), "X-API-Key": "synthetic-legacy"}
    assert client.get("/metrics", headers=headers).status_code == 401
    assert client.get("/api/users", headers={"Authorization": "Basic invalid"}).status_code == 401
    client.cookies.set("access_token", system["service_tokens"][0])
    assert client.get("/metrics").status_code == 401
    assert client.get("/api/users", headers=bearer(system["service_tokens"][0])).status_code == 401


@pytest.mark.parametrize("method,suffix,body", [
    ("post", "/disable", None),
    ("post", "/credentials", {"name": "denied", "scopes": ["jobs:read"]}),
    ("get", "/credentials", None),
    ("delete", "/credentials/{credential_id}", None),
])
def test_service_cannot_manage_credentials(system, method, suffix, body):
    service = system["services"][0]["id"]
    suffix = suffix.format(credential_id=system["credentials"][0]["id"])
    for token in (system["service_tokens"][0], system["human_tokens"][1]):
        response = system["client"].request(method, f"/api/v1/service-principals/{service}{suffix}",
                                            json=body, headers=bearer(token))
        assert response.status_code == 403


@pytest.mark.parametrize("body", [
    {"name": "n", "scopes": ["*"]}, {"name": "n", "scopes": ["users:write"]},
    {"name": "n", "scopes": []}, {"name": "n", "scopes": ["jobs:read", "jobs:read"]},
    {"name": "n", "scopes": ["jobs:read"], "expires_in_seconds": 0},
    {"name": "n", "scopes": ["jobs:read"], "expires_in_seconds": True},
    {"name": "n", "scopes": ["jobs:read"], "expires_in_seconds": 31536001},
    {"name": " ", "scopes": ["jobs:read"]}, {"name": "n", "scopes": ["jobs:read"], "owner": "user:1"},
])
def test_invalid_credential_contracts_rejected(system, body):
    path = f"/api/v1/service-principals/{system['services'][0]['id']}/credentials"
    response = system["client"].post(path, json=body, headers=system["admin"])
    assert response.status_code == 422
    assert len(system["identities"].list_credentials(system["services"][0]["id"])) == 1


def test_revoke_idempotent_cross_service_id_denied(system):
    sid, other = [s["id"] for s in system["services"]]
    cid = system["credentials"][0]["id"]
    client = system["client"]
    assert client.delete(f"/api/v1/service-principals/{other}/credentials/{cid}", headers=system["admin"]).status_code == 404
    path = f"/api/v1/service-principals/{sid}/credentials/{cid}"
    first = client.delete(path, headers=system["admin"])
    second = client.delete(path, headers=system["admin"])
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert client.get("/api/v1/me", headers=bearer(system["service_tokens"][0])).status_code == 401
    assert client.get("/api/v1/me", headers=bearer(system["service_tokens"][1])).status_code == 200


def test_concurrent_authentication_and_revoke_visible(system):
    store, token = system["identities"], system["service_tokens"][0]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.authenticate(token), range(32)))
    assert all(r["subject"] == system["services"][0]["id"] for r in results)
    store.revoke_credential(system["services"][0]["id"], system["credentials"][0]["id"], "test")
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(r is None for r in pool.map(lambda _: store.authenticate(token), range(32)))


def test_additive_job_owner_migration_and_immutable_owner(tmp_path):
    # Create a schema from before AM-03, including an unassigned historical job.
    from app.services.storage_service import _CREATE_TABLE_SQL
    db = tmp_path / "jobs.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute(_CREATE_TABLE_SQL)
    stamp = "2026-10-15T00:00:00+00:00"
    for job_id, owner in (("known", 7), ("orphan", None)):
        conn.execute("INSERT INTO jobs(job_id,filename,working_dir,status,owner_user_id,created_at,updated_at,input_pdf_path) VALUES (?,?,?,?,?,?,?,?)",
                     (job_id, "synthetic.pdf", str(tmp_path / job_id), "completed", owner, stamp, stamp, str(tmp_path / job_id / "original.pdf")))
    conn.commit()
    conn.close()
    store = JobStore(root_dir=tmp_path)
    try:
        assert store.get("known").owner_principal_id == "user:7"
        assert store.get("orphan").owner_principal_id is None
        assert store.get("known").created_at.isoformat() == stamp
        with pytest.raises(ValueError, match="immutable"):
            store.update("known", owner_principal_id="user:8")
        with pytest.raises(ValueError, match="Conflicting"):
            store.create("bad.pdf", pdf(), owner_user_id=7, owner_principal_id="service:" + "a" * 32)
        service = store.create("service.pdf", pdf(), owner_principal_id="service:" + "a" * 32)
    finally:
        store._conn.close()
    reloaded = JobStore(root_dir=tmp_path)
    try:
        assert reloaded.get(service.job_id).owner_principal_id == "service:" + "a" * 32
        assert reloaded.get("orphan").owner_principal_id is None
    finally:
        reloaded._conn.close()
