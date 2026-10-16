"""Browser checks for the reviewed interface: focus, keyboard, failure classification, intake and administration.

Synthetic data only. One-time credential secrets are compared in memory and never printed or captured.
"""
from playwright.sync_api import expect

from test_mcp_vertical import runtime
from test_upload_admission import pdf
from test_workspace_ui import browser, confirm, login, open_menu, ready, screenshot

UNLABELLED = """() => [...document.querySelectorAll('input:not([type=hidden]), select, textarea')]
  .filter((e) => !(e.labels && e.labels.length) && !e.getAttribute('aria-label')).map((e) => e.id || e.name)"""
RETENTION = {"state": "retained", "expires_at": "2026-10-16T12:00:00Z", "unavailable_at": None, "purged_at": None, "purge_pending": False}


def job(char="b", status="completed", **extra):
    job_id = char * 32
    return {"job_id": job_id, "filename": f"synthetic-{char}.pdf", "status": status,
            "progress_percent": 100 if status == "completed" else 10,
            "created_at": "2026-10-15T12:00:00Z", "updated_at": "2026-10-15T12:00:00Z",
            "download_url": f"/api/v1/jobs/{job_id}/artifact/content" if status == "completed" else None,
            "retention": RETENTION if status == "completed" else {"state": "active", "purge_pending": False},
            "options_requested": {"processing_mode": "extract_text", "languages": [], "output_format": "pdf_searchable",
                                  "compression_profile": "none", "fallback_policy": "disabled"}, **extra}


def mock_history(page, items):
    page.route("**/api/v1/jobs?*", lambda route: route.fulfill(json={"items": items() if callable(items) else items, "next_cursor": None}))


def new_page(runtime, browser, **context_options):
    context = browser.new_context(**context_options)
    login(context, runtime.origin)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    return context, page, errors


def test_polling_keeps_keyboard_focus_and_selection(runtime, browser):
    runtime.start()
    context, page, errors = new_page(runtime, browser)
    calls = {"n": 0}
    def history():
        calls["n"] += 1
        return [job("b", "processing", progress_percent=min(95, 10 + calls["n"]))]
    mock_history(page, history)
    ready(page, runtime.origin)
    meter = page.locator(".file-card .meter")
    cancel = page.locator('.file-card [data-action="cancel"]')
    cancel.focus()
    value = meter.get_attribute("aria-valuenow")
    expect(meter).not_to_have_attribute("aria-valuenow", value, timeout=15000)  # a poll re-rendered the row
    expect(cancel).to_be_focused()
    check = page.locator(".file-card .row-check")
    check.focus()
    page.keyboard.press("Space")
    expect(check).to_be_checked()
    expect(check).to_be_focused()
    expect(page.locator("#selection-count")).to_have_text("1 selecionado")
    value = meter.get_attribute("aria-valuenow")
    expect(meter).not_to_have_attribute("aria-valuenow", value, timeout=15000)
    expect(check).to_be_checked()  # selection survives refresh
    expect(check).to_be_focused()
    assert not errors
    context.close()


def test_detail_and_menus_work_from_the_keyboard(runtime, browser):
    runtime.start()
    context, page, errors = new_page(runtime, browser)
    mock_history(page, [job("c")])
    ready(page, runtime.origin)
    title = page.locator('.file-card [data-action="detail"]')
    title.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#detail-drawer")).to_be_visible()
    expect(page.locator("#detail-title")).to_be_focused()
    expect(page.locator("#detail-list")).to_contain_text("Extrair texto existente")  # requested options are visible
    page.keyboard.press("Escape")
    expect(page.locator("#detail-drawer")).to_be_hidden()
    expect(title).to_be_focused()
    summary = page.locator(".file-card .row-menu > summary")
    summary.focus()
    page.keyboard.press("Enter")
    expect(page.locator(".file-card .row-menu[open]")).to_have_count(1)
    page.keyboard.press("Escape")
    expect(page.locator(".file-card .row-menu[open]")).to_have_count(0)
    expect(summary).to_be_focused()
    assert page.evaluate(UNLABELLED) == []
    assert page.locator("h1").count() == 1
    assert not errors
    context.close()


