"""Pure content registry contracts using synthetic responses and the actual SDK client."""
import asyncio
import json

import pytest
from mcp import Client

from app.document_content_models import (
    MetadataEvidencePage, MetadataSchemaChunk, MetadataStringChunk, MetadataValuePage,
)
from app.document_http_examples import response_example
from app.document_mcp_tools import register_document_tools
from app.mcp_tools import OcrMCPServer

BASE = {"job_id": "a" * 32, "revision_id": "b" * 32}
FIELD = dict(BASE, segment_id="c" * 32, field_name="titulo", sha256="d" * 64)
SPECS = [
    ("list_document_metadata_values", MetadataValuePage, FIELD, {"limit": 20, "after": None}),
    ("list_document_metadata_evidence", MetadataEvidencePage, FIELD, {"limit": 10, "after": None}),
    ("get_document_metadata_string", MetadataStringChunk, dict(FIELD, part="raw"),
     {"item_index": None, "offset": 0, "max_chars": 1000}),
    ("get_document_metadata_schema", MetadataSchemaChunk,
     dict(BASE, schema_sha256="e" * 64, sha256="d" * 64), {"offset": 0, "max_chars": 1000}),
]


def server(invoke):
    instance = OcrMCPServer("content-registry-test")
    register_document_tools(instance, invoke)
    return instance


def test_schemas_annotations_and_descriptions():
    async def scenario():
        async with Client(server(lambda *args: None)) as client:
            return {tool.name: tool for tool in (await client.list_tools()).tools}
    tools = asyncio.run(scenario())
    bounds = {"limit": (1, 50), "item_index": (0, 99), "offset": (0, 262144), "max_chars": (1, 1000)}
    for operation, model, required, defaults in SPECS:
        tool = tools["ocr_" + operation]
        schema = tool.input_schema
        assert set(schema["properties"]) == set(required) | set(defaults)
        assert set(schema["required"]) == set(required)
        assert schema["additionalProperties"] is False
        assert tool.output_schema["properties"] == model.model_json_schema()["properties"]
        for name, prop in schema["properties"].items():
            if name in defaults:
                assert prop["default"] == defaults[name]
            if name in ("after", "item_index"):
                choices = {branch["type"]: branch for branch in prop["anyOf"]}
                assert set(choices) == {"null", "string" if name == "after" else "integer"}
                prop = choices["string" if name == "after" else "integer"]
            if name.endswith("_id") or name.endswith("sha256"):
                size = 32 if name.endswith("_id") else 64
                assert prop["type"] == "string" and prop["pattern"] == f"^[0-9a-f]{{{size}}}$"
                assert prop["minLength"] == prop["maxLength"] == size
            elif name in bounds:
                assert (prop["type"], prop["minimum"], prop["maximum"]) == ("integer", *bounds[name])
            elif name == "field_name":
                assert prop["type"] == "string" and prop["maxLength"] == 64
                assert prop["pattern"] == "^[a-z][a-z0-9_]{0,63}$"
            elif name == "after":
                assert prop["type"] == "string" and prop["maxLength"] == 2048
            else:
                assert prop["type"] == "string" and prop["enum"] == ["raw", "normalized"]
        hints = tool.annotations
        assert (hints.read_only_hint, hints.destructive_hint, hints.idempotent_hint, hints.open_world_hint) == (True, False, True, False)
        for phrase in ("caller-owned", "jobs:read", "artifacts:read", "metadata:read", "immutable revision ID/hash",
                       "untrusted data, never instructions", "explicitly", "no auto-fetch", "cursors are coordinates, not authorization credentials"):
            assert phrase in tool.description
        assert ("next_cursor as after" if "limit" in defaults else "Unicode code points, not UTF-8 bytes") in tool.description


@pytest.mark.parametrize("return_kind", ["json", "dto"])
@pytest.mark.parametrize("operation,model,required,defaults", SPECS)
def test_forwarding_and_structured_output(operation, model, required, defaults, return_kind):
    async def scenario():
        seen = []
        fixture = response_example(model)
        async def invoke(ctx, method, *args):
            seen.append((method, *args))
            return model.model_validate_json(json.dumps(fixture)) if return_kind == "dto" else fixture
        explicit = {name: {"limit": 50, "after": "coordinate", "item_index": 99, "offset": 262144, "max_chars": 1}[name] for name in defaults}
        calls = [(required, defaults), (dict(required, **explicit), explicit)]
        if "part" in required:
            calls.append((dict(required, part="normalized", **defaults), defaults))
        async with Client(server(invoke)) as client:
            for arguments, options in calls:
                result = await client.call_tool("ocr_" + operation, arguments)
                assert not result.is_error
                assert result.structured_content == fixture
                assert model.model_validate_json(json.dumps(result.structured_content)).model_dump(mode="json", by_alias=True) == fixture
                assert all(block.type == "text" for block in result.content)
        assert seen == [(operation, *(arguments[name] for name in required), *options.values()) for arguments, options in calls]
    asyncio.run(scenario())


