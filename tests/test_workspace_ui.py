"""Real browser against an isolated API, synthetic PDFs and human sessions only."""
import os
import json
from pathlib import Path
import subprocess
import sys

import pytest
from playwright.sync_api import sync_playwright, expect

from test_mcp_vertical import ROOT, runtime
from test_upload_admission import pdf, system


def test_browser_session_requires_same_origin_human_cookie(system):
    client = system.client
    headers = {"Origin": "http://testserver", "X-OCR-Browser": "1"}
    assert client.post("/auth/browser-session", headers=headers).status_code == 401
    client.cookies.set("access_token", system.tokens[0])
    for bad in ({}, {"Origin": "http://testserver"},
                {**headers, "Origin": "http://foreign.invalid"},
                {**headers, "Sec-Fetch-Site": "cross-site"}):
        assert client.post("/auth/browser-session", headers=bad).status_code == 403
    result = client.post("/auth/browser-session", headers=headers)
    assert result.status_code == 200
    assert result.json() == {"access_token": system.tokens[0], "token_type": "bearer", "subject": "user:1"}
    assert result.headers["cache-control"] == "no-store"
    assert "set-cookie" not in result.headers  # no renewal
    assert client.get("/api/v1/jobs").status_code == 401  # cookie is not v1 authentication
    for token in (system.tokens[2], "invalid"):
        client.cookies.set("access_token", token)
        assert client.post("/auth/browser-session", headers=headers).status_code == 401


@pytest.fixture
def browser():
    with sync_playwright() as pw:
        executable = os.environ.get("OCR_TEST_BROWSER")
        if not executable and os.name == "nt":
            executable = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        try:
            instance = pw.chromium.launch(executable_path=executable, headless=True)
        except Exception as exc:
            # Explicit opt-in to skip; CI must install Chromium or specify OCR_TEST_BROWSER.
            if os.environ.get("OCR_SKIP_BROWSER_TESTS") == "1":
                pytest.skip("Browser not installed")
            raise RuntimeError("Install Playwright Chromium or set OCR_TEST_BROWSER") from exc
        yield instance
        instance.close()


def login(context, origin):
    result = context.request.post(origin + "/auth/login", data={"email": "admin@localhost", "password": "synthetic-test-admin-password"})
    assert result.status == 200


def ready(page, origin):
    page.goto(origin)
    expect(page.locator("#job-state")).to_have_text("Histórico sincronizado.", timeout=15000)


def confirm(page):
    """Accept the in-page confirmation dialog (destructive and view-reset actions)."""
    expect(page.locator("#confirm-dialog")).to_be_visible()
    page.locator("#confirm-accept").click()
    expect(page.locator("#confirm-dialog")).to_be_hidden()


def open_menu(page, scope):
    page.locator(f"{scope} > summary").first.click()


def screenshot(page, name):
    folder = os.environ.get("OCR_UI_EVIDENCE_DIR")
    if folder:
        target = Path(folder)
        target.mkdir(parents=True, exist_ok=True)
        page.evaluate("window.scrollTo(0, 0)")
        page.screenshot(path=str(target / name), full_page=True, animations="disabled")


