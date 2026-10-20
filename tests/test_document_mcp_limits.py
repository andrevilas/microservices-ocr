"""Final wire limits, with in-process SDK clients and synthetic boundaries."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp import Client
from mcp.types import CallToolResult
from pydantic import BaseModel

from app.document_mcp_limits import DocumentMCPOutputGuard, MAX_DOCUMENT_MCP_RESULT_BYTES
from app.document_mcp_tools import DOCUMENT_OPERATIONS
from app.mcp_tools import OcrMCPServer

class Payload(BaseModel):
    value: str


NAME = "ocr_get_document_canonical_manifest"
MODERN = "2026-07-28"
MESSAGE = "response_too_large: Document response exceeds the MCP limit; request a smaller page or chunk."


def size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))


def context(name=NAME, method="tools/call", version=MODERN):
    return SimpleNamespace(method=method, params={"name": name}, protocol_version=version)


def apply(value, ctx=None):
    async def next_call(_):
        return value
    return asyncio.run(DocumentMCPOutputGuard()(ctx or context(), next_call))


@pytest.mark.parametrize("operation", sorted(DOCUMENT_OPERATIONS))
@pytest.mark.parametrize("extra", [0, 1])
def test_exact_final_result_boundary(operation, extra):
    value = {"content": [{"type": "text", "text": ""}], "structuredContent": {"v": "é🚀\u0000"},
             "_meta": {"server": "test"}, "resultType": "complete"}
    value["content"][0]["text"] = "x" * (MAX_DOCUMENT_MCP_RESULT_BYTES - size(value) + extra)
    original = json.dumps(value)
    result = apply(value, context("ocr_" + operation))
    assert json.dumps(value) == original
    assert size(value) == MAX_DOCUMENT_MCP_RESULT_BYTES + extra
    assert result is value if not extra else result["isError"] and result["content"][0]["text"] == MESSAGE
    if extra:
        assert set(result) == {"content", "isError", "resultType"}


@pytest.mark.parametrize("kind", ["type", "nan", "unicode", "cycle", "depth"])
def test_invalid_serialization_is_sanitized(kind):
    bad = {"type": object(), "nan": float("nan"), "unicode": "\ud800"}.get(kind, [])
    if kind == "cycle":
        bad.append(bad)
    if kind == "depth":
        for _ in range(2000):
            bad = [bad]
    result = apply({"content": [{"type": "text", "text": "PRIVATE_MARKER"}], "_meta": {"bad": bad}})
    assert result == {"content": [{"type": "text", "text":
        "invalid_api_response: Document response could not be serialized."}],
        "isError": True, "resultType": "complete"}


@pytest.mark.parametrize("method,name", [
    ("tools/call", "ocr_get_text"), ("tools/call", "ocr_get_context"),
    ("tools/call", "ocr_unknown"), ("tools/call", NAME + "_extra"),
    ("tools/call", [NAME]), ("tools/list", NAME), ("notifications/initialized", NAME),
])
def test_unrelated_dispatch_is_untouched(method, name):
    value = {"invalid": object(), "large": "x" * 100000}
    assert apply(value, context(name, method)) is value


@pytest.mark.parametrize("params", [None, [], {}, {"name": None}])
def test_non_tool_params_are_untouched(params):
    ctx = context()
    ctx.params = params
    value = object()
    assert apply(value, ctx) is value


@pytest.mark.parametrize("error", [RuntimeError("PRIVATE_MARKER"), asyncio.CancelledError()])
def test_call_next_exceptions_propagate_once(error):
    seen = []
    async def next_call(ctx):
        seen.append(ctx)
        raise error
    ctx = context()
    with pytest.raises(type(error)) as caught:
        asyncio.run(DocumentMCPOutputGuard()(ctx, next_call))
    assert caught.value is error and seen == [ctx]


async def roundtrip(kind, payload, mode, name=NAME, target=None):
    captured = []
    async def enrich(ctx, call_next):
        result = await call_next(ctx)
        if ctx.method == "tools/call" and (kind == "meta" or target is not None):
            result = dict(result)
            result["_meta"] = dict(result.get("_meta", {}), padding="")
            result["_meta"]["padding"] = "m" * (65536 if target is None else target - size(result))
        return result
    async def observe(ctx, call_next):
        result = await call_next(ctx)
        if ctx.method == "tools/call":
            captured.append(result)
        return result
    async def text_tool() -> str:
        return payload
    async def dict_tool() -> Payload:
        return Payload(value=payload)
    async def result_tool() -> CallToolResult:
        return CallToolResult.model_validate({"content": [{"type": "text", "text": payload}],
                                              "structuredContent": {"value": payload}})
    instance = OcrMCPServer("limits-test", middleware=[enrich])
    instance.middleware.insert(0, observe)
    tool = {"text": text_tool, "dict": dict_tool, "result": result_tool, "meta": dict_tool}[kind]
    tool.__name__ = name
    instance.tool()(tool)
    async with Client(instance, mode=mode) as client:
        response = await client.call_tool(name, {})
    return response, captured[-1]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_actual_sdk_typed_payload_preserves_both_representations(mode):
    payload = "é🚀\u0000"
    response, wire = asyncio.run(roundtrip("dict", payload, mode))
    assert not response.is_error
    assert wire["structuredContent"] == {"value": payload}
    assert len(wire["content"]) == 1
    assert wire["content"][0]["type"] == "text"
    assert json.loads(wire["content"][0]["text"]) == {"value": payload}


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("kind,payload", [
    ("text", "🚀" * 18000), ("text", "\u0000" * 12000),
    ("dict", "x" * 34000), ("result", "x" * 34000),
    ("dict", "🚀" * 18000), ("result", "\u0000" * 12000), ("meta", "small"),
], ids=["text-utf8", "text-control", "dict-ascii-dup", "result-ascii-dup",
        "dict-utf8-dup", "result-control-dup", "meta"])
def test_actual_sdk_rejects_oversized_final_results(mode, kind, payload):
    response, wire = asyncio.run(roundtrip(kind, payload, mode))
    assert response.is_error
    expected = {"content": [{"type": "text", "text": MESSAGE}], "isError": True}
    if mode == "auto":
        expected["resultType"] = "complete"
    assert wire == expected


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("extra", [0, 1])
def test_actual_sdk_counts_final_metadata_and_protocol_fields(mode, extra):
    response, wire = asyncio.run(roundtrip("dict", "é🚀\u0000", mode, target=65536 + extra))
    assert response.is_error == bool(extra)
    if not extra:
        assert size(wire) == 65536
        if mode == "auto":
            assert wire["resultType"] == "complete"
            assert "io.modelcontextprotocol/serverInfo" in wire["_meta"]
    else:
        assert wire["content"] == [{"type": "text", "text": MESSAGE}]
        assert "_meta" not in wire


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_actual_sdk_legacy_tool_is_unrestricted(mode):
    response, wire = asyncio.run(roundtrip("text", "x" * 70000, mode, name="ocr_get_text"))
    assert not response.is_error
    assert size(wire) > MAX_DOCUMENT_MCP_RESULT_BYTES
    assert wire["content"][0]["text"] == "x" * 70000
