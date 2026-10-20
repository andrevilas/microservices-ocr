"""HTTP manual saves through the real shared document runtime."""
from copy import deepcopy

import pytest

from app.metadata_models import SaveMetadataInput, SaveMetadataReceipt
from app.routes.documents import router
from app.security import require_principal
from test_document_http_basic import base, http
from test_document_metadata import BASE, KEY, body, env, idle, persisted, pytestmark, who


def put(client, e, request, key=KEY):
    return client.put(base(e), json=request, headers={"Idempotency-Key": key})


def test_created_replay_and_persisted_receipt(http):
    e, app, client, services = http
    request = body(e)
    first = put(client, e, request)
    assert first.status_code == 201, first.text
    assert first.headers["idempotency-replayed"] == "false"
    receipt = SaveMetadataReceipt.model_validate(first.json(), strict=True)
    assert persisted(e, receipt).state == "draft"
    replay = put(client, e, request)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert replay.headers["idempotency-replayed"] == "true"
    for response in (first, replay):
        assert response.headers["cache-control"] == "no-store"
        assert len(response.headers["x-request-id"]) == 32
    idle(e, 1)


@pytest.mark.parametrize("case", ["changed_body", "stale_head"])
def test_conflicts_do_not_create_revision(http, case):
    e, app, client, services = http
    request = body(e)
    assert put(client, e, request).status_code == 201
    key = KEY
    if case == "changed_body":
        request["segments"][0]["fields"][0]["raw"] = "Changed title"
    else:
        key = "document-http-stale-0001"
    response = put(client, e, request, key)
    assert response.status_code == 409, response.text
    idle(e, 1)


def test_catalog_scope_is_conditional_and_replay_can_be_historical(http):
    e, app, client, services = http
    request = body(e)
    app.dependency_overrides[require_principal] = lambda: who(scopes=BASE)
    denied = put(client, e, request)
    assert denied.status_code == 403
    idle(e, 0)
    app.dependency_overrides[require_principal] = lambda: e.owner
    first = put(client, e, request)
    assert first.status_code == 201, first.text
    receipt = SaveMetadataReceipt.model_validate(first.json(), strict=True)
    app.dependency_overrides[require_principal] = lambda: who(scopes=BASE)
    updated = put(client, e, body(e, receipt), "document-http-update-0001")
    assert updated.status_code == 201, updated.text
    assert updated.json()["segment_ids"] == receipt.segment_ids
    replay = put(client, e, request)
    assert replay.status_code == 200, replay.text
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json() == first.json()
    idle(e, 2)


@pytest.mark.parametrize("missing", sorted(BASE))
def test_scope_precedes_provider_and_body(http, missing):
    e, app, client, services = http
    app.dependency_overrides[require_principal] = lambda: who(scopes=BASE - {missing})

    def forbidden():
        pytest.fail("provider resolved before scope authorization")

    app.state.document_provider = forbidden
    response = client.put(base(e), content=b"PRIVATE malformed JSON", headers={
        "Idempotency-Key": KEY, "Content-Type": "application/json"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"
    assert "PRIVATE" not in response.text
    idle(e, 0)


def test_wrong_owner(http):
    e, app, client, services = http
    app.dependency_overrides[require_principal] = lambda: who("user:2")
    response = put(client, e, body(e))
    assert response.status_code == 404, response.text
    idle(e, 0)


@pytest.mark.parametrize("case,status,code", [
    ("oversize", 413, "request_too_large"),
    ("malformed", 422, "validation_error"),
    ("media", 415, "unsupported_media_type"),
])
def test_bounded_json_errors(http, case, status, code):
    e, app, client, services = http

    def chunks():
        yield b" " * (256 * 1024)
        yield b" " * (256 * 1024)
        yield b"PRIVATE"

    content = chunks() if case == "oversize" else b'{"PRIVATE":'
    response = client.put(base(e), content=content, headers={
        "Idempotency-Key": KEY,
        "Content-Type": "text/plain" if case == "media" else "application/json"})
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["details"] == []
    assert "PRIVATE" not in response.text
    idle(e, 0)


@pytest.mark.parametrize("key", [None, "short", "x" * 129])
def test_required_bounded_idempotency_header(http, key):
    e, app, client, services = http
    headers = {} if key is None else {"Idempotency-Key": key}
    response = client.put(base(e), json=body(e), headers=headers)
    assert response.status_code == 422
    idle(e, 0)


def test_busy_execution_releases_permit(http):
    e, app, client, services = http
    permit = services.execution.acquire(e.owner)
    try:
        assert put(client, e, body(e)).status_code == 429
        idle(e, 0)
    finally:
        permit.close()
    assert put(client, e, body(e)).status_code == 201
    idle(e, 1)


def test_openapi_preserves_null_examples_and_is_detached(http):
    e, app, client, services = http
    path = "/api/v1/jobs/{job_id}/metadata"
    route = next(route for route in router.routes
                 if getattr(route, "path", None) == path and "PUT" in route.methods)
    declared = deepcopy(route.openapi_extra)
    expected = client.get("/openapi.json").json()
    spec = expected["paths"][path]["put"]
    assert spec["operationId"] == "ocr_save_document_metadata"
    assert spec["x-required-scopes"] == [
        "jobs:read", "artifacts:read", "metadata:read", "metadata:write"]
    schema = spec["requestBody"]["content"]["application/json"]["schema"]
    assert {"expected_head", "expected_sha256"} <= set(schema["required"])
    sample = schema["examples"][0]
    assert sample["expected_head"] is None and sample["expected_sha256"] is None
    SaveMetadataInput.model_validate(sample, strict=True)
    for status in ("200", "201"):
        response = spec["responses"][status]
        SaveMetadataReceipt.model_validate(
            response["content"]["application/json"]["example"], strict=True)
        assert response["headers"]["Idempotency-Replayed"]["schema"]["enum"] == ["true", "false"]
    rendered = app.openapi()["paths"][path]["put"]["requestBody"]["content"]["application/json"]
    rendered["schema"]["examples"][0]["expected_head"] = "a" * 32
    assert route.openapi_extra == declared
    assert client.get("/openapi.json").json() == expected
    assert client.get("/openapi.json").json() == expected
