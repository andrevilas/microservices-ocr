"""Integrations page: actions are unavailable while bootstrapping or busy, failures recover and nothing is sent twice.

Synthetic data only. The one-time credential secret is never printed or captured.
"""
from urllib.parse import urlsplit

from playwright.sync_api import expect

from test_mcp_vertical import runtime
from test_ui_design import new_page
from test_workspace_ui import browser

SERVICES = "/api/v1/service-principals"


def is_services(url):
    return urlsplit(url).path == SERVICES


def wait_for(page, predicate, timeout=15000):
    for _ in range(timeout // 50):
        if predicate():
            return
        page.wait_for_timeout(50)
    raise AssertionError("condition not reached")


def track_creates(page):
    creates = []
    page.on("request", lambda r: creates.append(r.url) if r.method == "POST" and is_services(r.url) else None)
    return creates


def try_submit(page):
    """Attempt every way a user could submit the create form; none may reach the server while it is unavailable."""
    page.locator("#create-service-btn").click(force=True)
    page.locator("#service-name").press("Enter")


def test_delayed_bootstrap_disables_actions_and_first_click_after_load_creates_once(runtime, browser):
    runtime.start()
    context, page, errors = new_page(runtime, browser, viewport={"width": 1280, "height": 900})
    held = []
    page.route(lambda url: is_services(url), lambda route: held.append(route) if route.request.method == "GET" and not held else route.continue_())
    creates = track_creates(page)
    page.goto(runtime.origin + "/admin/integrations")
    wait_for(page, lambda: held)
    expect(page.locator("#integrations-loading")).to_be_visible()
    expect(page.locator("#integrations-loading")).to_have_attribute("role", "status")
    expect(page.locator("#create-service-form")).to_have_attribute("aria-busy", "true")
    page.locator("#create-service-disclosure > summary").click()
    page.locator("#service-name").fill("Importador atrasado")
    expect(page.locator("#create-service-btn")).to_be_disabled()  # visibly unavailable, not silently ignored
    try_submit(page)
    page.wait_for_timeout(300)
    assert creates == []
    held[0].continue_()
    expect(page.locator("#integrations-loading")).to_be_hidden()
    expect(page.locator("#create-service-btn")).to_be_enabled()
    expect(page.locator("#create-service-form")).to_have_attribute("aria-busy", "false")
    expect(page.locator("#service-name")).to_have_value("Importador atrasado")  # typed value survives bootstrap
    page.locator("#create-service-btn").click()  # the first click after loading is the one that creates
    expect(page.locator("#service-title")).to_have_text("Importador atrasado")
    expect(page.locator("#service-title")).to_be_focused()
    assert len(creates) == 1
    assert not errors
    context.close()


def test_create_in_flight_sends_a_single_post(runtime, browser):
    runtime.start()
    context, page, errors = new_page(runtime, browser)
    held = []
    page.route(lambda url: is_services(url), lambda route: held.append(route) if route.request.method == "POST" else route.continue_())
    creates = track_creates(page)
    page.goto(runtime.origin + "/admin/integrations")
    expect(page.locator("#create-service-btn")).to_be_enabled()
    page.locator("#create-service-disclosure > summary").click()
    page.locator("#service-name").fill("Importador único")
    page.locator("#create-service-btn").click()
    wait_for(page, lambda: held)
    expect(page.locator("#create-service-btn")).to_be_disabled()
    expect(page.locator("#service-list [data-service-id]").first).to_be_disabled()
    expect(page.locator("#create-service-form")).to_have_attribute("aria-busy", "true")
    try_submit(page)
    page.wait_for_timeout(300)
    assert len(creates) == 1
    held[0].continue_()
    expect(page.locator("#service-title")).to_have_text("Importador único")
    expect(page.locator("#create-service-btn")).to_be_enabled()
    expect(page.locator("#service-list [data-service-id]").first).to_be_enabled()
    assert len(creates) == 1 and len(held) == 1
    assert not errors
    context.close()


def test_service_list_failure_keeps_form_and_retry_recovers(runtime, browser):
    runtime.start()
    context, page, errors = new_page(runtime, browser)
    failures = []

    def services(route):
        if route.request.method == "GET" and not failures:
            failures.append(route.request.url)
            return route.fulfill(status=503, json={"error": {"code": "storage_unavailable", "message": "synthetic"}})
        route.continue_()

    page.route(lambda url: is_services(url), services)
    creates = track_creates(page)
    page.goto(runtime.origin + "/admin/integrations")
    expect(page.locator("#integrations-error")).to_be_visible()
    expect(page.locator("#integrations-retry")).to_be_visible()
    expect(page.locator("#integrations-retry")).to_be_enabled()
    expect(page.locator("#integrations-loading")).to_be_hidden()
    page.locator("#create-service-disclosure > summary").click()
    page.locator("#service-name").fill("Importador recuperado")
    expect(page.locator("#create-service-btn")).to_be_disabled()  # no session/list: creation is unavailable, not lost
    try_submit(page)
    page.wait_for_timeout(300)
    assert creates == []
    page.locator("#integrations-retry").click()
    expect(page.locator("#integrations-error")).to_be_hidden()
    expect(page.locator("#integrations-retry")).to_be_hidden()
    expect(page.locator("#create-service-btn")).to_be_enabled()
    expect(page.locator("#service-name")).to_have_value("Importador recuperado")  # the filled form was not discarded
    page.locator("#create-service-btn").click()
    expect(page.locator("#service-title")).to_have_text("Importador recuperado")
    assert len(creates) == 1 and len(failures) == 1
    assert not errors
    context.close()


def test_credential_flow_still_works_with_busy_controls(runtime, browser):
    runtime.start()
    context, page, errors = new_page(runtime, browser)
    issues = []
    page.on("request", lambda r: issues.append(r.url) if r.method == "POST" and urlsplit(r.url).path.endswith("/credentials") else None)
    page.goto(runtime.origin + "/admin/integrations")
    page.locator("#create-service-disclosure > summary").click()
    page.locator("#service-name").fill("Serviço de credenciais")
    page.locator("#create-service-btn").click()
    expect(page.locator("#service-title")).to_have_text("Serviço de credenciais")
    page.locator("#issue-disclosure > summary").click()
    page.locator("#credential-name").fill("Leitura sintética")
    page.locator("#issue-btn").click()
    expect(page.locator("#secret-dialog")).to_be_visible()
    expect(page.locator("#secret-done")).to_be_enabled()  # the one-time dialog is never blocked by the busy state
    assert page.locator("#secret-value").input_value().startswith("ocr_svc_")
    page.locator("#secret-done").click()
    expect(page.locator("#secret-dialog")).to_be_hidden()
    assert page.locator("#secret-value").input_value() == ""
    expect(page.locator("#credential-list")).to_contain_text("Ativa")
    expect(page.locator("#issue-btn")).to_be_enabled()
    assert len(issues) == 1
    revoke = page.locator("[data-revoke-id]")
    expect(revoke).to_be_enabled()
    revoke.locator(".visually-hidden").dispatch_event("click")  # a click on the button's inner text resolves to the right action
    expect(page.locator("#confirm-dialog")).to_be_visible()
    page.locator("#confirm-cancel").click()
    expect(page.locator("#confirm-dialog")).to_be_hidden()
    expect(page.locator("#credential-list")).to_contain_text("Ativa")  # cancelling the confirmation revokes nothing
    expect(revoke).to_be_enabled()
    revoke.click()
    expect(page.locator("#confirm-dialog")).to_be_visible()
    page.locator("#confirm-accept").click()
    expect(page.locator("#credential-list")).to_contain_text("Revogada")
    page.locator("#disable-service-btn").click()
    page.locator("#confirm-accept").click()
    expect(page.locator("#service-meta")).to_contain_text("Desativado")
    expect(page.locator("#issue-disclosure")).to_be_hidden()
    expect(page.locator("#create-service-btn")).to_be_enabled()
    assert not errors
    context.close()
