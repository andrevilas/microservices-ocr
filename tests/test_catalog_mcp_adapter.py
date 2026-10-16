"""Catalog MCP registry and stdio HTTP adapter (V2a). Happy paths run the real ApiClient over the real ASGI app
and SQLite stores; MockTransport is used only for transport boundaries (timeout, redirect, invalid/oversized
response, oversized request)."""
import asyncio
import json
import os
import subprocess
import sys

import httpx2
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
import pytest

from app.application import JobApplication
from app.catalog_mcp_tools import CATALOG_OPERATIONS, register_catalog_tools
from app.catalog_models import (ContextInput, ContextUpdateInput, TypeInput, TypePublishInput,
                                TypeUpdateInput)
from app.main import create_app
from app.mcp_stdio import ApiClient, MAX_RESPONSE_BYTES
from app.mcp_tools import OcrMCPServer
from app.security import require_principal
from app.services.storage_service import JobStore
from test_catalog_http import CTX, CTX2, OPERATIONS, OWNER, TYP, TYP2
from test_mcp_vertical import ROOT

READ_ONLY = {"ocr_list_contexts", "ocr_get_context", "ocr_list_document_types", "ocr_get_document_type",
             "ocr_list_document_type_versions", "ocr_get_document_type_version"}
UPDATES = {"ocr_update_context", "ocr_update_document_type"}
INVOKE_ARGS = {  # tool -> positional arguments seen by invoke, in matrix order
    "ocr_list_contexts": ("list_contexts", 25, None),
    "ocr_create_context": ("create_context", ContextInput, "create-context-1"),
    "ocr_get_context": ("get_context", "a" * 32),
    "ocr_update_context": ("update_context", "a" * 32, ContextUpdateInput),
    "ocr_list_document_types": ("list_document_types", "a" * 32, 25, None),
    "ocr_create_document_type": ("create_document_type", "a" * 32, TypeInput, "create-type-0001"),
    "ocr_get_document_type": ("get_document_type", "a" * 32),
    "ocr_update_document_type": ("update_document_type", "a" * 32, TypeUpdateInput),
    "ocr_publish_document_type": ("publish_document_type", "a" * 32, TypePublishInput, "publish-0000001"),
    "ocr_list_document_type_versions": ("list_document_type_versions", "a" * 32, 25, None),
    "ocr_get_document_type_version": ("get_document_type_version", "a" * 32, 1),
}
ARGUMENTS = {
    "ocr_list_contexts": {},
    "ocr_get_context": {"context_id": "a" * 32},
    "ocr_list_document_types": {"context_id": "a" * 32},
    "ocr_get_document_type": {"type_id": "a" * 32},
    "ocr_list_document_type_versions": {"type_id": "a" * 32},
    "ocr_create_context": {"body": CTX, "idempotency_key": "create-context-1"},
    "ocr_update_context": {"context_id": "a" * 32, "body": {"body": CTX, "expected_revision": 1}},
    "ocr_create_document_type": {"context_id": "a" * 32, "body": TYP, "idempotency_key": "create-type-0001"},
    "ocr_update_document_type": {"type_id": "a" * 32, "body": {"body": TYP, "expected_revision": 1}},
    "ocr_publish_document_type": {"type_id": "a" * 32, "body": {"expected_revision": 1, "expected_context_revision": 1},
                                  "idempotency_key": "publish-0000001"},
    "ocr_get_document_type_version": {"type_id": "a" * 32, "version": 1},
}


def run(coro):
    return asyncio.run(coro)


def isolated_server(invoke):
    server = OcrMCPServer("catalog-test")
    register_catalog_tools(server, invoke)
    return server


