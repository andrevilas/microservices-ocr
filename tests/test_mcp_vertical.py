"""Real SDK clients: HTTP and child-process stdio through the persisted OCR runtime."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver.exceptions import ToolError
from pypdf import PdfReader
import pytest

from app.mcp_stdio import ApiClient, MAX_RESPONSE_BYTES, configuration
from test_upload_admission import pdf, system, upload

ROOT = Path(__file__).resolve().parents[1]


class Runtime:
    def __init__(self, root):
        self.root = root
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.origin = f"http://127.0.0.1:{self.port}"
        self.env = {**os.environ, "OCR_TMP_DIR": str(root / "data"),
                    "USER_DB_PATH": str(root / "users.sqlite3"),
                    "JWT_SECRET": "synthetic-test-jwt-secret-not-a-production-credential",
                    "ADMIN_PASSWORD": "synthetic-test-admin-password", "JOB_WORKER_CONCURRENCY": "1",
                    "PYTHONPATH": str(ROOT / "tests"), "OCR_TEST_MARKER": str(root / "processing")}
        self.process = None
        # Synthetic identities only, stored inside this test's temporary directory.
        setup = subprocess.run([sys.executable, "-c", """
import json
from app.services.identity_store import get_identity_store
s = get_identity_store()
tokens=[]
for n in range(2):
    identity=s.create_service('Synthetic MCP '+str(n), 'user:1')
    tokens.append(s.issue_credential(identity['id'], 'Test', ['jobs:read','jobs:submit','artifacts:read','jobs:cancel'], 3600, 'user:1')[1])
