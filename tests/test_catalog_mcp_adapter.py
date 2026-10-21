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
from app.catalog_models import (ContextInput, ContextUpdateInput, TypeDeprecateInput, TypeInput, TypePublishInput,
                                TypeUpdateInput)
from app.context_templates import TemplateCloneInput, TemplateCloneReceipt
from app.main import create_app
from app.mcp_stdio import ApiClient, MAX_RESPONSE_BYTES
from app.mcp_tools import OcrMCPServer
from app.security import require_principal
from app.services.storage_service import JobStore
from test_catalog_http import CTX, CTX2, OWNER, TYP, TYP2
from test_mcp_vertical import ROOT

READ_ONLY = {"ocr_list_context_templates", "ocr_list_contexts", "ocr_get_context", "ocr_list_document_types", "ocr_get_document_type",
             "ocr_list_document_type_versions", "ocr_get_document_type_version"}
UPDATES = {"ocr_update_context", "ocr_update_document_type"}
DEPRECATIONS = {"ocr_deprecate_document_type_version"}
INVOKE_ARGS = {  # tool -> positional arguments seen by invoke, in matrix order
    "ocr_list_context_templates": ("list_context_templates",),
    "ocr_clone_context_template": ("clone_context_template", "starter-template", TemplateCloneInput, "clone-template-01"),
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
    "ocr_deprecate_document_type_version": ("deprecate_document_type_version", "a" * 32, 1,
                                             TypeDeprecateInput, "deprecate-000001"),
}
ARGUMENTS = {
    "ocr_list_context_templates": {},
    "ocr_clone_context_template": {"template_id": "starter-template",
                                   "body": {"expected_version": 1, "expected_sha256": "c" * 64},
                                   "idempotency_key": "clone-template-01"},
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
    "ocr_deprecate_document_type_version": {"type_id": "a" * 32, "version": 1,
                                           "body": {"expected_lifecycle_revision": 1},
                                           "idempotency_key": "deprecate-000001"},
}


def run(coro):
    return asyncio.run(coro)


def isolated_server(invoke):
    server = OcrMCPServer("catalog-test")
    register_catalog_tools(server, invoke)
    return server


def test_registry_lists_exactly_fourteen_tools_with_schemas_and_annotations():
    async def scenario():
        async with Client(isolated_server(lambda *a: None)) as client:
            return (await client.list_tools()).tools
    tools = {tool.name: tool for tool in run(scenario())}
    assert set(tools) == set(INVOKE_ARGS) == set(ARGUMENTS) and len(tools) == 14
    assert len(CATALOG_OPERATIONS) == 14 and {a[0] for a in INVOKE_ARGS.values()} == CATALOG_OPERATIONS
    for name, tool in tools.items():
        hints = tool.annotations
        assert hints.open_world_hint is False
        assert hints.read_only_hint is (name in READ_ONLY)
        assert hints.destructive_hint is (name in UPDATES | DEPRECATIONS)
        assert hints.idempotent_hint is (name not in UPDATES)
        assert tool.output_schema and tool.input_schema["additionalProperties"] is False
        assert "untrusted" in tool.description
        for forbidden in ("owner", "principal", "token", "path", "url"):
            assert forbidden not in tool.input_schema["properties"]
    assert "idempotency_key" in tools["ocr_create_context"].input_schema["required"]
    assert "idempotency_key" not in tools["ocr_update_context"].input_schema["properties"]
    assert tools["ocr_list_contexts"].input_schema["properties"]["limit"]["maximum"] == 100
    deprecate = tools["ocr_deprecate_document_type_version"]
    schema = deprecate.input_schema
    assert set(schema["required"]) == {"type_id", "version", "body", "idempotency_key"}
    assert schema["properties"]["version"]["minimum"] == 1
    assert schema["properties"]["version"]["maximum"] == 2**63 - 1
    assert set(deprecate.output_schema["properties"]) == {"type_id", "version", "schema_sha256", "lifecycle"}
    assert "historical" in tools["ocr_publish_document_type"].description
    assert "current lifecycle" in tools["ocr_get_document_type_version"].description
    assert "deprecated" in tools["ocr_list_document_type_versions"].description
    listing = tools["ocr_list_context_templates"]
    assert listing.input_schema["properties"] == {}
    assert not listing.input_schema.get("required", [])
    assert set(listing.output_schema["properties"]) == {"items"}
    assert listing.output_schema["properties"]["items"]["minItems"] == 3
    assert listing.output_schema["properties"]["items"]["maxItems"] == 3
    clone = tools["ocr_clone_context_template"]
    schema = clone.input_schema
    assert set(schema["properties"]) == set(schema["required"]) == {
        "template_id", "body", "idempotency_key"}
    slug = schema["properties"]["template_id"]
    assert slug["type"] == "string" and slug["maxLength"] == 64
    assert slug["pattern"] == "^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$"
    body = schema["properties"]["body"]
    if "$ref" in body:
        body = schema["$defs"][body["$ref"].rsplit("/", 1)[-1]]
    assert body["additionalProperties"] is False
    assert set(body["properties"]) == set(body["required"]) == {"expected_version", "expected_sha256"}
    version = body["properties"]["expected_version"]
    assert version["type"] == "integer" and version["minimum"] == 1
    digest = body["properties"]["expected_sha256"]
    assert digest["type"] == "string"
    assert digest["minLength"] == digest["maxLength"] == 64
    assert digest["pattern"] == "^[0-9a-f]{64}$"
    assert set(clone.output_schema["properties"]) == {
        "template_id", "template_version", "template_sha256", "context_id",
        "context_revision", "types", "created_at"}
    assert "historical" in clone.description and "current state" in clone.description


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
            for name in INVOKE_ARGS:
                result = await client.call_tool(name, ARGUMENTS[name])
                assert result.is_error
        return seen
    seen = run(scenario())
    assert len(seen) == 14 and [call[0] for call in seen] == [expected[0] for expected in INVOKE_ARGS.values()]
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


