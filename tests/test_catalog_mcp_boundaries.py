"""Catalog MCP boundaries: authorization, isolation, conflicts, input limit and shared provider.

Part 1 uses the real uvicorn runtime, real local users and real Bearer JWTs (helper catalog_accounts). The only
derived credentials are NEGATIVE authentication fixtures signed with the synthetic runtime secret (legacy JWT and
reduced-scope JWT); they never widen anything and are not productive issuance. Part 2 runs the real ASGI app over
real SQLite stores in-process; only the request authenticator is a synthetic principal (DI only)."""
import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
from uuid import uuid4

from fastapi import Request
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
import pytest

from app import main as app_main
from app.application import JobApplication, Principal, get_job_application
from app.main import create_app
from app.mcp_server import build_mcp_server
from app.services.catalog_runtime import CatalogServices
from app.services.context_service import ContextService
from app.services.context_template_service import ContextTemplateService
from app.services.storage_service import JobStore
from app.services.type_service import TypeService
from test_catalog_http import legacy_token
from test_catalog_mcp_vertical import (CATALOG_TOOLS, CREATE_TOOLS, CTX, CTX2, CTX3, DEPRECATE_TOOLS, READ_TOOLS, SCENARIO_TIMEOUT,  # noqa: F401
                                       TYP, TYP2, UPDATE_TOOLS, catalog_accounts, rest)
from test_mcp_vertical import connected, runtime  # noqa: F401  (fixtures)

try:
    from mcp.types import LATEST_PROTOCOL_VERSION as PROTOCOL  # the version supported by the installed SDK
except ImportError:
    PROTOCOL = None

SENTINEL = "PRIVATE-SENTINEL"
MIB = 1024 * 1024
TRANSPORTS = [("http", "auto"), ("stdio", "auto")]
FULL = frozenset({"contexts:read", "contexts:write", "contexts:publish"})
PRINCIPAL = Principal("user:1", 1, FULL)
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               **({"MCP-Protocol-Version": PROTOCOL} if PROTOCOL else {})}


def run(scenario):
    async def bounded():
        async with asyncio.timeout(SCENARIO_TIMEOUT):
            return await scenario()
    return asyncio.run(bounded())


async def failed(client, name, args):
    result = await client.call_tool(name, args)
    assert result.is_error, name
    return result.content[0].text


async def succeeded(client, name, args):
    result = await client.call_tool(name, args)
    assert not result.is_error, (name, result.content)
    return result.structured_content


async def catalog_calls(runtime, token, cid, tid, tag):
    """One valid call per catalog tool (revision 1 targets), using the public template catalog."""
    async with rest(runtime, token) as http:
        response = await http.get("/api/v1/context-templates")
        assert response.status_code == 200
        template = response.json()["items"][0]
    calls = {
        "ocr_list_context_templates": {},
        "ocr_clone_context_template": {
            "template_id": template["template_id"],
            "body": {"expected_version": template["version"], "expected_sha256": template["template_sha256"]},
            "idempotency_key": tag + "-clone"},
        "ocr_list_contexts": {},
        "ocr_create_context": {"body": CTX2, "idempotency_key": tag + "-create-context"},
        "ocr_get_context": {"context_id": cid},
        "ocr_update_context": {"context_id": cid, "body": {"body": CTX2, "expected_revision": 1}},
        "ocr_list_document_types": {"context_id": cid},
        "ocr_create_document_type": {"context_id": cid, "body": TYP2, "idempotency_key": tag + "-create-type"},
        "ocr_get_document_type": {"type_id": tid},
        "ocr_update_document_type": {"type_id": tid, "body": {"body": TYP2, "expected_revision": 1}},
        "ocr_publish_document_type": {"type_id": tid, "body": {"expected_revision": 1, "expected_context_revision": 1},
                                      "idempotency_key": tag + "-publish"},
        "ocr_list_document_type_versions": {"type_id": tid},
        "ocr_get_document_type_version": {"type_id": tid, "version": 1},
        "ocr_deprecate_document_type_version": {"type_id": tid, "version": 1,
                                               "body": {"expected_lifecycle_revision": 1},
                                               "idempotency_key": tag + "-deprecate"},
    }
    assert set(calls) == CATALOG_TOOLS
    return calls