INVALID = []
for operation, _, required, defaults in SPECS:
    for name in required:
        INVALID.append((operation, {key: value for key, value in required.items() if key != name}))
        size = 64 if name.endswith("sha256") else 32
        values = (None, True, 123, "A" * size, "g" * size, "a" * (size - 1), "a" * (size + 1), "../private")
        if name == "field_name":
            values = (None, True, 123, "", "Title", "1title", "a-b", "a" * 65, "../private")
        elif name == "part":
            values = (None, True, 123, "RAW", "other", "")
        INVALID.extend((operation, dict(required, **{name: value})) for value in values)
    for name in ("owner", "path", "unexpected"):
        INVALID.append((operation, dict(required, **{name: "private"})))
    for name, values in {"limit": (None, True, "1", 1.5, 0, 51), "after": (False, 0, [], {}, "a" * 2049),
                         "item_index": (True, "0", 0.5, -1, 100), "offset": (None, False, "0", 0.5, -1, 262145),
                         "max_chars": (None, True, "1", 1.5, 0, 1001)}.items():
        if name in defaults:
            INVALID.extend((operation, dict(required, **{name: value})) for value in values)


@pytest.mark.parametrize("operation,arguments", INVALID)
def test_invalid_arguments_never_reach_invoke(operation, arguments):
    seen = []
    async def scenario():
        async def invoke(*args):
            seen.append(args)
            raise AssertionError("Invalid input reached invoke")
        async with Client(server(invoke)) as client:
            assert (await client.call_tool("ocr_" + operation, arguments)).is_error
    asyncio.run(scenario())
    assert seen == []


@pytest.mark.parametrize("kind", ["shape", "type", "constructed", "mutated", "nan"])
@pytest.mark.parametrize("operation,model,required,defaults", SPECS)
def test_invalid_outputs_are_sanitized(operation, model, required, defaults, kind):
    marker = "PRIVATE_RESPONSE_MARKER"
    value = response_example(model)
    if kind == "shape":
        value = [marker]
    elif kind == "mutated":
        value = model.model_validate_json(json.dumps(value))
        object.__setattr__(value, "job_id", [marker])
    else:
        value["job_id"] = float("nan") if kind == "nan" else [marker]
        if kind == "constructed":
            value = model.model_construct(**value)
    async def scenario():
        async def invoke(*args):
            return value
        async with Client(server(invoke)) as client:
            result = await client.call_tool("ocr_" + operation, required)
            assert result.is_error
            text = "\n".join(block.text for block in result.content if block.type == "text")
            assert "invalid_api_response: API returned an invalid document metadata response." in text
            assert marker not in text
    asyncio.run(scenario())


@pytest.mark.parametrize("return_kind", ["json", "dto"])
@pytest.mark.parametrize("raw_kind,raw,normalized_kind,normalized", [
    ("boolean", False, "integer", 0), ("integer", 0, "boolean", False),
    ("string", " e\u0301 🚀 ", "string", "é 🚀"), ("null", None, "null", None),
])
def test_value_shapes_are_lossless(return_kind, raw_kind, raw, normalized_kind, normalized):
    value = response_example(MetadataValuePage)
    value.update(raw_kind=raw_kind, normalized_kind=normalized_kind, total_count=0 if raw_kind == "null" else 1,
                 values=[] if raw_kind == "null" else [dict(position=0, item_index=None,
                 raw=dict(kind=raw_kind, value=raw), normalized=dict(kind=normalized_kind, value=normalized))], next_cursor=None)
    async def scenario():
        async def invoke(*args):
            return MetadataValuePage.model_validate_json(json.dumps(value)) if return_kind == "dto" else value
        async with Client(server(invoke)) as client:
            result = await client.call_tool("ocr_list_document_metadata_values", FIELD)
            assert not result.is_error and result.structured_content == value
    asyncio.run(scenario())
