"""Real HTTP metadata content reads through the shared execution runtime."""
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from app.application import ApplicationError
from app.document_content_models import (
    MetadataEvidencePage, MetadataStringChunk, MetadataValuePage,
)
from app.security import require_principal
from test_document_http_basic import http
from test_document_metadata import env, persisted, save, who

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")
SCOPES = ["jobs:read", "artifacts:read", "metadata:read"]
VIEWS = [
    ("/values", "list_values", MetadataValuePage, "ocr_list_document_metadata_values", {}),
    ("/evidence", "list_evidence", MetadataEvidencePage, "ocr_list_document_metadata_evidence", {}),
    ("/strings/raw", "get_string", MetadataStringChunk, "ocr_get_document_metadata_string", {"part": "raw"}),
    ("/strings/normalized", "get_string", MetadataStringChunk, "ocr_get_document_metadata_string", {"part": "normalized"}),
]


@pytest.fixture
def data(http):
    e, app, client, services = http
    receipt, _ = save(e)
    snapshot = persisted(e, receipt)
    segment = snapshot.segments[0]
    field = segment.fields[0]
    url = (f"/api/v1/jobs/{snapshot.job_id}/metadata/revisions/{snapshot.revision_id}"
           f"/segments/{segment.segment_id}/fields/{field.field_name}")
    return SimpleNamespace(e=e, app=app, client=client, services=services,
                           snapshot=snapshot, segment=segment, field=field,
                           sha256=receipt.sha256, url=url)


def direct(d, method, principal=None, **kwargs):
    principal = d.e.owner if principal is None else principal
    arguments = dict(sha256=d.sha256, **kwargs)
    async def run():
        return await d.services.execution.run(
            principal, lambda control: getattr(d.services.queries, method)(
                principal, d.snapshot.job_id, d.snapshot.revision_id,
                d.segment.segment_id, d.field.field_name,
                checkpoint=control.checkpoint, **arguments))
    return d.client.portal.call(run)


def fetch(d, suffix, params=None, url=None):
    response = d.client.get((d.url if url is None else url) + suffix,
                            params={"sha256": d.sha256, **(params or {})})
    assert response.headers["cache-control"] == "no-store"
    assert len(response.headers["x-request-id"]) == 32
    assert not d.e.jobs._artifact_leases
    return response


@pytest.mark.parametrize("suffix,method,model,operation_id,kwargs", VIEWS)
def test_real_content_and_openapi(data, suffix, method, model, operation_id, kwargs):
    d = data
    response = fetch(d, suffix)
    assert response.status_code == 200, response.text
    dto = model.model_validate_json(response.content)
    assert dto.model_dump(mode="json") == direct(d, method, **kwargs).model_dump(mode="json")
    template = ("/api/v1/jobs/{job_id}/metadata/revisions/{revision_id}"
                "/segments/{segment_id}/fields/{field_name}")
    template += "/strings/{part}" if method == "get_string" else suffix
    spec = d.client.get("/openapi.json").json()["paths"][template]["get"]
    assert spec["operationId"] == operation_id
    assert spec["x-required-scopes"] == SCOPES
    model.model_validate_json(json.dumps(spec["responses"]["200"]["content"]["application/json"]["example"]))
    if method == "list_evidence":
        assert dto.evidence == d.field.evidence
        assert dto.evidence[0].quote == d.e.evidence["quote"]
        assert dto.evidence[0].text_sha256 == d.e.evidence["text_sha256"]
    assert not d.e.jobs._artifact_leases


@pytest.mark.parametrize("part", ["raw", "normalized"])
def test_unicode_string_reassembly(data, part):
    d, offset, pieces = data, 0, []
    source = getattr(d.field, part)
    while True:
        response = fetch(d, "/strings/" + part, {"offset": offset, "max_chars": 3})
        assert response.status_code == 200, response.text
        chunk = MetadataStringChunk.model_validate_json(response.content)
        assert chunk.offset == offset and chunk.end == min(offset + 3, len(source))
        assert chunk.text == source[offset:chunk.end]
        assert chunk.total_characters == len(source)
        assert chunk.utf8_bytes == len(source.encode("utf-8"))
        assert chunk.content_sha256 == hashlib.sha256(source.encode("utf-8")).hexdigest()
        assert chunk.item_index is None and "item_index" in response.json()
        pieces.append(chunk.text)
        if chunk.next_offset is None:
            break
        assert chunk.next_offset == chunk.end > offset
        offset = chunk.next_offset
    assert "".join(pieces) == source
    assert d.field.raw == d.e.evidence["quote"]


@pytest.mark.parametrize("missing", SCOPES)
def test_scopes_precede_provider(data, missing):
    d = data
    d.app.dependency_overrides[require_principal] = lambda: who(scopes=set(SCOPES) - {missing})
    def forbidden():
        pytest.fail("provider resolved before required scope check")
    d.app.state.document_provider = forbidden
    for suffix, *_ in VIEWS[:3]:
        response = fetch(d, suffix)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "insufficient_scope"


@pytest.mark.parametrize("case", ["owner", "sha256"])
@pytest.mark.parametrize("suffix,method,model,operation_id,kwargs", VIEWS[:3])
def test_safe_authority_errors(data, case, suffix, method, model, operation_id, kwargs):
    d = data
    principal = who("user:2") if case == "owner" else d.e.owner
    if case == "sha256":
        d.sha256 = ("0" if d.sha256[0] != "0" else "1") + d.sha256[1:]
    d.app.dependency_overrides[require_principal] = lambda: principal
    with pytest.raises(ApplicationError) as caught:
        direct(d, method, principal=principal, **kwargs)
    response = fetch(d, suffix)
    assert response.status_code == caught.value.status
    assert response.json()["error"]["code"] == caught.value.code
    assert response.json()["error"]["message"] == "Document metadata query failed."


@pytest.mark.parametrize("suffix,params,bad_field", [
    ("/values", {}, True), ("/strings/other", {}, False),
    *[("/strings/raw", {"item_index": n}, False) for n in (-1, 100)],
    *[("/strings/raw", {"offset": n}, False) for n in (-1, 262145)],
    *[("/strings/raw", {"max_chars": n}, False) for n in (0, 1001)],
    *[(suffix, {"limit": n}, False) for suffix in ("/values", "/evidence") for n in (0, 51)],
    *[(suffix, {"after": cursor}, False) for suffix in ("/values", "/evidence")
      for cursor in ("bad", "a" * 2049)],
    ("/values", {"sha256": "bad"}, False),
])
def test_invalid_parameters(data, suffix, params, bad_field):
    d = data
    url = d.url.rsplit("/", 1)[0] + "/BAD" if bad_field else d.url
    assert fetch(d, suffix, params, url).status_code == 422


def test_busy_permit_released(data):
    d = data
    permit = d.services.execution.acquire(d.e.owner)
    try:
        for suffix, *_ in VIEWS[:3]:
            assert fetch(d, suffix).status_code == 429
    finally:
        permit.close()
    for suffix, *_ in VIEWS[:3]:
        assert fetch(d, suffix).status_code == 200
    assert not d.e.jobs._artifact_leases
