"""Pending lifecycle adapters, exercised without registering a deprecation tool."""
import asyncio
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver.exceptions import ToolError
import pytest

from app.application import ApplicationError, JobApplication, Principal
from app.catalog_models import ContextCreate, TypeDeprecateInput, TypeDraft
from app.main import create_app
from app.mcp_server import build_mcp_server, execute_catalog
from app.mcp_stdio import ApiClient, MAX_RESPONSE_BYTES
from app.security import require_principal
from app.services.catalog_runtime import ContextService, TypeService
from app.services.storage_service import JobStore
from catalog_tool_expectations import assert_catalog_annotations, assert_tool_inventory

OPERATION = "deprecate_document_type_version"
SCOPES = frozenset({"contexts:read", "contexts:write", "contexts:publish"})
KEY = "deprecate-key-0001"


def dump(model):
    return model.model_dump(mode="json", by_alias=True)


def database_rows(store):
    with store._lock:
        return tuple(store._conn.iterdump())


def snapshot_rows(store):
    """Read persisted schema/hash columns independently of lifecycle metadata."""
    def identifier(value):
        return '"' + value.replace('"', '""') + '"'

    snapshots = {}
    with store._lock:
        tables = store._conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        for (table,) in tables:
            columns = [row[1] for row in store._conn.execute(f"PRAGMA table_info({identifier(table)})")]
            if "schema_sha256" not in columns or "version" not in columns:
                continue
            selected = [name for name in columns if "lifecycle" not in name and (
                name in {"type_id", "version"} or any(part in name for part in ("schema", "snapshot", "canonical"))
                or name.endswith("_json"))]
            assert any(name != "schema_sha256" and any(part in name for part in ("json", "canonical", "snapshot"))
                       for name in selected)
            sql = "SELECT " + ", ".join(map(identifier, selected)) + " FROM " + identifier(table)
            snapshots[table] = (tuple(selected), sorted((tuple(row) for row in store._conn.execute(sql)), key=repr))
    assert snapshots, "Expected persisted version snapshots and hashes"
    return snapshots


def assert_deprecated(payload):
    lifecycle = payload["lifecycle"]
    assert "deprecated" in lifecycle.values()
    assert any(type(value) is int and value == 2 for value in lifecycle.values())
    changed_at = datetime.fromisoformat(lifecycle["changed_at"].replace("Z", "+00:00"))
    assert changed_at.utcoffset() == timedelta(0)


@pytest.fixture
def catalog(tmp_path):
    store = JobStore(root_dir=tmp_path)
    try:
        contexts = ContextService(store)
        types = TypeService(store)
        principal = Principal(subject="owner", user_id=1, scopes=SCOPES)
        context = contexts.create(principal=principal, body=ContextCreate(name="Ctx"), key="context-key-0001")[0]
        draft = types.create(principal=principal, context_id=context.context_id,
                             body=TypeDraft(name="Synthetic", specific_fields=[]), key="type-key-0001")[0]
        published = types.publish(principal, draft.type_id, expected_revision=1,
                                  expected_context_revision=1, key="publish-key-0001")[0]
        yield SimpleNamespace(store=store, contexts=contexts, types=types, principal=principal,
                              type_id=draft.type_id, published=published)
    finally:
        with store._lock:
            store._conn.close()


def arguments(catalog, revision=1, key=KEY):
    return (catalog.type_id, 1, TypeDeprecateInput(expected_lifecycle_revision=revision), key)


