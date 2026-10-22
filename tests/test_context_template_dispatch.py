import asyncio
import json
from types import SimpleNamespace

import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app.application import ApplicationError, JobApplication, Principal
from app.context_templates import ContextTemplateList, TemplateCloneInput, TemplateCloneReceipt
from app.main import create_app
from app.mcp_server import execute_catalog
from app.mcp_stdio import ApiClient
from app.security import require_principal
from app.services.catalog_runtime import CatalogProvider
from app.services.storage_service import JobStore

SCOPES = frozenset({"contexts:read", "contexts:write"})
KEY = "template-dispatch-key-0001"
LIST = "list_context_templates"
CLONE = "clone_context_template"


def rows(store):
    with store._lock:
        return tuple(store._conn.iterdump())


@pytest.fixture
def catalog(tmp_path):
    store = JobStore(tmp_path)
    application = JobApplication(store)
    services = CatalogProvider(lambda: application)()
    owner = Principal(subject="owner", user_id=1, scopes=SCOPES)
    try:
        yield SimpleNamespace(store=store, application=application, services=services, owner=owner)
    finally:
        with store._lock:
            if store._conn is not None:
                store._conn.close()
                store._conn = None


def arguments(template, key=KEY):
    return (template.template_id, TemplateCloneInput(expected_version=template.template_version,
            expected_sha256=template.template_sha256), key)


def assert_clone(c, template, receipt):
    assert c.services.contexts.get(c.owner, receipt.context_id).body == template.context
    for cloned, definition in zip(receipt.types, template.types, strict=True):
        actual = c.services.types.get(c.owner, cloned.type_id)
        assert actual.body == definition.body
        assert actual.context_id == receipt.context_id
        assert c.services.types.list_versions(c.owner, cloned.type_id).items == []


def test_dispatch_ownership_replay_and_denials(catalog):
    c = catalog
    before = rows(c.store)
    templates = execute_catalog(c.services, c.owner, LIST, ())
    assert len(templates.items) == 3
    assert rows(c.store) == before
    assert c.services.contexts.list(c.owner).items == []
    template = templates.items[0]
    receipt = execute_catalog(c.services, c.owner, CLONE, arguments(template))
    saved = rows(c.store)
    assert execute_catalog(c.services, c.owner, CLONE, arguments(template)) == receipt
    assert rows(c.store) == saved
    assert_clone(c, template, receipt)
    foreign = Principal(subject="foreign", user_id=2, scopes=SCOPES)
    other = execute_catalog(c.services, foreign, CLONE, arguments(template))
    assert {receipt.context_id, *(t.type_id for t in receipt.types)}.isdisjoint(
        {other.context_id, *(t.type_id for t in other.types)})
    for admin in (False, True):
        principal = Principal(subject="foreign", user_id=2, scopes=SCOPES, is_admin=admin)
        for operation, args in (("get_context", (receipt.context_id,)),
                                ("get_document_type", (receipt.types[0].type_id,))):
            before = rows(c.store)
            with pytest.raises(ApplicationError) as error:
                execute_catalog(c.services, principal, operation, args)
            assert error.value.status == 404
            assert rows(c.store) == before
    reader = Principal(subject="owner", user_id=1, scopes=frozenset({"contexts:read"}))
    before = rows(c.store)
    with pytest.raises(ApplicationError) as error:
        execute_catalog(c.services, reader, CLONE, arguments(template, KEY + "-denied"))
    assert error.value.status == 403
    assert rows(c.store) == before
    assert execute_catalog(c.services, c.owner, LIST, ()) == templates