async def seed(runtime, token):
    """Owner catalog through the real REST API: context rev 1, draft type rev 1, published version 1."""
    async with rest(runtime, token) as http:
        context = await http.post("/api/v1/contexts", json=CTX, headers={"Idempotency-Key": "seed-context-01"})
        assert context.status_code == 201
        cid = context.json()["context_id"]
        typ = await http.post(f"/api/v1/contexts/{cid}/types", json=TYP, headers={"Idempotency-Key": "seed-type-0001"})
        assert typ.status_code == 201
        tid = typ.json()["type_id"]
        published = await http.post(f"/api/v1/document-types/{tid}/versions", headers={"Idempotency-Key": "seed-publish-01"},
                                    json={"expected_revision": 1, "expected_context_revision": 1})
        assert published.status_code == 201
    return cid, tid


async def snapshot(runtime, token, cid, tid):
    paths = ["/contexts", f"/contexts/{cid}", f"/contexts/{cid}/types", f"/document-types/{tid}",
             f"/document-types/{tid}/versions", f"/document-types/{tid}/versions/1"]
    async with rest(runtime, token) as http:
        responses = [await http.get("/api/v1" + path) for path in paths]
    assert all(r.status_code == 200 for r in responses)
    return [r.json() for r in responses]


# ---------------------------------------------------------------- authorization and isolation (real clients)

@pytest.mark.parametrize("transport,mode", TRANSPORTS)
def test_foreign_and_admin_identities_see_not_found_and_change_nothing(runtime, catalog_accounts, transport, mode):  # noqa: F811
    denied_tools = CATALOG_TOOLS - {
        "ocr_list_contexts", "ocr_create_context", "ocr_list_context_templates", "ocr_clone_context_template"}

    async def scenario():
        cid, tid = await seed(runtime, catalog_accounts.owner)
        before = await snapshot(runtime, catalog_accounts.owner, cid, tid)
        for who in ("other", "admin"):  # the administrator gets no owner bypass
            async with connected(runtime, transport, mode, getattr(catalog_accounts, who)) as client:
                foreign = await catalog_calls(runtime, catalog_accounts.owner, cid, tid, who + "-foreign")
                missing = await catalog_calls(runtime, catalog_accounts.owner, uuid4().hex, uuid4().hex, who + "-missing")
                for name in sorted(denied_tools):
                    foreign_text = await failed(client, name, foreign[name])
                    assert "not_found" in foreign_text, (who, name)
                    assert foreign_text == await failed(client, name, missing[name]), (who, name)  # indistinguishable
                assert (await succeeded(client, "ocr_list_contexts", {}))["items"] == []
                templates = await succeeded(client, "ocr_list_context_templates", {})
                assert len(templates["items"]) == 3
                own = await succeeded(client, "ocr_clone_context_template", foreign["ocr_clone_context_template"])
                assert own["context_id"] != cid and own["context_revision"] == 1
                assert (await succeeded(client, "ocr_get_context", {"context_id": own["context_id"]}))["revision"] == 1
                for item in own["types"]:
                    assert item["type_id"] != tid and item["revision"] == 1
                    assert (await succeeded(client, "ocr_get_document_type", {"type_id": item["type_id"]}))["context_id"] == own["context_id"]
        assert await snapshot(runtime, catalog_accounts.owner, cid, tid) == before  # original document intact

    run(scenario)
    assert not (runtime.root / "stdio-must-not-create").exists()


@pytest.mark.parametrize("credential", ["service", "legacy-jwt"])
@pytest.mark.parametrize("transport,mode", TRANSPORTS)
def test_credentials_without_catalog_scopes_get_insufficient_scope(runtime, catalog_accounts, transport, mode, credential):  # noqa: F811
    # service: runtime.tokens[0] (five legacy scopes, unchanged). legacy-jwt: synthetic signed NEGATIVE, no claims.
    token = runtime.tokens[0] if credential == "service" else legacy_token(catalog_accounts.owner_id, runtime.env["JWT_SECRET"])

    async def scenario():
        cid, tid = await seed(runtime, catalog_accounts.owner)
        before = await snapshot(runtime, catalog_accounts.owner, cid, tid)
        async with connected(runtime, transport, mode, token) as client:
            calls = await catalog_calls(runtime, catalog_accounts.owner, cid, tid, credential)
            for name, args in calls.items():
                assert "insufficient_scope" in await failed(client, name, args), name
        assert await snapshot(runtime, catalog_accounts.owner, cid, tid) == before

    run(scenario)
    assert not (runtime.root / "stdio-must-not-create").exists()


