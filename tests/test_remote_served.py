"""One production process serving the local surface and the OAuth MCP hostname (OCR-AM-13)."""
import asyncio
from dataclasses import replace

import httpx2
import pytest

from app.application import JobApplication
from app.remote import HostSplit, RemoteSurface, create_served_app, METADATA_PATH
from app.services.storage_service import JobStore
from test_remote_oauth import config, verifier_for  # noqa: F401  (fixture)

GATEWAY = "g" * 40
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                         "clientInfo": {"name": "served-test", "version": "1"}}}
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture
def served(config, tmp_path):
    secret = tmp_path / "gateway.txt"
    secret.write_text(GATEWAY + "\n", encoding="utf-8")
    config = replace(config, gateway_secret_file=secret)
    verifier, calls, users = verifier_for(config)
    jobs = JobStore(tmp_path / "data")
    owned = jobs.create("mine.pdf", b"synthetic", owner_user_id=1)
    app = create_served_app(config, verifier=verifier, service_provider=lambda: JobApplication(jobs))
    try:
        yield app, config, verifier, calls, owned
    finally:
        jobs._conn.close()


def remote_headers(config, **extra):
    return {"host": config.origin.removeprefix("https://"), "x-ocr-gateway": GATEWAY, **extra}


def test_remote_host_requires_gateway_and_oauth_local_host_unchanged(served):
    app, config, verifier, calls, owned = served
    token = {"authorization": "Bearer synthetic-access-token"}

    async def run():
        await verifier.start()
        try:
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
                # Remote hostname: only through the gateway, only OAuth, only allowlisted routes.
                for headers in ({"host": "ocr.test"}, remote_headers(config, **{"x-ocr-gateway": "x" * 40})):
                    assert (await client.get(METADATA_PATH, headers=headers)).status_code == 403
                r = await client.get(METADATA_PATH, headers=remote_headers(config))
                assert r.status_code == 200 and r.json()["resource"] == config.resource
                for path in ("/", "/health", "/docs", "/auth/login", "/api/v1/rag/destinations"):
                    assert (await client.get(path, headers=remote_headers(config))).status_code == 404
                r = await client.post("/mcp", headers=remote_headers(config, **MCP_HEADERS), json=INITIALIZE)
                assert r.status_code == 401 and "resource_metadata=" in r.headers["www-authenticate"]
                r = await client.get("/api/v1/me", headers=remote_headers(config, **token))
                assert r.status_code == 200 and r.json()["subject"] == "user:1"
                r = await client.get(f"/api/v1/jobs/{owned.job_id}", headers=remote_headers(config, **token))
                assert r.status_code == 200
                # MCP sessions need the application lifespan; the Keycloak lab covers them end to end.
                introspections = len(calls)
                # Local hostname: the existing surface; OAuth tokens and the gateway grant nothing here.
                assert (await client.get("/health")).status_code == 200
                assert (await client.get("/api/v1/me", headers=token)).status_code == 401
                assert (await client.get("/api/v1/me", headers={**token, "x-ocr-gateway": GATEWAY})).status_code == 401
                r = await client.post("/mcp", headers={"host": "ocr.public.test", **token, **MCP_HEADERS}, json=INITIALIZE)
                assert r.status_code in (401, 421)
                assert len(calls) == introspections  # Local requests never reach the identity provider.
        finally:
            await verifier.close()
    asyncio.run(run())


def test_provider_outage_keeps_local_surface_and_answers_503_remotely(served):
    app, config, verifier, calls, owned = served

    async def failing_start():
        raise ValueError("provider down")
    verifier.start = failing_start
    events = []

    async def inner(scope, receive, send):
        assert scope["type"] == "lifespan"
        message = await receive()
        events.append(message["type"])
        await send({"type": "lifespan.startup.complete"})
        message = await receive()
        events.append(message["type"])
        await send({"type": "lifespan.shutdown.complete"})

    split = HostSplit(inner, RemoteSurface(app, verifier), app.host.decode())
    split.RETRY_SECONDS = 0.01
    sent = []

    async def run():
        queue = asyncio.Queue()
        await queue.put({"type": "lifespan.startup"})
        task = asyncio.create_task(split({"type": "lifespan"}, queue.get, lambda m: _record(sent, m)))
        await asyncio.sleep(0.05)
        assert {"type": "lifespan.startup.complete"} in sent  # The process starts despite the outage.
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            r = await client.get("/api/v1/me", headers=remote_headers(config, authorization="Bearer synthetic-access-token"))
            assert r.status_code == 503 and r.headers["retry-after"] == "5"
            assert (await client.get("/health")).status_code == 200
        await queue.put({"type": "lifespan.shutdown"})
        await task
    asyncio.run(run())
    assert events == ["lifespan.startup", "lifespan.shutdown"]


async def _record(sent, message):
    sent.append(message)


def test_configuration_requires_absolute_gateway_secret(config, tmp_path):
    from pathlib import Path
    with pytest.raises(ValueError):
        replace(config, gateway_secret_file=Path("relative.txt"))
    short = tmp_path / "short.txt"
    short.write_text("too-short", encoding="utf-8")
    verifier, _, _ = verifier_for(replace(config, gateway_secret_file=short))
    with pytest.raises(ValueError):
        RemoteSurface(lambda *a: None, verifier)
