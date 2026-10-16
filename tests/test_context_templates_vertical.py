"""Real SDK template matrix; does not certify Desktop/CLI apps or production OAuth/TLS."""
import asyncio
import copy
import json

import pytest

from app.context_templates import TemplateCloneReceipt
from test_mcp_vertical import connected, runtime
from test_catalog_mcp_vertical import (
    catalog_accounts, Tools, rest, text_documents, check_discovery, SCENARIO_TIMEOUT,
)
from test_catalog_mcp_boundaries import reduced_token
from test_catalog_http import legacy_token


LIST = "ocr_list_context_templates"
CLONE = "ocr_clone_context_template"
BASE = "/api/v1"
SCOPES = {"contexts:read", "contexts:write"}


async def response(http, method, path, status, **kwargs):
    result = await http.request(method, BASE + path, **kwargs)
    assert result.status_code == status
    return result


async def document(http, method, path, status=200, **kwargs):
    return (await response(http, method, path, status, **kwargs)).json()


def receipt(result):
    structured = result.structured_content
    parsed = TemplateCloneReceipt.model_validate_json(json.dumps(structured))
    assert parsed.created_at.utcoffset().total_seconds() == 0
    assert all(item == structured for item in text_documents(result))
    assert parsed.model_dump(mode="json") == structured
    return structured


def check_schemas(tools):
    indexed = {tool.name: tool for tool in tools}
    listing, cloning = indexed[LIST], indexed[CLONE]
    assert not listing.input_schema.get("required", [])
    assert not listing.input_schema.get("properties", {})
    assert "items" in listing.output_schema["properties"]
    schema = cloning.input_schema
    assert set(schema["required"]) == {"template_id", "body", "idempotency_key"}
    assert set(schema["properties"]) == set(schema["required"])
    body = schema["properties"]["body"]
    if "$ref" in body:
        node = schema
        for part in body["$ref"].removeprefix("#/").split("/"):
            node = node[part]
        body = node
    assert body["additionalProperties"] is False
    assert set(body["required"]) == {"expected_version", "expected_sha256"}
    assert body["properties"]["expected_version"]["type"] == "integer"
    assert set(cloning.output_schema["required"]) == {
        "template_id", "template_version", "template_sha256", "context_id",
        "context_revision", "types", "created_at",
    }


async def clone_rest(http, args, status, replayed):
    result = await response(
        http, "POST", f"/context-templates/{args['template_id']}/clone", status,
        json=args["body"], headers={"Idempotency-Key": args["idempotency_key"]},
    )
    assert result.headers["idempotency-replayed"] == replayed
    return result.json()


async def unpublished(http, cloned):
    cid, tid = cloned["context_id"], cloned["types"][0]["type_id"]
    assert cloned["context_revision"] == cloned["types"][0]["revision"] == 1
    for path in (f"/contexts/{cid}", f"/document-types/{tid}"):
        record = await document(http, "GET", path)
        assert record["revision"] == 1
    assert await document(http, "GET", f"/document-types/{tid}/versions") == {
        "items": [], "next_cursor": None,
    }
    denied = await response(
        http, "POST", f"/document-types/{tid}/versions", 403,
        json={"expected_revision": 1, "expected_context_revision": 1},
        headers={"Idempotency-Key": "template-publish-denied"},
    )
    assert denied.json()["error"]["code"] == "insufficient_scope"


