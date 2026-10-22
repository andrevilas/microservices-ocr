"""Isolated document stdio adapter; HTTP overrides test policy, not Bearer/OIDC authentication."""
import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import httpx2
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from app.document_mcp_tools import register_document_tools
from app.mcp_stdio import ApiClient
from app.mcp_tools import MCP_VERSION, OcrMCPServer
from app.metadata_models import SaveMetadataInput, SaveMetadataReceipt
from app.security import require_principal
from test_document_http_basic import http
from test_document_metadata import QUOTE, body, env, persisted, who

ID, RID, SID, DIGEST = "a" * 32, "b" * 32, "c" * 32, "d" * 64
KEY = "stdio-document-save-0001"


def registry(adapter):
    server = OcrMCPServer("Isolated documents", version=MCP_VERSION, log_level="WARNING")
    register_document_tools(server, adapter.invoke)
    return server


async def tool(client, operation, **arguments):
    result = await client.call_tool("ocr_" + operation, arguments)
    assert not result.is_error
    payload = result.structured_content
    assert isinstance(payload, dict)
    assert json.loads(result.content[0].text) == payload
    return payload


@pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")
def test_sdk_all_twelve_operations_and_historical_replay(http):
    e, app, transport_client, services = http

    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                     headers={"Authorization": "Bearer synthetic-token"},
                                     follow_redirects=False, trust_env=False) as remote:
            adapter = ApiClient("http://testserver", remote)
            async with Client(registry(adapter)) as sdk:
                job = dict(job_id=e.job.job_id)
                request = body(e)
                saved = await tool(sdk, "save_document_metadata", **job,
                                   body=request, idempotency_key=KEY)
                rid, digest = saved["revision_id"], saved["sha256"]
                sid = saved["segment_ids"]["main"]
                pinned = dict(**job, revision_id=rid, sha256=digest)
                field = dict(**pinned, segment_id=sid, field_name="title")
                current = await tool(sdk, "get_document_metadata", **job)
                assert (current["revision_id"], current["sha256"]) == (rid, digest)
                revisions = await tool(sdk, "list_document_metadata_revisions", **job, limit=1)
                assert [(r["revision_id"], r["sha256"]) for r in revisions["revisions"]] == [(rid, digest)]
                assert await tool(sdk, "get_document_metadata_revision", **pinned) == current
                fields = await tool(sdk, "list_document_metadata_fields", **pinned, segment_id=sid)
                assert '"title"' in json.dumps(fields)
                values = await tool(sdk, "list_document_metadata_values", **field)
                assert QUOTE in json.dumps(values, ensure_ascii=False)
                evidence = await tool(sdk, "list_document_metadata_evidence", **field)
                assert e.evidence["text_sha256"] in json.dumps(evidence)
                text = await tool(sdk, "get_document_metadata_string", **field, part="raw")
                assert QUOTE in json.dumps(text, ensure_ascii=False)
                snapshot = persisted(e, SimpleNamespace(**saved))
                schema_hash = snapshot.segments[0].schema_sha256
                schema = await tool(sdk, "get_document_metadata_schema", **pinned, schema_sha256=schema_hash)
                assert schema_hash in json.dumps(schema)
                manifest = await tool(sdk, "get_document_canonical_manifest", **job)
                chunk = await tool(sdk, "get_document_canonical_chunk", **job, page=1, offset=21000)
                assert manifest["job_id"] == chunk["document_id"] == e.job.job_id
                assert chunk["canonical_sha256"] == manifest["sha256"] and chunk["text"] == QUOTE
                approval = dict(revision_id=rid, expected_head=rid, expected_sha256=digest,
                                reviewed_fields=[dict(segment_id=sid, field_name="title")])
                approver = who(scopes={"jobs:read", "artifacts:read", "metadata:read", "metadata:approve"})
                app.dependency_overrides[require_principal] = lambda: approver
                try:
                    approved = await tool(sdk, "approve_document_metadata", **job,
                                          body=approval, idempotency_key="stdio-document-approve-0001")
                    assert await tool(sdk, "approve_document_metadata", **job, body=approval,
                                      idempotency_key="stdio-document-approve-0001") == approved
                finally:
                    app.dependency_overrides[require_principal] = lambda: e.owner
                assert approved["revision_id"] != rid
                assert persisted(e, SimpleNamespace(**approved)).state == "approved"
                assert await tool(sdk, "save_document_metadata", **job, body=request, idempotency_key=KEY) == saved
                assert await tool(sdk, "get_document_metadata_revision", **pinned) == current
                assert (await tool(sdk, "get_document_metadata", **job))["revision_id"] == approved["revision_id"]
            permit = services.execution.acquire(e.owner)
            try:
                with pytest.raises(ToolError, match=r"^document_operations_busy:") as caught:
                    await adapter.invoke(None, "get_document_metadata", e.job.job_id)
                assert "API rejected the operation (HTTP 429)" in str(caught.value)
            finally:
                permit.close()
            for principal, status in ((who("user:2"), 404), (who(scopes=set()), 403)):
                app.dependency_overrides[require_principal] = lambda: principal
                try:
                    with pytest.raises(ToolError) as caught:
                        await adapter.invoke(None, "get_document_metadata", e.job.job_id)
                    assert f"API rejected the operation (HTTP {status})" in str(caught.value)
                finally:
                    app.dependency_overrides[require_principal] = lambda: e.owner
            assert not e.jobs._artifact_leases

    transport_client.portal.call(scenario)


