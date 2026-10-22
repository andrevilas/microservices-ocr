"""Opt-in TLS/Keycloak/browser integration lab, using synthetic identities only.

Run as python -m scripts.verify_remote_oauth --help. Requires an isolated unpacked
Keycloak distribution, Java, OpenSSL and Chromium. Never point it at production.
It starts and stops its own processes, uses an in-memory identity DB and writes
only a redacted report to --evidence. Temporary keys and logs are removed.
"""
import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.parse import urlencode, urlsplit, parse_qs
from uuid import uuid4

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from playwright.sync_api import sync_playwright
from reportlab.pdfgen import canvas

from app.oauth_realm import build_realm

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = 63


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def stop(process):
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=30)
    else:
        process.terminate()
    process.wait(timeout=30)


def wait_ready(process, client, url):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Lab process exited before readiness.")
        try:
            response = client.get(url)
            if response.status_code == 200:
                return response.json()
        except httpx2.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError("Lab readiness timed out.")


def run(args, report):
    keycloak = args.keycloak_dir.resolve()
    report["keycloak_distribution"] = keycloak.name
    # Refuse to modify an existing realm-import directory.
    import_dir = keycloak / "data" / "import"
    import_dir.mkdir(parents=True, exist_ok=True)
    realm_file = import_dir / "ocr-realm.json"
    if list(import_dir.iterdir()):
        raise ValueError("Use an isolated Keycloak distribution with an empty data/import directory.")
    processes, logs = [], []
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with tempfile.TemporaryDirectory(prefix="ocr-oauth-", dir=keycloak.parent) as temporary:
        temp = Path(temporary)
        try:
            report["step"] = "local TLS and realm setup"
            cert, key = temp / "tls.crt", temp / "tls.key"
            openssl_config = temp / "openssl.cnf"
            openssl_config.write_text("[req]\ndistinguished_name=dn\n[dn]\n", encoding="ascii")
            result = subprocess.run([str(args.openssl), "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-config", str(openssl_config),
                "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=127.0.0.1",
                "-addext", "subjectAltName=IP:127.0.0.1", "-addext", "basicConstraints=critical,CA:TRUE"],
                capture_output=True, timeout=30, creationflags=flags)
            assert result.returncode == 0, "Local TLS certificate generation failed."
            context = ssl.create_default_context(cafile=str(cert))
            origin, identity_origin = f"https://127.0.0.1:{port()}", f"https://127.0.0.1:{port()}"
            callback = origin + "/callback"
            issuer = identity_origin + "/realms/ocr"
            subject, password, secret = str(uuid4()), secrets.token_urlsafe(32), secrets.token_urlsafe(40)
            realm = build_realm(origin, {"ocr-lab": [callback], "ocr-other": [callback]})
            wrong_audience = json.loads(json.dumps(realm["clientScopes"][0]))
            wrong_audience["name"] = "other-audience"
            wrong_audience["protocolMappers"][0]["config"]["included.custom.audience"] = "https://other.invalid/mcp"
            realm["clientScopes"].append(wrong_audience)
            realm["clients"][1]["defaultClientScopes"] = ["other-audience"]
            realm["users"] = [{"id": subject, "username": "synthetic-user", "enabled": True,
                "firstName": "Synthetic", "lastName": "Test", "email": "synthetic@example.invalid", "emailVerified": True,
                "credentials": [{"type": "password", "value": password, "temporary": False}]}]
            realm_file.write_text(json.dumps(realm), encoding="utf-8")
            env = {**os.environ, "JAVA_HOME": str(args.java_home), "OCR_INTROSPECTION_SECRET": secret}
            # No management user or remote password grant is created by the lab.
            log = (temp / "identity.log").open("wb")
            logs.append(log)
            command = [str(keycloak / "bin" / ("kc.bat" if os.name == "nt" else "kc.sh")), "start-dev",
                "--db=dev-mem", "--http-enabled=false", "--http-host=127.0.0.1",
                "--https-port=" + str(urlsplit(identity_origin).port), "--hostname=" + identity_origin,
                "--https-certificate-file=" + str(cert), "--https-certificate-key-file=" + str(key), "--import-realm"]
            processes.append(subprocess.Popen(command, cwd=keycloak, env=env, stdout=log, stderr=log, creationflags=flags))
            with httpx2.Client(verify=context, trust_env=False, timeout=5, follow_redirects=False) as http:
                report["step"] = "Keycloak discovery"
                metadata = wait_ready(processes[0], http, issuer + "/.well-known/openid-configuration")
                assert metadata["issuer"] == issuer
                report["checks"]["oidc_discovery_https"] = True
                secret_file = temp / "introspection.txt"
                secret_file.write_text(secret, encoding="utf-8")
                config = {"origin": origin, "issuer": issuer, "introspection_client_id": "ocr-resource",
                    "secret_file": str(secret_file), "ca_file": str(cert), "allowed_clients": ["ocr-lab", "ocr-other"],
                    "subjects": {subject: {"user_id": 1, "scopes": ["jobs:read", "jobs:submit", "artifacts:read"]}}}
                config_file = temp / "remote.json"
                config_file.write_text(json.dumps(config), encoding="utf-8")
                runtime_env = {**os.environ, "OCR_REMOTE_CONFIG_FILE": str(config_file),
                    "OCR_TMP_DIR": str(temp / "ocr-data"), "USER_DB_PATH": str(temp / "users.sqlite3"),
                    "JWT_SECRET": secrets.token_urlsafe(40), "ADMIN_PASSWORD": secrets.token_urlsafe(32),
                    "JOB_WORKER_CONCURRENCY": "1"}
                log = (temp / "ocr.log").open("wb")
                logs.append(log)
                # The production entrypoint: app.main:app serves the OAuth host when OCR_REMOTE_CONFIG_FILE is set.
                processes.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app",
                    "--host", "127.0.0.1", "--port", str(urlsplit(origin).port), "--ssl-certfile", str(cert),
                    "--ssl-keyfile", str(key), "--no-proxy-headers", "--no-access-log", "--log-level", "warning"],
                    cwd=ROOT, env=runtime_env, stdout=log, stderr=log, creationflags=flags))
                report["step"] = "OCR resource discovery"
                protected = wait_ready(processes[1], http, origin + "/.well-known/oauth-protected-resource/mcp")
                assert protected["resource"] == origin + "/mcp"
                report["checks"]["protected_resource_discovery_https"] = True
                challenge = http.post(origin + "/mcp")
                assert challenge.status_code == 401 and "resource_metadata=" in challenge.headers["www-authenticate"]
                report["checks"]["unauthenticated_mcp_challenge"] = True
                base = {"client_id": "ocr-lab", "response_type": "code", "redirect_uri": callback,
                        "scope": "openid jobs:read jobs:submit artifacts:read", "resource": origin + "/mcp"}
                report["step"] = "PKCE missing challenge"
                missing = http.get(metadata["authorization_endpoint"], params=base)
                missing_error = parse_qs(urlsplit(missing.headers.get("location", "")).query).get("error")
                assert missing.status_code == 400 or (missing.status_code == 302 and missing_error == ["invalid_request"])
                report["checks"]["pkce_missing_rejected"] = True
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(executable_path=str(args.browser), headless=True)
                    report["chromium_version"] = browser.version
                    # Only the ephemeral loopback cert is untrusted by this isolated browser.
                    browser_context = browser.new_context(ignore_https_errors=True)
                    page = browser_context.new_page()
                    callbacks = []
                    def intercept(route):
                        callbacks.append(route.request.url)
                        route.fulfill(status=200, content_type="text/plain", body="Synthetic authorization callback received.")
                    page.route(callback + "**", intercept)

                    def authorize(client_id="ocr-lab", scope=None):
                        verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(32)
                        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                        params = {**base, "client_id": client_id, "state": state, "code_challenge": challenge,
                                  "code_challenge_method": "S256"}
                        if scope:
                            params["scope"] = scope
                        callbacks.clear()
                        page.goto(metadata["authorization_endpoint"] + "?" + urlencode(params))
                        if page.locator("#username").count():
                            page.locator("#username").fill("synthetic-user")
                            page.locator("#password").fill(password)
                            page.locator("#kc-login").click()
                        page.wait_for_function("() => document.querySelector('[name=accept]') || location.pathname === '/callback'", timeout=15000)
                        if page.locator('[name="accept"]').count():
                            page.locator('[name="accept"]').click()
                        try:
                            page.wait_for_url(lambda url: (urlsplit(str(url)).netloc == urlsplit(callback).netloc
                                                          and urlsplit(str(url)).path == "/callback"), timeout=15000)
                        except Exception:
                            report["browser_path"] = urlsplit(page.url).path
                            report["browser_headings"] = page.locator("h1,h2").all_text_contents()
                            report["browser_error"] = page.locator("#kc-error-message").all_text_contents()
                            report["browser_form_fields"] = page.locator("input,button").evaluate_all(
                                "els => els.map(e => ({id: e.id, name: e.name, type: e.type}))")
                            report["callback_count"] = len(callbacks)
                            raise
                        query = parse_qs(urlsplit(page.url).query)
                        assert query.get("state") == [state] and "code" in query, "OAuth callback validation failed."
                        return {"grant_type": "authorization_code", "client_id": client_id,
                                "code": query["code"][0], "redirect_uri": callback,
                                "code_verifier": verifier, "resource": origin + "/mcp"}

                    report["step"] = "PKCE wrong verifier"
                    bad = authorize()
                    bad["code_verifier"] = secrets.token_urlsafe(64)
                    assert http.post(metadata["token_endpoint"], data=bad).status_code == 400
                    report["checks"]["pkce_wrong_verifier_rejected"] = True
                    replay = authorize()
                    assert http.post(metadata["token_endpoint"], data=replay).status_code == 200
                    assert http.post(metadata["token_endpoint"], data=replay).status_code == 400
                    report["checks"]["authorization_code_reuse_rejected"] = True
                    report["step"] = "PKCE S256 valid exchange"
                    good = authorize()
                    response = http.post(metadata["token_endpoint"], data=good)
                    assert response.status_code == 200, "PKCE token exchange failed."
                    token_set = response.json()
                    token = token_set["access_token"]
                    report["checks"]["pkce_s256_browser_login"] = True
                    auth = {"Authorization": "Bearer " + token}
                    response = http.get(origin + "/api/v1/me", headers=auth)
                    if response.status_code != 200:
                        introspection = http.post(metadata["introspection_endpoint"],
                            data={"token": token, "token_type_hint": "access_token"}, auth=httpx2.BasicAuth("ocr-resource", secret))
                        diagnostic = introspection.json()
                        report["auth_diagnostic"] = {"http_status": response.status_code,
                            "introspection_status": introspection.status_code, "active": diagnostic.get("active"),
                            "claim_keys": sorted(diagnostic), "issuer_matches": diagnostic.get("iss") == issuer,
                            "subject_matches": diagnostic.get("sub") == subject,
                            "audience_matches": origin + "/mcp" in ([diagnostic.get("aud")] if isinstance(diagnostic.get("aud"), str) else (diagnostic.get("aud") or [])),
                            "client_matches": diagnostic.get("client_id") == "ocr-lab", "token_type_is_bearer": diagnostic.get("token_type") == "Bearer"}
                    assert response.status_code == 200, "Real provider token rejected by OCR."
                    assert response.json()["subject"] == "user:1"
                    report["checks"]["explicit_subject_binding"] = True
                    report["step"] = "MCP SDK and synthetic PDF"
                    async def mcp():
                        async with httpx2.AsyncClient(verify=context, trust_env=False, headers=auth) as client:
                            for mode in ("auto", "legacy"):
                                async with Client(streamable_http_client(origin + "/mcp", http_client=client), mode=mode) as mcp_client:
                                    listing = await mcp_client.list_tools()
                                    assert len(listing.tools) == EXPECTED_TOOLS
                                    result = await mcp_client.call_tool("ocr_whoami", {})
                                    assert not result.is_error and result.structured_content["subject"] == "user:1"
                            buffer = BytesIO()
                            pdf = canvas.Canvas(buffer)
                            pdf.drawString(60, 700, "Synthetic OAuth remote OCR validation")
                            pdf.save()
                            r = await client.post(origin + "/api/v1/uploads", files={"file": ("synthetic.pdf", buffer.getvalue(), "application/pdf")})
                            assert r.status_code == 201
                            r = await client.post(origin + "/api/v1/jobs", json={"upload_id": r.json()["upload_id"],
                                "options": {"processing_mode": "extract_text"}}, headers={"Idempotency-Key": "oauth-lab-synthetic"})
                            assert r.status_code == 202
                            job_id = r.json()["job_id"]
                            async with Client(streamable_http_client(origin + "/mcp", http_client=client)) as mcp_client:
                                async with asyncio.timeout(30):
                                    while True:
                                        result = await mcp_client.call_tool("ocr_get_job", {"job_id": job_id})
                                        assert not result.is_error and result.structured_content["status"] != "failed"
                                        if result.structured_content["status"] == "completed":
                                            break
                                        await asyncio.sleep(0.1)
                                result = await mcp_client.call_tool("ocr_get_text", {"job_id": job_id})
                                assert not result.is_error and "Synthetic OAuth" in result.structured_content["text"]
                        report["checks"]["mcp_sdk_auto_legacy_all_tools"] = True
                        report["checks"]["https_upload_processing_mcp_text"] = True
                    # Playwright's synchronous facade already owns an event loop.
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        pool.submit(lambda: asyncio.run(mcp())).result(timeout=90)
                    report["step"] = "signed wrong audience"
                    wrong = http.post(metadata["token_endpoint"], data=authorize("ocr-other"))
                    assert wrong.status_code == 200
                    assert http.get(origin + "/api/v1/me", headers={"Authorization": "Bearer " + wrong.json()["access_token"]}).status_code == 401
                    report["checks"]["real_signed_wrong_audience_rejected"] = True
                    report["step"] = "scope enforcement"
                    readonly = http.post(metadata["token_endpoint"], data=authorize(scope="openid jobs:read"))
                    assert readonly.status_code == 200
                    assert http.post(origin + "/api/v1/upload-sessions", headers={"Authorization": "Bearer " + readonly.json()["access_token"]},
                        json={"filename": "synthetic.pdf", "size_bytes": 100}).status_code == 403
                    report["checks"]["real_read_only_token_cannot_submit"] = True
                    report["step"] = "revocation"
                    assert http.get(origin + "/api/v1/me", headers=auth).status_code == 200
                    revoked = http.post(metadata["revocation_endpoint"], data={"client_id": "ocr-lab", "token": token_set["refresh_token"], "token_type_hint": "refresh_token"})
                    assert revoked.status_code == 200
                    assert http.get(origin + "/api/v1/me", headers=auth).status_code == 401
                    report["checks"]["refresh_revocation_disables_access_next_request"] = True
                    assert http.post(origin + "/mcp", headers=auth, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 401
                    report["checks"]["revoked_token_rejected_by_mcp"] = True
                    browser.close()
                report["checks"]["private_routes_hidden"] = all(http.get(origin + path, headers=auth).status_code == 404
                    for path in ("/", "/docs", "/auth/login", "/health", "/api/v1/services"))
                assert report["checks"]["private_routes_hidden"]
                report["step"] = "provider outage"
                stop(processes[0])
                assert http.get(origin + "/api/v1/me", headers=auth).status_code == 503
                report["checks"]["provider_outage_fails_closed"] = True
            report["step"] = "complete"
            report["passed"] = True
        finally:
            for process in reversed(processes):
                stop(process)
            for log in logs:
                log.close()
            realm_file.unlink(missing_ok=True)
    report["temporary_credentials_removed"] = True
    report["owned_processes_stopped"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("keycloak-dir", "java-home", "openssl", "browser", "evidence"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    report = {"passed": False, "checks": {}, "scope": "isolated loopback TLS laboratory; synthetic data only",
              "native_clients_homologated": [], "public_https_deployed": False,
              "captured_at": datetime.now(timezone.utc).isoformat(),
              "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "tls": "HTTPS with explicit test CA verification in HTTP/OCR clients; isolated browser accepts the ephemeral self-signed certificate"}
    try:
        run(args, report)
    except Exception as error:
        report["passed"] = False
        report["error_type"] = type(error).__name__  # Never persist codes, tokens, URLs with codes, or passwords.
        report["failure_location"] = [{"file": Path(frame.filename).name, "line": frame.lineno}
            for frame in traceback.extract_tb(error.__traceback__) if frame.filename == __file__]
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
