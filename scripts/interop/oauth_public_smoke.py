"""OCR-AM-13 public smoke: real browser PKCE login through the published hostnames.

Uses only the synthetic realm user/client prepared for the smoke (ocr-smoke). Writes a
report without codes, tokens or passwords. The password is read from a private file.
"""
import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import secrets
import threading
import time
import traceback
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from playwright.sync_api import sync_playwright
from reportlab.pdfgen import canvas

EXPECTED_TOOLS = 63


def pkce():
    verifier = secrets.token_urlsafe(64)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def run(args, report):
    password = Path(args.password_file).read_text().strip()
    resource = args.resource_origin + "/mcp"
    checks = report["checks"]
    with httpx2.Client(trust_env=False, timeout=30, follow_redirects=False) as http:
        protected = http.get(args.resource_origin + "/.well-known/oauth-protected-resource/mcp").json()
        assert protected["resource"] == resource
        issuer = protected["authorization_servers"][0]
        split = urlsplit(issuer)
        metadata = http.get(f"{split.scheme}://{split.netloc}/.well-known/oauth-authorization-server{split.path}").json()
        assert metadata["issuer"] == issuer and "S256" in metadata["code_challenge_methods_supported"]
        checks["rfc9728_and_rfc8414_discovery_public"] = True
        challenge = http.post(resource)
        assert challenge.status_code == 401 and "resource_metadata=" in challenge.headers["www-authenticate"]
        checks["unauthenticated_challenge"] = True
        hidden = {p: http.get(args.resource_origin + p).status_code for p in ("/", "/health", "/docs", "/auth/login")}
        assert set(hidden.values()) == {404}, hidden
        hidden_idp = {p: http.get(f"{split.scheme}://{split.netloc}{p}").status_code
                      for p in ("/admin/", "/realms/master/.well-known/openid-configuration", "/realms/ocr/account",
                                "/realms/ocr/clients-registrations/openid-connect")}
        assert set(hidden_idp.values()) == {404}, hidden_idp
        checks["private_routes_hidden_both_hosts"] = True

        def authorize(page, scope):
            verifier, challenge_value = pkce()
            state = secrets.token_urlsafe(24)
            params = {"client_id": args.client_id, "response_type": "code", "redirect_uri": args.callback,
                      "scope": scope, "resource": resource, "state": state,
                      "code_challenge": challenge_value, "code_challenge_method": "S256"}
            page.goto(metadata["authorization_endpoint"] + "?" + urlencode(params))
            if page.locator("#username").count():
                page.locator("#username").fill(args.username)
                page.locator("#password").fill(password)
                page.locator("#kc-login").click()
            try:
                page.wait_for_function("() => document.querySelector('[name=accept]') || location.pathname === '/callback'",
                                       timeout=20000)
            except Exception:
                report["browser_path"] = urlsplit(page.url)._replace(query="", fragment="").geturl()
                report["login_form_action"] = (page.locator("#kc-form-login").get_attribute("action") or "").split("?")[0] if page.locator("#kc-form-login").count() else None
                report["browser_headings"] = page.locator("h1,h2").all_text_contents()[:4]
                report["browser_messages"] = page.locator("#input-error, .kc-feedback-text, #kc-error-message, .alert-error, .pf-v5-c-alert__title").all_text_contents()[:4]
                raise
            if page.locator('[name="accept"]').count():
                page.locator('[name="accept"]').click()
            page.wait_for_url(lambda url: urlsplit(str(url)).path == "/callback", timeout=20000)
            query = parse_qs(urlsplit(page.url).query)
            assert query.get("state") == [state] and "code" in query
            token = http.post(metadata["token_endpoint"], data={
                "grant_type": "authorization_code", "client_id": args.client_id, "code": query["code"][0],
                "redirect_uri": args.callback, "code_verifier": verifier, "resource": resource})
            assert token.status_code == 200, token.status_code
            return token.json()

        # The provider redirects the browser to the loopback callback: a real listener answers it.
        callback = urlsplit(args.callback)

        class Callback(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"callback")

            def log_message(self, *_):
                pass
        listener = ThreadingHTTPServer((callback.hostname, callback.port), Callback)
        threading.Thread(target=listener.serve_forever, daemon=True).start()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=args.browser, headless=True)
            page = browser.new_context().new_page()
            full = authorize(page, "openid jobs:read jobs:submit artifacts:read")
            checks["browser_pkce_login_consent_public"] = True
            readonly = authorize(page, "openid jobs:read")
            browser.close()
        listener.shutdown()
        auth = {"Authorization": "Bearer " + full["access_token"]}
        me = http.get(args.resource_origin + "/api/v1/me", headers=auth)
        assert me.status_code == 200, me.status_code
        report["subject"] = me.json()["subject"]
        assert report["subject"] == args.expected_subject
        checks["oauth_subject_bound_to_non_admin_user"] = True
        assert http.get(args.local_origin + "/api/v1/me", headers=auth).status_code == 401
        checks["oauth_token_rejected_on_local_host"] = True
        r = http.post(args.resource_origin + "/api/v1/upload-sessions", json={"filename": "x.pdf", "size_bytes": 10},
                      headers={"Authorization": "Bearer " + readonly["access_token"]})
        assert r.status_code == 403, r.status_code
        checks["read_only_token_cannot_submit"] = True

        async def mcp():
            async with httpx2.AsyncClient(trust_env=False, timeout=60, headers=auth) as client:
                for mode in ("auto", "legacy"):
                    async with Client(streamable_http_client(resource, http_client=client), mode=mode) as session:
                        tools = await session.list_tools()
                        assert len(tools.tools) == EXPECTED_TOOLS, len(tools.tools)
                        who = await session.call_tool("ocr_whoami", {})
                        assert not who.is_error and who.structured_content["subject"] == args.expected_subject
                buffer = BytesIO()
                pdf = canvas.Canvas(buffer)
                pdf.drawString(60, 700, "Synthetic OAuth public smoke AM-13")
                pdf.save()
                data = buffer.getvalue()
                r = await client.post(args.resource_origin + "/api/v1/upload-sessions", json={
                    "filename": "oauth-smoke.pdf", "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
                assert r.status_code == 201, r.status_code
                upload = r.json()
                url = upload["content_url"] if upload["content_url"].startswith("http") else args.resource_origin + upload["content_url"]
                assert (await client.put(url, content=data, headers={"Content-Type": "application/pdf"})).status_code == 200
                r = await client.post(args.resource_origin + "/api/v1/jobs", json={
                    "upload_id": upload["upload_id"], "options": {"processing_mode": "extract_text"}},
                    headers={"Idempotency-Key": "oauth-public-smoke-" + upload["upload_id"]})
                assert r.status_code in (200, 201, 202), r.status_code
                job_id = r.json()["job_id"]
                async with Client(streamable_http_client(resource, http_client=client)) as session:
                    async with asyncio.timeout(240):
                        while True:
                            job = await session.call_tool("ocr_get_job", {"job_id": job_id})
                            assert not job.is_error and job.structured_content["status"] != "failed"
                            if job.structured_content["status"] == "completed":
                                break
                            await asyncio.sleep(2)
                    text = await session.call_tool("ocr_get_text", {"job_id": job_id})
                    assert not text.is_error and "Synthetic OAuth public smoke" in text.structured_content["text"]
                listing = (await client.get(args.resource_origin + "/api/v1/jobs")).json()["items"]
                assert {item["job_id"] for item in listing} >= {job_id}
                report["smoke_job_id"] = job_id
                report["jobs_visible_to_smoke_user"] = len(listing)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(lambda: asyncio.run(mcp())).result(timeout=400)
        checks["mcp_sdk_auto_legacy_all_tools_whoami"] = True
        checks["https_upload_job_text_via_mcp"] = True
        revoked = http.post(metadata["revocation_endpoint"], data={
            "client_id": args.client_id, "token": full["refresh_token"], "token_type_hint": "refresh_token"})
        assert revoked.status_code == 200
        assert http.get(args.resource_origin + "/api/v1/me", headers=auth).status_code == 401
        checks["refresh_revocation_disables_access_next_request"] = True
        http.post(metadata["revocation_endpoint"], data={"client_id": args.client_id, "token": readonly["refresh_token"],
                                                         "token_type_hint": "refresh_token"})
    report["passed"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-origin", default="https://ocr-mcp.andre.goiania.br")
    parser.add_argument("--local-origin", default="https://ocr.andre.goiania.br")
    parser.add_argument("--client-id", default="ocr-smoke")
    parser.add_argument("--callback", default="http://127.0.0.1:47613/callback")
    parser.add_argument("--username", default="ocr-smoke")
    parser.add_argument("--expected-subject", required=True)
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--browser", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    report = {"passed": False, "checks": {}, "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "scope": "public hostnames, synthetic realm user and OCR user, synthetic PDF"}
    try:
        run(args, report)
    except Exception as error:
        report["error_type"] = type(error).__name__
        report["failure_location"] = [{"line": f.lineno} for f in traceback.extract_tb(error.__traceback__)
                                      if f.filename == __file__]
    args.evidence.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