@pytest.mark.parametrize("override", [
    {"type_id": "A" * 32}, {"type_id": "../private"},
    {"version": True}, {"version": "1"}, {"version": 0}, {"version": 2**63},
    {"body": {}}, {"body": {"expected_lifecycle_revision": True}},
    {"body": {"expected_lifecycle_revision": "1"}},
    {"body": {"expected_lifecycle_revision": 0}},
    {"body": {"expected_lifecycle_revision": 2**31}},
    {"body": {"expected_lifecycle_revision": 1, "owner": "PRIVATE"}},
    {"idempotency_key": "short"}, {"idempotency_key": "x" * 129},
    {"idempotency_key": "nonascii-é"}, {"owner": "PRIVATE"},
])
def test_deprecation_invalid_arguments_never_invoke(override):
    seen = []

    async def scenario():
        async def invoke(ctx, operation, *args):
            seen.append(operation)
            raise ToolError("unexpected invocation")
        async with Client(isolated_server(invoke)) as client:
            result = await client.call_tool("ocr_deprecate_document_type_version", {
                **ARGUMENTS["ocr_deprecate_document_type_version"], **override})
            assert result.is_error
    run(scenario())
    assert seen == []


@pytest.mark.parametrize("override", [
    {"template_id": "Uppercase"}, {"template_id": "../private"},
    {"template_id": "nonascii-é"}, {"template_id": "a" * 65},
    {"template_id": "double--hyphen"}, {"template_id": "trailing-"},
    {"body": {}}, {"body": {"expected_version": True, "expected_sha256": "c" * 64}},
    {"body": {"expected_version": "1", "expected_sha256": "c" * 64}},
    {"body": {"expected_version": 0, "expected_sha256": "c" * 64}},
    {"body": {"expected_version": 1, "expected_sha256": "C" * 64}},
    {"body": {"expected_version": 1, "expected_sha256": "c" * 63}},
    {"body": {"expected_version": 1, "expected_sha256": "g" * 64}},
    {"body": {"expected_version": 1, "expected_sha256": "c" * 64, "owner": "PRIVATE"}},
    {"owner": "PRIVATE"}, {"name": "PRIVATE"}, {"publish": True},
    {"idempotency_key": "short"},
])
def test_template_clone_invalid_arguments_never_invoke(override):
    seen = []

    async def scenario():
        async def invoke(ctx, operation, *args):
            seen.append(operation)
            raise ToolError("unexpected invocation")
        async with Client(isolated_server(invoke)) as client:
            result = await client.call_tool("ocr_clone_context_template", {
                **ARGUMENTS["ocr_clone_context_template"], **override})
            assert result.is_error
    run(scenario())
    assert seen == []


