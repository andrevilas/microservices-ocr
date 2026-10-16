"""Remote trust boundary; the separate Keycloak lab covers actual login/PKCE."""
import asyncio
from dataclasses import replace
import json
import time
import sqlite3
from types import SimpleNamespace

import httpx2
import pytest
from starlette.requests import Request

from app.application import ApplicationError, JobApplication
from app.oauth_realm import build_realm
from app.remote import IntrospectionVerifier, RemoteConfig, RemoteSurface, create_remote_app, METADATA_PATH
from app.services.storage_service import JobStore


@pytest.fixture
def config(tmp_path):
    secret = tmp_path / "introspection.txt"
    secret.write_text("synthetic-test-secret", encoding="utf-8")
    return RemoteConfig("https://ocr.test", "https://identity.test/realms/ocr", "ocr-resource", secret,
                        {"subject-one": {"user_id": 1, "scopes": ["jobs:read"]}}, ("ocr-test",))


def discovery(config):
    return {"issuer": config.issuer, "code_challenge_methods_supported": ["S256"],
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code"],
            "introspection_endpoint_auth_methods_supported": ["client_secret_basic"],
            **{name + "_endpoint": config.issuer + "/" + name
               for name in ("authorization", "token", "introspection", "revocation")}}


def claims(config):
    return {"active": True, "iss": config.issuer, "exp": time.time()+300, "sub": "subject-one",
            "aud": config.resource, "client_id": "ocr-test", "scope": "jobs:read jobs:submit",
            "token_type": "Bearer", "roles": ["admin"]}


def request(token="synthetic-access-token"):
    return Request({"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]})


def verifier_for(config, token_claims=None, metadata=None, response=None):
    calls = []
    users = {1: {"id": 1, "role": "admin"}}
    def respond(req):
        calls.append(req)
        if req.method == "GET":
            assert "authorization" not in req.headers
            return httpx2.Response(200, json=metadata if metadata is not None else discovery(config))
        assert str(req.url) == discovery(config)["introspection_endpoint"]
        assert req.headers["authorization"].startswith("Basic ")
        return response or httpx2.Response(200, content=json.dumps(token_claims if token_claims is not None else claims(config)))
    verifier = IntrospectionVerifier(config, transport=httpx2.MockTransport(respond),
                                    users=lambda: SimpleNamespace(get_user_by_id=users.get))
    return verifier, calls, users


def test_scope_ceiling_user_deletion_and_revocation_without_cache(config):
    token_claims = claims(config)
    verifier, calls, users = verifier_for(config, token_claims)
    async def run():
        await verifier.start()
        try:
            principal = await verifier.authenticate(request())
            assert principal.subject == "user:1" and not principal.is_admin
            assert principal.scopes == {"jobs:read"}
            users.clear()
            with pytest.raises(ApplicationError) as error:
                await verifier.authenticate(request())
            assert error.value.status == 401
            users[1] = {"id": 1}
            token_claims["active"] = False
            with pytest.raises(ApplicationError) as error:
                await verifier.authenticate(request())
            assert error.value.status == 401
            assert len(calls) == 4
        finally:
            await verifier.close()
        assert verifier.secret is None and verifier.http is None
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"active": False}, {"active": "true"}, {"iss": "https://evil.test"}, {"exp": 1},
    {"exp": True}, {"exp": float("nan")}, {"exp": 10**400}, {"exp": None}, {"nbf": time.time()+86400},
    {"nbf": "0"}, {"aud": "wrong-resource"}, {"aud": [None]}, {"aud": {}},
    {"client_id": "unknown"}, {"token_type": None}, {"token_type": {}},
    {"token_type": "Refresh"}, {"sub": "unbound"}, {"sub": []}, {"scope": []}, {"scope": "x"*4097},
])
def test_untrusted_claims_fail_closed(config, change):
    verifier, _, _ = verifier_for(config, claims(config) | change)
    async def run():
        await verifier.start()
        try:
            with pytest.raises(ApplicationError) as error:
                await verifier.authenticate(request())
            assert error.value.status == 401
        finally:
            await verifier.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", [
    {"issuer": "https://other.test"}, {"code_challenge_methods_supported": ["plain"]},
    {"introspection_endpoint": "https://evil.test/steal"}, {"token_endpoint": "http://identity.test/token"},
    {"revocation_endpoint": None}, {"grant_types_supported": ["password"]},
    {"introspection_endpoint_auth_methods_supported": []},
])
def test_discovery_pins_issuer_endpoints_and_pkce(config, change):
    verifier, calls, _ = verifier_for(config, metadata=discovery(config) | change)
    async def run():
        with pytest.raises(ValueError, match="failed validation"):
            await verifier.start()
        assert verifier.http is None and verifier.secret is None
        assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("response", [
    httpx2.Response(302, headers={"Location": "https://evil.test"}),
    httpx2.Response(500, text="private upstream diagnostics"),
    httpx2.Response(200, content=b"x"*65537), httpx2.Response(200, json=[]),
    httpx2.Response(200, text="not json"),
])
def test_identity_unavailable_sanitized(config, response):
    verifier, calls, _ = verifier_for(config, response=response)
    async def run():
        await verifier.start()
        try:
            with pytest.raises(ApplicationError) as error:
                await verifier.authenticate(request())
            assert error.value.status == 503
            assert error.value.message == "Identity verification is unavailable."
            assert len(calls) == 2
        finally:
            await verifier.close()
    asyncio.run(run())


