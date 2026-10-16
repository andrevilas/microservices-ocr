"""Validate actual HTTP exchanges and examples against the published JSON schemas."""
import json
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
    assert schema["info"]["x-api-version"] == "1.6.0"
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
                    if media in ("application/pdf", "application/zip"):
                        assert content["schema"]["format"] == "binary"
                    else:
                        assert "example" in content, (path, status)
                        Draft202012Validator({"components": schema["components"], **content["schema"]}).validate(content["example"])
            if "requestBody" in operation:
                if "application/json" not in operation["requestBody"]["content"]:
                    continue  # Binary exchanges are covered by upload integration tests.
                body = operation["requestBody"]["content"]["application/json"]["schema"]
                model = schema["components"]["schemas"][body["$ref"].rsplit("/", 1)[1]] if "$ref" in body else body
                for example in model["examples"]:
                    Draft202012Validator({"components": schema["components"], **model}).validate(example)
    assert len(operations) == len(set(operations)) == 42


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
