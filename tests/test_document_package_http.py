"""HTTP, MCP runtime and stdio adapter coverage for document packages over real shared services."""
import asyncio
import hashlib
import io
import os
from zipfile import ZipFile

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

import app.services.package_service as package_service
from app.application import ApplicationError, JobApplication
from app.document_mcp_http import document_request
from app.document_mcp_runtime import execute_document
from app.http_contract import V1Boundary, error_response, install_openapi
from app.package_models import CreatePackageInput, PackageList, PackageReceipt, PackageView
from app.remote import OPERATIONS
from app.routes.packages import router
from app.security import require_principal
from app.services.catalog_runtime import CatalogProvider
from app.services.document_runtime import DocumentProvider
from test_document_metadata import who
from test_document_package import READER, Validator, env  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")


@pytest.fixture
def http(env, monkeypatch):
    e = env
    validator = Validator()
    monkeypatch.setattr(package_service, "_settings_validator", validator)
    app = FastAPI()
    app.include_router(router)
    catalog = CatalogProvider(lambda: JobApplication(e.jobs))()
    provider = DocumentProvider(lambda: e.bundle, lambda: catalog)
    app.state.document_provider = provider
    app.dependency_overrides[require_principal] = lambda: e.reader

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
            yield e, app, client, provider, validator
        finally:
            client.portal.call(provider.shutdown)


def base(e):
    return f"/api/v1/jobs/{e.job.job_id}/packages"


def post(client, e, body=None):
    return client.post(base(e), json={} if body is None else body)


def test_create_replay_list_get_and_download(http):
    e, app, client, provider, validator = http
    first = post(client, e)
    assert first.status_code == 201, first.text
    assert first.headers["cache-control"] == "no-store"
    receipt = PackageReceipt.model_validate_json(first.content)
    assert receipt.replayed is False and receipt.package.state == "ready"
    package = receipt.package
    assert package.download_path == f"{base(e)}/{package.package_id}/content"
    again = post(client, e)
    assert again.status_code == 200 and PackageReceipt.model_validate_json(again.content).replayed is True
    listed = PackageList.model_validate_json(client.get(base(e)).content)
    assert [p.package_id for p in listed.items] == [package.package_id]
    got = PackageView.model_validate_json(client.get(f"{base(e)}/{package.package_id}").content)
    assert got == package
    download = client.get(package.download_path)
    assert download.status_code == 200, download.text
    assert download.headers["content-type"] == "application/zip"
    assert download.headers["content-disposition"] == 'attachment; filename="package.zip"'
    assert download.headers["x-content-sha256"] == package.sha256 == hashlib.sha256(download.content).hexdigest()
    assert int(download.headers["content-length"]) == package.size_bytes
    names = ZipFile(io.BytesIO(download.content)).namelist()
    assert set(names) == {"document.pdf", "original.pdf", "metadata.json", "pages.jsonl", "validation.json",
                          "manifest.json"}
    assert len(validator.calls) == 1
    assert not e.jobs._artifact_leases


def test_tampered_package_download_is_409_and_stays_invalid(http):
    e, app, client, provider, validator = http
    package = PackageReceipt.model_validate_json(post(client, e).content).package
    archive = e.job.working_dir / "packages" / package.package_id / "package.zip"
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 0x01
    archive.write_bytes(bytes(data))
    response = client.get(package.download_path)
    assert response.status_code == 409 and response.json()["error"]["code"] == "package_invalid"
    view = client.get(f"{base(e)}/{package.package_id}").json()
    assert view["state"] == "invalid" and view["download_path"] is None


def test_validation_failure_is_422_without_package(http):
    e, app, client, provider, validator = http
    validator.status, validator.reasons = "failed", ("non_compliant",)
    response = post(client, e)
    assert response.status_code == 422 and response.json()["error"]["code"] == "package_pdfa_validation_failed"
    assert client.get(base(e)).json()["items"] == []


