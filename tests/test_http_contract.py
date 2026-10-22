"""Validate actual HTTP exchanges and examples against the published JSON schemas."""
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from app.application import JobApplication, Principal, get_job_application
from app.export_openapi import v1_schema
from app.main import create_app
from app.security import require_principal
from app.services.identity_store import IdentityStore, get_identity_store
from app.services.storage_service import JobStore
from test_document_metadata import PAGES, body, env, persisted, save


@pytest.fixture
def contract(tmp_path):
    jobs = JobStore(root_dir=tmp_path / "jobs")
    identities = IdentityStore(tmp_path / "identities.sqlite3")
    job = jobs.create("example.pdf", b"%PDF-synthetic", owner_user_id=1)
    output = job.working_dir / "result.pdf"
    output.write_bytes(b"%PDF-synthetic-artifact")
    jobs.update(job.job_id, status="completed", output_pdf_path=output)
    pending = jobs.create("pending.pdf", b"synthetic", owner_user_id=1)
    app = create_app(lambda: JobApplication(jobs))
    app.dependency_overrides[require_principal] = lambda: Principal("user:1", 1, frozenset({"jobs:read", "artifacts:read"}), is_admin=True)
    app.dependency_overrides[get_identity_store] = lambda: identities
    client = TestClient(app)
    try:
        yield app, client, job, pending
    finally:
        client.close()
        identities.close()
        jobs._conn.close()


def validate_response(app, response, path, method="get"):
    schema = v1_schema(app)
    spec = schema["paths"][path][method]["responses"][str(response.status_code)]
    content = spec["content"][response.headers["content-type"].split(";")[0]]
    Draft202012Validator({"components": schema["components"], **content["schema"]}).validate(response.json())
    assert response.headers["cache-control"] == "no-store"
    assert len(response.headers["x-request-id"]) == 32
    return response.json()


def test_all_published_examples_and_security(contract):
    app, _, _, _ = contract
    schema = v1_schema(app)
    assert schema["info"]["x-api-version"] == "1.9.0"
    assert list(schema["components"]["securitySchemes"]) == ["PrincipalBearer"]
    operations = []
    for path, methods in schema["paths"].items():
        assert path.startswith("/api/v1/")
        for operation in methods.values():
            operations.append(operation["operationId"])
            assert operation["description"]
            assert operation["security"] == [{"PrincipalBearer": []}]
            assert isinstance(operation["x-required-scopes"], list)
            assert operation["x-human-admin-required"] == path.startswith("/api/v1/service-principals")
            for status, response in operation["responses"].items():
                assert "X-Request-ID" in response["headers"]
                for media, content in response["content"].items():
                    if media in ("application/pdf", "application/zip", "application/x-ndjson"):
                        assert content["schema"]["format"] == "binary"
                        if media != "application/x-ndjson":
                            continue
                    assert "example" in content, (path, status)
                    Draft202012Validator({"components": schema["components"], **content["schema"]}).validate(content["example"])
            if "requestBody" in operation:
                if "application/json" not in operation["requestBody"]["content"]:
                    continue  # Binary exchanges are covered by upload integration tests.
                body = operation["requestBody"]["content"]["application/json"]["schema"]
                model = schema["components"]["schemas"][body["$ref"].rsplit("/", 1)[1]] if "$ref" in body else body
                for example in model["examples"]:
                    Draft202012Validator({"components": schema["components"], **model}).validate(example)
    assert len(operations) == len(set(operations)) == 69


def test_real_success_exchanges_match_openapi(contract):
    app, client, job, _ = contract
    for actual, template in (("/me", "/me"), ("/jobs", "/jobs"), ("/metrics", "/metrics"),
                             (f"/jobs/{job.job_id}", "/jobs/{job_id}"),
                             (f"/jobs/{job.job_id}/artifact", "/jobs/{job_id}/artifact")):
        response = client.get("/api/v1" + actual)
        assert response.status_code == 200
        validate_response(app, response, "/api/v1" + template)
    base = "/api/v1/service-principals"
    response = client.post(base, json={"name": "Contract test"})
    service = validate_response(app, response, base, "post")
    validate_response(app, client.get(base), base)
    path = f"{base}/{service['id']}/credentials"
    response = client.post(path, json={"name": "Reader", "scopes": ["jobs:read"]})
    credential = validate_response(app, response, base + "/{principal_id}/credentials", "post")["credential"]
    validate_response(app, client.get(path), base + "/{principal_id}/credentials")
    response = client.delete(path + "/" + credential["id"])
    validate_response(app, response, base + "/{principal_id}/credentials/{credential_id}", "delete")
    response = client.post(f"{base}/{service['id']}/disable")
    validate_response(app, response, base + "/{principal_id}/disable", "post")