def reduced_token(token, scopes, secret):
    """Negative-authentication fixture, NOT productive issuance: the real owner claims (sub/role/exp/version) are
    kept, the scopes are only reduced, and the result is signed with the synthetic runtime secret."""
    head, body, _ = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert {"sub", "role", "exp", "ocr_authz_version", "scopes"} <= set(claims)
    assert set(scopes) < set(claims["scopes"])  # strictly narrower, never wider
    claims["scopes"] = sorted(scopes)
    encode = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    signing = head + "." + encode(json.dumps(claims, separators=(",", ":")).encode())
    return signing + "." + encode(hmac.new(secret.encode(), signing.encode(), hashlib.sha256).digest())


@pytest.mark.parametrize("scopes,denied", [
    (frozenset({"contexts:read"}), CREATE_TOOLS | UPDATE_TOOLS | DEPRECATE_TOOLS),
    (frozenset({"contexts:read", "contexts:write"}), {"ocr_publish_document_type", "ocr_deprecate_document_type_version"}),
], ids=["read-only", "read-write"])
@pytest.mark.parametrize("transport,mode", TRANSPORTS)
def test_reduced_scope_users_cannot_exceed_their_scopes(runtime, catalog_accounts, transport, mode, scopes, denied):  # noqa: F811
    token = reduced_token(catalog_accounts.owner, scopes, runtime.env["JWT_SECRET"])

    async def scenario():
        cid, tid = await seed(runtime, catalog_accounts.owner)
        before = await snapshot(runtime, catalog_accounts.owner, cid, tid)
        async with connected(runtime, transport, mode, token) as client:
            calls = await catalog_calls(runtime, catalog_accounts.owner, cid, tid, "reduced")
            for name in sorted(denied):
                assert "insufficient_scope" in await failed(client, name, calls[name]), name
            assert await snapshot(runtime, catalog_accounts.owner, cid, tid) == before
            for name in sorted(READ_TOOLS):
                await succeeded(client, name, calls[name])
            if "ocr_update_context" not in denied:
                assert (await succeeded(client, "ocr_update_context", calls["ocr_update_context"]))["revision"] == 2
                cloned = await succeeded(client, "ocr_clone_context_template", calls["ocr_clone_context_template"])
                assert cloned["context_revision"] == 1 and cloned["context_id"] != cid
                assert (await succeeded(client, "ocr_get_context", {"context_id": cloned["context_id"]}))["revision"] == 1
                for item in cloned["types"]:
                    assert item["revision"] == 1
                    assert (await succeeded(client, "ocr_list_document_type_versions", {"type_id": item["type_id"]}))["items"] == []

    run(scenario)
    assert not (runtime.root / "stdio-must-not-create").exists()


def test_mcp_http_without_authorization_is_401_before_any_session(runtime, catalog_accounts):  # noqa: F811
    initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": PROTOCOL or "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "synthetic", "version": "0"}}}

    async def scenario():
        # Fresh client: no headers, no cookies, no service fallback. This is the HTTP boundary, not a tool is_error.
        async with httpx2.AsyncClient(base_url=runtime.origin, trust_env=False, timeout=15) as anonymous:
            for extra in ({}, {"Authorization": "Bearer not-a-token"}):
                response = await anonymous.post("/mcp", json=initialize, headers={**MCP_HEADERS, **extra})
                assert response.status_code == 401
                assert response.headers["www-authenticate"] == "Bearer"
                assert response.json()["error"]["code"] == "authentication_required"

    run(scenario)


# ---------------------------------------------------------------- conflicts and validation (real clients)