@pytest.mark.parametrize("missing", ["template_id", "body", "idempotency_key"])
def test_template_clone_requires_every_public_argument(missing):
    async def scenario():
        async def invoke(ctx, operation, *args):
            raise AssertionError("invoke must not run for missing input")
        args = {key: value for key, value in ARGUMENTS["ocr_clone_context_template"].items() if key != missing}
        async with Client(isolated_server(invoke)) as client:
            assert (await client.call_tool("ocr_clone_context_template", args)).is_error
            assert (await client.call_tool("ocr_list_context_templates", {"owner": "PRIVATE"})).is_error
    run(scenario())


def clone_receipt_payload():
    return {
        "template_id": "generic", "template_version": 1, "template_sha256": "a" * 64,
        "context_id": "b" * 32, "context_revision": 1,
        "types": [{"type_key": "document", "type_id": "c" * 32, "revision": 1}],
        "created_at": "2026-10-16T00:00:00Z",
    }


def clone_tool_arguments():
    return {
        "template_id": "generic",
        "body": {"expected_version": 1, "expected_sha256": "a" * 64},
        "idempotency_key": "clone-boundary-01",
    }


def assert_clone_receipt_content(result, expected):
    assert not result.is_error
    assert result.structured_content == expected
    assert len(result.content) == 1 and result.content[0].type == "text"
    text = result.content[0].text
    assert json.loads(text) == expected
    assert TemplateCloneReceipt.model_validate_json(text).model_dump(mode="json") == expected


@pytest.mark.parametrize("representation", ["Z", "+00:00", "dto"])
def test_template_clone_output_accepts_json_utc_and_revalidates_dto(representation):
    payload = clone_receipt_payload()
    canonical = TemplateCloneReceipt.model_validate_json(json.dumps(payload))
    if representation == "+00:00":
        payload["created_at"] = "2026-10-16T00:00:00+00:00"
    returned = canonical if representation == "dto" else payload
    seen = []

    async def scenario():
        async def invoke(ctx, operation, *args):
            seen.append((operation, *args))
            return returned
        async with Client(isolated_server(invoke)) as client:
            return await client.call_tool("ocr_clone_context_template", clone_tool_arguments())

    result = run(scenario())
    assert_clone_receipt_content(result, canonical.model_dump(mode="json"))
    assert len(seen) == 1
    operation, template_id, body, key = seen[0]
    assert operation == "clone_context_template" and template_id == "generic"
    assert isinstance(body, TemplateCloneInput)
    assert body.model_dump(mode="json") == clone_tool_arguments()["body"]
    assert key == clone_tool_arguments()["idempotency_key"]


