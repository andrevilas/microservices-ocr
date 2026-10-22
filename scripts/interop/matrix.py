"""OCR-AM-16 interoperability matrix: real clients against a running OCR API. Synthetic PDFs only.

  python scripts/interop/matrix.py --api http://127.0.0.1:18080 --admin-env <server.env> --out <dir> \
      [--restart "docker restart ocr-am16"] [--rate-limit 120]
  python scripts/interop/matrix.py --api https://<host> --tokens-file <tokens.json> --out <dir> --rate-limit 600

Each protocol client gets its own service principal, so ownership and isolation are real. The admin
password is read from the server env file and never printed; issued tokens live in memory, except the
stdio client's, which needs a private file that is deleted at the end. results.json holds no secret.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from pypdf import PdfReader
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[2]
FULL = ["jobs:read", "jobs:submit", "jobs:cancel", "artifacts:read", "artifacts:delete"]
CLIENTS = ("http", "mcp-http", "mcp-http-legacy", "mcp-stdio")
MARKER = "Interop sintetico 2026 TrueNAS"


def synthetic_pdf(pages: int = 1, marker: str = MARKER) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    for page in range(pages):
        pdf.setFont("Helvetica", 18)
        pdf.drawString(50, 760, f"{marker} pagina {page + 1}")
        pdf.drawString(50, 720, "Documento sintetico para homologacao de clientes.")
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


class Recorder:
    def __init__(self):
        self.rows: list[dict] = []

    def check(self, client: str, name: str, ok: bool, **detail):
        self.rows.append({"client": client, "check": name, "ok": bool(ok), **detail})
        print(("PASS " if ok else "FAIL ") + client + " " + name, flush=True)
        return ok


# ── provisioning (admin JWT over HTTP) ───────────────────────────────────────────────────────

def admin_token(http, env_file: Path) -> str:
    values = dict(line.split("=", 1) for line in env_file.read_text().splitlines() if "=" in line)
    response = http.post("/auth/login", json={"email": values["ADMIN_EMAIL"], "password": values["ADMIN_PASSWORD"]})
    assert response.status_code == 200, response.status_code
    return response.json()["access_token"]


def issue(http, admin: str, name: str, scopes: list[str]) -> tuple[str, str]:
    headers = {"Authorization": "Bearer " + admin}
    principal = http.post("/api/v1/service-principals", json={"name": name}, headers=headers)
    assert principal.status_code in (200, 201), principal.text[:200]
    pid = principal.json()["id"]
    issued = http.post(f"/api/v1/service-principals/{pid}/credentials",
                       json={"name": name, "scopes": scopes, "expires_in_seconds": 3600}, headers=headers)
    assert issued.status_code in (200, 201), issued.text[:200]
    return pid, issued.json()["token"]


# ── one interface, four transports ───────────────────────────────────────────────────────────

class HttpOps:
    """Plain HTTP v1 client."""

    def __init__(self, api, token):
        self.http = httpx2.Client(base_url=api, trust_env=False, timeout=60, headers={"Authorization": "Bearer " + token})

    async def call(self, op, **a):
        h = self.http
        if op == "whoami":
            return self._json(h.get("/api/v1/me"))
        if op == "capabilities":
            return self._json(h.get("/api/v1/capabilities"))
        if op == "prepare_upload":
            return self._json(h.post("/api/v1/upload-sessions", json={"filename": a["filename"], "size_bytes": a["size_bytes"],
                                                                        "sha256": a["sha256"]}))
        if op == "submit_job":
            return self._json(h.post("/api/v1/jobs", json={"upload_id": a["upload_id"], "options": a.get("options") or {}},
                                     headers={"Idempotency-Key": a["idempotency_key"]}))
        if op == "get_job":
            return self._json(h.get(f"/api/v1/jobs/{a['job_id']}"))
        if op == "list_jobs":
            return self._json(h.get("/api/v1/jobs", params={"limit": 50}))
        if op == "get_text":
            return self._json(h.get(f"/api/v1/jobs/{a['job_id']}/text"))
        if op == "get_report":
            return self._json(h.get(f"/api/v1/jobs/{a['job_id']}/report"))
        if op == "get_artifact":
            return self._json(h.get(f"/api/v1/jobs/{a['job_id']}/artifact"))
        if op == "cancel_job":
            return self._json(h.post(f"/api/v1/jobs/{a['job_id']}/cancel"))
        if op == "retry_job":
            return self._json(h.post(f"/api/v1/jobs/{a['job_id']}/retry", json={},
                                     headers={"Idempotency-Key": a["idempotency_key"]}))
        if op == "delete_job_artifacts":
            return self._json(h.delete(f"/api/v1/jobs/{a['job_id']}/artifacts"))
        raise KeyError(op)

    @staticmethod
    def _json(response):
        body = response.json() if response.content else {}
        if response.status_code >= 400:
            return {"_error": response.status_code, "_code": (body.get("error") or {}).get("code")}
        return body

    async def close(self):
        self.http.close()


class McpOps:
    """MCP client session (Streamable HTTP or stdio); errors come back as tool errors."""

    TOOL = {"capabilities": "get_capabilities"}  # HTTP-flavoured op name -> MCP tool suffix

    def __init__(self, client):
        self.client = client

    async def call(self, op, **a):
        result = await self.client.call_tool("ocr_" + self.TOOL.get(op, op), a)
        if result.is_error:
            text = " ".join(getattr(c, "text", "") for c in result.content)
            return {"_error": "tool", "_code": next((w for w in text.replace('"', " ").replace("'", " ").split()
                                                     if "_" in w and w.islower()), None), "_text": text[:300]}
        return result.structured_content

    async def tools(self):
        return [t.name for t in (await self.client.list_tools()).tools]


async def open_client(kind, api, token, private: Path):
    """Return (ops, closer). MCP clients are entered here and exited by the closer."""
    if kind == "http":
        ops = HttpOps(api, token)
        return ops, ops.close
    from contextlib import AsyncExitStack
    stack = AsyncExitStack()
    if kind in ("mcp-http", "mcp-http-legacy"):
        http = await stack.enter_async_context(httpx2.AsyncClient(headers={"Authorization": "Bearer " + token},
                                                                  trust_env=False, timeout=60))
        transport = streamable_http_client(api + "/mcp", http_client=http)
        mode = "legacy" if kind.endswith("legacy") else "auto"
    else:
        credential = private / "stdio-credential.txt"
        credential.write_text(token, encoding="utf-8")
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "scripts" / "ocr_mcp.py")],
                                       env={**os.environ, "OCR_API_URL": api, "OCR_API_TOKEN": "",
                                            "OCR_API_TOKEN_FILE": str(credential), "PYTHONUTF8": "1"})
        log = stack.enter_context((private / "stdio.log").open("w"))
        transport = stdio_client(params, errlog=log)
        mode = "auto"
    client = await stack.enter_async_context(Client(transport, mode=mode))
    return McpOps(client), stack.aclose


# ── scenarios ───────────────────────────────────────────────────────────────────────────────

def put_bytes(api, token, receipt, data) -> int:
    """MCP hosts cannot carry PDF bytes: the client side transfers them over HTTP with the same identity."""
    url = receipt["content_url"] if receipt["content_url"].startswith("http") else api + receipt["content_url"]
    with httpx2.Client(trust_env=False, timeout=60) as http:
        return http.put(url, content=data, headers={"Authorization": "Bearer " + token,
                                                     "Content-Type": "application/pdf"}).status_code


async def wait_terminal(ops, job_id, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = await ops.call("get_job", job_id=job_id)
        if job.get("status") in ("completed", "failed", "canceled"):
            return job
        await asyncio.sleep(1)
    return job


async def submit(ops, api, token, name, data, options=None):
    receipt = await ops.call("prepare_upload", filename=name, size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    assert "_error" not in receipt, receipt
    assert put_bytes(api, token, receipt, data) == 200
    key = "interop-" + receipt["upload_id"]
    args = {"upload_id": receipt["upload_id"], "idempotency_key": key}
    if options:
        args["options"] = options
    return await ops.call("submit_job", **args), args


async def flow(kind, api, token, other_token, readonly_token, rec: Recorder, private: Path, state: dict):
    ops, close = await open_client(kind, api, token, private)
    try:
        if isinstance(ops, McpOps):
            names = await ops.tools()
            rec.check(kind, "tools_list", len(names) == 63 and "ocr_submit_job" in names, tools=len(names),
                      protocol_version=str(ops.client.protocol_version), server=getattr(ops.client.server_info, "version", None))
        me = await ops.call("whoami")
        rec.check(kind, "whoami_service_identity", me.get("kind") == "service" and "jobs:submit" in me.get("scopes", []))
        caps = await ops.call("capabilities")
        rec.check(kind, "capabilities", caps.get("compression_profiles") == ["none", "balanced", "preservation"]
                  and "pdfa" in caps.get("output_formats", []))
        data = synthetic_pdf(2)
        admission, args = await submit(ops, api, token, f"interop-{kind}.pdf", data, {"languages": ["por", "eng"]})
        job_id = admission.get("job_id")
        rec.check(kind, "submit_job", bool(job_id), status=admission.get("status"))
        replay = await ops.call("submit_job", **args)
        rec.check(kind, "submit_idempotent_replay", replay.get("job_id") == job_id)
        job = await wait_terminal(ops, job_id)
        rec.check(kind, "job_completed", job.get("status") == "completed", status=job.get("status"))
        text = await ops.call("get_text", job_id=job_id)
        rec.check(kind, "get_text", MARKER in (text.get("text") or ""))
        report = await ops.call("get_report", job_id=job_id)
        validation = report.get("output_validation") or {}
        rec.check(kind, "get_report_pdfa_passed", validation.get("pdfa_conformance") == "passed"
                  and report.get("page_count") == 2)
        artifact = await ops.call("get_artifact", job_id=job_id)
        with httpx2.Client(base_url=api, trust_env=False, timeout=60) as http:
            download = http.get(f"/api/v1/jobs/{job_id}/artifact/content", headers={"Authorization": "Bearer " + token})
            foreign = http.get(f"/api/v1/jobs/{job_id}/artifact/content", headers={"Authorization": "Bearer " + other_token})
        rec.check(kind, "download_matches_validated_hash", download.status_code == 200
                  and hashlib.sha256(download.content).hexdigest() == validation.get("pdfa_validated_sha256")
                  and len(PdfReader(io.BytesIO(download.content)).pages) == 2 and "_error" not in artifact)
        rec.check(kind, "other_owner_download_denied", foreign.status_code == 404, status=foreign.status_code)
        listed = [j["job_id"] for j in (await ops.call("list_jobs")).get("items", [])]
        rec.check(kind, "list_jobs_owned_only", job_id in listed and state["foreign_job"] not in listed, listed=len(listed))
        # Isolation through this same client type, with the other principal's identity.
        other, close_other = await open_client(kind, api, other_token, private)
        try:
            denied = await other.call("get_job", job_id=job_id)
            rec.check(kind, "other_owner_get_job_denied", "_error" in denied, code=denied.get("_code"))
            denied = await other.call("cancel_job", job_id=job_id)
            rec.check(kind, "other_owner_cancel_denied", "_error" in denied, code=denied.get("_code"))
        finally:
            await close_other()
        reader, close_reader = await open_client(kind, api, readonly_token, private)
        try:
            receipt = await reader.call("prepare_upload", filename="ro.pdf", size_bytes=10, sha256=None)
            rec.check(kind, "readonly_scope_cannot_upload", "_error" in receipt, code=receipt.get("_code"))
        finally:
            await close_reader()
        refused, _ = await submit(ops, api, token, f"interop-{kind}-economic.pdf", data,
                                  {"languages": ["eng"], "compression_profile": "economic"})
        rec.check(kind, "withdrawn_preset_refused", "_error" in refused
                  and "compression_profile_unavailable" in json.dumps(refused), code=refused.get("_code"))
        receipt = await ops.call("prepare_upload", filename="bad.pdf", size_bytes=9, sha256=None)
        status = put_bytes(api, token, receipt, b"not a pdf")
        rec.check(kind, "invalid_pdf_refused", status == 422, status=status)
        # Cancel a queued job (a long document keeps the single worker busy), then retry and delete.
        long_job, _ = await submit(ops, api, token, f"interop-{kind}-long.pdf", synthetic_pdf(40), {"languages": ["eng"]})
        queued, _ = await submit(ops, api, token, f"interop-{kind}-cancel.pdf", synthetic_pdf(1), {"languages": ["eng"]})
        canceled = await ops.call("cancel_job", job_id=queued["job_id"])
        final = await wait_terminal(ops, queued["job_id"])
        rec.check(kind, "cancel_job", "_error" not in canceled and final.get("status") == "canceled",
                  status=final.get("status"))
        retried = await ops.call("retry_job", job_id=queued["job_id"], idempotency_key="retry-" + queued["job_id"])
        retry_id = retried.get("job_id") or (retried.get("job") or {}).get("job_id")
        retried_final = await wait_terminal(ops, retry_id) if retry_id else {}
        rec.check(kind, "retry_job", retried_final.get("status") == "completed", status=retried_final.get("status"))
        await wait_terminal(ops, long_job["job_id"], timeout=600)
        deleted = await ops.call("delete_job_artifacts", job_id=job_id)
        with httpx2.Client(base_url=api, trust_env=False, timeout=60) as http:
            gone = http.get(f"/api/v1/jobs/{job_id}/artifact/content", headers={"Authorization": "Bearer " + token})
        rec.check(kind, "delete_artifacts", "_error" not in deleted and gone.status_code == 410, status=gone.status_code)
        if state["restart"]:
            state["pending_after_restart"][kind] = await submit(ops, api, token, f"interop-{kind}-restart.pdf",
                                                                 synthetic_pdf(30), {"languages": ["eng"]})
    finally:
        await close()


async def after_restart(kind, api, token, rec, private, state):
    """A fresh session after the server restarted: the job admitted before keeps its identity and finishes."""
    admission, args = state["pending_after_restart"][kind]
    ops, close = await open_client(kind, api, token, private)
    try:
        job = await wait_terminal(ops, admission["job_id"], timeout=900)
        rec.check(kind, "restart_job_survives", job.get("status") == "completed", status=job.get("status"))
        replay = await ops.call("submit_job", **args)
        rec.check(kind, "restart_replay_same_job", replay.get("job_id") == admission["job_id"])
    finally:
        await close()


def generic(api, token, other_token, rate_limit, rec: Recorder):
    with httpx2.Client(base_url=api, trust_env=False, timeout=30) as http:
        r = http.get("/api/v1/jobs")
        rec.check("http", "no_token_401", r.status_code == 401 and bool(r.headers.get("x-request-id")))
        r = http.get("/api/v1/jobs", headers={"Authorization": "Bearer ocr_svc_invalid"})
        rec.check("http", "invalid_token_401", r.status_code == 401)
        r = http.post("/mcp", headers={"Authorization": "Bearer ocr_svc_invalid", "Accept": "application/json, text/event-stream"},
                      json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        rec.check("mcp-http", "invalid_token_401", r.status_code == 401 and bool(r.headers.get("x-request-id")))
        if rate_limit:
            # The bucket refills at rate/60 per second: send in parallel so the burst outpaces the refill.
            from concurrent.futures import ThreadPoolExecutor
            def hit(_):
                r = http.get("/api/v1/me", headers={"Authorization": "Bearer " + other_token})
                return r.status_code, r.headers.get("retry-after")
            with ThreadPoolExecutor(max_workers=32) as pool:
                results = list(pool.map(hit, range(rate_limit + 200)))
            limited = [retry for status, retry in results if status == 429]
            rec.check("http", "per_principal_rate_limit_429", bool(limited) and all(limited),
                      requests=len(results), refused=len(limited))
            r = http.get("/api/v1/me", headers={"Authorization": "Bearer " + token})
            rec.check("http", "rate_limit_is_per_principal", r.status_code == 200)


async def main_async(args):
    out = Path(args.out)
    private = out / "private"
    private.mkdir(parents=True, exist_ok=True)
    rec, state = Recorder(), {"pending_after_restart": {}, "restart": bool(args.restart)}
    if args.tokens_file:
        # Pre-issued (e.g. inside the production pod): {"<client>"|"other"|"readonly"|"rate": token}
        issued = json.loads(Path(args.tokens_file).read_text(encoding="utf-8"))
        tokens = {kind: ("", issued[kind]) for kind in CLIENTS}
        other, readonly, limited = (("", issued[k]) for k in ("other", "readonly", "rate"))
    else:
        with httpx2.Client(base_url=args.api, trust_env=False, timeout=30) as http:
            admin = admin_token(http, Path(args.admin_env))
            tokens = {kind: issue(http, admin, "interop-" + kind, FULL) for kind in CLIENTS}
            other = issue(http, admin, "interop-other-owner", FULL)
            readonly = issue(http, admin, "interop-readonly", ["jobs:read"])
            limited = issue(http, admin, "interop-rate", ["jobs:read"])
    # A job of the other owner, which no client may see in its own listing.
    foreign_ops = HttpOps(args.api, other[1])
    foreign, _ = await submit(foreign_ops, args.api, other[1], "interop-foreign.pdf", synthetic_pdf(1), {"languages": ["eng"]})
    await foreign_ops.close()
    state["foreign_job"] = foreign["job_id"]
    clients = CLIENTS
    if args.public_mcp_refused:
        # The main app's /mcp only serves loopback hosts (DNS-rebinding protection); remote MCP is the
        # separate OAuth entrypoint (app.remote, AM-13). Through a public hostname, expect a refusal.
        clients = tuple(k for k in CLIENTS if not k.startswith("mcp-http"))
        for version in ("2026-07-28", "2025-11-25"):
            r = httpx2.post(args.api + "/mcp", trust_env=False, timeout=30, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "am16", "version": "1"}}},
                headers={"Authorization": "Bearer " + tokens["mcp-http"][1], "MCP-Protocol-Version": version,
                         "Accept": "application/json, text/event-stream"})
            rec.check("mcp-http", "public_host_refused_" + version, r.status_code == 421, status=r.status_code)
    try:
        for kind in clients:
            try:
                await flow(kind, args.api, tokens[kind][1], other[1], readonly[1], rec, private, state)
            except Exception as exc:  # A client failure is a matrix result, not a reason to stop the others.
                rec.check(kind, "flow_aborted", False, error=type(exc).__name__ + ": " + str(exc)[:300])
        generic(args.api, tokens["http"][1], limited[1], args.rate_limit, rec)
        if args.restart:
            subprocess.run(shlex.split(args.restart), check=True, capture_output=True, timeout=120)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    if httpx2.get(args.api + "/health", trust_env=False, timeout=5).status_code == 200:
                        break
                except Exception:
                    pass
                time.sleep(2)
            for kind in clients:
                if kind in state["pending_after_restart"]:
                    try:
                        await after_restart(kind, args.api, tokens[kind][1], rec, private, state)
                    except Exception as exc:
                        rec.check(kind, "restart_aborted", False, error=type(exc).__name__ + ": " + str(exc)[:300])
    finally:
        for path in private.glob("*credential*"):
            path.unlink()
    log = (private / "stdio.log")
    secrets_in_log = log.is_file() and any(t[1] in log.read_text(errors="replace") for t in [*tokens.values(), other, readonly])
    rec.check("mcp-stdio", "stdio_log_has_no_token", not secrets_in_log)
    summary = {}
    for row in rec.rows:
        entry = summary.setdefault(row["client"], {"passed": 0, "failed": []})
        if row["ok"]:
            entry["passed"] += 1
        else:
            entry["failed"].append(row["check"])
    (out / "results.json").write_text(json.dumps({"api": args.api, "clients": CLIENTS, "summary": summary,
                                                  "checks": rec.rows}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=1))
    return 0 if all(not v["failed"] for v in summary.values()) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--admin-env", help="server env file with ADMIN_EMAIL/ADMIN_PASSWORD (local server)")
    group.add_argument("--tokens-file", help="JSON of pre-issued service tokens (remote server)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--restart")
    parser.add_argument("--rate-limit", type=int, default=0)
    parser.add_argument("--public-mcp-refused", action="store_true",
                        help="remote hostname: check that /mcp refuses it instead of running the MCP HTTP flows")
    sys.exit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
