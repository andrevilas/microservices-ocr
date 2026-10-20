"""Synthetic HTTP coverage using real metadata approval and shared execution."""
import copy
import json

import pytest

from app.metadata_models import ApprovalInput, ApproveMetadataReceipt, snapshot_bytes
from app.security import require_principal
from test_document_http_basic import base, http, pytestmark
from test_document_metadata import env, persisted, who
from test_document_metadata_approval import (
    APPROVAL_KEY, APPROVAL_SCOPES, deprecate, later_draft, ready,
)


def post(client, e, *, key=APPROVAL_KEY, body=None):
    return client.post(base(e) + "/approve", json=e.approval_body if body is None else body,
                       headers={"Idempotency-Key": key})


def authorize(app, e):
    assert e.approver.scopes == frozenset(APPROVAL_SCOPES)
    assert not ({"metadata:write", "contexts:read"} & e.approver.scopes)
    app.dependency_overrides[require_principal] = lambda: e.approver


def test_create_and_historical_replay(http, ready):
    e, app, client, services = http
    assert e is ready
    authorize(app, e)
    first = post(client, e)
    assert first.status_code == 201, first.text
    assert first.headers["Idempotency-Replayed"] == "false"
    assert first.headers["Cache-Control"] == "no-store"
    receipt = ApproveMetadataReceipt.model_validate_json(first.content)
    assert receipt.operation == "approve-v1" and receipt.state == "approved"
    assert receipt.reviewed_revision_id == e.draft.revision_id
    assert receipt.reviewed_sha256 == e.draft.sha256
    assert receipt.revision_id != e.draft.revision_id
    approved = persisted(e, receipt)
    assert approved.state == "approved"
    assert approved.parent_revision_id == e.draft.revision_id
    assert snapshot_bytes(persisted(e, e.draft)) == e.original_bytes
    for advanced in (False, True):
        if advanced:
            later_draft(e, receipt)
            deprecate(e)
        replay = post(client, e)
        assert replay.status_code == 200, replay.text
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert replay.json() == first.json()
    assert snapshot_bytes(persisted(e, e.draft)) == e.original_bytes
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("principal", [
    who("user:2", scopes=APPROVAL_SCOPES),
    who("user:2", scopes=APPROVAL_SCOPES, admin=True),
    who("service:" + "f" * 32, scopes=APPROVAL_SCOPES, admin=True),
])
def test_owner_and_service_policy(http, ready, principal):
    e, app, client, services = http
    app.dependency_overrides[require_principal] = lambda: principal
    response = post(client, e)
    assert response.status_code in {403, 404}
    assert snapshot_bytes(persisted(e, e.draft)) == e.original_bytes
    assert not e.jobs._artifact_leases


def test_missing_scope_precedes_provider_and_body(http, ready):
    e, app, client, services = http
    app.dependency_overrides[require_principal] = lambda: who(
        scopes=APPROVAL_SCOPES - {"metadata:approve"}, admin=True)

    def forbidden():
        pytest.fail("provider resolved before approval scope check")

    app.state.document_provider = forbidden
    response = client.post(base(e) + "/approve", content=b"invalid JSON",
                           headers={"Idempotency-Key": APPROVAL_KEY,
                                    "Content-Type": "text/plain"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"


def test_stale_head_with_new_key_conflicts(http, ready):
    e, app, client, services = http
    authorize(app, e)
    assert post(client, e).status_code == 201
    response = post(client, e, key="document-http-approve-stale-0001")
    assert response.status_code == 409
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("case,status", [
    ("large", 413), ("media", 415), ("json", 422), ("extra", 422),
])
def test_invalid_body_is_safe(http, ready, case, status):
    e, app, client, services = http
    authorize(app, e)
    marker = "synthetic-private-input-marker"
    headers = {"Idempotency-Key": APPROVAL_KEY, "Content-Type": "application/json"}
    if case == "large":
        content = b" " * (512 * 1024 + 1)
    elif case == "media":
        headers["Content-Type"] = "text/plain"
        content = marker.encode()
    elif case == "json":
        content = ('{"' + marker + '":').encode()
    else:
        body = copy.deepcopy(e.approval_body)
        body["actor_subject"] = marker
        content = json.dumps(body).encode()
    response = client.post(base(e) + "/approve", content=content, headers=headers)
    assert response.status_code == status, response.text
    assert marker not in response.text
    assert response.json()["error"]["details"] == []
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("key", ["short", "a" * 129])
def test_invalid_key_length(http, ready, key):
    e, app, client, services = http
    authorize(app, e)
    assert post(client, e, key=key).status_code == 422


def test_busy_execution_releases_permit(http, ready):
    e, app, client, services = http
    authorize(app, e)
    permit = services.execution.acquire(e.approver)
    try:
        assert post(client, e).status_code == 429
    finally:
        permit.close()
    assert post(client, e).status_code == 201
    assert not e.jobs._artifact_leases


def test_openapi_contract(http, ready):
    e, app, client, services = http
    paths = client.get("/openapi.json").json()["paths"]
    assert sum(method in {"get", "put", "post", "delete", "patch"}
               for path in paths.values() for method in path) == 15
    spec = paths["/api/v1/jobs/{job_id}/metadata/approve"]["post"]
    assert spec["operationId"] == "ocr_approve_document_metadata"
    assert spec["x-required-scopes"] == [
        "jobs:read", "artifacts:read", "metadata:read", "metadata:approve"]
    request_schema = spec["requestBody"]["content"]["application/json"]["schema"]
    ApprovalInput.model_validate_json(json.dumps(request_schema["examples"][0]))
    header = next(p for p in spec["parameters"] if p["name"].lower() == "idempotency-key")
    assert header["required"] and header["in"] == "header"
    assert header["schema"]["minLength"] == 8
    assert header["schema"]["maxLength"] == 128
    for status in ("200", "201"):
        response = spec["responses"][status]
        ApproveMetadataReceipt.model_validate_json(json.dumps(
            response["content"]["application/json"]["example"]))
        assert response["headers"]["Idempotency-Replayed"]["schema"]["enum"] == ["true", "false"]