@pytest.mark.parametrize("value", ["http://ocr.test", "https://OCR.test", "https://ocr.test/", "https://x\".test",
    "https://u:p@ocr.test", "https://ocr.test?q=x", "https://ocr.test#x", "https://*.test", "https://ocr.test:0",
    "https://ocr.test\\x", "https://ocr.test/%20", "https://ocr.test/../x", "https://ocr.test/x//y"])
def test_invalid_urls(config, value):
    with pytest.raises(ValueError):
        replace(config, origin=value)
    with pytest.raises(ValueError):
        replace(config, issuer=value)


def test_remote_surface_restricts_routes_transport_and_credentials(config, tmp_path):
    verifier, calls, _ = verifier_for(config)
    jobs = JobStore(tmp_path / "data")
    owned = jobs.create("mine.pdf", b"synthetic", owner_user_id=1)
    other = jobs.create("other.pdf", b"synthetic", owner_user_id=2)
    app = create_remote_app(config, verifier=verifier, service_provider=lambda: JobApplication(jobs))
    # Real application routes, with isolated storage; no global worker lifespan.
    async def run():
        await verifier.start()
        try:
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=config.origin) as client:
                r = await client.get(METADATA_PATH)
                assert r.status_code == 200 and r.json()["resource"] == config.resource
                assert r.headers["cache-control"] == "no-store"
                r = await client.post("/mcp")
                assert r.status_code == 401 and 'resource_metadata="https://ocr.test/' in r.headers["www-authenticate"]
                for path in ("/", "/health", "/readiness", "/docs", "/openapi.json", "/auth/login", "/api/jobs", "/api/v1/services", "/mcp/", "/api/v1/future"):
                    assert (await client.get(path)).status_code == 404
                for headers in ({"host": "evil.test"}, {"origin": "https://evil.test"}, {"origin": "null"},
                                [("host", "ocr.test"), ("host", "ocr.test")]):
                    assert (await client.get(METADATA_PATH, headers=headers)).status_code == 403
                assert (await client.get("http://ocr.test/api/v1/me", headers={"x-forwarded-proto": "https"})).status_code == 403
                assert (await client.get(METADATA_PATH + "?access_token=test")).status_code == 400
                assert (await client.get("/api/v1/me", headers={"authorization": "Bearer ocr_svc_test"})).status_code == 401
                assert len(calls) == 1  # All rejected before introspection.
                r = await client.get("/api/v1/me", headers={"authorization": "Bearer synthetic-access-token"})
                assert r.status_code == 200, r.text
                assert r.json()["subject"] == "user:1" and r.json()["scopes"] == ["jobs:read"]
                assert len(calls) == 2  # Dependency override doesn't re-introspect.
                headers = {"authorization": "Bearer synthetic-access-token"}
                assert (await client.get(f"/api/v1/jobs/{owned.job_id}", headers=headers)).status_code == 200
                assert (await client.get(f"/api/v1/jobs/{other.job_id}", headers=headers)).status_code == 404
                listing = await client.get("/api/v1/jobs", headers=headers)
                assert [job["job_id"] for job in listing.json()["items"]] == [owned.job_id]
                r = await client.post("/api/v1/upload-sessions", headers={"authorization": "Bearer synthetic-access-token"},
                                      json={"filename": "test.pdf", "size_bytes": 100})
                assert r.status_code == 403
        finally:
            await verifier.close()
            jobs._conn.close()
    asyncio.run(run())