@pytest.mark.parametrize("transport,mode", TRANSPORTS)
def test_idempotency_and_revision_conflicts_do_not_change_records(runtime, catalog_accounts, transport, mode):  # noqa: F811
    async def scenario():
        async with connected(runtime, transport, mode, catalog_accounts.owner) as client:
            receipt = await succeeded(client, "ocr_create_context", {"body": CTX, "idempotency_key": "conflict-ctx-01"})
            cid = receipt["context_id"]
            assert await succeeded(client, "ocr_create_context", {"body": CTX, "idempotency_key": "conflict-ctx-01"}) == receipt
            assert "idempotency_conflict" in await failed(
                client, "ocr_create_context", {"body": CTX2, "idempotency_key": "conflict-ctx-01"})
            record = await succeeded(client, "ocr_get_context", {"context_id": cid})
            assert record["revision"] == 1 and record["body"]["name"] == CTX["name"]
            assert len((await succeeded(client, "ocr_list_contexts", {}))["items"]) == 1

            updated = await succeeded(client, "ocr_update_context", {"context_id": cid, "body": {"body": CTX2, "expected_revision": 1}})
            assert updated["revision"] == 2
            # Stale revision: rejected, never merged. No automatic resend; the caller reads the current record.
            assert "revision_conflict" in await failed(
                client, "ocr_update_context", {"context_id": cid, "body": {"body": CTX3, "expected_revision": 1}})
            assert await succeeded(client, "ocr_get_context", {"context_id": cid}) == updated

            typ = await succeeded(client, "ocr_create_document_type", {"context_id": cid, "body": TYP, "idempotency_key": "conflict-type-01"})
            tid = typ["type_id"]
            assert "idempotency_conflict" in await failed(
                client, "ocr_create_document_type", {"context_id": cid, "body": TYP2, "idempotency_key": "conflict-type-01"})
            draft = await succeeded(client, "ocr_update_document_type", {"type_id": tid, "body": {"body": TYP2, "expected_revision": 1}})
            assert "revision_conflict" in await failed(
                client, "ocr_update_document_type", {"type_id": tid, "body": {"body": TYP, "expected_revision": 1}})
            assert await succeeded(client, "ocr_get_document_type", {"type_id": tid}) == draft
            stale_context = {"expected_revision": 2, "expected_context_revision": 1}
            assert "revision_conflict" in await failed(
                client, "ocr_publish_document_type", {"type_id": tid, "body": stale_context, "idempotency_key": "conflict-pub-001"})
            assert (await succeeded(client, "ocr_list_document_type_versions", {"type_id": tid}))["items"] == []

    run(scenario)
    assert not (runtime.root / "stdio-must-not-create").exists()


@pytest.mark.parametrize("transport,mode", TRANSPORTS)
def test_invalid_inputs_are_validation_errors_without_echo_or_writes(runtime, catalog_accounts, transport, mode):  # noqa: F811
    async def scenario():
        cid, tid = await seed(runtime, catalog_accounts.owner)
        before = await snapshot(runtime, catalog_accounts.owner, cid, tid)
        key = "validation-key-01"
        update = lambda **fields: {"context_id": cid, "body": {"body": CTX2, "expected_revision": 1, **fields}}
        type_update = lambda **fields: {"type_id": tid, "body": {"body": TYP2, "expected_revision": 1, **fields}}
        cases = [
            ("ocr_create_context", {"body": {**CTX, "owner": SENTINEL}, "idempotency_key": key}),
            ("ocr_create_context", {"body": {**CTX, "owner_id": SENTINEL}, "idempotency_key": key}),
            ("ocr_create_context", {"body": CTX, "idempotency_key": key, "owner_id": SENTINEL}),
            ("ocr_create_context", {"body": CTX, "idempotency_key": "bad key " + SENTINEL}),
            ("ocr_create_context", {"body": SENTINEL, "idempotency_key": key}),
            ("ocr_create_context", {"body": {}, "idempotency_key": key}),
            ("ocr_create_context", {"idempotency_key": key}),
            ("ocr_get_context", {"context_id": "../" + SENTINEL}),
            ("ocr_get_context", {"context_id": cid, "path": SENTINEL}),
            ("ocr_update_context", update(owner=SENTINEL)),
            ("ocr_update_context", update(path=SENTINEL)),
            ("ocr_update_context", update(expected_revision=True)),
            ("ocr_update_context", update(expected_revision="1" + SENTINEL)),
            ("ocr_update_context", {"context_id": cid, "body": {"body": CTX2}}),
            ("ocr_update_document_type", type_update(owner=SENTINEL)),
            ("ocr_update_document_type", type_update(expected_revision=True)),
            ("ocr_update_document_type", type_update(expected_revision="1" + SENTINEL)),
            ("ocr_deprecate_document_type_version", {"type_id": tid, "version": True,
                                                     "body": {"expected_lifecycle_revision": 1}, "idempotency_key": key}),
            ("ocr_deprecate_document_type_version", {"type_id": tid, "version": "1",
                                                     "body": {"expected_lifecycle_revision": 1}, "idempotency_key": key}),
            ("ocr_deprecate_document_type_version", {"type_id": tid, "version": 1,
                                                     "body": {"expected_lifecycle_revision": True}, "idempotency_key": key}),
            ("ocr_deprecate_document_type_version", {"type_id": tid, "version": 1,
                                                     "body": {"expected_lifecycle_revision": "1"}, "idempotency_key": key}),
            ("ocr_deprecate_document_type_version", {"type_id": tid, "version": 1,
                                                     "body": {"expected_lifecycle_revision": 2**31}, "idempotency_key": key}),
            ("ocr_deprecate_document_type_version", {"type_id": tid, "version": 1,
                                                     "body": {"expected_lifecycle_revision": 1, "owner": SENTINEL},
                                                     "idempotency_key": key}),
            ("ocr_deprecate_document_type_version", {"type_id": tid, "version": 1,
                                                     "body": {"expected_lifecycle_revision": 1},
                                                     "idempotency_key": "bad key " + SENTINEL}),
            ("ocr_publish_document_type", {"type_id": tid, "idempotency_key": key,
                                           "body": {"expected_revision": 1, "expected_context_revision": True}}),
            ("ocr_publish_document_type", {"type_id": tid, "idempotency_key": key,
                                           "body": {"expected_revision": 1, "expected_context_revision": 1, "owner": SENTINEL}}),
        ]
        async with connected(runtime, transport, mode, catalog_accounts.owner) as client:
            for name, args in cases:
                text = await failed(client, name, args)
                assert "validation_error" in text, (name, text)
                assert SENTINEL not in text, name
        assert await snapshot(runtime, catalog_accounts.owner, cid, tid) == before  # nothing created, edited or published

    run(scenario)
    assert not (runtime.root / "stdio-must-not-create").exists()


