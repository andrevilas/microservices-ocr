"""Direct MCP dispatch integration using the HTTP fixture's shared runtime."""
import asyncio

import pytest

from app.application import ApplicationError
from app.document_mcp_runtime import execute_document
from app.document_mcp_tools import DOCUMENT_OPERATIONS
from app.metadata_models import ApprovalInput, SaveMetadataInput, snapshot_bytes
from test_document_http_basic import http, pytestmark
from test_document_metadata import env, body, persisted, who, QUOTE


READ = {"jobs:read", "artifacts:read"}
APPROVE = READ | {"metadata:read", "metadata:approve"}
ALL = APPROVE | {"metadata:write", "contexts:read"}
CANONICAL = {"get_document_canonical_manifest", "get_document_canonical_chunk"}


def call(http, principal, operation, *args):
    _, app, client, _ = http
    return client.portal.call(
        execute_document, app.state.document_provider, principal, operation, args)


def error(action, status, code=None):
    with pytest.raises(ApplicationError) as caught:
        action()
    assert caught.value.status == status
    if code is not None:
        assert caught.value.code == code
    return caught.value


def draft(http):
    e = http[0]
    request = SaveMetadataInput.model_validate(body(e))
    receipt = call(http, e.owner, "save_document_metadata", e.job.job_id,
                   request, "mcp-document-save-0001")
    return request, receipt


def test_all_twelve_operations_and_historical_replays(http):
    e, _, _, services = http
    request, saved = draft(http)
    job, rid, digest = e.job.job_id, saved.revision_id, saved.sha256
    sid = saved.segment_ids["main"]
    snapshot = persisted(e, saved)
    original_bytes = snapshot_bytes(snapshot)
    current = call(http, e.owner, "get_document_metadata", job)
    assert (current.revision_id, current.sha256) == (rid, digest)
    revisions = call(http, e.owner, "list_document_metadata_revisions", job, 1, None)
    assert [(r.revision_id, r.sha256) for r in revisions.revisions] == [(rid, digest)]
    pinned = call(http, e.owner, "get_document_metadata_revision", job, rid, digest)
    assert pinned == current
    fields = call(http, e.owner, "list_document_metadata_fields",
                  job, rid, sid, digest, 50, None)
    assert '"title"' in fields.model_dump_json()
    values = call(http, e.owner, "list_document_metadata_values",
                  job, rid, sid, "title", digest, 50, None)
    assert QUOTE in values.model_dump_json()
    evidence = call(http, e.owner, "list_document_metadata_evidence",
                    job, rid, sid, "title", digest, 50, None)
    assert e.evidence["text_sha256"] in evidence.model_dump_json()
    assert '"start":21000' in evidence.model_dump_json()
    text = call(http, e.owner, "get_document_metadata_string",
                job, rid, sid, "title", digest, "raw", None, 0, 1000)
    assert QUOTE in text.model_dump_json()
    schema_hash = snapshot.segments[0].schema_sha256
    schema = call(http, e.owner, "get_document_metadata_schema",
                  job, rid, schema_hash, digest, 0, 1000)
    assert schema_hash in schema.model_dump_json()
    canonical_reader = who(scopes=READ)
    manifest = call(http, canonical_reader, "get_document_canonical_manifest", job)
    chunk = call(http, canonical_reader, "get_document_canonical_chunk",
                 job, 1, 21000, 1000)
    assert manifest.job_id == chunk.document_id == job
    assert chunk.canonical_sha256 == manifest.sha256 and chunk.text == QUOTE
    approval = ApprovalInput.model_validate(dict(
        revision_id=rid, expected_head=rid, expected_sha256=digest,
        reviewed_fields=[dict(segment_id=sid, field_name="title")]))
    approver = who(scopes=APPROVE)
    approved = call(http, approver, "approve_document_metadata",
                    job, approval, "mcp-document-approve-0001")
    assert approved.revision_id != rid
    approved_snapshot = persisted(e, approved)
    assert approved_snapshot.state == "approved"
    assert approved_snapshot.parent_revision_id == rid
    assert approved_snapshot.segments[0].segment_id == sid
    assert call(http, approver, "approve_document_metadata", job, approval,
                "mcp-document-approve-0001") == approved
    assert call(http, e.owner, "save_document_metadata", job, request,
                "mcp-document-save-0001") == saved
    assert call(http, e.owner, "get_document_metadata_revision", job, rid, digest) == pinned
    head = call(http, e.owner, "get_document_metadata", job)
    assert (head.revision_id, head.sha256) == (approved.revision_id, approved.sha256)
    assert snapshot_bytes(persisted(e, saved)) == original_bytes
    assert services.jobs is e.jobs and not e.jobs._artifact_leases


