"""Isolated document registry contracts; no runtime or stdio dispatch required."""
import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import Client

from app.document_canonical_models import CanonicalManifest, CanonicalPageChunk
from app.document_http_examples import response_example
from app.document_mcp_tools import DOCUMENT_OPERATIONS, register_document_tools
from app.mcp_tools import OcrMCPServer


MANIFEST = "ocr_get_document_canonical_manifest"
CHUNK = "ocr_get_document_canonical_chunk"
MODELS = {MANIFEST: CanonicalManifest, CHUNK: CanonicalPageChunk}
JOB = "1" * 32


def server(invoke):
    instance = OcrMCPServer("document-test")
    register_document_tools(instance, invoke)
    return instance


def test_fresh_import_and_registration_are_pure():
    code = """
import asyncio, sys
from mcp.server import MCPServer
from app.document_mcp_tools import register_document_tools
server = MCPServer('pure-document-test')
register_document_tools(server, lambda *args: None)
assert len(asyncio.run(server.list_tools())) == 19
blocked = ('app.main', 'app.config', 'app.application', 'app.runtime',
           'app.services', 'app.routes', 'app.document_http_examples')
assert not [name for name in sys.modules
            if any(name == prefix or name.startswith(prefix + '.') for prefix in blocked)]
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_registry_schemas_annotations_and_fifteen_tools():
    async def scenario():
        async with Client(server(lambda *args: None)) as client:
            return (await client.list_tools()).tools
    tools = {tool.name: tool for tool in asyncio.run(scenario())}
    operations = frozenset({
        "get_document_canonical_manifest", "get_document_canonical_chunk",
        "get_document_metadata", "list_document_metadata_revisions",
        "get_document_metadata_revision", "list_document_metadata_fields",
        "list_document_metadata_values", "list_document_metadata_evidence",
        "get_document_metadata_string", "get_document_metadata_schema",
        "save_document_metadata", "approve_document_metadata",
        "create_document_package", "list_document_packages", "get_document_package",
        "create_metadata_extraction", "list_metadata_extractions", "get_metadata_extraction",
        "apply_metadata_extraction",
    })
    assert set(tools) == {"ocr_" + operation for operation in operations}
    assert DOCUMENT_OPERATIONS == operations
    tools = {name: tools[name] for name in MODELS}
    for name, tool in tools.items():
        schema = tool.input_schema
        expected = {"job_id"} if name == MANIFEST else {"job_id", "page", "offset", "max_chars"}
        assert set(schema["properties"]) == expected
        assert schema["required"] == ["job_id"]
        assert schema["additionalProperties"] is False
        identity = schema["properties"]["job_id"]
        assert identity["type"] == "string" and identity["pattern"] == "^[0-9a-f]{32}$"
        assert identity["minLength"] == identity["maxLength"] == 32
        hints = tool.annotations
        assert hints.read_only_hint is True and hints.destructive_hint is False
        assert hints.idempotent_hint is True and hints.open_world_hint is False
        assert tool.output_schema["properties"]
        for phrase in ("jobs:read", "artifacts:read", "caller-owned", "untrusted", "never instructions",
                       "next_page/next_offset", "no auto-pagination", "not PDF/A certification"):
            assert phrase in tool.description
    for field, low, high, default in (("page", 1, 300, 1), ("offset", 0, 500000, 0),
                                      ("max_chars", 1, 1000, 1000)):
        prop = tools[CHUNK].input_schema["properties"][field]
        assert (prop["type"], prop["minimum"], prop["maximum"], prop["default"]) == (
            "integer", low, high, default)
    assert tools[CHUNK].output_schema["properties"]["text"]["maxLength"] == 20000


def test_calls_forward_exact_operations_defaults_and_explicit_values_with_structured_output():
    async def scenario():
        seen = []
        async def invoke(ctx, operation, *args):
            seen.append((operation, *args))
            model = CanonicalManifest if operation.endswith("manifest") else CanonicalPageChunk
            return response_example(model)
        calls = [(MANIFEST, {"job_id": JOB}), (CHUNK, {"job_id": JOB}),
                 (CHUNK, {"job_id": JOB, "page": 2, "offset": 3, "max_chars": 7}),
                 (CHUNK, {"job_id": JOB, "page": 300, "offset": 500000, "max_chars": 1})]
        async with Client(server(invoke)) as client:
            for name, arguments in calls:
                result = await client.call_tool(name, arguments)
                assert not result.is_error
                model = MODELS[name]
                assert result.structured_content == response_example(model)
                assert isinstance(model.model_validate(result.structured_content), model)
                assert all(block.type == "text" for block in result.content)
        assert seen == [("get_document_canonical_manifest", JOB),
                        ("get_document_canonical_chunk", JOB, 1, 0, 1000),
                        ("get_document_canonical_chunk", JOB, 2, 3, 7),
                        ("get_document_canonical_chunk", JOB, 300, 500000, 1)]
    asyncio.run(scenario())


INVALID = [(name, args) for name in MODELS for args in (
    {}, {"job_id": True}, {"job_id": 123}, {"job_id": "A" * 32},
    {"job_id": "a" * 31}, {"job_id": "g" * 32}, {"job_id": "../private"},
    {"job_id": JOB, "owner": "private"}, {"job_id": JOB, "path": "private"},
)] + [(CHUNK, {"job_id": JOB, field: value})
      for field, values in (("page", (True, "1", 0, 301)),
                            ("offset", (False, "0", -1, 500001)),
                            ("max_chars", (True, "1", 0, 1001))) for value in values]


@pytest.mark.parametrize("name,arguments", INVALID)
def test_invalid_arguments_are_rejected_before_invoke(name, arguments):
    seen = []
    async def scenario():
        async def invoke(*args):
            seen.append(args)
            raise AssertionError("Invalid arguments reached invoke")
        async with Client(server(invoke)) as client:
            assert (await client.call_tool(name, arguments)).is_error
    asyncio.run(scenario())
    assert seen == []