# ---------------------------------------------------------------- MCP HTTP request size limit

def test_mcp_http_chunked_body_over_limit_is_413_before_parsing(runtime, catalog_accounts):  # noqa: F811
    seen = []

    async def record(request):
        seen.append(request.headers)

    async def chunks():
        # Deliberately invalid JSON + marker, then whitespace: a parser would answer 400, the size limit must win.
        yield (b"{not-json " + SENTINEL.encode()).ljust(MIB, b" ")
        yield b" " * MIB
        yield b" " * MIB  # 3 MiB total against the 2 MiB MCP limit

    async def scenario():
        cid, tid = await seed(runtime, catalog_accounts.owner)
        before = await snapshot(runtime, catalog_accounts.owner, cid, tid)
        headers = {**MCP_HEADERS, "Authorization": "Bearer " + catalog_accounts.owner}
        async with httpx2.AsyncClient(trust_env=False, timeout=30, event_hooks={"request": [record]}) as http:
            chunked = await http.post(runtime.origin + "/mcp", headers=headers, content=chunks())
            assert chunked.status_code == 413
            known = await http.post(runtime.origin + "/mcp", headers=headers, content=b" " * (2 * MIB + 1))
            assert known.status_code == 413
        assert seen[0].get("transfer-encoding") == "chunked" and "content-length" not in seen[0]
        assert seen[1].get("content-length") == str(2 * MIB + 1)
        for response in (chunked, known):  # the 413 body format belongs to the MCP SDK; only its content is checked
            assert SENTINEL not in response.text and "Traceback" not in response.text
        assert await snapshot(runtime, catalog_accounts.owner, cid, tid) == before

    run(scenario)


# ---------------------------------------------------------------- shared provider (real ASGI + SQLite, in-process)

async def authenticate(request: Request) -> Principal:
    """Synthetic principal, dependency-injection test only (never used for the real authentication tests)."""
    return PRINCIPAL


@asynccontextmanager
async def in_process(server, asgi):
    async with server.session_manager.run():  # not the full app lifespan: no global workers
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=asgi), headers={"Authorization": "Bearer synthetic"}) as http:
            async with Client(streamable_http_client("http://localhost/mcp", http_client=http)) as client:
                yield client