@pytest.mark.parametrize("operation,missing", [
    (op, scope) for op in sorted(DOCUMENT_OPERATIONS)
    for scope in sorted(READ | (set() if op in CANONICAL else {"metadata:read"})
                        | ({"metadata:write"} if op == "save_document_metadata" else set())
                        | ({"metadata:approve"} if op == "approve_document_metadata" else set()))
])
def test_scope_denial_precedes_provider(http, operation, missing):
    _, app, _, _ = http
    calls = []

    def spy():
        calls.append(True)
        pytest.fail("scope-denied dispatch resolved the provider")

    app.state.document_provider = spy
    error(lambda: call(http, who(scopes=ALL - {missing}), operation),
          403, "insufficient_scope")
    assert calls == []


def test_foreign_owner_and_admin_cannot_read_or_approve(http):
    e = http[0]
    _, saved = draft(http)
    approval = ApprovalInput.model_validate(dict(
        revision_id=saved.revision_id, expected_head=saved.revision_id,
        expected_sha256=saved.sha256, reviewed_fields=[dict(
            segment_id=saved.segment_ids["main"], field_name="title")]))
    for principal in (who("user:2", scopes=ALL), who("user:3", scopes=ALL, admin=True)):
        error(lambda: call(http, principal, "get_document_metadata", e.job.job_id), 404)
        error(lambda: call(http, principal, "approve_document_metadata", e.job.job_id,
                           approval, "mcp-foreign-approve-0001"), 404)


def test_bad_cas_and_shared_http_permit(http):
    e, _, client, services = http
    _, saved = draft(http)
    stale = SaveMetadataInput.model_validate(body(e))
    error(lambda: call(http, e.owner, "save_document_metadata", e.job.job_id,
                       stale, "mcp-stale-save-0001"), 409)
    permit = services.execution.acquire(e.owner)
    try:
        error(lambda: call(http, e.owner, "get_document_metadata", e.job.job_id), 429)
    finally:
        permit.close()
    assert client.get(f"/api/v1/jobs/{e.job.job_id}/metadata").status_code == 200
    assert call(http, e.owner, "get_document_metadata", e.job.job_id).revision_id == saved.revision_id
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("max_chars", [1001, 20000, True])
def test_internal_canonical_chunk_cannot_bypass_mcp_bound(http, max_chars):
    e = http[0]
    failure = error(lambda: call(http, who(scopes=READ), "get_document_canonical_chunk",
                                 e.job.job_id, 1, 0, max_chars), 422, "invalid_metadata_query")
    assert failure.message == "Invalid document metadata query."
    assert call(http, who(scopes=READ), "get_document_canonical_chunk",
                e.job.job_id, 1, 0, 1000).text == "x" * 1000


def test_unknown_operation_and_absent_provider(http):
    e = http[0]
    failure = error(lambda: asyncio.run(execute_document(
        None, who(scopes=set()), "get_document_canonical_page", ())), 422, "unknown_operation")
    assert failure.message == "Unknown document operation."
    failure = error(lambda: asyncio.run(execute_document(
        None, e.owner, "get_document_metadata", (e.job.job_id,))), 503, "metadata_unavailable")
    assert failure.message == "Stored metadata is unavailable."