@pytest.mark.parametrize("kind,status,code", [
    ("auth", 401, "authentication_required"), ("scope", 403, "insufficient_scope"),
    ("owner", 404, "job_not_found"), ("state", 409, "artifact_unavailable"),
    ("limit", 422, "validation_error"), ("cursor", 422, "invalid_cursor"),
    ("exception", 500, "internal_error"),
])
def test_domain_errors_match_contract_and_do_not_leak(contract, kind, status, code, caplog):
    app, client, job, pending = contract
    path, template = f"/api/v1/jobs/{job.job_id}", "/api/v1/jobs/{job_id}"
    if kind == "auth":
        app.dependency_overrides.pop(require_principal)
    elif kind == "scope":
        app.dependency_overrides[require_principal] = lambda: Principal("service:limited", None, frozenset())
    elif kind == "owner":
        app.dependency_overrides[require_principal] = lambda: Principal("user:2", 2, frozenset({"jobs:read"}))
    elif kind == "state":
        path, template = f"/api/v1/jobs/{pending.job_id}/artifact/content", "/api/v1/jobs/{job_id}/artifact/content"
    elif kind in ("limit", "cursor"):
        path, template = "/api/v1/jobs?" + ("limit=0" if kind == "limit" else "cursor=invalid"), "/api/v1/jobs"
    elif kind == "exception":
        def broken():
            raise RuntimeError("PRIVATE-SENTINEL credential or internal path")
        app.dependency_overrides[get_job_application] = broken
    response = client.get(path, headers={"X-Request-ID": "PRIVATE-SENTINEL"})
    assert response.status_code == status
    body = validate_response(app, response, template)
    assert body["error"]["code"] == code
    assert body["request_id"] == response.headers["x-request-id"]
    assert "PRIVATE-SENTINEL" not in response.text + caplog.text
    if status == 401:
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("method,path,status,code", [
    ("get", "/api/v1", 404, "route_not_found"),
    ("get", "/api/v1/missing", 404, "route_not_found"),
    ("post", "/api/v1/me", 405, "method_not_allowed"),
    ("delete", "/api/v1/jobs", 405, "method_not_allowed"),
])
def test_routing_errors_are_consistent(contract, method, path, status, code):
    app, client, _, _ = contract
    response = client.request(method, path)
    assert response.status_code == status
    body = validate_response(app, response, "/api/v1/jobs")
    assert body["error"]["code"] == code
    if status == 405:
        assert response.headers["allow"] == ("GET, POST" if path == "/api/v1/jobs" else "GET")


