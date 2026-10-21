"""HTTP, MCP runtime and remote adapter coverage for E3 extraction runs over real shared services."""
import asyncio
import os
import re
import time

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

import app.services.extraction_service as extraction_service
from app.application import ApplicationError, JobApplication
from app.document_mcp_http import document_request
from app.document_mcp_runtime import execute_document
from app.extraction_models import ExtractionList, ExtractionReceipt, ExtractionView
from app.http_contract import V1Boundary, error_response, install_openapi
from app.metadata_models import SaveMetadataReceipt
from app.remote import OPERATIONS
from app.routes.extractions import router
from app.security import require_principal
from app.services.catalog_runtime import CatalogProvider
from app.services.document_runtime import DocumentProvider
from test_document_metadata import who
from test_extraction_service import EXTRACT, FakeProvider, content, env  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")


@pytest.fixture
def http(env, monkeypatch):
    e = env
    e.fake = FakeProvider(content(EXTRACT))
    monkeypatch.setattr(extraction_service, "settings_provider", lambda: e.fake)
    app = FastAPI()
    app.include_router(router)
    catalog = CatalogProvider(lambda: JobApplication(e.jobs))()
    provider = DocumentProvider(lambda: e.bundle, lambda: catalog)
    app.state.document_provider = provider
    app.dependency_overrides[require_principal] = lambda: e.owner

    @app.exception_handler(ApplicationError)
    async def application_error(request, error):
        return error_response(request, error.status, error.code, error.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return error_response(request, 422, "validation_error", "Invalid request parameters.")

    routes = list(router.routes)
    app.add_middleware(V1Boundary, routes=routes)
    install_openapi(app, routes)
    with TestClient(app) as client:
        try:
            yield e, app, client, provider
        finally:
            client.portal.call(provider.shutdown)


def base(e):
    return f"/api/v1/jobs/{e.job.job_id}/extractions"


def finished(client, e, run_id):
    for _ in range(200):
        view = ExtractionView.model_validate_json(client.get(f"{base(e)}/{run_id}").content)
        if view.state in ("completed", "failed"):
            return view
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def test_queue_poll_replay_list_and_apply(http):
    e, app, client, provider = http
    first = client.post(base(e), json={"type_id": e.version.type_id})
    assert first.status_code == 202, first.text
    assert first.headers["cache-control"] == "no-store"
    run = ExtractionReceipt.model_validate_json(first.content).run
    view = finished(client, e, run.run_id)
    assert view.outcome == "completed" and {f.field_name for f in view.fields} >= {"numero", "objeto"}
    again = client.post(base(e), json={"type_id": e.version.type_id})
    assert again.status_code == 200 and ExtractionReceipt.model_validate_json(again.content).replayed is True
    listed = ExtractionList.model_validate_json(client.get(base(e)).content)
    assert [item.run_id for item in listed.items] == [run.run_id]
    applied = client.post(f"{base(e)}/{run.run_id}/apply", json={"expected_head": None, "expected_sha256": None},
                          headers={"Idempotency-Key": "extract-http-apply-1"})
    assert applied.status_code == 201, applied.text
    assert applied.headers["idempotency-replayed"] == "false"
    receipt = SaveMetadataReceipt.model_validate_json(applied.content)
    replay = client.post(f"{base(e)}/{run.run_id}/apply", json={"expected_head": None, "expected_sha256": None},
                         headers={"Idempotency-Key": "extract-http-apply-1"})
    assert replay.status_code == 200 and SaveMetadataReceipt.model_validate_json(replay.content) == receipt


@pytest.mark.parametrize("body", [
    {}, {"type_id": "a" * 32, "context_id": "b" * 32}, {"type_id": "A" * 32}, {"type_id": "a" * 32, "extra": 1},
    {"type_id": "a" * 32, "page_start": 2}, {"type_id": "a" * 32, "page_start": 3, "page_end": 2},
    {"context_id": "a" * 32, "type_version": 1}, {"type_id": "a" * 32, "retry": "yes"},
])
def test_invalid_bodies_are_rejected(http, body):
    e, app, client, provider = http
    response = client.post(base(e), json=body)
    assert response.status_code == 422 and e.fake.calls == []


def test_disabled_server_answers_503_and_apply_requires_key(http, monkeypatch):
    e, app, client, provider = http
    monkeypatch.setattr(extraction_service, "settings_provider", lambda: None)
    fresh = DocumentProvider(lambda: e.bundle, lambda: CatalogProvider(lambda: JobApplication(e.jobs))())
    app.state.document_provider = fresh
    response = client.post(base(e), json={"type_id": e.version.type_id})
    assert response.status_code == 503 and response.json()["error"]["code"] == "extraction_disabled"
    missing_key = client.post(f"{base(e)}/{'1' * 32}/apply", json={"expected_head": None, "expected_sha256": None})
    assert missing_key.status_code == 422
    client.portal.call(fresh.shutdown)


def test_scopes_are_enforced_before_services(http):
    e, app, client, provider = http
    app.dependency_overrides[require_principal] = lambda: who(scopes={"jobs:read", "artifacts:read", "metadata:read",
                                                                      "metadata:write"})
    assert client.get(base(e)).status_code == 200
    assert client.post(base(e), json={"type_id": e.version.type_id}).status_code == 403
    app.dependency_overrides[require_principal] = lambda: who(scopes={"jobs:read", "artifacts:read", "metadata:read",
                                                                      "contexts:read"})
    assert client.post(base(e), json={"type_id": e.version.type_id}).status_code == 403
    assert client.post(f"{base(e)}/{'1' * 32}/apply", json={"expected_head": None, "expected_sha256": None},
                       headers={"Idempotency-Key": "extract-http-scope-1"}).status_code == 403
    app.dependency_overrides[require_principal] = lambda: who(scopes={"jobs:read", "metadata:read"})
    assert client.get(base(e)).status_code == 403
    assert e.fake.calls == []


def test_openapi_documents_four_operations_with_scopes(http):
    e, app, client, provider = http
    schema = app.openapi()
    ops = {op["operationId"]: op for path in schema["paths"].values() for op in path.values()}
    assert set(ops) == {"ocr_create_metadata_extraction", "ocr_list_metadata_extractions",
                        "ocr_get_metadata_extraction", "ocr_apply_metadata_extraction"}
    read = ["jobs:read", "artifacts:read", "metadata:read"]
    assert ops["ocr_list_metadata_extractions"]["x-required-scopes"] == read
    assert ops["ocr_get_metadata_extraction"]["x-required-scopes"] == read
    assert ops["ocr_create_metadata_extraction"]["x-required-scopes"] == read + ["metadata:write", "contexts:read"]
    assert ops["ocr_apply_metadata_extraction"]["x-required-scopes"] == read + ["metadata:write"]


def test_mcp_runtime_and_remote_adapter_map_the_same_operations(http):
    e, app, client, provider = http
    receipt = asyncio.run(execute_document(provider, e.owner, "create_metadata_extraction",
                                           (e.job.job_id, {"type_id": e.version.type_id})))
    assert isinstance(receipt, ExtractionReceipt)
    view = finished(client, e, receipt.run.run_id)
    got = asyncio.run(execute_document(provider, e.owner, "get_metadata_extraction", (e.job.job_id, view.run_id)))
    assert got.run_id == view.run_id
    listed = asyncio.run(execute_document(provider, e.owner, "list_metadata_extractions", (e.job.job_id,)))
    assert [item.run_id for item in listed.items] == [view.run_id]
    applied = asyncio.run(execute_document(provider, e.owner, "apply_metadata_extraction", (
        e.job.job_id, view.run_id, {"expected_head": None, "expected_sha256": None}, "extract-mcp-apply-1")))
    assert isinstance(applied, SaveMetadataReceipt)
    reader = who(scopes={"jobs:read", "artifacts:read", "metadata:read"})
    assert asyncio.run(execute_document(provider, reader, "list_metadata_extractions", (e.job.job_id,))).items
    for operation, args in (("create_metadata_extraction", (e.job.job_id, {"type_id": e.version.type_id})),
                            ("apply_metadata_extraction", (e.job.job_id, view.run_id,
                                                           {"expected_head": None, "expected_sha256": None},
                                                           "extract-mcp-apply-2"))):
        with pytest.raises(ApplicationError):
            asyncio.run(execute_document(provider, reader, operation, args))

    job, run = e.job.job_id, view.run_id
    assert document_request("create_metadata_extraction", (job, {"type_id": e.version.type_id}))[:2] == (
        "POST", f"/jobs/{job}/extractions")
    assert document_request("list_metadata_extractions", (job,))[:2] == ("GET", f"/jobs/{job}/extractions")
    assert document_request("get_metadata_extraction", (job, run))[:2] == ("GET", f"/jobs/{job}/extractions/{run}")
    method, path, query, body, headers = document_request(
        "apply_metadata_extraction", (job, run, {"expected_head": None, "expected_sha256": None}, "extract-key-1"))
    assert (method, path, headers) == ("POST", f"/jobs/{job}/extractions/{run}/apply",
                                       {"Idempotency-Key": "extract-key-1"})
    for bad in ((job, {}), ("x", {"type_id": e.version.type_id})):
        with pytest.raises(ToolError):
            document_request("create_metadata_extraction", bad)
    with pytest.raises(ToolError):
        document_request("apply_metadata_extraction", (job, run, {"expected_head": None, "expected_sha256": None}, "k"))
    for method, path in (("GET", f"/api/v1/jobs/{job}/extractions"), ("GET", f"/api/v1/jobs/{job}/extractions/{run}"),
                         ("POST", f"/api/v1/jobs/{job}/extractions"),
                         ("POST", f"/api/v1/jobs/{job}/extractions/{run}/apply")):
        assert any(re.fullmatch(pattern, path) for pattern in OPERATIONS[method]), (method, path)
