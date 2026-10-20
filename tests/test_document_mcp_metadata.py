"""Pure metadata registry contracts with synthetic DTOs; no authorization E2E."""
import asyncio

import pytest
from mcp import Client

from app.document_http_examples import response_example
from app.document_mcp_tools import register_document_tools
from app.document_transport_models import MetadataFieldPage, MetadataRevisionPage, MetadataView
from app.mcp_tools import OcrMCPServer


JOB, REVISION, SEGMENT, SHA = "a" * 32, "b" * 32, "c" * 32, "d" * 64
SPECS = [
    ("get_document_metadata", MetadataView, {"job_id": JOB}, {}),
    ("list_document_metadata_revisions", MetadataRevisionPage, {"job_id": JOB},
     {"limit": 25, "after": None}),
    ("get_document_metadata_revision", MetadataView,
     {"job_id": JOB, "revision_id": REVISION, "sha256": SHA}, {}),
    ("list_document_metadata_fields", MetadataFieldPage,
     {"job_id": JOB, "revision_id": REVISION, "segment_id": SEGMENT, "sha256": SHA},
     {"limit": 20, "after": None}),
]


def server(invoke):
    instance = OcrMCPServer("metadata-registry-test")
    register_document_tools(instance, invoke)
    return instance


def test_metadata_schemas_annotations_and_descriptions():
    async def scenario():
        async with Client(server(lambda *args: None)) as client:
            return {tool.name: tool for tool in (await client.list_tools()).tools}
    tools = asyncio.run(scenario())
    for operation, model, required, defaults in SPECS:
        tool = tools["ocr_" + operation]
        schema = tool.input_schema
        props = schema["properties"]
        assert set(props) == set(required) | set(defaults)
        assert set(schema["required"]) == set(required)
        assert schema["additionalProperties"] is False
        assert tool.output_schema["properties"] == model.model_json_schema()["properties"]
        for field in required:
            prop = props[field]
            length = 64 if field == "sha256" else 32
            assert prop["type"] == "string"
            assert prop["pattern"] == f"^[0-9a-f]{{{length}}}$"
            assert prop["minLength"] == prop["maxLength"] == length
        if defaults:
            high = 100 if operation.endswith("revisions") else 50
            prop = props["limit"]
            assert (prop["type"], prop["minimum"], prop["maximum"], prop["default"]) == (
                "integer", 1, high, defaults["limit"])
            assert props["after"]["default"] is None
            choices = {branch["type"]: branch for branch in props["after"]["anyOf"]}
            assert set(choices) == {"string", "null"}
            assert choices["string"]["maxLength"] == 2048
        hints = tool.annotations
        assert hints.read_only_hint is True and hints.destructive_hint is False
        assert hints.idempotent_hint is True and hints.open_world_hint is False
        for phrase in ("caller-owned", "jobs:read", "artifacts:read", "metadata:read",
                       "untrusted", "never instructions", "explicitly", "no auto-fetch",
                       "cursors are coordinates, not authorization credentials", "revision ID/hash"):
            assert phrase in tool.description
        if defaults:
            assert "next_cursor as after" in tool.description
    assert "current HEAD metadata summary" in tools["ocr_get_document_metadata"].description
    for operation in ("get_document_metadata_revision", "list_document_metadata_fields"):
        description = tools["ocr_" + operation].description
        assert "required SHA-256" in description and "does not follow later HEAD" in description


@pytest.mark.parametrize("operation,model,required,defaults", SPECS)
def test_exact_forwarding_and_typed_structured_output(operation, model, required, defaults):
    async def scenario():
        seen = []
        async def invoke(ctx, method, *args):
            seen.append((method, *args))
            return response_example(model)
        calls = [(required, defaults)]
        if defaults:
            high = 100 if operation.endswith("revisions") else 50
            calls += [(dict(required, limit=high, after="coordinate_token"),
                       {"limit": high, "after": "coordinate_token"}),
                      (dict(required, limit=1, after=None), {"limit": 1, "after": None})]
        async with Client(server(invoke)) as client:
            for arguments, options in calls:
                result = await client.call_tool("ocr_" + operation, arguments)
                assert not result.is_error
                fixture = response_example(model)
                assert result.structured_content == fixture
                assert isinstance(model.model_validate(result.structured_content), model)
                assert all(block.type == "text" for block in result.content)
                if "next_cursor" in fixture:
                    assert result.structured_content["next_cursor"] is None
        assert seen == [(operation, *required.values(), *options.values())
                        for _, options in calls]
    asyncio.run(scenario())


INVALID = []
for operation, _, required, defaults in SPECS:
    for field in required:
        INVALID.append((operation, {key: value for key, value in required.items() if key != field}))
        length = 64 if field == "sha256" else 32
        for value in (None, True, 123, "A" * length, "g" * length,
                      "a" * (length - 1), "a" * (length + 1), "../private", "a" * length + "\n"):
            INVALID.append((operation, dict(required, **{field: value})))
    for field in ("owner", "path", "unexpected"):
        INVALID.append((operation, dict(required, **{field: "private"})))
    if defaults:
        high = 100 if operation.endswith("revisions") else 50
        for value in (None, True, "1", 1.5, -1, 0, high + 1):
            INVALID.append((operation, dict(required, limit=value)))
        for value in (False, 0, [], {}, "a" * 2049):
            INVALID.append((operation, dict(required, after=value)))


@pytest.mark.parametrize("operation,arguments", INVALID)
def test_invalid_arguments_never_reach_invoke(operation, arguments):
    seen = []
    async def scenario():
        async def invoke(*args):
            seen.append(args)
            raise AssertionError("Invalid arguments reached invoke")
        async with Client(server(invoke)) as client:
            assert (await client.call_tool("ocr_" + operation, arguments)).is_error
    asyncio.run(scenario())
    assert seen == []