def test_history_failure_keeps_error_and_does_not_block_sending(runtime, browser):
    runtime.start()
    context, page, _ = new_page(runtime, browser)
    page.route("**/api/v1/jobs?*", lambda route: route.fulfill(status=503, json={"error": {"code": "storage_unavailable", "message": "synthetic"}}))
    page.goto(runtime.origin)
    expect(page.locator("#job-state")).to_contain_text("storage_unavailable", timeout=15000)
    expect(page.locator("#notice")).to_have_attribute("data-tone", "error")
    expect(page.locator("#sync-status")).to_contain_text("Falha ao atualizar")
    page.locator("#multi-file-input").set_input_files({"name": "synthetic.pdf", "mimeType": "application/pdf", "buffer": pdf()})
    expect(page.locator("#queue-button")).to_be_enabled()  # capabilities loaded independently
    expect(page.locator("#queue-button")).to_have_text("Enviar 1 PDF(s)")
    page.wait_for_timeout(6000)  # a later poll must not erase the persistent error
    expect(page.locator("#job-state")).to_contain_text("storage_unavailable")
    context.close()


def test_export_keeps_key_when_ambiguous_and_releases_it_when_definitive(runtime, browser):
    runtime.start()
    context, page, _ = new_page(runtime, browser)
    mock_history(page, [job("d")])
    keys = []
    def exports(route):
        if route.request.method != "POST":
            return route.continue_()
        keys.append(route.request.headers["idempotency-key"])
        if len(keys) == 1:
            route.abort("connectionreset")
        else:
            route.fulfill(status=413, json={"error": {"code": "export_too_large", "message": "synthetic"}})
    page.route("**/api/v1/exports", exports)
    ready(page, runtime.origin)
    page.locator("#select-visible-button").click()
    page.locator("#download-all-button").click()
    expect(page.locator("#download-all-button")).to_have_text("Retomar ZIP pendente")
    page.locator("#download-all-button").click()
    expect(page.locator("#job-state")).to_contain_text("export_too_large")
    expect(page.locator("#download-all-button")).to_have_text("Baixar ZIP (1)")
    page.locator("#notice-close").click()
    page.locator("#download-all-button").click()
    expect(page.locator("#notice")).to_be_visible()
    assert len(keys) == 3 and keys[0] == keys[1] and keys[2] != keys[0]
    context.close()


def test_retry_keeps_key_when_ambiguous_and_releases_it_when_definitive(runtime, browser):
    runtime.start()
    context, page, _ = new_page(runtime, browser)
    mock_history(page, [job("e")])
    keys, bodies = [], []
    def retry(route):
        keys.append(route.request.headers["idempotency-key"])
        bodies.append(route.request.post_data)
        if len(keys) == 1:
            route.abort("connectionreset")
        else:
            route.fulfill(status=410, json={"error": {"code": "input_expired", "message": "synthetic"}})
    page.route("**/retry", retry)
    ready(page, runtime.origin)
    for expected in (1, 2, 3):
        page.locator("#notice-close").click() if page.locator("#notice").is_visible() else None
        open_menu(page, ".file-card .row-menu")
        page.locator('.file-card [data-action="retry"]').click()
        expect(page.locator("#notice")).to_be_visible()
        assert len(keys) == expected
    expect(page.locator("#job-state")).to_contain_text("input_expired")
    assert keys[0] == keys[1] and keys[2] != keys[0]
    assert bodies[0] == bodies[1] == "{}"  # "same options" inherits the original request
    context.close()