print(json.dumps(tokens))
"""], cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=20)
        assert setup.returncode == 0, "Synthetic identity setup failed"
        self.tokens = json.loads(setup.stdout)

    def start(self, pause=False, export_pause=False, control_pause=False):
        self.log = (self.root / "runtime.log").open("ab")
        self.process = subprocess.Popen([sys.executable, "-m", "uvicorn", "mcp_runtime_fixture:app",
                                         "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"],
                                        cwd=ROOT, env={**self.env, "OCR_TEST_PAUSE": "1" if pause else "0", "OCR_TEST_EXPORT_PAUSE": "1" if export_pause else "0", "OCR_TEST_CONTROL_PAUSE": "1" if control_pause else "0"},
                                        stdout=self.log, stderr=self.log,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.monotonic() + 20
        with httpx2.Client(trust_env=False, timeout=1) as client:
            while time.monotonic() < deadline:
                assert self.process.poll() is None, "Synthetic runtime exited during startup"
                try:
                    if client.get(self.origin + "/health").status_code == 200:
                        return
                except httpx2.HTTPError:
                    pass
                time.sleep(0.05)
        raise AssertionError("Synthetic runtime startup timed out")

    def stop(self):
        if self.process is not None:
            self.process.kill()
            self.process.wait(timeout=10)
            self.process = None
            self.log.close()


@pytest.fixture
def runtime(tmp_path):
    runtime = Runtime(tmp_path)
    try:
        yield runtime
    finally:
        runtime.stop()


@asynccontextmanager
async def connected(runtime, transport, mode, token=None):
    token = token or runtime.tokens[0]
    if transport == "stdio":
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "app.mcp_stdio"], cwd=ROOT,
                                           env={"OCR_API_URL": runtime.origin, "OCR_API_TOKEN": token,
                                                "OCR_TMP_DIR": str(runtime.root / "stdio-must-not-create")})
        with (runtime.root / "stdio.log").open("a") as error_log:
            async with Client(stdio_client(parameters, errlog=error_log), mode=mode) as client:
                yield client
    else:
        async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token}, trust_env=False) as http:
            async with Client(streamable_http_client(runtime.origin + "/mcp", http_client=http), mode=mode) as client:
                yield client


async def call(client, name, args=None):
    result = await client.call_tool(name, args or {})
    assert not result.is_error, result.content
    return result.structured_content


async def complete(client, job_id):
    async with asyncio.timeout(30):
        while True:
            job = await call(client, "ocr_get_job", {"job_id": job_id})
            assert job["status"] != "failed", job
            if job["status"] == "completed":
                return job
            await asyncio.sleep(0.1)


@pytest.mark.parametrize("transport,mode", [("http", "auto"), ("http", "legacy"), ("stdio", "auto"), ("stdio", "legacy")])
def test_real_clients_upload_submit_poll_text_pdf_and_isolation(runtime, transport, mode):
    runtime.start()

    async def scenario():
        async with connected(runtime, transport, mode) as client:
            listing = await client.list_tools()
            tools = {t.name: t for t in listing.tools}
            assert len(tools) == 19
            assert not tools["ocr_prepare_upload"].annotations.idempotent_hint
            assert tools["ocr_submit_job"].annotations.idempotent_hint
            assert all(t.output_schema for t in tools.values())
            assert all(t.input_schema["additionalProperties"] is False for t in tools.values())
            for name, args in [("ocr_prepare_upload", {"filename": "PRIVATE", "size_bytes": "PRIVATE"}),
                               ("ocr_prepare_upload", {"filename": "safe.pdf", "size_bytes": 100, "path": "PRIVATE"}),
                               ("ocr_submit_job", {"upload_id": "0" * 32, "idempotency_key": "PRIVATE\r\nheader"})]:
                invalid = await client.call_tool(name, args)
                assert invalid.is_error and "validation_error" in invalid.content[0].text
                assert "PRIVATE" not in invalid.content[0].text
            data = pdf(2)
            upload = await call(client, "ocr_prepare_upload", {"filename": "synthetic.pdf", "size_bytes": len(data),
                                                               "sha256": hashlib.sha256(data).hexdigest()})
            async with httpx2.AsyncClient(base_url=runtime.origin, headers={"Authorization": "Bearer " + runtime.tokens[0]}, trust_env=False) as http:
                response = await http.put(upload["content_url"], content=data, headers={"Content-Type": "application/pdf"})
                assert response.status_code == 200
                ready = await call(client, "ocr_get_upload", {"upload_id": upload["upload_id"]})
                assert ready == response.json()
                args = {"upload_id": upload["upload_id"], "idempotency_key": "mcp-vertical-key", "options": {"processing_mode": "extract_text"}}
                receipt = await call(client, "ocr_submit_job", args)
                job_id = receipt["job_id"]
                assert await call(client, "ocr_submit_job", args) == receipt
                # Cross-transport replay has identical persisted admission.
                replay = await http.post("/api/v1/jobs", json={k: v for k, v in args.items() if k != "idempotency_key"},
                                         headers={"Idempotency-Key": args["idempotency_key"]})
                assert replay.status_code == 202 and replay.json() == receipt
                assert replay.headers["idempotency-replayed"] == "true"
                conflict = await client.call_tool("ocr_submit_job", {**args, "options": {"processing_mode": "preserve_existing_text"}})
                assert conflict.is_error and "idempotency_conflict" in conflict.content[0].text
                await complete(client, job_id)
                text = await call(client, "ocr_get_text", {"job_id": job_id, "page": 2})
                assert "Synthetic upload page 1" in text["text"]
                report = await call(client, "ocr_get_report", {"job_id": job_id})
                artifact = await call(client, "ocr_get_artifact", {"job_id": job_id})
                output = await http.get(artifact["download_url"])
                assert output.status_code == 200
                assert hashlib.sha256(output.content).hexdigest() == report["output"]["sha256"]
                assert len(PdfReader(BytesIO(output.content)).pages) == 2
                assert len((await call(client, "ocr_list_jobs"))["items"]) == 1
                other_auth = {"Authorization": "Bearer " + runtime.tokens[1]}
                assert (await http.get(artifact["download_url"], headers=other_auth)).status_code == 404
            async with connected(runtime, transport, mode, runtime.tokens[1]) as other:
                for name, args in [("ocr_get_upload", {"upload_id": upload["upload_id"]}),
                                   ("ocr_submit_job", {"upload_id": upload["upload_id"], "idempotency_key": "other-key"}),
                                   *[(name, {"job_id": job_id}) for name in ("ocr_get_job", "ocr_get_text", "ocr_get_report", "ocr_get_artifact")]]:
                    denied = await other.call_tool(name, args)
                    assert denied.is_error and "not_found" in denied.content[0].text
                assert not (await call(other, "ocr_list_jobs"))["items"]
        assert not (runtime.root / "stdio-must-not-create").exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure,code", [(OSError("PRIVATE path"), "storage_unavailable"),
                                        (RuntimeError("PRIVATE traceback"), "internal_error")])
def test_mcp_sanitizes_service_errors(system, monkeypatch, failure, code):
    from app.application import JobApplication
    from app.mcp_server import build_mcp_server
    server, transport = build_mcp_server(lambda: JobApplication(system.jobs), lambda: system.uploads, lambda: system.queue)

    def fail(*_):
        raise failure
    monkeypatch.setattr(system.uploads, "reserve", fail)

    async def scenario():
        async with server.session_manager.run():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=transport), headers={"Authorization": "Bearer " + system.tokens[0]}) as http:
                async with Client(streamable_http_client("http://localhost/mcp", http_client=http)) as client:
                    result = await client.call_tool("ocr_prepare_upload", {"filename": "safe.pdf", "size_bytes": 100})
                    assert result.is_error and code in result.content[0].text
                    assert "PRIVATE" not in result.content[0].text
    asyncio.run(scenario())


def test_mcp_queue_failure_retry_and_submit_scope(system):
    from app.application import JobApplication
    from app.mcp_server import build_mcp_server
    ready = upload(system)
    server, transport = build_mcp_server(lambda: JobApplication(system.jobs), lambda: system.uploads, lambda: system.queue)
    no_submit = system.identities.issue_credential(system.services[0]["id"], "Reader", ["jobs:read"], 3600, "user:1")[1]

    async def scenario():
        async with server.session_manager.run():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=transport), headers={"Authorization": "Bearer " + system.tokens[0]}) as http:
                async with Client(streamable_http_client("http://localhost/mcp", http_client=http)) as client:
                    args = {"upload_id": ready["upload_id"], "idempotency_key": "dispatch-key", "options": {"processing_mode": "extract_text"}}
                    def unavailable(_):
                        raise RuntimeError("PRIVATE queue")
                    system.queue.enqueue = unavailable
                    failed = await client.call_tool("ocr_submit_job", args)
                    assert failed.is_error and "queue_unavailable" in failed.content[0].text
                    assert "PRIVATE" not in failed.content[0].text
                    assert len(system.jobs.snapshot()) == 1
                    system.queue.enqueue = system.queue.enqueued.append
                    retried = await call(client, "ocr_submit_job", args)
                    assert system.queue.enqueued == [retried["job_id"]]
                    assert len(system.jobs.snapshot()) == 1
                    http.headers["Authorization"] = "Bearer " + no_submit
                    for name, fields in [("ocr_submit_job", args), ("ocr_get_upload", {"upload_id": ready["upload_id"]}),
                                         ("ocr_prepare_upload", {"filename": "safe.pdf", "size_bytes": 100})]:
                        denied = await client.call_tool(name, fields)
                        assert denied.is_error and "insufficient_scope" in denied.content[0].text
    asyncio.run(scenario())


def test_disconnect_crash_recovery_and_one_runtime_owner(runtime):
    runtime.start(pause=True)

    async def submit():
        async with connected(runtime, "http", "auto") as client:
            data = pdf()
            upload = await call(client, "ocr_prepare_upload", {"filename": "restart.pdf", "size_bytes": len(data)})
            async with httpx2.AsyncClient(trust_env=False) as http:
                assert (await http.put(runtime.origin + upload["content_url"], content=data,
                                       headers={"Authorization": "Bearer " + runtime.tokens[0], "Content-Type": "application/pdf"})).status_code == 200
            args = {"upload_id": upload["upload_id"], "idempotency_key": "restart-key", "options": {"processing_mode": "extract_text"}}
            receipt = await call(client, "ocr_submit_job", args)
            async with asyncio.timeout(10):
                while not (runtime.root / "processing").exists():
                    await asyncio.sleep(0.05)
            return args, receipt

    args, receipt = asyncio.run(submit())  # MCP disconnected while persisted job is processing.
    second = subprocess.run([sys.executable, "-c", "from pathlib import Path; import sys; from app.services.runtime_lease import RuntimeLease; RuntimeLease(Path(sys.argv[1])).acquire()", str(runtime.root / "data")],
                            cwd=ROOT, env=runtime.env, capture_output=True, text=True, timeout=10)
    assert second.returncode != 0 and "already has an active server" in second.stderr
    runtime.stop()  # Abrupt process death at the synthetic processing crash point.
    runtime.start()

    async def recover():
        async with connected(runtime, "stdio", "auto") as client:
            await complete(client, receipt["job_id"])
            assert await call(client, "ocr_submit_job", args) == receipt
            assert "Synthetic upload" in (await call(client, "ocr_get_text", {"job_id": receipt["job_id"]}))["text"]
            assert len((await call(client, "ocr_list_jobs"))["items"]) == 1
    asyncio.run(recover())


def test_stdio_import_has_no_server_state_or_threads(tmp_path):
    code = """
