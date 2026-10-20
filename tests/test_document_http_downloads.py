"""Real HTTP downloads through the shared document streaming runtime."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from app.application import ApplicationError
from app.document_http_examples import canonical_jsonl_example
from app.metadata_models import MetadataSnapshot, snapshot_bytes
from app.security import require_principal
from test_document_http_basic import http
from test_document_metadata import PAGES, QUOTE, env, persisted, save, who

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")


def url(e, kind, receipt=None, digest=None):
    root = f"/api/v1/jobs/{e.job.job_id}"
    if kind == "canonical":
        return root + "/canonical/pages.jsonl"
    revision = receipt.revision_id if receipt else "a" * 32
    digest = digest or (receipt.sha256 if receipt else "b" * 64)
    return root + f"/metadata/revisions/{revision}/content?sha256={digest}"


@pytest.mark.parametrize("kind", ["metadata", "canonical"])
def test_exact_download_and_openapi(http, kind):
    e, app, client, services = http
    receipt, _ = save(e)
    if kind == "metadata":
        expected = snapshot_bytes(persisted(e, receipt))
        media, filename = "application/json", "metadata.json"
        template = "/metadata/revisions/{revision_id}/content"
        operation_id = "ocr_download_document_metadata"
        scopes = ["jobs:read", "artifacts:read", "metadata:read"]
    else:
        with services.streams.canonical_service.open(e.owner, e.job.job_id) as prepared:
            expected = b"".join(prepared.iter_jsonl())
        media, filename = "application/x-ndjson", "pages.jsonl"
        template = "/canonical/pages.jsonl"
        operation_id = "ocr_download_document_canonical"
        scopes = ["jobs:read", "artifacts:read"]
    response = client.get(url(e, kind, receipt))
    assert response.status_code == 200, response.text
    assert response.content == expected
    assert response.headers["content-type"] == media
    assert response.headers["content-disposition"] == f'attachment; filename="{filename}"'
    assert response.headers["content-length"] == str(len(expected))
    assert response.headers["x-content-sha256"] == hashlib.sha256(expected).hexdigest()
    assert response.headers["cache-control"] == "no-store"
    assert len(response.headers["x-request-id"]) == 32
    if kind == "metadata":
        assert MetadataSnapshot.model_validate_json(response.content).revision_id == receipt.revision_id
        assert len(expected) <= 1048576
    else:
        assert [json.loads(line)["text"] for line in response.content.splitlines()] == PAGES
    paths = client.get("/openapi.json").json()["paths"]
    spec = paths["/api/v1/jobs/{job_id}" + template]["get"]
    assert spec["operationId"] == operation_id
    assert sum(op.get("operationId") == operation_id for path in paths.values()
               for op in path.values() if isinstance(op, dict)) == 1
    assert spec["x-required-scopes"] == scopes
    success = spec["responses"]["200"]
    assert set(success["content"]) == {media}
    headers = success["headers"]
    assert {"Content-Length", "X-Content-SHA256", "Content-Disposition", "Cache-Control"} <= headers.keys()
    assert headers["Content-Disposition"]["schema"]["enum"] == [f'attachment; filename="{filename}"']
    assert headers["Cache-Control"]["schema"]["enum"] == ["no-store"]
    example = success["content"][media]["example"]
    if kind == "metadata":
        MetadataSnapshot.model_validate_json(json.dumps(example))
        assert "$ref" in success["content"][media]["schema"]
    else:
        assert success["content"][media]["schema"] == {"type": "string", "format": "binary"}
        assert example == canonical_jsonl_example() and example.endswith("\n")
        assert json.loads(example)["schema_version"] == "ocr-canonical-page/1"
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("kind,missing", [
    ("metadata", "jobs:read"), ("metadata", "artifacts:read"),
    ("metadata", "metadata:read"), ("canonical", "jobs:read"),
    ("canonical", "artifacts:read"),
])
def test_scope_before_provider(http, kind, missing):
    e, app, client, services = http
    scopes = {"jobs:read", "artifacts:read", "metadata:read"} - {missing}
    app.dependency_overrides[require_principal] = lambda: who(scopes=scopes)

    def forbidden():
        pytest.fail("download resolved provider before checking scopes")

    app.state.document_provider = forbidden
    response = client.get(url(e, kind))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"


def test_canonical_needs_only_jobs_and_artifacts(http):
    e, app, client, services = http
    app.dependency_overrides[require_principal] = lambda: who(scopes={"jobs:read", "artifacts:read"})
    response = client.get(url(e, "canonical"))
    assert response.status_code == 200
    assert [json.loads(line)["text"] for line in response.content.splitlines()] == PAGES
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("kind,case", [
    ("metadata", "owner"), ("metadata", "expired"), ("metadata", "hash"),
    ("canonical", "owner"), ("canonical", "expired"),
])
def test_authority_errors_before_headers_match_real_stream(http, kind, case):
    e, app, client, services = http
    receipt, _ = save(e)
    principal = who("user:2") if case == "owner" else e.owner
    digest = receipt.sha256
    if case == "hash":
        digest = ("0" if digest[0] != "0" else "1") + digest[1:]
    if case == "expired":
        with e.jobs._lock:
            e.job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            e.jobs._persist(e.job)
    app.dependency_overrides[require_principal] = lambda: principal

    async def probe():
        context = (services.streams.open_metadata(
            principal, e.job.job_id, receipt.revision_id, sha256=digest)
            if kind == "metadata" else services.streams.open_canonical(principal, e.job.job_id))
        async with context:
            pytest.fail("invalid authority exposed a download header")

    with pytest.raises(ApplicationError) as caught:
        client.portal.call(probe)
    response = client.get(url(e, kind, receipt, digest))
    assert response.status_code == caught.value.status
    assert response.json()["error"]["code"] == caught.value.code
    assert "content-disposition" not in response.headers
    assert "x-content-sha256" not in response.headers
    assert QUOTE not in response.text and str(e.job.working_dir) not in response.text
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("kind", ["metadata", "canonical"])
def test_shared_capacity_busy_then_success(http, kind):
    e, app, client, services = http
    receipt, _ = save(e)
    permit = services.execution.acquire(e.owner)
    try:
        assert client.get(url(e, kind, receipt)).status_code == 429
    finally:
        permit.close()
    assert client.get(url(e, kind, receipt)).status_code == 200
    assert not e.jobs._artifact_leases
