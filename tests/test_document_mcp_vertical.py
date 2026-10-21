"""Actual SDK/local JWT qualification over HTTP and child-process stdio.

Exercises four transport/mode combinations against the real persisted uvicorn runtime.
This does not qualify six desktop clients or production remote OIDC. All identities,
PDFs and stores are synthetic and temporary; no live providers or external network.
"""
from catalog_tool_expectations import EXPECTED_TOOL_COUNT
import asyncio
import copy
import os

import pytest

from test_mcp_vertical import runtime, connected, call, complete, pdf  # noqa: F401
from test_catalog_mcp_vertical import catalog_accounts, rest, Tools  # noqa: F401

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")
DOCUMENT_TOOLS = frozenset({
    "ocr_get_document_canonical_manifest", "ocr_get_document_canonical_chunk",
    "ocr_get_document_metadata", "ocr_list_document_metadata_revisions",
    "ocr_get_document_metadata_revision", "ocr_list_document_metadata_fields",
    "ocr_list_document_metadata_values", "ocr_list_document_metadata_evidence",
    "ocr_get_document_metadata_string", "ocr_get_document_metadata_schema",
    "ocr_save_document_metadata", "ocr_approve_document_metadata",
})


@pytest.mark.parametrize("transport,mode", [
    ("http", "auto"), ("http", "legacy"),
    ("stdio", "auto"), ("stdio", "legacy"),
])
def test_document_twelve_tools_real_sdk_local_jwt(runtime, catalog_accounts, transport, mode):
    async def scenario():
        async with asyncio.timeout(120):
            async with connected(runtime, transport, mode, catalog_accounts.owner) as client:
                listing = await client.list_tools()
                names = {tool.name for tool in listing.tools}
                assert len(listing.tools) == len(names) == EXPECTED_TOOL_COUNT
                assert DOCUMENT_TOOLS <= names
                assert all(tool.output_schema for tool in listing.tools)
                tools = Tools(client, listing.tools)
                async with rest(runtime, catalog_accounts.owner) as http:
                    data = pdf(2)
                    upload = await call(client, "ocr_prepare_upload", {
                        "filename": "synthetic-document.pdf", "size_bytes": len(data),
                    })
                    response = await http.put(upload["content_url"], content=data,
                                              headers={"Content-Type": "application/pdf"})
                    assert response.status_code == 200
                    submitted = await call(client, "ocr_submit_job", {
                        "upload_id": upload["upload_id"], "idempotency_key": "document-submit-0001",
                        "options": {"processing_mode": "extract_text"},
                    })
                    job_id = submitted["job_id"]
                    await complete(client, job_id)

                context = await tools("ocr_create_context", {
                    "body": {"name": "Synthetic document context", "common_fields": []},
                    "idempotency_key": "document-context-0001",
                })
                draft = await tools("ocr_create_document_type", {
                    "context_id": context["context_id"],
                    "body": {"name": "Synthetic document type", "specific_fields": [
                        {"name": "titulo", "type": "string", "description": "Synthetic title."},
                    ]},
                    "idempotency_key": "document-type-0001",
                })
                published = await tools("ocr_publish_document_type", {
                    "type_id": draft["type_id"],
                    "body": {"expected_revision": 1, "expected_context_revision": 1},
                    "idempotency_key": "document-publish-0001",
                })
                manifest = await tools("ocr_get_document_canonical_manifest", {"job_id": job_id})
                assert manifest["page_count"] == 2
                chunk = await tools("ocr_get_document_canonical_chunk", {
                    "job_id": job_id, "page": 1, "offset": 0, "max_chars": 64,
                })
                assert chunk["canonical_sha256"] == manifest["sha256"]
                assert 0 < len(chunk["text"]) <= 64
                quote = chunk["text"].strip()[:20].strip()
                assert quote
                start = chunk["offset"] + chunk["text"].index(quote)
                body = {
                    "expected_head": None, "expected_sha256": None,
                    "segments": [{
                        "client_key": "main", "page_start": 1, "page_end": 2,
                        "type_id": draft["type_id"], "type_version": published["version"],
                        "fields": [{"field_name": "titulo", "raw": quote, "origin": "manual",
                                    "evidence": [{
                                        "document_id": job_id, "artifact_version": 1,
                                        "text_version": 1, "page": chunk["page"],
                                        "text_sha256": chunk["text_sha256"],
                                        "start": start, "end": start + len(quote), "quote": quote,
                                    }]}],
                    }],
                }
                save_args = {"job_id": job_id, "body": body,
                             "idempotency_key": "document-save-0001"}
                saved = await tools("ocr_save_document_metadata", copy.deepcopy(save_args))
                sid = saved["segment_ids"]["main"]
                pin = {"job_id": job_id, "revision_id": saved["revision_id"], "sha256": saved["sha256"]}
                current = await tools("ocr_get_document_metadata", {"job_id": job_id})
                assert current["revision_id"] == saved["revision_id"]
                assert current["sha256"] == saved["sha256"] and current["state"] == "draft"
                await tools("ocr_list_document_metadata_revisions", {"job_id": job_id, "limit": 1})
                pinned = await tools("ocr_get_document_metadata_revision", pin)
                assert pinned["revision_id"] == saved["revision_id"]
                assert pinned["sha256"] == saved["sha256"]
                await tools("ocr_list_document_metadata_fields", {**pin, "segment_id": sid, "limit": 1})
                field = {**pin, "segment_id": sid, "field_name": "titulo"}
                await tools("ocr_list_document_metadata_values", {**field, "limit": 1})
                await tools("ocr_list_document_metadata_evidence", {**field, "limit": 1})
                for part in ("raw", "normalized"):
                    await tools("ocr_get_document_metadata_string", {
                        **field, "part": part, "item_index": None, "offset": 0, "max_chars": 5,
                    })
                await tools("ocr_get_document_metadata_schema", {
                    **pin, "schema_sha256": published["schema_sha256"], "offset": 0, "max_chars": 64,
                })
                approve_args = {
                    "job_id": job_id, "idempotency_key": "document-approve-0001",
                    "body": {
                        "revision_id": saved["revision_id"], "expected_head": saved["revision_id"],
                        "expected_sha256": saved["sha256"],
                        "reviewed_fields": [{"segment_id": sid, "field_name": "titulo"}],
                    },
                }
                approved = await tools("ocr_approve_document_metadata", copy.deepcopy(approve_args))
                assert approved["state"] == "approved"
                assert approved["revision_id"] != saved["revision_id"]
                assert approved["reviewed_revision_id"] == saved["revision_id"]
                assert approved["reviewed_sha256"] == saved["sha256"]
                assert await tools("ocr_approve_document_metadata", copy.deepcopy(approve_args)) == approved
                assert await tools("ocr_save_document_metadata", copy.deepcopy(save_args)) == saved
                head = await tools("ocr_get_document_metadata", {"job_id": job_id})
                assert head["revision_id"] == approved["revision_id"]
                assert head["sha256"] == approved["sha256"] and head["state"] == "approved"
                assert await tools("ocr_get_document_metadata_revision", pin) == pinned
                async with connected(runtime, transport, mode, catalog_accounts.other) as other:
                    denied = Tools(other, (await other.list_tools()).tools)
                    await denied.denied("ocr_get_document_metadata", {"job_id": job_id}, "not_found")
                    await denied.denied("ocr_approve_document_metadata", approve_args, "not_found")
                async with connected(runtime, transport, mode, runtime.tokens[0]) as service:
                    denied = Tools(service, (await service.list_tools()).tools)
                    await denied.denied("ocr_get_document_metadata", {"job_id": job_id}, "insufficient_scope")
                    await denied.denied("ocr_approve_document_metadata", approve_args, "insufficient_scope")
                assert tools.called & DOCUMENT_TOOLS == DOCUMENT_TOOLS
                assert len(tools.called & DOCUMENT_TOOLS) == 12

    asyncio.run(scenario())
    assert not (runtime.root / "stdio-must-not-create").exists()
