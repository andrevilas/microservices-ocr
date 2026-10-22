"""OCR-AM-15: one request_id across HTTP v1, MCP and jobs; per-principal rate; content-free logs."""
import ast
import asyncio
import logging
import pathlib
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.routing import Mount

from app import observability
from app.application import JobApplication, get_job_application
from app.config import settings
from app.main import app
from app.mcp_server import build_mcp_server
from app.observability import (ContentFreeFormatter, CorrelationFilter, PrincipalRateLimiter, bound,
                               current_request_id, redact)
from app.services.auth_service import create_token
from app.services.storage_service import JobStore
from app.services.user_store import get_user_store

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def principal(tmp_path):
    user = get_user_store().create_user("Observed", f"{uuid4().hex}@test.invalid", "synthetic-test-password")
    token = create_token(user["id"], user["email"], user["role"], settings.jwt_secret)
    store = JobStore(root_dir=tmp_path)
    service = JobApplication(store)
    app.dependency_overrides[get_job_application] = lambda: service
    try:
        yield service, token
    finally:
        app.dependency_overrides.pop(get_job_application, None)
        store._conn.close()


def _render(record_factory, formatter=None):
    record = record_factory()
    CorrelationFilter().filter(record)
    return (formatter or ContentFreeFormatter(fmt=observability.LOG_FORMAT)).format(record)


# ── redaction ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("secret, leaked", [
    ("Authorization: Bearer abc.def-ghi", "abc.def-ghi"),
    ("credential ocr_svc_Zm9vYmFyMTIz used", "Zm9vYmFyMTIz"),
    ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl here", "c2lnbmF0dXJl"),
    ("password=hunter2&next=1", "hunter2"),
    ('{"client_secret": "s3cr3t-value"}', "s3cr3t-value"),
    ("api_key: k-123456", "k-123456"),
    ("owner person.name@example.org changed", "person.name@example.org"),
])
def test_known_secret_shapes_are_masked(secret, leaked):
    assert leaked not in redact(secret)


def test_lines_are_capped_and_ids_survive():
    request_id = uuid4().hex
    assert request_id in redact(f"request {request_id} tokens=12")
    assert redact("x" * 5000).endswith("[truncated]")
    assert len(redact("x" * 5000)) < 2100


def test_exception_messages_never_reach_output():
    def record():
        try:
            try:
                raise ValueError("CONTRATO CONFIDENCIAL página 3")
            except ValueError as inner:
                raise RuntimeError("Bearer leaked-token") from inner
        except RuntimeError:
            import sys
            return logging.LogRecord("app.x", logging.ERROR, __file__, 1, "Worker failed for job %s", ("a" * 32,),
                                     sys.exc_info())
    text = _render(record)
    assert "CONFIDENCIAL" not in text and "leaked-token" not in text
    assert "builtins.RuntimeError" in text and "caused by builtins.ValueError" in text
    assert "test_observability.py" in text


def test_uvicorn_style_formatter_is_wrapped_and_query_strings_dropped():
    inner = logging.Formatter('%(message)s')
    record = lambda: logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                                       ("127.0.0.1:1", "GET", "/api/v1/jobs?filename=Contrato%20Sigiloso&token=x", "1.1", 200), None)
    text = _render(record, ContentFreeFormatter(inner=inner))
    assert "Sigiloso" not in text and "/api/v1/jobs?[query-redacted]" in text


def test_install_is_idempotent():
    observability.install_logging()
    observability.install_logging()
    guarded = [h for h in logging.getLogger().handlers if getattr(h, "_ocr_content_free", False)]
    assert len(guarded) == 1


# ── rate per principal ─────────────────────────────────────────────────────────────────

def test_token_bucket_per_subject():
    now = [0.0]
    limiter = PrincipalRateLimiter(3, clock=lambda: now[0])
    assert [limiter.admit("user:1") for _ in range(3)] == [None, None, None]
    assert limiter.admit("user:1") == 20
    assert limiter.admit("service:other") is None
    now[0] = 20.0
    assert limiter.admit("user:1") is None and limiter.admit("user:1") is not None
    assert PrincipalRateLimiter(0).admit("user:1") is None


def test_idle_full_buckets_are_dropped_at_capacity():
    now = [0.0]
    limiter = PrincipalRateLimiter(60, clock=lambda: now[0], max_subjects=2)
    limiter.admit("a"); limiter.admit("b")
    now[0] = 120.0
    limiter.admit("c")
    assert set(limiter._buckets) == {"c"}


def test_subject_count_stays_bounded_when_no_bucket_is_full():
    now = [0.0]
    limiter = PrincipalRateLimiter(60, clock=lambda: now[0], max_subjects=2)
    for subject in ("a", "b", "c", "d"):
        now[0] += 0.1
        limiter.admit(subject)
    assert set(limiter._buckets) == {"c", "d"}


def test_http_v1_limit_returns_rate_limited_with_retry_after(principal, monkeypatch):
    _, token = principal
    monkeypatch.setattr(settings, "principal_requests_per_minute", 2)
    client, headers = TestClient(app), {"Authorization": f"Bearer {token}"}
    assert [client.get("/api/v1/me", headers=headers).status_code for _ in range(2)] == [200, 200]
    denied = client.get("/api/v1/me", headers=headers)
    assert denied.status_code == 429
    assert denied.json()["error"]["code"] == "rate_limited"
    assert int(denied.headers["Retry-After"]) >= 1
    assert denied.headers["X-Request-ID"] == denied.json()["request_id"]