def test_user_store_failure_is_sanitized(config):
    verifier, _, _ = verifier_for(config)
    def unavailable():
        raise sqlite3.OperationalError("private diagnostic")
    verifier.users = unavailable
    async def run():
        await verifier.start()
        try:
            with pytest.raises(ApplicationError) as error:
                await verifier.authenticate(request())
            assert error.value.status == 503 and "private diagnostic" not in str(error.value)
        finally:
            await verifier.close()
    asyncio.run(run())


def test_configuration_rejects_relative_paths_and_invalid_bindings(config, monkeypatch, tmp_path):
    for change in ({"secret_file": tmp_path.relative_to(tmp_path.anchor)},
                   {"ca_file": "ca.pem"}, {"allowed_clients": "ocr-test"},
                   {"introspection_client_id": "bad\nheader"},
                   {"subjects": {"subject": {"user_id": True, "scopes": ["jobs:read"]}}},
                   {"subjects": {"subject": {"user_id": 1, "scopes": ["admin"]}}}):
        with pytest.raises(ValueError):
            replace(config, **change)
    path = tmp_path / "config.json"
    data = {"origin": config.origin, "issuer": config.issuer,
            "secret_file": str(config.secret_file), "introspection_client_id": config.introspection_client_id,
            "subjects": config.subjects, "allowed_clients": list(config.allowed_clients)}
    path.write_text(json.dumps(data))
    monkeypatch.setenv("OCR_REMOTE_CONFIG_FILE", str(path))
    assert RemoteConfig.from_environment() == config
    path.write_text(json.dumps(data | {"allowed_clients": "ocr-test"}))
    with pytest.raises(ValueError, match="valid absolute private configuration"):
        RemoteConfig.from_environment()


def test_startup_failure_does_not_start_storage_workers(config):
    verifier, _, _ = verifier_for(config, metadata={})
    started = []
    async def inner(scope, receive, send):
        await receive()
        started.append(True)
    surface = RemoteSurface(inner, verifier)
    messages = []
    async def run():
        async def receive():
            return {"type": "lifespan.startup"}
        async def send(message):
            messages.append(message)
        with pytest.raises(ValueError, match="OAuth provider validation failed"):
            await surface({"type": "lifespan"}, receive, send)
    asyncio.run(run())
    assert not started
    assert messages == [{"type": "lifespan.startup.failed", "message": "OAuth provider validation failed."}]


def test_realm_requires_pkce_exact_redirects_and_no_embedded_users():
    realm = build_realm("https://ocr.test", {"ocr-test": ["http://127.0.0.1:9187/callback"]})
    client = realm["clients"][0]
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    assert client["consentRequired"] and not client["directAccessGrantsEnabled"] and not client["implicitFlowEnabled"]
    assert realm["sslRequired"] == "all" and "users" not in realm
    assert realm["revokeRefreshToken"] and realm["accessTokenLifespan"] == 300
    assert realm["clients"][-1]["secret"] == "${OCR_INTROSPECTION_SECRET}"
    mapper = realm["clientScopes"][0]["protocolMappers"][0]["config"]
    assert mapper["included.custom.audience"] == "https://ocr.test/mcp"
    mappers = realm["clientScopes"][0]["protocolMappers"]
    assert mappers[1]["config"]["included.client.audience"] == "ocr-resource"
    assert mappers[2]["protocolMapper"] == "oidc-sub-mapper"
    for value in ("http://external.test/callback", "https://client.test/*", "https://client.test/cb?next=x", "javascript:alert(1)"):
        with pytest.raises(ValueError):
            build_realm("https://ocr.test", {"ocr-test": [value]})