async def scenario(runtime, accounts, transport, mode):
    secret = runtime.env["JWT_SECRET"]
    writer = reduced_token(accounts.owner, SCOPES, secret)
    reader = reduced_token(accounts.owner, {"contexts:read"}, secret)
    legacy = legacy_token(accounts.owner_id, secret)
    async with rest(runtime, accounts.admin) as admin:
        principal = await document(admin, "POST", "/service-principals", 201,
                                   json={"name": "Synthetic templates"})
        credential = await document(
            admin, "POST", f"/service-principals/{principal['id']}/credentials", 201,
            json={"name": "Synthetic clone", "scopes": sorted(SCOPES),
                  "expires_in_seconds": 3600},
        )
        service = credential["token"]
    async with rest(runtime, writer) as http:
        me = await document(http, "GET", "/me")
        assert set(me["scopes"]) == SCOPES
        assert me["kind"] and me["kind"] != "service"
        async with connected(runtime, transport, mode, writer) as client:
            tools = check_discovery(await client.list_tools())
            check_schemas(tools)
            api = Tools(client, tools)
            listing = await api(LIST, {})
            assert [item["template_id"] for item in listing["items"]] == [
                "generic", "real-estate-deed", "procurement-notice",
            ]
            assert listing == await document(http, "GET", "/context-templates")
            template = listing["items"][0]
            args = {"template_id": "generic", "idempotency_key": "template-mcp-first",
                    "body": {"expected_version": template["template_version"],
                             "expected_sha256": template["template_sha256"]}}
            original = receipt(await api.raw(CLONE, args))
            await unpublished(http, original)
            assert await clone_rest(http, args, 200, "true") == original
            second_args = dict(args, idempotency_key="template-rest-first")
            second = await clone_rest(http, second_args, 201, "false")
            assert receipt(await api.raw(CLONE, second_args)) == second
            paths = (f"/contexts/{original['context_id']}",
                     f"/document-types/{original['types'][0]['type_id']}")
            edited = {}
            for path in paths:
                record = await document(http, "GET", path)
                body = copy.deepcopy(record["body"])
                body["name"] += " edited"
                edited[path] = await document(http, "PUT", path,
                    json={"body": body, "expected_revision": 1})
                assert edited[path]["revision"] == 2
            assert receipt(await api.raw(CLONE, args)) == original
            for path in paths:
                assert await document(http, "GET", path) == edited[path]
            wrong = copy.deepcopy(args)
            digest = wrong["body"]["expected_sha256"]
            wrong["body"]["expected_sha256"] = ("0" if digest[0] != "0" else "1") + digest[1:]
            conflict = await response(http, "POST", "/context-templates/generic/clone", 409,
                json=wrong["body"], headers={"Idempotency-Key": args["idempotency_key"]})
            assert conflict.json()["error"]["code"] == "idempotency_conflict"
            assert receipt(await api.raw(CLONE, args)) == original
            before = await document(http, "GET", "/contexts")
            assert before["next_cursor"] is None
            invalid = [copy.deepcopy(args) for _ in range(3)]
            invalid[0]["template_id"] = "INVALID/slug"
            invalid[1]["body"]["expected_version"] = True
            invalid[2]["body"]["owner_id"] = accounts.other_id
            for index, request in enumerate(invalid):
                request["idempotency_key"] = f"template-invalid-{index}"
                result = await client.call_tool(CLONE, request)
                assert result.is_error
            assert await document(http, "GET", "/contexts") == before
    for token, can_list in ((reader, True), (legacy, False)):
        async with connected(runtime, transport, mode, token) as client:
            api = Tools(client, tools)
            if can_list:
                assert await api(LIST, {}) == listing
            else:
                await api.denied(LIST, {}, "insufficient_scope")
            await api.denied(CLONE, args, "insufficient_scope")
    async with rest(runtime, service) as http:
        me = await document(http, "GET", "/me")
        assert me["kind"] == "service"
        assert set(me["scopes"]) == SCOPES
        async with connected(runtime, transport, mode, service) as client:
            api = Tools(client, tools)
            assert await api(LIST, {}) == listing
            own = receipt(await api.raw(CLONE, dict(args, idempotency_key="service-clone-key")))
            await unpublished(http, own)
    for index, token in enumerate((accounts.other, accounts.admin)):
        async with rest(runtime, token) as http:
            async with connected(runtime, transport, mode, token) as client:
                api = Tools(client, tools)
                assert await api(LIST, {}) == listing
                own = receipt(await api.raw(CLONE, dict(args, idempotency_key=f"other-clone-{index}")))
                assert own["context_id"] != original["context_id"]
                assert own["types"][0]["type_id"] != original["types"][0]["type_id"]
                for path in paths:
                    await response(http, "GET", path, 404)
                for path in (f"/contexts/{own['context_id']}",
                             f"/document-types/{own['types'][0]['type_id']}"):
                    record = await document(http, "GET", path)
                    assert record["revision"] == 1


@pytest.mark.parametrize("transport,mode", [
    ("http", "auto"), ("http", "legacy"), ("stdio", "auto"), ("stdio", "legacy"),
])
def test_context_templates_vertical(runtime, catalog_accounts, transport, mode):
    asyncio.run(asyncio.wait_for(
        scenario(runtime, catalog_accounts, transport, mode), SCENARIO_TIMEOUT,
    ))
