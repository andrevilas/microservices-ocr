"""Standalone HTTP integration with the real shared document services."""
import asyncio
import json
import os
from threading import Event

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.application import ApplicationError, JobApplication
from app.document_transport_models import MetadataRevisionPage, MetadataView
from app.http_contract import V1Boundary, error_response, install_openapi
from app.routes.documents import router
from app.security import require_principal
from app.services.catalog_runtime import CatalogProvider
from app.services.document_runtime import DocumentProvider
from test_document_metadata import env, persisted, save, who

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")


@pytest.fixture
def http(env):
    e = env
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

    app.add_middleware(V1Boundary, routes=app.routes)
    install_openapi(app, app.routes)
    services = provider()
    with TestClient(app) as client:
        try:
            yield e, app, client, services
        finally:
            client.portal.call(provider.shutdown)


def base(e):
    return f"/api/v1/jobs/{e.job.job_id}/metadata"


@pytest.mark.parametrize("view", ["current", "list", "revision"])
def test_success_and_openapi(http, view):
    e, app, client, services = http
    receipt, _ = save(e)
    stored = persisted(e, receipt)
    suffix, template, model, operation_id = {
        "current": ("", "", MetadataView, "ocr_get_document_metadata"),
        "list": ("/revisions", "/revisions", MetadataRevisionPage,
                 "ocr_list_document_metadata_revisions"),
        "revision": (f"/revisions/{receipt.revision_id}?sha256={receipt.sha256}",
                     "/revisions/{revision_id}", MetadataView,
                     "ocr_get_document_metadata_revision"),
    }[view]
    response = client.get(base(e) + suffix)
    assert response.status_code == 200, response.text
    dto = model.model_validate_json(response.content)
    assert dto.job_id == stored.job_id
    if view == "list":
        assert [item.revision_id for item in dto.revisions] == [receipt.revision_id]
        assert dto.revisions[0].sha256 == receipt.sha256
    else:
        assert dto.revision_id == stored.revision_id and dto.sha256 == receipt.sha256
    assert response.headers["cache-control"] == "no-store"
    assert len(response.headers["x-request-id"]) == 32
    path = "/api/v1/jobs/{job_id}/metadata" + template
    spec = client.get("/openapi.json").json()["paths"][path]["get"]
    assert spec["operationId"] == operation_id
    assert spec["x-required-scopes"] == ["jobs:read", "artifacts:read", "metadata:read"]
    model.model_validate_json(json.dumps(spec["responses"]["200"]["content"]["application/json"]["example"]))
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("suffix", [
    "/revisions/" + "a" * 32,
    "/revisions/BAD?sha256=" + "b" * 64,
    "/revisions/" + "a" * 32 + "?sha256=bad",
    "/revisions?limit=0", "/revisions?limit=101",
    "/revisions?after=" + "a" * 2049,
    "invalid-job",
])
def test_invalid_parameters(http, suffix):
    e, app, client, services = http
    url = base(e) + suffix
    if suffix == "invalid-job":
        url = "/api/v1/jobs/INVALID/metadata"
    response = client.get(url)
    assert response.status_code == 422
    assert response.json()["error"] == {
        "code": "validation_error", "message": "Invalid request parameters.", "details": []}


@pytest.mark.parametrize("case", ["owner", "hash", "missing"])
def test_authority_errors_match_real_query(http, case):
    e, app, client, services = http
    principal, method, kwargs, suffix = e.owner, "get_current", {}, ""
    if case != "missing":
        receipt, _ = save(e)
    if case == "owner":
        principal = who("user:2")
    if case == "hash":
        digest = ("0" if receipt.sha256[0] != "0" else "1") + receipt.sha256[1:]
        method = "get_revision"
        kwargs = dict(revision_id=receipt.revision_id, sha256=digest)
        suffix = f"/revisions/{receipt.revision_id}?sha256={digest}"
    app.dependency_overrides[require_principal] = lambda: principal

    async def query():
        return await services.execution.run(principal, lambda control: getattr(
            services.queries, method)(principal, e.job.job_id,
                                     checkpoint=control.checkpoint, **kwargs))

    with pytest.raises(ApplicationError) as caught:
        client.portal.call(query)
    response = client.get(base(e) + suffix)
    assert response.status_code == caught.value.status
    assert response.json()["error"]["code"] == caught.value.code
    assert response.status_code in ({409, 404} if case == "hash" else {404, 403})
    if case == "missing":
        assert response.status_code == 404
    assert not e.jobs._artifact_leases


def test_scope_checked_before_provider(http):
    e, app, client, services = http
    app.dependency_overrides[require_principal] = lambda: who(
        scopes={"jobs:read", "artifacts:read"})

    def forbidden():
        pytest.fail("provider resolved before the required scope check")

    app.state.document_provider = forbidden
    for suffix in ("", "/revisions", "/revisions/" + "a" * 32 + "?sha256=" + "b" * 64):
        response = client.get(base(e) + suffix)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "insufficient_scope"


def test_busy_execution_returns_429_and_releases_permits(http):
    e, app, client, services = http
    release, busy = Event(), Event()

    async def occupy():
        tasks = [asyncio.create_task(services.execution.run(
            e.owner, lambda control: release.wait(10))) for _ in range(64)]
        try:
            for _ in range(500):
                if any(task.done() and not task.cancelled() and isinstance(
                        task.exception(), ApplicationError) and task.exception().status == 429
                       for task in tasks):
                    busy.set()
                    break
                await asyncio.sleep(0.01)
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)

    future = client.portal.start_task_soon(occupy)
    try:
        assert busy.wait(7), "execution admission did not report saturation"
        assert client.get(base(e)).status_code == 429
    finally:
        release.set()
        future.result(timeout=15)
    assert client.get(base(e)).status_code == 404
    assert not e.jobs._artifact_leases