def test_browser_upload_results_reload_retry_zip_delete_and_visual_reset(runtime, browser):
    runtime.start()
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
    login(context, runtime.origin)
    page = context.new_page()
    errors, requests = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("request", lambda request: requests.append((request.method, request.url, bool(request.headers.get("authorization")))))
    ready(page, runtime.origin)
    page.locator("#options-disclosure > summary").click()
    page.locator("#processing-mode").select_option("extract_text")
    page.locator("#multi-file-input").set_input_files({"name": "synthetic.pdf", "mimeType": "application/pdf", "buffer": pdf(2)})
    expect(page.locator(".staged-file")).to_have_count(1)
    page.locator("#queue-button").click()
    expect(page.locator(".file-card.status-completed")).to_have_count(1, timeout=20000)
    expect(page.locator(".staged-file")).to_have_count(0)
    job_id = page.locator(".file-card").get_attribute("data-card-id")
    page.reload()
    expect(page.locator(".file-card.status-completed")).to_have_count(1, timeout=15000)
    screenshot(page, "workspace-overview-desktop.png")
    page.locator('.file-card [data-action="detail"]').click()
    expect(page.locator("#detail-title")).to_be_focused()
    page.locator('#detail-body [data-action="text"]').click()
    expect(page.locator("#result-content")).to_contain_text("Synthetic upload page 0")
    page.get_by_role("button", name="Próximo trecho").click()
    expect(page.locator("#result-content")).to_contain_text("Synthetic upload page 1")
    page.locator("#text-page").select_option("1")
    expect(page.locator("#result-content")).to_contain_text("Synthetic upload page 0")
    page.locator('#detail-body [data-action="report"]').click()
    expect(page.locator("#result-content")).to_contain_text('"sha256"')
    expect(page.locator("#result-content")).to_contain_text("Qualidade")
    screenshot(page, "workspace-desktop.png")
    page.locator("#detail-close-button").click()
    expect(page.locator("#detail-drawer")).to_be_hidden()
    with page.expect_download() as download:
        page.locator('.file-card [data-action="download"]').click()
    assert Path(download.value.path()).read_bytes().startswith(b"%PDF-")
    # ZIP is built from the explicit selection.
    page.locator("#select-visible-button").click()
    expect(page.locator("#selection-bar")).to_be_visible()
    with page.expect_download(timeout=20000) as download:
        page.locator("#download-all-button").click()
    assert Path(download.value.path()).read_bytes().startswith(b"PK")
    page.locator("#options-disclosure > summary").click()  # closed again after the reload
    page.locator("#processing-mode").select_option("extract_text")
    open_menu(page, ".file-card .row-menu")
    page.locator('.file-card [data-action="retry"]').click()
    expect(page.locator(".file-card.status-completed")).to_have_count(2, timeout=20000)
    before = len(requests)
    open_menu(page, "#view-menu")
    page.locator("#reset-workspace-button").click()
    confirm(page)
    expect(page.locator(".file-card")).to_have_count(0)
    assert not any(method != "GET" for method, url, auth in requests[before:])
    open_menu(page, "#view-menu")
    page.locator("#show-hidden-button").click()
    expect(page.locator(".file-card")).to_have_count(2)
    page.locator("#refresh-workspace-button").click()
    expect(page.locator(".file-card")).to_have_count(2)
    open_menu(page, f'[data-card-id="{job_id}"] .row-menu')
    page.locator(f'[data-card-id="{job_id}"] [data-action="delete"]').click()
    confirm(page)
    expect(page.locator(f'[data-card-id="{job_id}"]')).to_contain_text("Arquivos excluídos")
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#options-disclosure > summary").click()
    screenshot(page, "workspace-mobile.png")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    stored = page.evaluate("JSON.stringify({...localStorage})")
    assert job_id not in stored and "access_token" not in stored
    assert not errors
    api_requests = [(method, url, auth) for method, url, auth in requests if "/api/" in url]
    assert api_requests and all("/api/v1/" in url and auth for _, url, auth in api_requests)
    context.close()


def test_browser_paginates_backend_and_ignores_local_history(runtime, browser):
    # Seed terminal records before startup; no external tools or real documents.
    seed = subprocess.run([sys.executable, "-c", """
from app.services.storage_service import get_job_store
s=get_job_store()
for n in range(103):
 j=s.create('synthetic-%03d.pdf'%n,b'%PDF-synthetic',owner_user_id=1)
 s.update(j.job_id,status='canceled')
j=s.create('foreign-secret.pdf',b'%PDF-synthetic',owner_user_id=2)
s.update(j.job_id,status='canceled')
"""], cwd=ROOT, env=runtime.env, capture_output=True, timeout=20)
    assert seed.returncode == 0
    runtime.start()
    context = browser.new_context()
    login(context, runtime.origin)
    page = context.new_page()
    page.add_init_script("localStorage.setItem('ocr-recognizer-workspace-v2', JSON.stringify({jobs:[{filename:'stale-secret.pdf'}]}))")
    paths = []
    page.on("request", lambda request: paths.append(request.url))
    ready(page, runtime.origin)
    # All 103 records are restored from several API pages; the list reveals them 50 at a time.
    expect(page.locator("#results-summary")).to_contain_text("de 103")
    expect(page.locator(".file-card")).to_have_count(50)
    page.locator("#show-more-button").click()
    expect(page.locator(".file-card")).to_have_count(100)
    page.locator("#show-more-button").click()
    expect(page.locator(".file-card")).to_have_count(103)
    expect(page.locator("#show-more-button")).to_be_hidden()
    assert any("cursor=" in path for path in paths)
    assert "foreign-secret" not in page.locator("#cards-grid").inner_text()
    assert "stale-secret" not in page.locator("#cards-grid").inner_text()
    assert page.evaluate("localStorage.getItem('ocr-recognizer-workspace-v2')") is None
    page.locator("#search-input").fill("synthetic-102")
    expect(page.locator(".file-card")).to_have_count(1)
    context.close()