def test_validation_omits_values_and_unknown_field_names(contract):
    app, client, _, _ = contract
    response = client.post("/api/v1/service-principals", json={"name": "Valid", "PRIVATE-SENTINEL": "PRIVATE-VALUE"})
    body = validate_response(app, response, "/api/v1/service-principals", "post")
    assert body["error"]["details"] == [{"location": [], "code": "extra_forbidden"}]
    assert "PRIVATE" not in response.text
    response = client.post("/api/v1/service-principals", content='{"name": PRIVATE-SENTINEL', headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    validate_response(app, response, "/api/v1/service-principals", "post")
    assert "PRIVATE" not in response.text


@pytest.mark.parametrize("range_header,status", [(None, 200), ("bytes=0-3", 206), ("garbage", 400), ("bytes=99999-", 416)])
def test_binary_and_range_responses(contract, range_header, status):
    app, client, job, _ = contract
    response = client.get(f"/api/v1/jobs/{job.job_id}/artifact/content", headers={"Range": range_header} if range_header else {})
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    if status >= 400:
        validate_response(app, response, "/api/v1/jobs/{job_id}/artifact/content")
        if status == 416:
            assert response.headers["content-range"].startswith("bytes */")
    else:
        assert response.headers["content-type"] == "application/pdf"
        assert response.content == (b"%PDF" if status == 206 else b"%PDF-synthetic-artifact")


def test_export_is_deterministic_and_does_not_start_runtime(tmp_path):
    target, state = tmp_path / "openapi.json", tmp_path / "no-runtime"
    env = {**os.environ, "OCR_TMP_DIR": str(state)}
    command = [sys.executable, "-m", "app.export_openapi", str(target)]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert not state.exists()
    result = subprocess.run(command + ["--check"], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert all(p.startswith("/api/v1/") for p in json.loads(target.read_text(encoding="utf-8"))["paths"])
    target.write_text("{}", encoding="utf-8")
    result = subprocess.run(command + ["--check"], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert target.read_text(encoding="utf-8") == "{}"


DOCUMENT_BASE = "/api/v1/jobs/{job_id}"
CANONICAL_SCOPES = {"jobs:read", "artifacts:read"}
METADATA_SCOPES = CANONICAL_SCOPES | {"metadata:read"}
DOCUMENT_OPERATIONS = {
    ("put", DOCUMENT_BASE + "/metadata"): METADATA_SCOPES | {"metadata:write"},
    ("post", DOCUMENT_BASE + "/metadata/approve"): METADATA_SCOPES | {"metadata:approve"},
    ("get", DOCUMENT_BASE + "/metadata"): METADATA_SCOPES,
    ("get", DOCUMENT_BASE + "/metadata/revisions"): METADATA_SCOPES,
    ("get", DOCUMENT_BASE + "/metadata/revisions/{revision_id}"): METADATA_SCOPES,
    ("get", DOCUMENT_BASE + "/metadata/revisions/{revision_id}/segments/{segment_id}/fields"): METADATA_SCOPES,
    ("get", DOCUMENT_BASE + "/metadata/revisions/{revision_id}/segments/{segment_id}/fields/{field_name}/values"): METADATA_SCOPES,
    ("get", DOCUMENT_BASE + "/metadata/revisions/{revision_id}/segments/{segment_id}/fields/{field_name}/evidence"): METADATA_SCOPES,
    ("get", DOCUMENT_BASE + "/metadata/revisions/{revision_id}/segments/{segment_id}/fields/{field_name}/strings/{part}"): METADATA_SCOPES,
    ("get", DOCUMENT_BASE + "/metadata/revisions/{revision_id}/schemas/{schema_sha256}"): METADATA_SCOPES,
    ("get", DOCUMENT_BASE + "/metadata/revisions/{revision_id}/content"): METADATA_SCOPES,
    ("get", DOCUMENT_BASE + "/canonical/pages.jsonl"): CANONICAL_SCOPES,
    ("get", DOCUMENT_BASE + "/canonical/manifest"): CANONICAL_SCOPES,
    ("get", DOCUMENT_BASE + "/canonical/pages"): CANONICAL_SCOPES,
    ("get", DOCUMENT_BASE + "/canonical/pages/{page}"): CANONICAL_SCOPES,
}


def test_published_document_operations_and_exact_scopes(contract):
    schema = v1_schema(contract[0])
    actual = {(method, path): set(operation["x-required-scopes"])
              for path, methods in schema["paths"].items()
              if path.startswith((DOCUMENT_BASE + "/metadata", DOCUMENT_BASE + "/canonical"))
              for method, operation in methods.items()}
    assert len(DOCUMENT_OPERATIONS) == 15
    assert actual == DOCUMENT_OPERATIONS
    description = schema["paths"][DOCUMENT_BASE + "/metadata"]["put"]["description"]
    assert "contexts:read is required only for new or changed catalog bindings." in description


@pytest.fixture
def mounted_document(env, monkeypatch):
    import app.main as main
    from app.config import settings
    from app.services.upload_service import get_upload_service
    from app.services.batch_service import get_batch_service
    from app.services.control_service import get_control_service
    from app.services.export_service import get_export_service
    from app.services.job_queue import get_job_queue_processor

    e = env
    monkeypatch.setattr(settings, "ocr_tmp_dir", e.jobs.root_dir)
    monkeypatch.setattr(main, "get_user_store", Mock(return_value=Mock()))
    monkeypatch.setattr(main, "RetentionWorker", Mock())
    app = create_app(lambda: JobApplication(e.jobs))
    principal = replace(e.owner, scopes=e.owner.scopes | {"metadata:approve"})
    uploads = SimpleNamespace(jobs=e.jobs)
    app.dependency_overrides.update({
        require_principal: lambda: principal,
        get_upload_service: lambda: uploads,
        get_batch_service: lambda: SimpleNamespace(jobs=e.jobs, uploads=uploads),
        get_control_service: lambda: SimpleNamespace(jobs=e.jobs, uploads=uploads),
        get_export_service: Mock(return_value=Mock(jobs=e.jobs)),
        get_job_queue_processor: Mock(return_value=Mock(job_store=e.jobs)),
    })
    with TestClient(app) as client:
        provider = app.state.document_provider
        assert provider().jobs is e.jobs
        yield e, app, client
    assert provider._execution.active_count == 0
    assert app.state.document_provider is not provider
    assert app.state.runtime_lease._file is None


@pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")
@pytest.mark.parametrize("method,suffix,status", [
    ("get", "/metadata", 200),
    ("get", "/canonical/pages", 200),
    ("get", "/metadata/revisions/{revision_id}/content", 200),
    ("get", "/canonical/pages.jsonl", 200),
    ("put", "/metadata", 201),
    ("post", "/metadata/approve", 201),
])
def test_mounted_document_exchanges(mounted_document, method, suffix, status):
    e, app, client = mounted_document
    head = save(e)[0] if "/metadata" in suffix and method != "put" else None
    template = DOCUMENT_BASE + suffix
    path = template.replace("{job_id}", e.job.job_id)
    kwargs = {}
    if method == "put":
        kwargs["json"] = body(e)
    elif method == "post":
        snapshot = persisted(e, head)
        kwargs["json"] = dict(
            revision_id=head.revision_id,
            expected_head=head.revision_id, expected_sha256=head.sha256,
            reviewed_fields=[dict(segment_id=segment.segment_id, field_name=field.field_name)
                             for segment in snapshot.segments for field in segment.fields])
    if method in ("put", "post"):
        kwargs["headers"] = {"Idempotency-Key": "mounted-document-operation-0001"}
    if "{revision_id}" in path:
        path = path.replace("{revision_id}", head.revision_id)
        kwargs["params"] = {"sha256": head.sha256}
    response = client.request(method, path, **kwargs)
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert len(response.headers["x-request-id"]) == 32
    if suffix.endswith("pages.jsonl"):
        content = v1_schema(app)["paths"][template][method]["responses"]["200"]["content"]["application/x-ndjson"]
        assert response.headers["content-type"].split(";")[0] == "application/x-ndjson"
        Draft202012Validator(content["schema"]).validate(response.text)
        assert [json.loads(line)["text"] for line in response.text.splitlines()] == PAGES
    else:
        payload = validate_response(app, response, template, method)
        if method in ("put", "post"):
            assert response.headers["Idempotency-Replayed"] == "false"
            snapshot = persisted(e, SimpleNamespace(**payload))
            assert snapshot.state == ("draft" if method == "put" else "approved")
        elif suffix == "/metadata":
            assert payload["revision_id"] == head.revision_id
        elif suffix == "/canonical/pages":
            assert payload["text"] == PAGES[0][:8000]
    if suffix.endswith(("/content", "pages.jsonl")):
        filename = "metadata.json" if suffix.endswith("/content") else "pages.jsonl"
        assert response.headers["content-disposition"] == f'attachment; filename="{filename}"'
        assert int(response.headers["content-length"]) == len(response.content)
        assert response.headers["x-content-sha256"] == hashlib.sha256(response.content).hexdigest()
        if filename == "metadata.json":
            expected = e.job.working_dir / "results" / "metadata" / (head.revision_id + ".json")
            assert response.content == expected.read_bytes()


@pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")
def test_mounted_document_authentication_and_canonical_only_scopes(mounted_document):
    e, app, client = mounted_document
    path = f"/api/v1/jobs/{e.job.job_id}"
    app.dependency_overrides.pop(require_principal)
    response = client.get(path + "/metadata")
    assert response.status_code == 401
    assert validate_response(app, response, DOCUMENT_BASE + "/metadata")["error"]["code"] == "authentication_required"
    assert response.headers["www-authenticate"] == "Bearer"
    canonical_reader = replace(e.owner, scopes=frozenset(CANONICAL_SCOPES))
    app.dependency_overrides[require_principal] = lambda: canonical_reader
    response = client.get(path + "/metadata")
    assert response.status_code == 403
    assert validate_response(app, response, DOCUMENT_BASE + "/metadata")["error"]["code"] == "insufficient_scope"
    response = client.get(path + "/canonical/pages")
    assert response.status_code == 200
    assert validate_response(app, response, DOCUMENT_BASE + "/canonical/pages")["text"] == PAGES[0][:8000]