def test_catalog_provider_follows_the_dependency_override(tmp_path, monkeypatch):
    captured = {}
    original = app_main.build_mcp_server

    def capture(*args, **kwargs):  # only delegates and remembers the real server and transport
        captured["server"], captured["transport"] = built = original(*args, **kwargs)
        return built

    monkeypatch.setattr(app_main, "build_mcp_server", capture)
    store_a, store_b = JobStore(root_dir=tmp_path / "a"), JobStore(root_dir=tmp_path / "b")
    resolved = []

    def provider_a():
        resolved.append("a")
        return JobApplication(store_a)

    application = create_app(provider_a, request_authenticator=authenticate)
    assert resolved == [] and captured["server"] is not None

    def keys(page):
        return [item["context_id"] for item in page["items"]]

    async def scenario():
        async with in_process(captured["server"], application) as mcp:
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=application), base_url="http://localhost") as http:
                assert CATALOG_TOOLS <= {t.name for t in (await mcp.list_tools()).tools}
                assert resolved == []  # discovery never resolves a store
                created = await http.post("/api/v1/contexts", json=CTX, headers={"Idempotency-Key": "provider-ctx-a1"})
                assert created.status_code == 201 and resolved
                cid_a = created.json()["context_id"]
                assert (await succeeded(mcp, "ocr_get_context", {"context_id": cid_a}))["revision"] == 1
                pair_a = application.state.catalog_provider()
                assert pair_a is application.state.catalog_provider()
                assert [i.context_id for i in pair_a.contexts.list(PRINCIPAL).items] == [cid_a]

                # Override to store B: both transports follow it and neither sees A.
                application.dependency_overrides[get_job_application] = lambda: JobApplication(store_b)
                assert (await http.get("/api/v1/contexts")).json()["items"] == []
                assert (await succeeded(mcp, "ocr_list_contexts", {}))["items"] == []
                assert (await http.get(f"/api/v1/contexts/{cid_a}")).status_code == 404
                assert "not_found" in await failed(mcp, "ocr_get_context", {"context_id": cid_a})
                receipt = await succeeded(mcp, "ocr_create_context", {"body": CTX2, "idempotency_key": "provider-ctx-b1"})
                cid_b = receipt["context_id"]
                assert (await http.get(f"/api/v1/contexts/{cid_b}")).json()["body"]["name"] == CTX2["name"]
                pair_b = application.state.catalog_provider()
                assert pair_b is not pair_a and pair_b is application.state.catalog_provider()
                assert [i.context_id for i in pair_b.contexts.list(PRINCIPAL).items] == [cid_b]
                assert [i.context_id for i in pair_a.contexts.list(PRINCIPAL).items] == [cid_a]  # A kept, B not copied into it

                # Removing the override returns to the fixture's original store; both transports agree again.
                del application.dependency_overrides[get_job_application]
                assert keys((await http.get("/api/v1/contexts")).json()) == [cid_a]
                assert keys(await succeeded(mcp, "ocr_list_contexts", {})) == [cid_a]
                assert [i.context_id for i in application.state.catalog_provider().contexts.list(PRINCIPAL).items] == [cid_a]

    try:
        run(scenario)
    finally:  # stores are closed only after the session and every client are finished
        store_a._conn.close()
        store_b._conn.close()


def test_build_mcp_server_catalog_provider_is_keyword_only_and_lazy(tmp_path):
    store = JobStore(root_dir=tmp_path)
    calls = []

    def provider():
        calls.append("called")
        return CatalogServices(ContextService(store), TypeService(store), ContextTemplateService(store))

    with pytest.raises(TypeError):
        build_mcp_server(None, None, None, None, None, None, provider)
    server, asgi = build_mcp_server(catalog_provider=provider, request_authenticator=authenticate)
    assert calls == []

    async def scenario():
        async with in_process(server, asgi) as client:
            assert CATALOG_TOOLS <= {t.name for t in (await client.list_tools()).tools} and calls == []
            receipt = await succeeded(client, "ocr_create_context", {"body": CTX, "idempotency_key": "lazy-provider-01"})
            assert calls
            assert (await succeeded(client, "ocr_get_context", {"context_id": receipt["context_id"]}))["revision"] == 1

    try:
        run(scenario)
    finally:
        store._conn.close()


@pytest.mark.parametrize("failure,code", [(OSError(SENTINEL + " C:\\private\\path"), "storage_unavailable"),
                                        (RuntimeError(SENTINEL + " Traceback"), "internal_error")])
def test_provider_failures_are_sanitized_and_updates_say_read_first(failure, code):
    def broken():
        raise failure

    server, asgi = build_mcp_server(catalog_provider=broken, request_authenticator=authenticate)
    missing = uuid4().hex

    async def scenario():
        async with in_process(server, asgi) as client:
            texts = {
                "get": await failed(client, "ocr_get_context", {"context_id": missing}),
                "update": await failed(client, "ocr_update_context",
                                       {"context_id": missing, "body": {"body": CTX2, "expected_revision": 1}}),
                "create": await failed(client, "ocr_create_context", {"body": CTX, "idempotency_key": "provider-fail-01"}),
            }
            for text in texts.values():
                assert code in text and SENTINEL not in text and "private" not in text and "Traceback" not in text
            assert "read the current revision" in texts["update"] and "same key" not in texts["update"]
            assert "same key" in texts["create"]

    run(scenario)