def test_api_client_real_asgi_contract(catalog):
    c = catalog
    api = create_app(lambda: c.application)
    # Explicit policy/adapter injection; this does not prove credential authentication.
    api.dependency_overrides[require_principal] = lambda: c.owner
    requests, responses = [], []

    async def request_hook(request):
        requests.append((request.method, request.url.path, request.url.query,
                         request.headers.get("Idempotency-Key"),
                         request.headers.get("Content-Type"), bytes(request.content)))

    async def response_hook(response):
        responses.append((response.status_code, response.headers.get("Idempotency-Replayed")))

    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=api), event_hooks={
                "request": [request_hook], "response": [response_hook]}) as http:
            client = ApiClient("http://localhost", http)
            before = rows(c.store)
            listing = await client.invoke(None, LIST)
            templates = ContextTemplateList.model_validate_json(json.dumps(listing), strict=True)
            assert len(templates.items) == 3
            assert rows(c.store) == before
            assert c.services.contexts.list(c.owner).items == []
            assert requests == [("GET", "/api/v1/context-templates", b"", None, None, b"")]
            template = templates.items[0]
            result = await client.invoke(None, CLONE, *arguments(template))
            saved = rows(c.store)
            assert await client.invoke(None, CLONE, *arguments(template)) == result
            assert rows(c.store) == saved
            assert [status for status, _ in responses] == [200, 201, 200]
            assert [replay for _, replay in responses[1:]] == ["false", "true"]
            for method, path, query, key, content_type, body in requests[1:]:
                assert (method, path, query, key, content_type) == (
                    "POST", f"/api/v1/context-templates/{template.template_id}/clone",
                    b"", KEY, "application/json")
                assert json.loads(body) == arguments(template)[1].model_dump(mode="json")
            receipt = TemplateCloneReceipt.model_validate_json(json.dumps(result), strict=True)
            assert execute_catalog(c.services, c.owner, CLONE, arguments(template)) == receipt
            assert_clone(c, template, receipt)
            assert await client.invoke(None, LIST) == listing
            before = rows(c.store)
            with pytest.raises(ToolError, match="HTTP 404"):
                await client.invoke(None, CLONE, "unknown-valid-template", arguments(template)[1], KEY + "-404")
            assert rows(c.store) == before
    try:
        asyncio.run(scenario())
    finally:
        api.dependency_overrides.clear()


@pytest.mark.parametrize("slug", [None, 1, "", "a/b", "a\\b", "..", "%61", "a\n", "A", "é", "a" * 65])
def test_invalid_slug_never_sends(slug):
    run_boundary(slug, "validation_error", expected_calls=0)


def run_boundary(slug="a" * 64, match="HTTP 404", expected_calls=1, failure=None, caplog=None):
    calls = []
    token = "synthetic-bearer-token"
    body = TemplateCloneInput(expected_version=1, expected_sha256="a" * 64)

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer " + token
        assert request.headers["Idempotency-Key"] == KEY
        if failure == "timeout":
            raise TimeoutError("PRIVATE endpoint")
        if failure == "redirect":
            return httpx2.Response(302, headers={"Location": "https://private.invalid/PRIVATE"})
        if failure == "invalid":
            return httpx2.Response(200, content=b"PRIVATE invalid JSON")
        if failure == "oversized":
            return httpx2.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))
        status = 503 if failure == "storage" else 404
        return httpx2.Response(status, json={"error": {"code": "storage_unavailable" if status == 503
                                                     else "not_found", "message": "PRIVATE"}})

    async def scenario():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler), follow_redirects=False,
                trust_env=False, headers={"Authorization": "Bearer " + token}) as http:
            with pytest.raises(ToolError, match=match) as error:
                await ApiClient("http://localhost", http).invoke(None, CLONE, slug, body, KEY)
            message = str(error.value)
            assert "PRIVATE" not in message and token not in message
            if failure in {"timeout", "storage"}:
                assert "same key" in message
            assert len(calls) == expected_calls
    asyncio.run(scenario())
    if caplog is not None:
        assert "PRIVATE" not in caplog.text and token not in caplog.text


def test_valid_slug_boundary():
    run_boundary()


@pytest.mark.parametrize("failure,code", [
    ("timeout", "api_unavailable"),
    ("redirect", "upstream_redirect"),
    ("invalid", "invalid_api_response"),
    ("oversized", "upstream_response_limit"),
    ("storage", "storage_unavailable"),
])
def test_transport_failures_are_single_attempt_and_sanitized(failure, code, caplog):
    run_boundary(failure=failure, match=code, caplog=caplog)


def test_oversized_request_never_sends(monkeypatch):
    monkeypatch.setattr("app.mcp_stdio.MAX_CATALOG_REQUEST_BYTES", 1)
    run_boundary(match="request_too_large", expected_calls=0)
