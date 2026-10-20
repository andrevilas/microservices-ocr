"""Real MCP protocol with synthetic authentication, not OAuth or six-client qualification.

Document tools use the real global 46-tool registry.
"""
import asyncio
from dataclasses import replace
import os
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import httpx2
from fastapi.testclient import TestClient
from starlette.requests import Request
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
import pytest

from app.application import ApplicationError, JobApplication, Principal
from test_document_metadata import PAGES, body, env, persisted

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")
READ = frozenset({"jobs:read", "artifacts:read", "metadata:read"})


@pytest.fixture
def backend(env, monkeypatch):
    import app.main as main
    from app.config import settings
    from app.services.upload_service import get_upload_service
    from app.services.batch_service import get_batch_service
    from app.services.control_service import get_control_service
    from app.services.export_service import get_export_service
    from app.services.job_queue import get_job_queue_processor

    e = env
    principals = {
        "owner": e.owner,
        "other": replace(e.owner, subject="user:2", user_id=2, is_admin=True),
        "approver": replace(e.owner, scopes=READ | {"metadata:approve"}),
        "canonical": replace(e.owner, scopes=READ - {"metadata:read"}),
        "service": Principal("service:" + "f" * 32, None,
                             READ | {"metadata:approve"}, auth_source="service"),
    }

    async def authenticate(request: Request):
        header = request.headers.get("authorization", "")
        principal = principals.get(header.removeprefix("Bearer ")) if header.startswith("Bearer ") else None
        if principal is None:
            raise ApplicationError(401, "authentication_required", "A valid Bearer token is required.")
        return principal

    monkeypatch.setattr(settings, "ocr_tmp_dir", e.jobs.root_dir)
    monkeypatch.setattr(main, "get_user_store", Mock(return_value=Mock()))
    monkeypatch.setattr(main, "RetentionWorker", Mock())
    app = main.create_app(lambda: JobApplication(e.jobs), request_authenticator=authenticate)
    uploads = SimpleNamespace(jobs=e.jobs)
    app.dependency_overrides.update({
        get_upload_service: lambda: uploads,
        get_batch_service: lambda: SimpleNamespace(jobs=e.jobs, uploads=uploads),
        get_control_service: lambda: SimpleNamespace(jobs=e.jobs, uploads=uploads),
        get_export_service: Mock(return_value=Mock(jobs=e.jobs)),
        get_job_queue_processor: Mock(return_value=Mock(job_store=e.jobs)),
    })
    yield e, app
    assert app.state.runtime_lease._file is None


async def ok(client, name, args):
    result = await client.call_tool("ocr_" + name, args)
    assert not result.is_error, result.content
    return result.structured_content