def test_registry_lists_exactly_eleven_tools_with_schemas_and_annotations():
    async def scenario():
        async with Client(isolated_server(lambda *a: None)) as client:
            return (await client.list_tools()).tools
    tools = {tool.name: tool for tool in run(scenario())}
    assert set(tools) == set(OPERATIONS) and len(tools) == 11
    assert len(CATALOG_OPERATIONS) == 11 and {a[0] for a in INVOKE_ARGS.values()} == CATALOG_OPERATIONS
    for name, tool in tools.items():
        hints = tool.annotations
        assert hints.open_world_hint is False
        assert hints.read_only_hint is (name in READ_ONLY)
        assert hints.destructive_hint is (name in UPDATES)
        assert hints.idempotent_hint is (name not in UPDATES)
        assert tool.output_schema and tool.input_schema["additionalProperties"] is False
        assert "untrusted" in tool.description
        for forbidden in ("owner", "principal", "token", "path", "url"):
            assert forbidden not in tool.input_schema["properties"]
    assert "idempotency_key" in tools["ocr_create_context"].input_schema["required"]
    assert "idempotency_key" not in tools["ocr_update_context"].input_schema["properties"]
    assert tools["ocr_list_contexts"].input_schema["properties"]["limit"]["maximum"] == 100


def test_version_record_keeps_schema_alias_in_output_schema_and_structured_content():
    record = {"type_id": "a" * 32, "context_id": "b" * 32, "version": 1, "draft_revision": 2, "context_revision": 1,
              "type_name": "T", "context_name": "C", "schema_version": "1", "schema_sha256": "c" * 64,
              "created_at": "2026-01-01T00:00:00Z",
              "lifecycle": {"state": "published", "revision": 1, "changed_at": "2026-01-01T00:00:00Z"},
              "schema": {"type": "object"}}

    async def scenario():
        async def invoke(ctx, operation, *args):
            return record
        async with Client(isolated_server(invoke)) as client:
            tool = next(t for t in (await client.list_tools()).tools if t.name == "ocr_get_document_type_version")
            return tool, await client.call_tool("ocr_get_document_type_version", {"type_id": "a" * 32, "version": 1})
    tool, result = run(scenario())
    assert "schema" in tool.output_schema["properties"] and "schema_document" not in tool.output_schema["properties"]
    assert not result.is_error
    assert result.structured_content["schema"] == {"type": "object"} and "schema_document" not in result.structured_content


def test_tools_pass_typed_arguments_to_invoke_in_matrix_order():
    async def scenario():
        seen = []

        async def invoke(ctx, operation, *args):
            seen.append((operation, *args))
            raise ToolError("stop: recorded")
        async with Client(isolated_server(invoke)) as client:
            for name in OPERATIONS:
                result = await client.call_tool(name, ARGUMENTS[name])
                assert result.is_error
        return seen
    seen = run(scenario())
    assert len(seen) == 11 and [call[0] for call in seen] == [expected[0] for expected in INVOKE_ARGS.values()]
    for (operation, *args), (name, expected) in zip(seen, INVOKE_ARGS.items()):
        assert operation == expected[0] and len(args) == len(expected) - 1, name
        for got, want in zip(args, expected[1:]):
            assert isinstance(got, want) if isinstance(want, type) else got == want, name


def test_registry_rejects_extra_arguments_and_body_properties():
    async def scenario():
        async def invoke(ctx, operation, *args):
            raise AssertionError("invoke must not run for invalid input")
        async with Client(isolated_server(invoke)) as client:
            extra = await client.call_tool("ocr_get_context", {"context_id": "a" * 32, "owner": "x"})
            body = await client.call_tool("ocr_create_context", {"body": {**CTX, "owner": "x"}, "idempotency_key": "create-context-1"})
            bad = [await client.call_tool("ocr_list_contexts", {"limit": 0}),
                   await client.call_tool("ocr_list_contexts", {"limit": 101}),
                   await client.call_tool("ocr_list_contexts", {"after": "latest"}),
                   await client.call_tool("ocr_get_document_type_version", {"type_id": "a" * 32, "version": "latest"}),
                   await client.call_tool("ocr_get_document_type_version", {"type_id": "a" * 32, "version": 0}),
                   await client.call_tool("ocr_create_context", {"body": CTX, "idempotency_key": "short"})]
            return [extra, body, *bad]
    assert all(result.is_error for result in run(scenario()))