import sqlite3, threading, sys
def forbidden(*a, **kw): raise AssertionError('Local database or worker created')
sqlite3.connect = forbidden
threading.Thread.start = forbidden
import app.mcp_stdio
from mcp.server import MCPServer
app.mcp_stdio.register_tools(MCPServer('test'), lambda *a: None)
assert not {'app.main','app.config','app.services.storage_service','app.services.job_queue'} & set(sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            env={**os.environ, "OCR_TMP_DIR": str(tmp_path / "forbidden")}, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "forbidden").exists()


@pytest.mark.parametrize("url", ["http://remote.invalid", "https://user:password@example.test", "https://example.test/path", "https://example.test?q=x", "https://example.test#x", "file:///tmp/data", "http://[bad", "http://localhost:0"])
def test_stdio_rejects_unsafe_configuration(monkeypatch, url):
    monkeypatch.setenv("OCR_API_URL", url)
    monkeypatch.setenv("OCR_API_TOKEN", "synthetic")
    with pytest.raises(ValueError):
        configuration()


@pytest.mark.parametrize("status,body,code", [(302, {}, "upstream_redirect"), (503, {"error": {"code": "storage_unavailable", "message": "PRIVATE"}}, "storage_unavailable"),
                                            (500, {"error": {"code": "PRIVATE/secret"}}, "upstream_error"), (200, [], "invalid_api_response")])
def test_stdio_sanitizes_upstream_errors(status, body, code):
    async def scenario():
        def handler(request):
            assert request.headers["Authorization"] == "Bearer synthetic"
            return httpx2.Response(status, json=body, headers={"Location": "https://other.invalid"})
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler), headers={"Authorization": "Bearer synthetic"}) as http:
            with pytest.raises(ToolError, match=code) as error:
                await ApiClient("http://localhost", http).invoke(None, "whoami")
            assert "PRIVATE" not in str(error.value)
    asyncio.run(scenario())


def test_stdio_limits_response_and_handles_connection_failure():
    async def scenario():
        for oversized in (True, False):
            def handler(request):
                if oversized:
                    return httpx2.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))
                raise httpx2.ConnectError("PRIVATE endpoint/token", request=request)
            async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
                with pytest.raises(ToolError, match="upstream_response_limit" if oversized else "api_unavailable") as error:
                    await ApiClient("http://localhost", http).invoke(None, "whoami")
                assert "PRIVATE" not in str(error.value)
    asyncio.run(scenario())
