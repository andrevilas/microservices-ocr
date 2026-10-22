"""Canonical HTTP reads through the real shared document runtime."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os

import pytest

from app.application import ApplicationError
from app.document_canonical_models import (
    CanonicalManifest, CanonicalPageChunk, CanonicalPageText,
)
from app.security import require_principal
from test_document_http_basic import http
from test_document_metadata import env, PAGES, QUOTE, who

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")
SCOPES = {"jobs:read", "artifacts:read"}
VIEWS = {
    "manifest": ("/manifest", CanonicalManifest),
    "chunk": ("/pages", CanonicalPageChunk),
    "page": ("/pages/1", CanonicalPageText),
}


def get(http, suffix):
    e, app, client, services = http
    response = client.get(f"/api/v1/jobs/{e.job.job_id}/canonical" + suffix)
    assert response.headers["cache-control"] == "no-store"
    assert len(response.headers["x-request-id"]) == 32
    assert not e.jobs._artifact_leases
    return response


def query(http, view, principal=None):
    e, app, client, services = http
    principal = e.owner if principal is None else principal

    async def run():
        kwargs = {"page": 1} if view == "page" else {}
        return await services.execution.run(
            principal, lambda control: getattr(
                services.queries, "get_canonical_" + view)(
                    principal, e.job.job_id, checkpoint=control.checkpoint, **kwargs))

    return client.portal.call(run)


@pytest.mark.parametrize("view", VIEWS)
def test_success_values_and_openapi_with_only_canonical_scopes(http, view):
    e, app, client, services = http
    principal = who(scopes=SCOPES)
    app.dependency_overrides[require_principal] = lambda: principal
    suffix, model = VIEWS[view]
    expected = query(http, view, principal)
    response = get(http, suffix)
    assert response.status_code == 200, response.text
    dto = model.model_validate_json(response.content)
    assert dto.model_dump(mode="json") == expected.model_dump(mode="json")
    manifest = query(http, "manifest", principal)
    assert dto.document_id == e.job.job_id
    if view == "manifest":
        assert dto.page_count == len(PAGES)
        assert dto.total_characters == sum(map(len, PAGES))
    else:
        assert dto.canonical_sha256 == manifest.sha256
        assert dto.text_sha256 == hashlib.sha256(PAGES[0].encode("utf-8")).hexdigest()
        if view == "page":
            assert dto.text == PAGES[0] and QUOTE in dto.text
            assert len(dto.text) > 20000
        else:
            assert dto.text == PAGES[0][:8000]
            assert (dto.offset, dto.end_offset) == (0, 8000)
            assert dto.truncated and (dto.next_page, dto.next_offset) == (1, 8000)
    template = "/pages/{page}" if view == "page" else suffix
    spec = client.get("/openapi.json").json()["paths"][
        "/api/v1/jobs/{job_id}/canonical" + template]["get"]
    assert spec["operationId"] == "ocr_get_document_canonical_" + view
    assert spec["x-required-scopes"] == ["jobs:read", "artifacts:read"]
    model.model_validate_json(json.dumps(
        spec["responses"]["200"]["content"]["application/json"]["example"]))
    assert not e.jobs._artifact_leases


def test_chunk_continuation_bounds_and_unicode(http):
    first = CanonicalPageChunk.model_validate_json(get(http, "/pages").content)
    next_chunk = CanonicalPageChunk.model_validate_json(get(
        http, f"/pages?page={first.next_page}&offset={first.next_offset}&max_chars=20000").content)
    assert next_chunk.text == PAGES[0][8000:]
    assert len(next_chunk.text) <= 20000 and not next_chunk.truncated
    assert (next_chunk.next_page, next_chunk.next_offset) == (2, 0)
    unicode_chunk = CanonicalPageChunk.model_validate_json(get(
        http, f"/pages?offset=21000&max_chars={len(QUOTE)}").content)
    assert unicode_chunk.text == QUOTE
    assert unicode_chunk.end_offset == 21000 + len(QUOTE)
    final = CanonicalPageChunk.model_validate_json(get(http, "/pages?page=2").content)
    assert final.text == PAGES[1]
    assert final.next_page is None and final.next_offset is None
    assert first.canonical_sha256 == next_chunk.canonical_sha256 == final.canonical_sha256


@pytest.mark.parametrize("missing", sorted(SCOPES))
def test_scope_failure_precedes_provider(http, missing):
    e, app, client, services = http
    app.dependency_overrides[require_principal] = lambda: who(scopes=SCOPES - {missing})

    def forbidden():
        pytest.fail("canonical provider resolved before scope validation")

    app.state.document_provider = forbidden
    for suffix, model in VIEWS.values():
        response = get(http, suffix)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "insufficient_scope"


@pytest.mark.parametrize("view", VIEWS)
@pytest.mark.parametrize("case", ["owner", "expired"])
def test_authority_errors_match_queries(http, view, case):
    e, app, client, services = http
    principal = who("user:2", scopes=SCOPES) if case == "owner" else e.owner
    app.dependency_overrides[require_principal] = lambda: principal
    if case == "expired":
        with e.jobs._lock:
            e.job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            e.jobs._persist(e.job)
    with pytest.raises(ApplicationError) as caught:
        query(http, view, principal)
    response = get(http, VIEWS[view][0])
    assert response.status_code == caught.value.status == (404 if case == "owner" else 410)
    assert response.json()["error"]["code"] == caught.value.code


@pytest.mark.parametrize("suffix", [
    "/pages?page=0", "/pages?page=301", "/pages?page=bad",
    "/pages?offset=-1", "/pages?offset=500001",
    "/pages?max_chars=0", "/pages?max_chars=20001",
    "/pages/0", "/pages/301", "/pages/bad",
])
def test_invalid_ranges(http, suffix):
    response = get(http, suffix)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_busy_execution_releases_permit(http):
    e, app, client, services = http
    permit = services.execution.acquire(e.owner)
    try:
        for suffix, model in VIEWS.values():
            assert get(http, suffix).status_code == 429
    finally:
        permit.close()
    for suffix, model in VIEWS.values():
        assert get(http, suffix).status_code == 200