def test_execute_catalog_action_replay_conflicts_and_immutable_snapshot(catalog):
    snapshots = snapshot_rows(catalog.store)
    original_version = dump(catalog.types.get_version(catalog.principal, catalog.type_id, 1))
    publication = dump(catalog.published)
    receipt = execute_catalog(catalog, catalog.principal, OPERATION, arguments(catalog))
    assert dump(execute_catalog(catalog, catalog.principal, OPERATION, arguments(catalog))) == dump(receipt)
    assert_deprecated(dump(receipt))
    assert receipt.type_id == catalog.type_id and receipt.version == 1
    assert receipt.schema_sha256 == catalog.published.schema_sha256
    assert snapshot_rows(catalog.store) == snapshots
    current_version = dump(catalog.types.get_version(catalog.principal, catalog.type_id, 1))
    assert_deprecated(current_version)
    assert {k: v for k, v in current_version.items() if k != "lifecycle"} == {
        k: v for k, v in original_version.items() if k != "lifecycle"}
    for revision, key in ((1, "stale-key-0001"), (2, KEY), (2, "deprecated-key-0001")):
        before = database_rows(catalog.store)
        with pytest.raises(ApplicationError) as error:
            execute_catalog(catalog, catalog.principal, OPERATION, arguments(catalog, revision, key))
        assert error.value.status == 409
        assert database_rows(catalog.store) == before
    replay = catalog.types.publish(catalog.principal, catalog.type_id, expected_revision=1,
                                   expected_context_revision=1, key="publish-key-0001")[0]
    assert dump(replay) == publication
    assert "lifecycle" not in publication


@pytest.mark.parametrize("principal,status", [
    (Principal(subject="owner", user_id=1, scopes=frozenset({"contexts:read"})), 403),
    (Principal(subject="owner", user_id=1, scopes=frozenset({"contexts:publish"})), 403),
    (Principal(subject="foreign-admin", user_id=2, scopes=SCOPES, is_admin=True), 404),
])
def test_execute_catalog_denials_do_not_write(catalog, principal, status):
    before = database_rows(catalog.store)
    with pytest.raises(ApplicationError) as error:
        execute_catalog(catalog, principal, OPERATION, arguments(catalog))
    assert error.value.status == status
    assert database_rows(catalog.store) == before


def test_api_client_uses_real_http_action_replay_reads_and_denials(catalog):
    api = create_app(lambda: JobApplication(catalog.store))
    current = [catalog.principal]
    api.dependency_overrides[require_principal] = lambda: current[0]
    requests, statuses = [], []

    async def observe_request(request):
        requests.append((request.method, request.url.path, request.url.query,
                         request.headers.get("Idempotency-Key"), request.headers.get("Content-Type"),
                         bytes(request.content)))

    async def observe_response(response):
        statuses.append(response.status_code)

    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=api),
                                     event_hooks={"request": [observe_request], "response": [observe_response]}) as http:
            client = ApiClient("http://localhost", http)
            snapshots = snapshot_rows(catalog.store)
            receipt = await client.invoke(None, OPERATION, *arguments(catalog))
            assert await client.invoke(None, OPERATION, *arguments(catalog)) == receipt
            assert statuses == [200, 200]
            path = f"/api/v1/document-types/{catalog.type_id}/versions/1/deprecate"
            expected_body = dump(arguments(catalog)[2])
            assert len(requests) == 2
            for method, actual_path, query, key, content_type, content in requests:
                assert (method, actual_path, query, key, content_type) == ("POST", path, b"", KEY, "application/json")
                assert json.loads(content) == expected_body
            assert receipt == dump(catalog.types.deprecate(catalog.principal, catalog.type_id, 1,
                                                          expected_lifecycle_revision=1, key=KEY)[0])
            assert_deprecated(receipt)
            version = await http.get(f"http://localhost/api/v1/document-types/{catalog.type_id}/versions/1")
            listing = await http.get(f"http://localhost/api/v1/document-types/{catalog.type_id}/versions")
            assert version.status_code == listing.status_code == 200
            assert_deprecated(version.json())
            assert_deprecated(next(item for item in listing.json()["items"] if item["version"] == 1))
            assert version.json()["schema_sha256"] == receipt["schema_sha256"]
            assert snapshot_rows(catalog.store) == snapshots
            for principal, status in (
                (Principal(subject="owner", user_id=1, scopes=frozenset({"contexts:read"})), 403),
                (Principal(subject="owner", user_id=1, scopes=frozenset({"contexts:publish"})), 403),
                (Principal(subject="foreign-admin", user_id=2, scopes=SCOPES, is_admin=True), 404),
            ):
                current[0] = principal
                before = database_rows(catalog.store)
                with pytest.raises(ToolError, match=f"HTTP {status}"):
                    await client.invoke(None, OPERATION, *arguments(catalog, key="denied-key-0001"))
                assert database_rows(catalog.store) == before

    try:
        asyncio.run(scenario())
    finally:
        api.dependency_overrides.clear()