# -- real ASGI app + real stores: every mapping, alias, replay and CAS through ApiClient and the MCP registry
class Live:
    def __init__(self, root):
        self.store = JobStore(root_dir=root)
        self.app = create_app(lambda: JobApplication(self.store))
        self.app.dependency_overrides[require_principal] = lambda: OWNER
        self.requests = []

    async def record(self, request):
        self.requests.append(request)

    def http(self):
        return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), follow_redirects=False, trust_env=False,
                                  event_hooks={"request": [self.record]})


@pytest.fixture
def live(tmp_path):
    instance = Live(tmp_path / "live")
    yield instance
    instance.store._conn.close()


def test_api_client_exercises_all_eleven_operations_over_real_http(live):
    async def scenario():
        async with live.http() as http:
            api = ApiClient("http://testserver", http)
            invoke = api.invoke
            context = ContextInput(**CTX)
            receipt = await invoke(None, "create_context", context, "create-context-1")
            assert await invoke(None, "create_context", context, "create-context-1") == receipt  # replay: receipt only
            with pytest.raises(ToolError, match="idempotency_conflict"):
                await invoke(None, "create_context", ContextInput(**CTX2), "create-context-1")
            cid = receipt["context_id"]
            second = await invoke(None, "create_context", ContextInput(**CTX2), "create-context-2")
            first_page = await invoke(None, "list_contexts", 1, None)
            assert len(first_page["items"]) == 1 and first_page["next_cursor"]
            next_page = await invoke(None, "list_contexts", 1, first_page["next_cursor"])
            assert {first_page["items"][0]["context_id"], next_page["items"][0]["context_id"]} == {cid, second["context_id"]}
            assert (await invoke(None, "get_context", cid))["revision"] == 1
            updated = await invoke(None, "update_context", cid, ContextUpdateInput(body=CTX2, expected_revision=1))
            assert updated["revision"] == 2 and updated["body"]["name"] == CTX2["name"]
            with pytest.raises(ToolError, match="revision_conflict") as stale:
                await invoke(None, "update_context", cid, ContextUpdateInput(body=CTX, expected_revision=1))
            assert "same key" not in str(stale.value) and "read the current revision" in str(stale.value)

            tkey = "create-type-0001"
            typ = await invoke(None, "create_document_type", cid, TypeInput(**TYP), tkey)
            assert await invoke(None, "create_document_type", cid, TypeInput(**TYP), tkey) == typ
            tid = typ["type_id"]
            assert [i["type_id"] for i in (await invoke(None, "list_document_types", cid, 25, None))["items"]] == [tid]
            assert (await invoke(None, "get_document_type", tid))["body"]["name"] == TYP["name"]
            changed = await invoke(None, "update_document_type", tid, TypeUpdateInput(body=TYP2, expected_revision=1))
            assert changed["revision"] == 2
            with pytest.raises(ToolError, match="revision_conflict"):
                await invoke(None, "update_document_type", tid, TypeUpdateInput(body=TYP, expected_revision=1))

            publish = TypePublishInput(expected_revision=2, expected_context_revision=2)
            summary = await invoke(None, "publish_document_type", tid, publish, "publish-0000001")
            assert summary["version"] == 1 and "schema" not in summary
            assert await invoke(None, "publish_document_type", tid, publish, "publish-0000001") == summary
            versions = await invoke(None, "list_document_type_versions", tid, 25, None)
            assert [{k: v for k, v in item.items() if k != "lifecycle"}
                    for item in versions["items"]] == [summary] and versions["next_cursor"] is None
            assert versions["items"][0]["lifecycle"] == {
                "state": "published", "revision": 1, "changed_at": summary["created_at"]}
            record = await invoke(None, "get_document_type_version", tid, 1)
            assert record["schema"] and "schema_document" not in record

            # The same flow through the MCP registry keeps the wire alias "schema".
            async with Client(isolated_server(invoke)) as client:
                result = await client.call_tool("ocr_get_document_type_version", {"type_id": tid, "version": 1})
                assert not result.is_error and result.structured_content["schema"] == record["schema"]
                assert "schema_document" not in result.structured_content
        return cid, tid

    cid, tid = run(scenario())
    seen = [(r.method, r.url.path) for r in live.requests]
    for expected in [("GET", "/api/v1/contexts"), ("POST", "/api/v1/contexts"), ("GET", f"/api/v1/contexts/{cid}"),
                     ("PUT", f"/api/v1/contexts/{cid}"), ("GET", f"/api/v1/contexts/{cid}/types"),
                     ("POST", f"/api/v1/contexts/{cid}/types"), ("GET", f"/api/v1/document-types/{tid}"),
                     ("PUT", f"/api/v1/document-types/{tid}"), ("POST", f"/api/v1/document-types/{tid}/versions"),
                     ("GET", f"/api/v1/document-types/{tid}/versions"), ("GET", f"/api/v1/document-types/{tid}/versions/1")]:
        assert expected in seen
    for request in live.requests:
        assert request.headers.get("content-type", "application/json") == "application/json"
        has_key = "idempotency-key" in request.headers
        assert has_key == (request.method == "POST")
        if request.method == "GET":
            assert not request.content and set(request.url.params) <= {"limit", "after"}
        else:
            body = request.content
            assert b'"idempotency' not in body and b": " not in body and b", " not in body  # compact, key only in header
            assert json.loads(body)
    queries = [dict(r.url.params) for r in live.requests if r.method == "GET" and r.url.path == "/api/v1/contexts"]
    assert len(queries) == 2 and queries[0] == {"limit": "1"}  # explicit limit, after=None omitted
    assert set(queries[1]) == {"limit", "after"} and queries[1]["limit"] == "1" and queries[1]["after"]