@pytest.mark.parametrize("body", [{"include_original": "yes"}, {"metadata_revision_id": "XYZ"}, {"extra": 1}])
def test_invalid_body_is_rejected(http, body):
    e, app, client, provider, validator = http
    assert post(client, e, body).status_code == 422
    assert validator.calls == []


def test_other_owner_and_missing_scope(http):
    e, app, client, provider, validator = http
    package = PackageReceipt.model_validate_json(post(client, e).content).package
    app.dependency_overrides[require_principal] = lambda: who("user:2", scopes=READER, admin=True)
    assert client.get(f"{base(e)}/{package.package_id}").status_code == 404
    assert client.get(package.download_path).status_code == 404
    assert post(client, e).status_code == 404
    app.dependency_overrides[require_principal] = lambda: who(scopes=READER - {"metadata:read"})
    assert post(client, e).status_code == 403
    assert client.get(base(e)).status_code == 403


def test_openapi_declares_scopes(http):
    e, app, client, provider, validator = http
    paths = client.get("/openapi.json").json()["paths"]
    root = "/api/v1/jobs/{job_id}/packages"
    assert paths[root]["post"]["operationId"] == "ocr_create_document_package"
    assert paths[root]["get"]["operationId"] == "ocr_list_document_packages"
    assert paths[root + "/{package_id}"]["get"]["operationId"] == "ocr_get_document_package"
    assert paths[root + "/{package_id}/content"]["get"]["operationId"] == "ocr_download_document_package"
    assert paths[root]["post"]["requestBody"]["required"] is True


def test_remote_allowlist_covers_package_routes():
    job, package = "a" * 32, "b" * 32
    import re
    def allowed(method, path):
        return any(re.fullmatch(pattern, path) for pattern in OPERATIONS[method])
    assert allowed("POST", f"/api/v1/jobs/{job}/packages")
    for path in (f"/api/v1/jobs/{job}/packages", f"/api/v1/jobs/{job}/packages/{package}",
                 f"/api/v1/jobs/{job}/packages/{package}/content"):
        assert allowed("GET", path)
    assert not allowed("DELETE", f"/api/v1/jobs/{job}/packages/{package}")
    assert not allowed("PUT", f"/api/v1/jobs/{job}/packages/{package}")


def test_mcp_runtime_create_list_get(http):
    e, app, client, provider, validator = http

    async def run(operation, *args):
        return await execute_document(provider, e.reader, operation, args)

    receipt = asyncio.run(run("create_document_package", e.job.job_id, CreatePackageInput()))
    assert isinstance(receipt, PackageReceipt) and receipt.replayed is False
    assert asyncio.run(run("list_document_packages", e.job.job_id)).items[0].package_id == receipt.package.package_id
    view = asyncio.run(run("get_document_package", e.job.job_id, receipt.package.package_id))
    assert view.package_id == receipt.package.package_id
    with pytest.raises(ApplicationError) as caught:
        asyncio.run(execute_document(provider, who(scopes=READER - {"metadata:read"}),
                                     "list_document_packages", (e.job.job_id,)))
    assert caught.value.status == 403


def test_stdio_adapter_mapping():
    job, package = "a" * 32, "b" * 32
    assert document_request("list_document_packages", (job,)) == ("GET", f"/jobs/{job}/packages", {}, None, None)
    assert document_request("get_document_package", (job, package)) == (
        "GET", f"/jobs/{job}/packages/{package}", {}, None, None)
    method, path, query, body, headers = document_request(
        "create_document_package", (job, {"include_original": False}))
    assert (method, path, body, headers) == (
        "POST", f"/jobs/{job}/packages", {"metadata_revision_id": None, "include_original": False}, None)
    for bad in ((job,), ("x" * 32, {}), (job, {"include_original": "no"}), (job, {"extra": 1})):
        with pytest.raises(ToolError):
            document_request("create_document_package", bad)
    with pytest.raises(ToolError):
        document_request("get_document_package", (job, "../etc"))
