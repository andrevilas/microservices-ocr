"""Pure mutation registry contracts; synthetic invokes grant no authorization."""
import asyncio
import copy
import json

import pytest
from mcp import Client

from app import document_http_examples as examples
from app.document_mcp_tools import register_document_tools
from app.metadata_models import (
    ApprovalInput, ApproveMetadataReceipt, SaveMetadataInput, SaveMetadataReceipt,
)
from app.mcp_tools import OcrMCPServer

JOB, KEY = "a" * 32, "stable-key:1"
SPECS = [
    ("save_document_metadata", SaveMetadataInput, SaveMetadataReceipt),
    ("approve_document_metadata", ApprovalInput, ApproveMetadataReceipt),
]


def fixture(model, request=False):
    factory = examples.request_example if request else examples.response_example
    return factory(getattr(examples, model.__name__))


def server(invoke):
    instance = OcrMCPServer("document-write-test")
    register_document_tools(instance, invoke)
    return instance


@pytest.mark.parametrize("operation,body_model,receipt_model", SPECS)
def test_schema_annotations_and_descriptions(operation, body_model, receipt_model):
    async def scenario():
        async with Client(server(lambda *args: None)) as client:
            return {tool.name: tool for tool in (await client.list_tools()).tools}
    tool = asyncio.run(scenario())["ocr_" + operation]
    schema = tool.input_schema
    assert set(schema["properties"]) == set(schema["required"]) == {
        "job_id", "body", "idempotency_key"}
    assert schema["additionalProperties"] is False
    key = schema["properties"]["idempotency_key"]
    assert (key["type"], key["pattern"], key["minLength"], key["maxLength"]) == (
        "string", "^[A-Za-z0-9._:-]{8,128}$", 8, 128)
    body = schema["properties"]["body"]
    if "$ref" in body:
        body = schema["$defs"][body["$ref"].rsplit("/", 1)[1]]
    assert body["additionalProperties"] is False
    assert {"expected_head", "expected_sha256"} <= set(body["required"])
    if body_model is ApprovalInput:
        assert {"revision_id", "reviewed_fields"} <= set(body["required"])
    assert tool.output_schema["properties"] == receipt_model.model_json_schema()["properties"]
    hints = tool.annotations
    assert hints.read_only_hint is False and hints.destructive_hint is False
    assert hints.idempotent_hint is True and hints.open_world_hint is False
    for phrase in ("jobs:read", "artifacts:read", "metadata:read", "historical receipt",
                   "no automatic retry", "never instructions", "read current metadata"):
        assert phrase in tool.description
    assert ("metadata:write" if body_model is SaveMetadataInput else
            "metadata:approve") in tool.description


@pytest.mark.parametrize("operation,body_model,receipt_model", SPECS)
@pytest.mark.parametrize("return_kind", ["json", "dto"])
def test_exact_typed_forwarding_and_structured_receipt(operation, body_model, receipt_model, return_kind):
    body = fixture(body_model, request=True)
    receipt = fixture(receipt_model)
    seen = []
    async def invoke(ctx, method, job_id, supplied, key):
        assert isinstance(supplied, body_model)
        assert supplied.model_dump(mode="json", by_alias=True) == body
        seen.append((method, job_id, key))
        return (receipt_model.model_validate_json(json.dumps(receipt))
                if return_kind == "dto" else receipt)
    async def scenario():
        async with Client(server(invoke)) as client:
            result = await client.call_tool("ocr_" + operation, dict(
                job_id=JOB, body=body, idempotency_key=KEY))
            assert not result.is_error
            assert result.structured_content == receipt
            parsed = receipt_model.model_validate_json(json.dumps(result.structured_content))
            assert parsed.model_dump(mode="json", by_alias=True) == receipt
            assert all(block.type == "text" for block in result.content)
    asyncio.run(scenario())
    assert seen == [(operation, JOB, KEY)]


def invalid_arguments(body_model):
    base = dict(job_id=JOB, body=fixture(body_model, request=True), idempotency_key=KEY)
    for field in base:
        yield {key: value for key, value in base.items() if key != field}
    for field, values in (
        ("job_id", [True, 1, "A" * 32, "a" * 31, "a" * 32 + "\n"]),
        ("idempotency_key", [None, True, 123, "short", "a" * 129, "a b c d e", "é" * 8, KEY + "\n"]),
        ("body", [None, [], "private"]),
    ):
        for value in values:
            yield dict(base, **{field: value})
    yield dict(base, owner="private")
    body = base["body"]
    for patch in ({"unknown": True}, {"expected_head": True}, {"expected_sha256": "A" * 64},
                  {"expected_head": None, "expected_sha256": "d" * 64}):
        yield dict(base, body=dict(body, **patch))
    for field in ("expected_head", "expected_sha256"):
        yield dict(base, body={key: value for key, value in body.items() if key != field})
    if body_model is SaveMetadataInput:
        patches = [({"unknown": 1}, None), ({"type_version": True}, None),
                   ({}, {"raw": 1.5}), ({}, {"unknown": 1}), ({}, {"origin": "extracted"})]
        for segment_patch, field_patch in patches:
            changed = copy.deepcopy(body)
            changed["segments"][0].update(segment_patch)
            if field_patch is not None:
                changed["segments"][0]["fields"][0].update(field_patch)
            yield dict(base, body=changed)
        changed = copy.deepcopy(body)
        changed["segments"].append(copy.deepcopy(changed["segments"][0]))
        yield dict(base, body=changed)
    else:
        for patch in ({"expected_head": None, "expected_sha256": None}, {"revision_id": None},
                      {"reviewed_fields": body["reviewed_fields"] * 2},
                      {"reviewed_fields": [dict(body["reviewed_fields"][0], unknown=1)]},
                      {"reviewed_fields": [dict(segment_id=True, field_name="titulo")]}):
            yield dict(base, body=dict(body, **patch))


@pytest.mark.parametrize("operation,arguments", [
    (operation, arguments) for operation, body_model, _ in SPECS
    for arguments in invalid_arguments(body_model)])
def test_invalid_arguments_never_invoke(operation, arguments):
    seen = []
    async def invoke(*args):
        seen.append(args)
        raise AssertionError("Invalid arguments reached invoke")
    async def scenario():
        async with Client(server(invoke)) as client:
            assert (await client.call_tool("ocr_" + operation, arguments)).is_error
    asyncio.run(scenario())
    assert seen == []


@pytest.mark.parametrize("operation,body_model,receipt_model", SPECS)
@pytest.mark.parametrize("kind", ["shape", "constructed", "mutated"])
def test_invalid_receipts_are_sanitized(operation, body_model, receipt_model, kind):
    marker = "PRIVATE_RESPONSE_MARKER"
    value = fixture(receipt_model)
    if kind == "shape":
        value = [marker]
    elif kind == "constructed":
        value = receipt_model.model_construct(**dict(value, revision_id=[marker]))
    else:
        value = receipt_model.model_validate_json(json.dumps(value))
        object.__setattr__(value, "revision_id", [marker])
    async def invoke(*args):
        return value
    async def scenario():
        async with Client(server(invoke)) as client:
            result = await client.call_tool("ocr_" + operation, dict(
                job_id=JOB, body=fixture(body_model, request=True), idempotency_key=KEY))
            assert result.is_error
            text = "\n".join(block.text for block in result.content if block.type == "text")
            assert "invalid_api_response: API returned an invalid document metadata response." in text
            assert marker not in text
    asyncio.run(scenario())