def test_browser_replays_ambiguous_batch_with_same_key_and_options(runtime, browser):
    runtime.start(pause=True)
    context = browser.new_context()
    login(context, runtime.origin)
    page = context.new_page()
    ready(page, runtime.origin)
    page.locator("#options-disclosure > summary").click()
    page.locator("#processing-mode").select_option("extract_text")
    sent = []
    def intercept(route):
        sent.append((route.request.headers["idempotency-key"], route.request.post_data))
        response = route.fetch()  # server accepts, client loses first response
        if len(sent) == 1:
            route.abort("connectionreset")
        else:
            route.fulfill(response=response)
    page.route("**/api/v1/batches", intercept)
    page.locator("#multi-file-input").set_input_files([
        {"name": name, "mimeType": "application/pdf", "buffer": pdf()}
        for name in ("retry.pdf", "queued.pdf")])
    page.locator("#queue-button").click()
    expect(page.locator("#queue-button")).to_have_text("Retomar envio anterior")
    expect(page.locator("#reset-workspace-button")).to_be_disabled()
    expect(page.locator("#discard-pending-button")).to_be_visible()
    # Changed UI options must not mutate the uncertain request.
    page.locator("#queue-button").click()
    expect(page.locator(".file-card")).to_have_count(2)
    expect(page.locator("#queue-button")).to_have_text("Enviar PDFs")
    assert len(sent) == 2 and sent[0] == sent[1]
    page.locator('.file-card.status-processing [data-action="cancel"]').click()
    confirm(page)
    expect(page.locator(".file-card.status-cancel_requested")).to_have_count(1)
    page.locator("#select-visible-button").click()
    page.locator("#clear-queue-button").click()
    confirm(page)
    expect(page.locator(".file-card.status-canceled")).to_have_count(1)
    expect(page.locator(".file-card.status-cancel_requested")).to_have_count(1)
    context.close()


def test_browser_rejected_pdf_does_not_lock_visual_reset(runtime, browser):
    runtime.start()
    context = browser.new_context()
    login(context, runtime.origin)
    page = context.new_page()
    ready(page, runtime.origin)
    page.locator("#multi-file-input").set_input_files({"name": "invalid.pdf", "mimeType": "application/pdf", "buffer": b"invalid"})
    page.locator("#queue-button").click()
    expect(page.locator("#job-state")).to_contain_text("invalid_pdf")
    expect(page.locator("#notice")).to_have_attribute("data-tone", "error")
    expect(page.locator(".staged-file.is-rejected")).to_contain_text("Rejeitado")
    expect(page.locator("#reset-workspace-button")).to_be_enabled()
    open_menu(page, "#view-menu")
    page.locator("#reset-workspace-button").click()
    confirm(page)
    expect(page.locator(".staged-file")).to_have_count(0)
    expect(page.locator(".file-card")).to_have_count(0)
    context.close()


@pytest.mark.parametrize("field", ["filename", "relativePath", "error", "text", "report", "api_error", "download_url", "local_file"])
def test_browser_treats_document_metadata_and_results_as_text(runtime, browser, field):
    runtime.start()
    context = browser.new_context(accept_downloads=True)
    login(context, runtime.origin)
    page = context.new_page()
    payload = '<img src=x onerror="globalThis.__xss=1">'
    job = {"job_id": "a" * 32, "filename": "synthetic.pdf", "status": "completed", "progress_percent": 100,
           "download_url": "/api/v1/jobs/" + "a" * 32 + "/artifact/content"}
    if field in ("filename", "relativePath", "error"):
        job[field] = payload
    if field == "download_url":
        job[field] = "javascript:globalThis.__xss=1"
    page.route("**/api/v1/jobs?*", lambda route: route.fulfill(json={"items": [job], "next_cursor": None}))
    ready(page, runtime.origin)
    if field == "local_file":
        page.locator("#multi-file-input").set_input_files({"name": payload + ".pdf", "mimeType": "application/pdf", "buffer": pdf()})
        expect(page.locator("#staging-list")).to_contain_text(payload)
    elif field == "download_url":
        page.route("**/artifact/content", lambda route: route.fulfill(body=pdf(), content_type="application/pdf"))
        with page.expect_download():
            page.locator('.file-card [data-action="download"]').click()
    else:
        page.locator('.file-card [data-action="detail"]').click()
        if field in ("text", "report", "api_error"):
            action = "report" if field == "report" else "text"
            if field == "api_error":
                page.route("**/text", lambda route: route.fulfill(status=500, json={"error": {"code": "test_error", "message": payload}}))
            else:
                body = {"page": 1, "page_count": 1, "next_page": None, "text": payload} if field == "text" else {"warnings": [payload]}
                page.route("**/" + action, lambda route: route.fulfill(json=body))
            page.locator('#detail-body [data-action="' + action + '"]').click()
            if field == "report":
                expect(page.locator("#result-content")).to_contain_text('"warnings"')
                page.get_by_text("Relatório técnico completo (JSON)", exact=True).click()
                assert json.loads(page.locator("#report-json").inner_text())["warnings"] == [payload]
                expect(page.locator("#result-content .warnings")).to_contain_text(payload)
            else:
                expect(page.locator("#job-state" if field == "api_error" else "#result-content")).to_contain_text(payload)
        elif field == "filename":
            expect(page.locator("#detail-title")).to_have_text(payload)
        elif field == "error":
            expect(page.locator("#detail-body")).to_contain_text(payload)
        else:
            expect(page.locator("#cards-grid")).to_contain_text(payload)
    assert page.locator('img[src="x"]').count() == 0
    assert page.evaluate("globalThis.__xss") is None
    context.close()
