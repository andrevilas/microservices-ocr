"""Raw body boundaries before SDK validation, without runtime I/O."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from mcp import Client

from app.document_mcp_limits import DocumentMCPInputGuard, MAX_DOCUMENT_MCP_REQUEST_BYTES
from app.mcp_tools import OcrMCPServer

NAMES = ("ocr_save_document_metadata", "ocr_approve_document_metadata")
MODERN = "2026-07-28"
MARKER = "PRIVATE_INPUT_MARKER"
LARGE = "request_too_large: Document request body exceeds 524288 bytes."
INVALID = "validation_error: Document request body could not be serialized."


def size(value):
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8"))


def context(arguments, name=NAMES[0], method="tools/call", version=MODERN):
    return SimpleNamespace(method=method, params={"name": name, "arguments": arguments},
                           protocol_version=version)


def error(message, version=MODERN):
    result = {"content": [{"type": "text", "text": message}], "isError": True}
    if version == MODERN:
        result["resultType"] = "complete"
    return result


def boundary_body(target, unit):
    remaining = target - size({"value": MARKER})
    count, padding = divmod(remaining, size(unit) - 2)
    return {"value": MARKER + unit * count + "x" * padding}


async def roundtrip(name, body, mode):
    counts, seen, captured = [0, 0], [], []
    original = json.dumps(body)

    class ProbeServer(OcrMCPServer):
        async def call_tool(self, name, arguments, context=None):
            counts[0] += 1
            return await super().call_tool(name, arguments, context)

    async def tool(body: dict) -> str:
        counts[1] += 1
        seen.append(body)
        return "ok"

    async def observe(ctx, call_next):
        result = await call_next(ctx)
        if ctx.method == "tools/call":
            captured.append(result)
        return result

    instance = ProbeServer("input-limits-test")
    instance.middleware.insert(0, observe)
    tool.__name__ = name
    instance.tool()(tool)
    async with Client(instance, mode=mode) as client:
        result = await client.call_tool(name, {"body": body})
    assert json.dumps(body) == original
    return result, captured[-1], counts, seen


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("unit", ["x", "é", "🚀", "\u0000"])
@pytest.mark.parametrize("extra", [0, 1])
def test_actual_client_body_boundary_before_dispatch(mode, name, unit, extra):
    body = boundary_body(MAX_DOCUMENT_MCP_REQUEST_BYTES + extra, unit)
    assert size(body) == MAX_DOCUMENT_MCP_REQUEST_BYTES + extra
    # The argument frame is already larger than the body budget at the boundary.
    assert size({"body": body}) > MAX_DOCUMENT_MCP_REQUEST_BYTES
    response, wire, counts, seen = asyncio.run(roundtrip(name, body, mode))
    assert response.is_error == bool(extra)
    if extra:
        assert counts == [0, 0] and seen == []
        assert wire == error(LARGE, MODERN if mode == "auto" else "legacy")
        assert MARKER not in json.dumps(wire)
    else:
        assert counts == [1, 1] and seen == [body]


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("body", [None, [], MARKER, 123])
def test_small_invalid_types_defer_to_sdk(mode, body):
    response, wire, counts, seen = asyncio.run(roundtrip(NAMES[0], body, mode))
    assert response.is_error and counts == [1, 0] and seen == []
    assert "validation_error:" in wire["content"][0]["text"]
    assert MARKER not in json.dumps(wire)


@pytest.mark.parametrize("version", [MODERN, "legacy"])
@pytest.mark.parametrize("kind", ["type", "nan", "unicode", "cycle", "depth"])
def test_invalid_serialization_is_sanitized_before_next(version, kind):
    bad = {"type": object(), "nan": float("nan"), "unicode": "\ud800"}.get(kind, [])
    if kind == "cycle":
        bad.append(bad)
    if kind == "depth":
        for _ in range(2000):
            bad = [bad]
    async def next_call(_):
        pytest.fail("Invalid body reached SDK dispatch")
    ctx = context({"body": {MARKER: bad}}, version=version)
    assert asyncio.run(DocumentMCPInputGuard()(ctx, next_call)) == error(INVALID, version)


@pytest.mark.parametrize("method,name,arguments", [
    ("tools/call", NAMES[0], None), ("tools/call", NAMES[0], []),
    ("tools/call", NAMES[0], {}), ("tools/call", NAMES[1], {"job_id": MARKER}),
    ("tools/call", "ocr_unknown", {"body": object()}),
    ("tools/call", "ocr_get_document_metadata", {"body": object()}),
    ("tools/call", "ocr_get_context", {"body": object()}),
    ("tools/call", NAMES[0] + "_extra", {"body": object()}),
    ("tools/call", [NAMES[0]], {"body": object()}),
    ("tools/list", NAMES[0], {"body": object()}),
    ("notifications/initialized", NAMES[0], {"body": object()}),
])
def test_unrelated_or_missing_arguments_pass_through(method, name, arguments):
    ctx, result, seen = context(arguments, name, method), object(), []
    async def next_call(supplied):
        seen.append(supplied)
        assert supplied.params["arguments"] is arguments
        return result
    assert asyncio.run(DocumentMCPInputGuard()(ctx, next_call)) is result
    assert seen == [ctx]


@pytest.mark.parametrize("params", [None, [], {}, {"name": None}])
def test_non_tool_params_pass_through(params):
    ctx, result = context({}), object()
    ctx.params = params
    async def next_call(supplied):
        assert supplied is ctx
        return result
    assert asyncio.run(DocumentMCPInputGuard()(ctx, next_call)) is result


@pytest.mark.parametrize("exception", [RuntimeError(MARKER), asyncio.CancelledError()])
def test_downstream_exceptions_propagate_once_without_mutation(exception):
    body, seen = {"value": MARKER}, []
    ctx = context({"body": body, "idempotency_key": "stable-key"})
    async def next_call(supplied):
        seen.append(supplied)
        assert supplied is ctx and supplied.params["arguments"]["body"] is body
        raise exception
    with pytest.raises(type(exception)) as caught:
        asyncio.run(DocumentMCPInputGuard()(ctx, next_call))
    assert caught.value is exception and seen == [ctx]
    assert body == {"value": MARKER}