def test_write_bytes_headers_and_invalid_arguments_send_nothing():
    async def scenario():
        request = SaveMetadataInput.model_validate(dict(expected_head=None, expected_sha256=None,
            segments=[dict(client_key="main", segment_id=None, page_start=1, page_end=1,
                           type_id=ID, type_version=1, fields=[])]))
        dumped = request.model_dump(mode="json", by_alias=True, exclude_none=False)
        receipt = dict(revision_id=RID, sha256=DIGEST, segment_ids={"main": SID})
        SaveMetadataReceipt.model_validate(receipt)
        seen = []

        def handler(outgoing):
            seen.append(outgoing)
            assert outgoing.method == "PUT" and outgoing.url.path == f"/api/v1/jobs/{ID}/metadata"
            assert outgoing.headers["authorization"] == "Bearer synthetic-token"
            assert outgoing.headers["content-type"] == "application/json"
            assert outgoing.headers["idempotency-key"] == KEY
            assert outgoing.content == json.dumps(dumped, ensure_ascii=False, allow_nan=False,
                                                   separators=(",", ":")).encode("utf-8")
            assert json.loads(outgoing.content) == dumped
            return httpx2.Response(200, json=receipt)

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler),
                                     headers={"Authorization": "Bearer synthetic-token"},
                                     follow_redirects=False, trust_env=False) as remote:
            adapter = ApiClient("http://testserver", remote)
            assert await adapter.invoke(None, "save_document_metadata", ID, request, KEY) == receipt
            assert len(seen) == 1
            seen.clear()
            for args in (("../private-marker", request, KEY), (ID, request, "bad"), (ID, {}, KEY)):
                with pytest.raises(ToolError) as caught:
                    await adapter.invoke(None, "save_document_metadata", *args)
                assert "private-marker" not in str(caught.value)
            assert seen == []

    asyncio.run(scenario())


@pytest.mark.parametrize("failure,code", [("redirect", "upstream_redirect"), ("timeout", "api_unavailable")])
def test_transport_errors_are_sanitized_without_retry(failure, code):
    async def scenario():
        seen = []

        def handler(request):
            seen.append(request)
            if failure == "timeout":
                raise httpx2.ReadTimeout("private-marker", request=request)
            return httpx2.Response(302, headers={"Location": "https://private-marker.invalid"},
                                   content=b"private-marker")

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler),
                                     follow_redirects=False, trust_env=False) as remote:
            with pytest.raises(ToolError) as caught:
                await ApiClient("http://testserver", remote).invoke(None, "get_document_metadata", ID)
            assert str(caught.value).startswith(code + ":")
            assert "private-marker" not in str(caught.value)
            assert len(seen) == 1

    asyncio.run(scenario())


def test_fresh_stdio_import_is_pure():
    script = (
        "import sys; import app.mcp_stdio; "
        "forbidden = ('app.application', 'app.config', 'app.main', 'app.services'); "
        "assert not [n for n in sys.modules if any(n == p or n.startswith(p + '.') for p in forbidden)]"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            timeout=30, check=True)
    assert result.stdout == ""