@pytest.mark.parametrize("case", [
    "missing_created_at", "naive", "non_utc", "invalid_datetime",
    "bool_context_revision", "bool_template_version", "bool_type_revision",
    "bad_hash", "bad_context_id", "bad_type_id", "extra_owner", "non_object",
    "non_json_value", "nan", "recursive", "constructed_dto",
])
def test_template_clone_invalid_output_is_sanitized_without_retry(case, caplog):
    payload = clone_receipt_payload()
    marker = "PRIVATE Bearer synthetic-token https://private.invalid"
    if case == "missing_created_at":
        payload.pop("created_at")
    elif case == "naive":
        payload["created_at"] = "2026-10-16T00:00:00"
    elif case == "non_utc":
        payload["created_at"] = "2026-10-16T00:00:00+01:00"
    elif case == "invalid_datetime":
        payload["created_at"] = marker
    elif case == "bool_context_revision":
        payload["context_revision"] = True
    elif case == "bool_template_version":
        payload["template_version"] = True
    elif case == "bool_type_revision":
        payload["types"][0]["revision"] = True
    elif case == "bad_hash":
        payload["template_sha256"] = marker
    elif case == "bad_context_id":
        payload["context_id"] = marker
    elif case == "bad_type_id":
        payload["types"][0]["type_id"] = marker
    elif case == "extra_owner":
        payload["owner"] = marker
    elif case == "non_object":
        payload = [marker]
    elif case == "non_json_value":
        payload["created_at"] = {marker}
    elif case == "nan":
        payload["context_revision"] = float("nan")
    elif case == "recursive":
        payload["owner"] = payload
    elif case == "constructed_dto":
        canonical = TemplateCloneReceipt.model_validate_json(json.dumps(payload))
        payload = TemplateCloneReceipt.model_construct(**{
            **canonical.model_dump(mode="python"), "created_at": marker})
    seen = []

    async def scenario():
        async def invoke(ctx, operation, *args):
            seen.append((operation, *args))
            return payload
        async with Client(isolated_server(invoke)) as client:
            return await client.call_tool("ocr_clone_context_template", clone_tool_arguments())

    result = run(scenario())
    assert result.is_error
    message = "\n".join(block.text for block in result.content if block.type == "text")
    assert (
        "invalid_api_response: API returned an invalid template clone receipt; "
        "retry submissions with the same key."
    ) in message
    for secret in ("PRIVATE", "synthetic-token", "private.invalid"):
        assert secret not in message and secret not in caplog.text
    assert len(seen) == 1
    assert seen[0][0] == "clone_context_template"
    assert seen[0][1] == "generic"
    assert seen[0][-1] == clone_tool_arguments()["idempotency_key"]
    assert seen[0][2].model_dump(mode="json") == clone_tool_arguments()["body"]


@pytest.mark.parametrize("message", [
    "insufficient_scope: contexts:write required",
    "idempotency_conflict: request differs",
    "storage_unavailable: try later",
])
def test_template_clone_invoke_errors_propagate_unchanged(message):
    seen = []

    async def scenario():
        async def invoke(ctx, operation, *args):
            seen.append((operation, *args))
            raise ToolError(message)
        async with Client(isolated_server(invoke)) as client:
            return await client.call_tool("ocr_clone_context_template", clone_tool_arguments())

    result = run(scenario())
    assert result.is_error
    assert [block.text for block in result.content if block.type == "text"] == [
        f"Error executing tool ocr_clone_context_template: {message}"
    ]
    assert len(seen) == 1
    assert seen[0][0] == "clone_context_template"
    assert seen[0][-1] == clone_tool_arguments()["idempotency_key"]


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


def test_template_clone_registry_over_real_http_preserves_receipt_and_replay(live):
    statuses = []

    async def record_response(response):
        if response.request.method == "POST":
            statuses.append(response.status_code)

    async def scenario():
        async with live.http() as http:
            http.event_hooks["response"].append(record_response)
            api = ApiClient("http://testserver", http)
            async with Client(isolated_server(api.invoke)) as client:
                listing = await client.call_tool("ocr_list_context_templates", {})
                assert not listing.is_error
                template = listing.structured_content["items"][0]
                assert template["context"] and template["types"]
                arguments = {
                    "template_id": template["template_id"],
                    "body": {
                        "expected_version": template["template_version"],
                        "expected_sha256": template["template_sha256"],
                    },
                    "idempotency_key": "clone-real-http-01",
                }
                first = await client.call_tool("ocr_clone_context_template", arguments)
                assert not first.is_error
                canonical = TemplateCloneReceipt.model_validate_json(
                    json.dumps(first.structured_content)).model_dump(mode="json")
                assert_clone_receipt_content(first, canonical)
                replay = await client.call_tool("ocr_clone_context_template", arguments)
                assert_clone_receipt_content(replay, canonical)
                contexts = await client.call_tool("ocr_list_contexts", {})
                assert not contexts.is_error
                assert [item["context_id"] for item in contexts.structured_content["items"]] == [canonical["context_id"]]
                assert contexts.structured_content["next_cursor"] is None
                assert canonical["template_id"] == template["template_id"]
                assert canonical["template_version"] == template["template_version"]
                assert canonical["template_sha256"] == template["template_sha256"]
                return arguments

    arguments = run(scenario())
    assert statuses == [201, 200]
    posts = [request for request in live.requests if request.method == "POST"]
    assert len(posts) == 2
    for request in posts:
        assert request.headers["idempotency-key"] == arguments["idempotency_key"]
        assert json.loads(request.content) == arguments["body"]
    assert posts[0].url == posts[1].url
    assert posts[0].content == posts[1].content