async def denied(client, name, args, code):
    result = await client.call_tool("ocr_" + name, args)
    assert result.is_error and code in " ".join(item.text for item in result.content)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_shared_document_protocol(backend, mode):
    e, app = backend

    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers={"Authorization": "Bearer owner"}) as http:
            for token in (None, "unknown"):
                headers = {} if token is None else {"Authorization": "Bearer " + token}
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as raw:
                    assert (await raw.post("http://localhost/mcp", headers=headers, json={})).status_code == 401
            async with Client(streamable_http_client("http://localhost/mcp", http_client=http), mode=mode) as client:
                args = {"job_id": e.job.job_id}
                base = "http://localhost/api/v1/jobs/" + e.job.job_id
                request = dict(**args, body=body(e), idempotency_key="backend-document-save-0001")
                first = await ok(client, "save_document_metadata", request)
                current = await ok(client, "get_document_metadata", args)
                assert current["revision_id"] == first["revision_id"]
                assert (await http.get(base + "/metadata")).json() == current
                chunk = await ok(client, "get_document_canonical_chunk", dict(**args, max_chars=1000))
                assert chunk["text"] == PAGES[0][:1000]
                pinned = dict(**args, revision_id=first["revision_id"], sha256=first["sha256"])
                assert (await ok(client, "get_document_metadata_revision", pinned))["revision_id"] == first["revision_id"]
                provider = app.state.document_provider
                def forbidden():
                    pytest.fail("scope denial resolved document services")
                http.headers["Authorization"] = "Bearer canonical"
                app.state.document_provider = forbidden
                try:
                    await denied(client, "get_document_metadata", args, "insufficient_scope")
                finally:
                    app.state.document_provider = provider
                assert (await ok(client, "get_document_canonical_chunk", args))["text"] == PAGES[0][:1000]
                http.headers["Authorization"] = "Bearer other"
                await denied(client, "get_document_metadata", args, "not_found")
                assert (await http.get(base + "/metadata")).status_code == 404
                snapshot = persisted(e, SimpleNamespace(**first))
                approval = dict(**args, idempotency_key="backend-document-approve-0001", body=dict(
                    revision_id=first["revision_id"], expected_head=first["revision_id"], expected_sha256=first["sha256"],
                    reviewed_fields=[dict(segment_id=s.segment_id, field_name=f.field_name)
                                     for s in snapshot.segments for f in s.fields]))
                http.headers["Authorization"] = "Bearer service"
                result = await client.call_tool("ocr_approve_document_metadata", approval)
                assert result.is_error
                http.headers["Authorization"] = "Bearer approver"
                approved = await ok(client, "approve_document_metadata", approval)
                assert await ok(client, "approve_document_metadata", approval) == approved
                assert (await http.get(base + "/metadata")).json()["revision_id"] == approved["revision_id"]
                assert (await ok(client, "get_document_metadata_revision", pinned))["revision_id"] == first["revision_id"]
                http.headers["Authorization"] = "Bearer owner"
                assert await ok(client, "save_document_metadata", request) == first
                assert (await ok(client, "get_document_metadata", args))["revision_id"] == approved["revision_id"]
    with TestClient(app) as lifespan:
        lifespan.portal.call(scenario)


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_shared_permits_and_pre_start_provider_replacement(backend, mode):
    """Pre-start replacement uses normal captured shutdown; live swaps are unsupported."""
    from app.services.document_runtime import DocumentProvider

    e, app = backend
    original_provider = app.state.document_provider
    # Close the unused coordinator and replace it before the single lifespan.
    asyncio.run(original_provider.shutdown())
    provider = DocumentProvider(
        lambda: app.state.metadata_provider(),
        lambda: app.state.catalog_provider())
    app.state.document_provider = provider
    with TestClient(app) as lifespan:
        # Startup captures this replacement; HTTP and MCP resolve it lazily.
        assert app.state.document_provider is provider

        async def scenario():
            execution = provider().execution
            release, tasks = Event(), []
            try:
                # Discover the real bounded capacity without assuming its configured size.
                async with asyncio.timeout(5):
                    for _ in range(64):
                        before = execution.active_count
                        task = asyncio.create_task(execution.run(e.owner, lambda control: release.wait(10)))
                        tasks.append(task)
                        while execution.active_count == before and not task.done():
                            await asyncio.sleep(0.001)
                        if task.done():
                            with pytest.raises(ApplicationError) as saturated:
                                task.result()
                            assert saturated.value.status == 429
                            break
                    else:
                        pytest.fail("document execution did not enforce bounded admission")
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers={"Authorization": "Bearer owner"}) as http:
                    response = await http.get("http://localhost/api/v1/jobs/" + e.job.job_id + "/metadata")
                    assert response.status_code == 429
                    async with Client(streamable_http_client("http://localhost/mcp", http_client=http), mode=mode) as client:
                        await denied(client, "get_document_metadata", {"job_id": e.job.job_id}, response.json()["error"]["code"])
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
            assert execution.active_count == 0
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers={"Authorization": "Bearer owner"}) as http:
                async with Client(streamable_http_client("http://localhost/mcp", http_client=http), mode=mode) as client:
                    assert (await ok(client, "get_document_canonical_chunk", {"job_id": e.job.job_id}))["text"] == PAGES[0][:1000]
            return execution

        execution = lifespan.portal.call(scenario)

    # Normal lifespan shutdown drains the captured replacement and rotates state.
    async def assert_closed():
        with pytest.raises(ApplicationError) as closed:
            await execution.run(e.owner, lambda control: None)
        assert closed.value.status == 503

    asyncio.run(assert_closed())
    assert execution.active_count == 0
    assert app.state.document_provider is not provider
    assert app.state.runtime_lease._file is None
