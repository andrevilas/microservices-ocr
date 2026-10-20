import asyncio
import hashlib
import json
from dataclasses import replace
from fastapi.testclient import TestClient
from fastapi import Request
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
import pytest
from app.application import JobApplication
from app.main import create_app
from app.mcp_server import build_mcp_server
from app.mcp_stdio import ApiClient
from app.rag_models import DeliveryInput
from app.services.rag_application import RagApplication
from test_rag_outbox import outbox, DELETE_OWNER
from test_rag_delivery import source, response, receipt_for
from test_results import OWNER


@pytest.fixture
def http(outbox):
    queue, job, dest = outbox
    current = [DELETE_OWNER]
    async def auth(request: Request):
        return current[0]
    app = create_app(lambda: JobApplication(queue.store), request_authenticator=auth)
    service = RagApplication(queue, {"normas": dest})
    app.state.rag_service = service
    return TestClient(app), app, service, current, job, dest


def test_http_delivery_recovery_receipt_and_replay(http):
    client, app, service, current, job, dest = http
    listed = client.get("/api/v1/rag/destinations")
    assert listed.status_code == 200
    assert dest.token not in listed.text and dest.url not in listed.text
    body = {"job_id": job.job_id, "destination": "normas"}
    admitted = client.post("/api/v1/rag/deliveries", json=body)
    assert admitted.status_code == 200, admitted.text
    delivery = admitted.json()["id"]
    assert client.post("/api/v1/rag/deliveries", json=body).json() == admitted.json()
    base = "/api/v1/rag/deliveries/" + delivery
    info = client.get(base + "/recovery").json()
    raw = client.get(info["content_url"])
    assert hashlib.sha256(raw.content).hexdigest() == info["sha256"]
    assert raw.headers["cache-control"] == "no-store"
    assert service.outbox.status(OWNER, delivery)["state"] == "queued"
    assert client.get(base + "/receipt").status_code == 409
    service.outbox.run_one(delivery, dest, transport=httpx2.MockTransport(lambda r: response(receipt_for(r.content))))
    assert client.get(base + "/receipt").json()["indexing_state"] == "indexed"
    assert client.get(base + "/recovery/content").status_code == 409
    current[0] = replace(DELETE_OWNER, subject="user:2", user_id=2)
    assert client.get("/api/v1/rag/destinations").json() == {"items": []}
    for suffix in ("", "/receipt", "/recovery", "/recovery/content"):
        assert client.get(base + suffix).status_code == 404
    assert client.post(base + "/cancel").status_code == 404
    assert client.post("/api/v1/rag/deliveries", json=body).status_code == 404


def test_http_input_and_disabled_runtime(http):
    client, app, service, current, job, _ = http
    body = {"job_id": job.job_id, "destination": "normas", "url": "http://other.invalid"}
    assert client.post("/api/v1/rag/deliveries", json=body).status_code == 422
    assert client.post("/api/v1/rag/deliveries", content=b"x" * 4097,
                       headers={"Content-Type": "application/json"}).status_code == 413
    current[0] = replace(OWNER, scopes=frozenset())
    assert client.post("/api/v1/rag/deliveries", json=body).status_code == 403
    app.state.rag_service = None
    assert client.get("/api/v1/rag/destinations").status_code == 503


def test_runtime_store_mismatch_fails_closed(http):
    from app.application import get_job_application
    from types import SimpleNamespace
    client, app, _, _, _, _ = http
    app.dependency_overrides[get_job_application] = lambda: SimpleNamespace(store=object())
    assert client.get("/api/v1/rag/destinations").status_code == 503


def test_stdio_http_and_remote_mcp_use_same_owner_service(http):
    _, app, service, _, job, _ = http
    async def auth(request):
        return DELETE_OWNER
    server, remote = build_mcp_server(rag_provider=lambda: service, request_authenticator=auth)
    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as transport:
            client = ApiClient("http://localhost", transport)
            admitted = await client.invoke(None, "submit_rag_delivery", DeliveryInput(job_id=job.job_id, destination="normas"))
            delivery = admitted["id"]
            assert (await client.invoke(None, "get_rag_delivery", delivery))["state"] == "queued"
            link = await client.invoke(None, "get_rag_recovery", delivery)
            assert "content_url" in link and "artifacts" not in link
            async with server.session_manager.run():
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=remote)) as wire:
                    async with Client(streamable_http_client("http://localhost/mcp", http_client=wire)) as mcp:
                        result = await mcp.call_tool("ocr_get_rag_delivery", {"delivery_id": delivery})
                        assert not result.is_error
                        assert result.structured_content["state"] == "queued"
                        canceled = await mcp.call_tool("ocr_cancel_rag_delivery", {"delivery_id": delivery})
                        assert not canceled.is_error
                        assert canceled.structured_content["state"] == "canceled"
            assert (await client.invoke(None, "get_rag_delivery", delivery))["state"] == "canceled"
    asyncio.run(scenario())