def test_mcp_shares_the_principal_limit(principal, monkeypatch):
    service, token = principal
    monkeypatch.setattr(settings, "principal_requests_per_minute", 1)
    server, transport = build_mcp_server(lambda: service)
    asgi_app = Starlette(routes=[Mount("/", app=transport)])

    async def scenario():
        async with server.session_manager.run():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=asgi_app)) as http:
                body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
                headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
                first = await http.post("http://localhost/mcp", json=body, headers=headers)
                second = await http.post("http://localhost/mcp", json=body, headers=headers)
                return first, second
    first, second = asyncio.run(scenario())
    assert first.status_code == 200
    assert second.status_code == 429 and second.json()["error"]["code"] == "rate_limited"
    assert "Retry-After" in second.headers and len(second.headers["X-Request-ID"]) == 32


# ── correlation ────────────────────────────────────────────────────────────────────────

def test_mcp_tool_runs_under_the_response_request_id(principal):
    service, token = principal
    seen, returned = [], []
    original = service.whoami

    def whoami(p):
        seen.append(current_request_id())
        return original(p)
    service.whoami = whoami
    server, transport = build_mcp_server(lambda: service)
    asgi_app = Starlette(routes=[Mount("/", app=transport)])

    async def capture(response):
        returned.append(response.headers.get("X-Request-ID"))

    async def scenario():
        async with server.session_manager.run():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=asgi_app),
                                          headers={"Authorization": f"Bearer {token}"},
                                          event_hooks={"response": [capture]}) as http:
                async with Client(streamable_http_client("http://localhost/mcp", http_client=http)) as client:
                    result = await client.call_tool("ocr_whoami", {})
                    assert not result.is_error
    asyncio.run(scenario())
    assert len(seen) == 1 and seen[0] in returned
    assert len(set(returned)) == len(returned), "each MCP HTTP request gets its own ID"


def test_http_v1_id_is_bound_for_the_handler(principal):
    service, token = principal
    seen = []
    original = service.whoami

    def whoami(p):
        seen.append(current_request_id())
        return original(p)
    service.whoami = whoami
    response = TestClient(app).get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200 and seen == [response.headers["X-Request-ID"]]
    assert current_request_id() is None


def test_job_keeps_origin_request_id_across_restart(tmp_path):
    store = JobStore(root_dir=tmp_path)
    with bound(request_id="a" * 32):
        admitted = store.create("synthetic.pdf", b"%PDF-synthetic", owner_user_id=1)
    legacy = store.create("legacy.pdf", b"%PDF-synthetic", owner_user_id=1)
    store.update(admitted.job_id, progress_percent=50)
    store._conn.close()
    reloaded = JobStore(root_dir=tmp_path)
    try:
        assert reloaded.get(admitted.job_id).origin_request_id == "a" * 32
        assert reloaded.get(legacy.job_id).origin_request_id is None
    finally:
        reloaded._conn.close()


def test_worker_logs_carry_origin_request_id_and_job_id(tmp_path, caplog):
    from app.services.job_queue import JobQueueProcessor
    store = JobStore(root_dir=tmp_path)
    with bound(request_id="b" * 32):
        job = store.create("synthetic.pdf", b"%PDF-synthetic", owner_user_id=1)
    processor = JobQueueProcessor(store, worker_count=1)
    captured = []

    class Orchestrator:
        def process_job(self, job_id):
            captured.append((current_request_id(), observability.job_id_var.get()))
            raise RuntimeError("document text must not be logged")
    processor.orchestrator = Orchestrator()
    processor._queue.put(job.job_id)
    import threading
    stopper = threading.Timer(1.5, processor._stop_event.set)
    stopper.start()
    with caplog.at_level(logging.ERROR):
        processor._worker_loop()
    store._conn.close()
    assert captured == [("b" * 32, job.job_id)]
    record = next(r for r in caplog.records if "Worker could not finish" in r.getMessage())
    rendered = ContentFreeFormatter(fmt=observability.LOG_FORMAT).format(record)
    assert f"request_id={'b' * 32}" in rendered and f"job_id={job.job_id}" in rendered
    assert "document text" not in rendered


# ── static guard ───────────────────────────────────────────────────────────────────────

FORBIDDEN = {"text", "content", "body", "payload", "password", "token", "secret", "email", "quote",
             "prompt", "messages", "value", "values", "filename", "page_text", "raw", "credentials"}


def _names(node):
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            yield child.id
        elif isinstance(child, ast.Attribute):
            yield child.attr
        elif isinstance(child, ast.Subscript) and isinstance(child.slice, ast.Constant):
            yield str(child.slice.value)


def test_log_calls_do_not_pass_content_or_credentials():
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and call.func.attr in ("debug", "info", "warning", "error", "exception", "critical", "log")
                    and isinstance(call.func.value, ast.Name) and call.func.value.id in ("logger", "logging", "log")):
                continue
            for argument in call.args[1:]:
                hit = FORBIDDEN.intersection(_names(argument))
                if hit:
                    offenders.append(f"{path.relative_to(ROOT)}:{call.lineno} {sorted(hit)}")
            if isinstance(call.args[0] if call.args else None, ast.JoinedStr):
                offenders.append(f"{path.relative_to(ROOT)}:{call.lineno} f-string message")
    assert offenders == []