def test_api_client_exercises_all_twelve_operations_over_real_http(live):
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
            deprecate = TypeDeprecateInput(expected_lifecycle_revision=1)
            receipt = await invoke(None, "deprecate_document_type_version", tid, 1, deprecate, "deprecate-000001")
            assert set(receipt) == {"type_id", "version", "schema_sha256", "lifecycle"}
            assert receipt["type_id"] == tid and receipt["version"] == 1
            assert receipt["schema_sha256"] == record["schema_sha256"]
            assert receipt["lifecycle"]["state"] == "deprecated" and receipt["lifecycle"]["revision"] == 2
            assert await invoke(None, "deprecate_document_type_version", tid, 1, deprecate, "deprecate-000001") == receipt
            current = await invoke(None, "get_document_type_version", tid, 1)
            assert current["schema"] == record["schema"] and current["lifecycle"] == receipt["lifecycle"]
            assert await invoke(None, "publish_document_type", tid, publish, "publish-0000001") == summary
        return cid, tid

    cid, tid = run(scenario())
    seen = [(r.method, r.url.path) for r in live.requests]
    for expected in [("GET", "/api/v1/contexts"), ("POST", "/api/v1/contexts"), ("GET", f"/api/v1/contexts/{cid}"),
                     ("PUT", f"/api/v1/contexts/{cid}"), ("GET", f"/api/v1/contexts/{cid}/types"),
                     ("POST", f"/api/v1/contexts/{cid}/types"), ("GET", f"/api/v1/document-types/{tid}"),
                     ("PUT", f"/api/v1/document-types/{tid}"), ("POST", f"/api/v1/document-types/{tid}/versions"),
                     ("GET", f"/api/v1/document-types/{tid}/versions"), ("GET", f"/api/v1/document-types/{tid}/versions/1"),
                     ("POST", f"/api/v1/document-types/{tid}/versions/1/deprecate")]:
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
    deprecations = [r for r in live.requests if r.url.path == f"/api/v1/document-types/{tid}/versions/1/deprecate"]
    assert len(deprecations) == 2
    for request in deprecations:
        assert request.method == "POST" and not request.url.params
        assert request.headers["idempotency-key"] == "deprecate-000001"
        assert request.content == b'{"expected_lifecycle_revision":1}'
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
    ("deprecate_document_type_version", ("a" * 32, 1, TypeDeprecateInput(expected_lifecycle_revision=1),
                                           "deprecate-000001"), "same key"),
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
assert len(asyncio.run(server.list_tools())) == 14
shared = MCPServer('test')
app.mcp_tools.register_tools(shared, lambda *a: None)
names = [tool.name for tool in asyncio.run(shared.list_tools())]
assert len(names) == len(set(names)) == 62, len(names)
assert {
    'ocr_get_document_canonical_manifest', 'ocr_get_document_canonical_chunk',
    'ocr_get_document_metadata', 'ocr_list_document_metadata_revisions',
    'ocr_get_document_metadata_revision', 'ocr_list_document_metadata_fields',
    'ocr_list_document_metadata_values', 'ocr_list_document_metadata_evidence',
    'ocr_get_document_metadata_string', 'ocr_get_document_metadata_schema',
    'ocr_save_document_metadata', 'ocr_approve_document_metadata',
} <= set(names)
app.mcp_stdio.register_tools(MCPServer('test'), lambda *a: None)
blocked = {'app.main', 'app.config', 'app.application', 'app.services.storage_service', 'app.services.job_queue',
           'app.services.catalog_runtime', 'app.services.context_service', 'app.services.type_service', 'app.routes.catalog'}
assert not blocked & set(sys.modules), sorted(blocked & set(sys.modules))
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            env={**os.environ, "OCR_TMP_DIR": str(tmp_path / "forbidden")}, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "forbidden").exists()