@pytest.mark.parametrize("type_id,version", [
    ("../document-types", 1), ("A" * 32, 1), ("a" * 31, 1), (None, 1),
    ("a" * 32 + "/versions/1", 1), ("a" * 32, True), ("a" * 32, False),
    ("a" * 32, 0), ("a" * 32, -1), ("a" * 32, 1.0), ("a" * 32, "1"),
    ("a" * 32, "latest"), ("a" * 32, 2**63),
])
def test_invalid_pending_resource_never_sends_http(type_id, version):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(200, json={})

    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
            with pytest.raises(ToolError, match="(?:type|version)_not_found"):
                await ApiClient("http://localhost", http).invoke(
                    None, OPERATION, type_id, version, TypeDeprecateInput(expected_lifecycle_revision=1), KEY)
        assert calls == []

    asyncio.run(scenario())


def test_pending_version_upper_bound_and_unknown_operation():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(200, json={})

    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
            client = ApiClient("http://localhost", http)
            args = ("a" * 32, 2**63 - 1, TypeDeprecateInput(expected_lifecycle_revision=1), KEY)
            assert await client.invoke(None, OPERATION, *args) == {}
            assert len(calls) == 1
            assert calls[0].url.path == f"/api/v1/document-types/{'a' * 32}/versions/{2**63 - 1}/deprecate"
            with pytest.raises(ToolError, match="unknown_operation"):
                await client.invoke(None, OPERATION + "_other", *args)
            assert len(calls) == 1

    asyncio.run(scenario())


def test_pending_body_is_bounded_before_http(monkeypatch):
    import app.mcp_stdio as stdio
    monkeypatch.setattr(stdio, "MAX_CATALOG_REQUEST_BYTES", 1)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx2.Response(200, json={})

    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
            with pytest.raises(ToolError, match="request_too_large"):
                await ApiClient("http://localhost", http).invoke(
                    None, OPERATION, "a" * 32, 1, TypeDeprecateInput(expected_lifecycle_revision=1), KEY)
        assert calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("failure,code", [
    ("timeout", "api_unavailable"), ("redirect", "upstream_redirect"),
    ("oversized", "upstream_response_limit"), ("invalid", "invalid_api_response"),
    ("storage", "storage_unavailable"),
])
def test_pending_http_boundaries_do_not_retry_or_leak(failure, code, caplog):
    calls = []
    token = "synthetic-bearer-token"

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer " + token
        assert request.headers["Idempotency-Key"] == KEY
        if failure == "timeout":
            raise TimeoutError("PRIVATE endpoint/credential")
        if failure == "redirect":
            return httpx2.Response(302, headers={"Location": "https://private.invalid/PRIVATE"})
        if failure == "oversized":
            return httpx2.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
        if failure == "invalid":
            return httpx2.Response(200, content=b"PRIVATE invalid JSON")
        return httpx2.Response(503, json={"error": {"code": "storage_unavailable", "message": "PRIVATE"}})

    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler), follow_redirects=False,
                                     trust_env=False, headers={"Authorization": "Bearer " + token}) as http:
            with pytest.raises(ToolError, match=code) as error:
                await ApiClient("http://localhost", http).invoke(
                    None, OPERATION, "a" * 32, 1, TypeDeprecateInput(expected_lifecycle_revision=1), KEY)
            message = str(error.value)
            assert "PRIVATE" not in message and token not in message
            if failure in {"timeout", "storage"}:
                assert "same key" in message
                assert "read the current revision" not in message
            assert len(calls) == 1
        assert token not in caplog.text

    asyncio.run(scenario())


def test_actual_registry_retains_shared_inventory(catalog):
    async def authenticate(_request):
        return catalog.principal

    server, transport = build_mcp_server(lambda: JobApplication(catalog.store),
                                         catalog_provider=lambda: catalog,
                                         request_authenticator=authenticate)

    async def scenario():
        async with server.session_manager.run():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=transport)) as http:
                async with Client(streamable_http_client("http://localhost/mcp", http_client=http)) as client:
                    listing = await client.list_tools()
                    assert_tool_inventory(listing.tools)
                    assert_catalog_annotations(listing.tools)

    asyncio.run(scenario())