def test_invalid_ids_and_versions_never_reach_the_network(live):
    async def scenario():
        async with live.http() as http:
            api = ApiClient("http://testserver", http)
            for operation, args in [("get_context", ("../jobs",)), ("get_context", ("A" * 32,)),
                                    ("list_document_types", ("a/b", 25, None)), ("get_document_type", ("a" * 31,)),
                                    ("get_document_type_version", ("a" * 32, 0)),
                                    ("get_document_type_version", ("a" * 32, True)),
                                    ("get_document_type_version", ("a" * 32, "latest"))]:
                with pytest.raises(ToolError, match="not_found"):
                    await api.invoke(None, operation, *args)
    run(scenario())
    assert live.requests == []


# -- transport boundaries only
def mock_client(handler):
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler), headers={"Authorization": "Bearer synthetic"},
                              follow_redirects=False, trust_env=False)


@pytest.mark.parametrize("operation,args,hint", [
    ("get_context", ("a" * 32,), "same key"),
    ("update_context", ("a" * 32, ContextUpdateInput(body=CTX, expected_revision=1)), "read the current revision"),
    ("update_document_type", ("a" * 32, TypeUpdateInput(body=TYP, expected_revision=1)), "read the current revision"),
    ("create_context", (ContextInput(**CTX), "create-context-1"), "same key"),
])
def test_timeout_is_sanitized_and_hint_matches_operation(operation, args, hint):
    def handler(request):
        raise httpx2.ReadTimeout("PRIVATE http://internal.invalid Bearer synthetic", request=request)

    async def scenario():
        async with mock_client(handler) as http:
            with pytest.raises(ToolError, match="api_unavailable") as error:
                await ApiClient("http://localhost", http).invoke(None, operation, *args)
            return str(error.value)
    message = run(scenario())
    assert hint in message and "PRIVATE" not in message and "internal.invalid" not in message
    if hint != "same key":
        assert "same key" not in message


