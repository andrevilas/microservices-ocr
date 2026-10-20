"""Fields and pinned schema HTTP reads through the shared runtime."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.application import ApplicationError
from app.document_content_models import MetadataSchemaChunk
from app.document_transport_models import MetadataFieldPage
from app.security import require_principal
from test_document_http_basic import http
from test_document_metadata import env, persisted, save, who

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")


@pytest.fixture
def document(http):
    e, app, client, services = http
    receipt, _ = save(e)
    return e, app, client, services, receipt, persisted(e, receipt)


def target(document, view):
    e, _, _, _, receipt, stored = document
    root = f"/api/v1/jobs/{e.job.job_id}/metadata/revisions/{receipt.revision_id}"
    kwargs = dict(revision_id=receipt.revision_id, sha256=receipt.sha256)
    if view == "fields":
        kwargs["segment_id"] = stored.segments[0].segment_id
        return (root + f"/segments/{kwargs['segment_id']}/fields?sha256={receipt.sha256}",
                "list_fields", MetadataFieldPage, kwargs)
    kwargs["schema_sha256"] = stored.schemas[0].schema_sha256
    return (root + f"/schemas/{kwargs['schema_sha256']}?sha256={receipt.sha256}",
            "get_schema", MetadataSchemaChunk, kwargs)


def query(document, principal, method, kwargs):
    e, _, client, services, _, _ = document
    async def run():
        return await services.execution.run(principal, lambda control: getattr(
            services.queries, method)(principal, e.job.job_id,
                                     checkpoint=control.checkpoint, **kwargs))
    return client.portal.call(run)


@pytest.mark.parametrize("view", ["fields", "schema"])
def test_success_and_openapi(document, view):
    e, _, client, _, receipt, stored = document
    url, method, model, kwargs = target(document, view)
    response = client.get(url)
    assert response.status_code == 200, response.text
    dto = model.model_validate_json(response.content)
    assert dto.model_dump(mode="json") == query(
        document, e.owner, method, kwargs).model_dump(mode="json")
    assert response.headers["cache-control"] == "no-store"
    assert len(response.headers["x-request-id"]) == 32
    path = "/api/v1/jobs/{job_id}/metadata/revisions/{revision_id}/" + (
        "segments/{segment_id}/fields" if view == "fields" else "schemas/{schema_sha256}")
    spec = client.get("/openapi.json").json()["paths"][path]["get"]
    assert spec["operationId"] == ("ocr_list_document_metadata_fields" if view == "fields"
                                   else "ocr_get_document_metadata_schema")
    assert spec["x-required-scopes"] == ["jobs:read", "artifacts:read", "metadata:read"]
    model.model_validate_json(json.dumps(
        spec["responses"]["200"]["content"]["application/json"]["example"]))
    if view == "fields":
        assert [field.field_name for field in dto.fields] == ["title"]
        assert dto.next_cursor is None
        assert client.get(url + "&limit=1").json() == response.json()
        assert client.get(url + "&limit=50").json() == response.json()
    else:
        expected = json.dumps(stored.schemas[0].schema_document, sort_keys=True,
                              separators=(",", ":"), ensure_ascii=False)
        text, offset = "", 0
        while True:
            response = client.get(url + f"&offset={offset}&max_chars=7")
            assert response.status_code == 200, response.text
            chunk = model.model_validate_json(response.content)
            assert chunk.offset == offset and chunk.end == offset + len(chunk.text)
            assert chunk.text == expected[offset:chunk.end]
            assert chunk.total_characters == len(expected)
            text += chunk.text
            if chunk.next_offset is None:
                break
            assert chunk.next_offset == chunk.end > offset
            offset = chunk.next_offset
        assert text == expected
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == stored.schemas[0].schema_sha256
        assert chunk.canonical_utf8_bytes == len(text.encode("utf-8"))
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("view", ["fields", "schema"])
@pytest.mark.parametrize("case", ["owner", "ttl", "missing", "hash"])
def test_authority_errors_match_query(document, view, case):
    e, app, client, _, receipt, _ = document
    url, method, _, kwargs = target(document, view)
    principal = who("user:2") if case == "owner" else e.owner
    if case == "ttl":
        with e.jobs._lock:
            e.job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            e.jobs._persist(e.job)
    if case == "missing":
        kwargs["revision_id"] = "0" * 32 if receipt.revision_id != "0" * 32 else "1" * 32
        url = url.replace(receipt.revision_id, kwargs["revision_id"])
    if case == "hash":
        kwargs["sha256"] = ("0" if receipt.sha256[0] != "0" else "1") + receipt.sha256[1:]
        url = url.replace("?sha256=" + receipt.sha256, "?sha256=" + kwargs["sha256"])
    app.dependency_overrides[require_principal] = lambda: principal
    with pytest.raises(ApplicationError) as caught:
        query(document, principal, method, kwargs)
    response = client.get(url)
    assert response.status_code == caught.value.status
    assert response.json()["error"]["code"] == caught.value.code
    assert response.json()["error"]["message"] == caught.value.message
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("view", ["fields", "schema"])
def test_invalid_parameters(document, view):
    e, _, client, _, receipt, _ = document
    url, _, _, _ = target(document, view)
    urls = [url.replace(e.job.job_id, "BAD"), url.replace(receipt.revision_id, "BAD"),
            url.split("?")[0], url.split("?")[0] + "?sha256=BAD"]
    if view == "fields":
        urls += [url.replace("/segments/", "/segments/BAD", 1)]
        urls += [url + suffix for suffix in ("&limit=0", "&limit=51", "&after=!",
                                             "&after=" + "a" * 2049)]
    else:
        urls += [url.split("/schemas/")[0] + "/schemas/" + "A" * 64 + url[url.index("?"):]]
        urls += [url + suffix for suffix in ("&offset=-1", "&offset=262145",
                                             "&max_chars=0", "&max_chars=1001")]
    for invalid in urls:
        response = client.get(invalid)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["details"] == []
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("missing", ["jobs:read", "artifacts:read", "metadata:read"])
def test_scope_before_provider(document, missing):
    _, app, client, _, _, _ = document
    app.dependency_overrides[require_principal] = lambda: who(
        scopes={"jobs:read", "artifacts:read", "metadata:read"} - {missing})
    def forbidden():
        pytest.fail("provider resolved before required scopes")
    app.state.document_provider = forbidden
    for view in ("fields", "schema"):
        response = client.get(target(document, view)[0])
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "insufficient_scope"


def test_busy_and_release(document):
    e, _, client, services, _, _ = document
    permit = services.execution.acquire(e.owner)
    try:
        for view in ("fields", "schema"):
            assert client.get(target(document, view)[0]).status_code == 429
    finally:
        permit.close()
    for view in ("fields", "schema"):
        assert client.get(target(document, view)[0]).status_code == 200
    assert not e.jobs._artifact_leases