def test_quota_rejection_releases_admission_and_reuses_uploaded_file(runtime, browser):
    runtime.start()
    context, page, _ = new_page(runtime, browser)
    keys, reservations = [], []
    page.on("request", lambda r: reservations.append(r.url) if r.method == "POST" and r.url.endswith("/api/v1/upload-sessions") else None)
    def batches(route):
        if route.request.method != "POST":
            return route.continue_()
        keys.append(route.request.headers["idempotency-key"])
        if len(keys) == 1:
            route.fulfill(status=429, json={"error": {"code": "job_quota_exceeded", "message": "synthetic"}})
        else:
            route.continue_()
    page.route("**/api/v1/batches", batches)
    ready(page, runtime.origin)
    page.locator("#options-disclosure > summary").click()
    page.locator("#processing-mode").select_option("extract_text")
    page.locator("#multi-file-input").set_input_files({"name": "quota.pdf", "mimeType": "application/pdf", "buffer": pdf()})
    page.locator("#queue-button").click()
    expect(page.locator("#job-state")).to_contain_text("job_quota_exceeded")
    expect(page.locator("#queue-button")).to_have_text("Enviar 1 PDF(s)")  # nothing pending to replay
    expect(page.locator("#reset-workspace-button")).to_be_enabled()
    page.locator("#queue-button").click()
    expect(page.locator(".file-card")).to_have_count(1, timeout=20000)
    assert len(keys) == 2 and keys[0] != keys[1]
    assert len(reservations) == 1  # the validated upload was reused, not sent twice
    context.close()


def test_quick_send_during_pending_batch_keeps_file_and_discard_is_explicit(runtime, browser):
    runtime.start()
    context, page, _ = new_page(runtime, browser)
    keys = []
    def batches(route):
        if route.request.method != "POST":
            return route.continue_()
        keys.append(route.request.headers["idempotency-key"])
        route.abort("connectionreset")
    page.route("**/api/v1/batches", batches)
    ready(page, runtime.origin)
    page.locator("#options-disclosure > summary").click()
    page.locator("#processing-mode").select_option("extract_text")
    page.locator("#multi-file-input").set_input_files({"name": "first.pdf", "mimeType": "application/pdf", "buffer": pdf()})
    page.locator("#queue-button").click()
    expect(page.locator("#queue-button")).to_have_text("Retomar envio anterior")
    page.locator("#file-input").set_input_files({"name": "second.pdf", "mimeType": "application/pdf", "buffer": pdf()})
    expect(page.locator("#job-state")).to_contain_text("envio anterior")
    expect(page.locator(".staged-file")).to_have_count(2)
    assert len(keys) == 1  # the quick send did not replace or resend the uncertain request
    page.locator("#discard-pending-button").click()
    expect(page.locator("#confirm-body")).to_contain_text("duplicados")
    confirm(page)
    expect(page.locator("#queue-button")).to_have_text("Enviar 2 PDF(s)")
    expect(page.locator("#reset-workspace-button")).to_be_enabled()
    context.close()


def test_mixed_batch_admits_valid_files_and_keeps_rejected_visible(runtime, browser):
    runtime.start()
    context, page, _ = new_page(runtime, browser)
    ready(page, runtime.origin)
    page.locator("#options-disclosure > summary").click()
    page.locator("#processing-mode").select_option("extract_text")
    page.locator("#multi-file-input").set_input_files([
        {"name": "valid.pdf", "mimeType": "application/pdf", "buffer": pdf()},
        {"name": "broken.pdf", "mimeType": "application/pdf", "buffer": b"not a pdf"}])
    page.locator("#queue-button").click()
    expect(page.locator(".file-card")).to_have_count(1, timeout=20000)
    expect(page.locator(".staged-file.is-rejected")).to_contain_text("broken.pdf")
    expect(page.locator("#job-state")).to_contain_text("rejeitado")
    expect(page.locator("#queue-button")).to_be_disabled()  # only the rejected file remains
    context.close()