@pytest.mark.parametrize("response,code", [
    (lambda: httpx2.Response(302, headers={"Location": "https://other.invalid/PRIVATE"}), "upstream_redirect"),
    (lambda: httpx2.Response(200, content=b"<html>PRIVATE</html>"), "invalid_api_response"),
    (lambda: httpx2.Response(200, json=[]), "invalid_api_response"),
    (lambda: httpx2.Response(409, json={"error": {"code": "context_revision_conflict", "message": "PRIVATE"}}), "context_revision_conflict"),
    (lambda: httpx2.Response(500, content=b"<html>PRIVATE</html>"), "invalid_api_response"),
])
def test_catalog_boundary_responses_are_sanitized(response, code):
    async def scenario():
        async with mock_client(lambda request: response()) as http:
            with pytest.raises(ToolError, match=code) as error:
                await ApiClient("http://localhost", http).invoke(None, "get_context", "a" * 32)
            assert "PRIVATE" not in str(error.value) and "other.invalid" not in str(error.value)
    run(scenario())


def test_oversized_catalog_response_is_cut_before_json_parse():
    chunk = b"x" * 65536

    async def stream():
        for _ in range(MAX_RESPONSE_BYTES // len(chunk) + 2):
            yield chunk

    async def scenario():
        async with mock_client(lambda request: httpx2.Response(200, content=stream())) as http:
            with pytest.raises(ToolError, match="upstream_response_limit"):
                await ApiClient("http://localhost", http).invoke(None, "get_document_type_version", "a" * 32, 1)
    run(scenario())


class RawBody:
    def __init__(self, payload):
        self.payload = payload

    def model_dump(self, mode="json", by_alias=False):
        assert mode == "json" and by_alias is True
        return self.payload


def test_oversized_or_unserializable_request_is_refused_without_sending():
    sent = []

    def handler(request):
        sent.append(request)
        return httpx2.Response(200, json={})

    async def scenario():
        async with mock_client(handler) as http:
            api = ApiClient("http://localhost", http)
            with pytest.raises(ToolError, match="request_too_large"):
                await api.invoke(None, "create_context", RawBody({"name": "x" * (2 * 1024 * 1024 + 1)}), "create-context-1")
            with pytest.raises(ToolError, match="invalid_request") as error:
                await api.invoke(None, "update_context", "a" * 32, RawBody({"secret-marker": float("nan")}))
            assert "secret-marker" not in str(error.value)
            await api.invoke(None, "create_context", RawBody({"name": "é" * 10, "n": [1, 2]}), "create-context-1")
    run(scenario())
    assert len(sent) == 1  # only the compact, in-limit body went out
    request = sent[0]
    assert request.content == '{"name":"éééééééééé","n":[1,2]}'.encode("utf-8")
    assert request.headers["content-type"] == "application/json" and request.headers["idempotency-key"] == "create-context-1"
    assert request.url.path == "/api/v1/contexts"


def test_catalog_import_and_registration_open_no_server_state(tmp_path):
    code = """
import sqlite3, threading, sys
def forbidden(*a, **kw): raise AssertionError('Local database or worker created')
sqlite3.connect = forbidden
threading.Thread.start = forbidden
import asyncio
import app.catalog_mcp_tools, app.mcp_stdio, app.mcp_tools
from mcp.server import MCPServer
server = MCPServer('test')
app.catalog_mcp_tools.register_catalog_tools(server, lambda *a: None)
assert len(asyncio.run(server.list_tools())) == 11
shared = MCPServer('test')
app.mcp_tools.register_tools(shared, lambda *a: None)
names = [tool.name for tool in asyncio.run(shared.list_tools())]
assert len(names) == len(set(names)) == 31, len(names)
app.mcp_stdio.register_tools(MCPServer('test'), lambda *a: None)
blocked = {'app.main', 'app.config', 'app.application', 'app.services.storage_service', 'app.services.job_queue',
           'app.services.catalog_runtime', 'app.services.context_service', 'app.services.type_service', 'app.routes.catalog'}
assert not blocked & set(sys.modules), sorted(blocked & set(sys.modules))
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            env={**os.environ, "OCR_TMP_DIR": str(tmp_path / "forbidden")}, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "forbidden").exists()