def test_integrations_issue_secret_once_revoke_and_disable(runtime, browser):
    runtime.start()
    context, page, errors = new_page(runtime, browser, viewport={"width": 1280, "height": 900})
    page.goto(runtime.origin + "/admin/integrations")
    expect(page.locator("h1")).to_have_text("Integrações")
    page.locator("#create-service-disclosure > summary").click()
    page.locator("#service-name").fill("Importador sintético")
    page.locator("#create-service-btn").click()
    expect(page.locator("#service-title")).to_have_text("Importador sintético")
    expect(page.locator("#service-title")).to_be_focused()
    page.locator("#issue-disclosure > summary").click()
    page.locator("#credential-name").fill("Leitura sintética")
    page.locator("#issue-btn").click()
    expect(page.locator("#secret-dialog")).to_be_visible()
    secret = page.locator("#secret-value").input_value()
    assert secret.startswith("ocr_svc_")
    assert secret not in page.content()  # held in the input property only, never serialized markup
    page.keyboard.press("Escape")
    expect(page.locator("#secret-dialog")).to_be_visible()  # an accidental Escape cannot discard it
    page.locator("#secret-done").click()
    expect(page.locator("#secret-dialog")).to_be_hidden()
    assert page.locator("#secret-value").input_value() == ""
    assert secret not in page.content()
    assert secret not in page.evaluate("JSON.stringify({...localStorage, ...sessionStorage})")
    assert context.request.get(runtime.origin + "/api/v1/me", headers={"Authorization": "Bearer " + secret}).status == 200
    expect(page.locator("#credential-list")).to_contain_text("Leitura sintética")
    expect(page.locator("#credential-list")).to_contain_text("Ativa")
    screenshot(page, "integrations-desktop.png")
    page.locator("[data-revoke-id]").click()
    confirm(page)
    expect(page.locator("#credential-list")).to_contain_text("Revogada")
    assert context.request.get(runtime.origin + "/api/v1/me", headers={"Authorization": "Bearer " + secret}).status == 401
    del secret
    page.locator("#disable-service-btn").click()
    confirm(page)
    expect(page.locator("#service-meta")).to_contain_text("Desativado")
    expect(page.locator("#issue-disclosure")).to_be_hidden()
    assert page.evaluate(UNLABELLED) == []
    assert not errors
    context.close()


def test_login_account_and_users_pages_are_usable(runtime, browser):
    runtime.start()
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(runtime.origin + "/login")
    assert page.evaluate(UNLABELLED) == []
    screenshot(page, "login-desktop.png")
    page.locator("#email").fill("admin@localhost")
    page.locator("#password").fill("wrong-synthetic-password")
    page.locator("#login-btn").click()
    expect(page.locator("#login-error")).to_have_text("E-mail ou senha inválidos.")
    page.locator("#password").fill("synthetic-test-admin-password")
    page.locator("#login-btn").click()
    expect(page.locator("#job-state")).to_have_text("Histórico sincronizado.", timeout=15000)
    expect(page.get_by_role("link", name="Integrações")).to_be_visible()

    page.goto(runtime.origin + "/account")
    expect(page.locator("h1")).to_have_text("Minha conta")
    page.locator("#account-password").fill("nova-senha-sintetica")
    page.locator("#account-password-confirm").fill("diferente")
    page.locator("#account-save-btn").click()
    expect(page.locator("#account-error")).to_contain_text("confirmação")
    assert page.evaluate(UNLABELLED) == []
    screenshot(page, "account-desktop.png")

    page.goto(runtime.origin + "/admin/users")
    expect(page.locator("h1")).to_have_text("Usuários")
    page.locator("#create-disclosure > summary").click()
    page.locator("#new-name").fill("Pessoa Sintética")
    page.locator("#new-email").fill("pessoa.sintetica@example.invalid")
    page.locator("#new-password").fill("senha-sintetica-1")
    page.locator("#new-password-confirm").fill("senha-sintetica-1")
    page.locator("#create-btn").click()
    expect(page.locator("#users-status")).to_contain_text("criado")
    row = page.locator("#users-tbody tr", has_text="pessoa.sintetica@example.invalid")
    expect(row).to_contain_text("Usuário")
    edit = row.locator("[data-edit-user-id]")
    edit.click()
    expect(page.locator("#edit-title")).to_be_focused()
    page.keyboard.press("Escape")
    expect(page.locator("#edit-card")).to_be_hidden()
    expect(edit).to_be_focused()
    assert page.evaluate(UNLABELLED) == []
    screenshot(page, "users-desktop.png")
    for path, name in (("/admin/users", "users-mobile.png"), ("/account", "account-mobile.png"), ("/admin/integrations", "integrations-mobile.png"), ("/", "workspace-empty-mobile.png")):
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(runtime.origin + path)
        expect(page.locator("h1")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), path
        screenshot(page, name)
    assert not errors
    context.close()
