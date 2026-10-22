"""Integrations page: actions are unavailable while bootstrapping or busy, failures recover and nothing is sent twice.

Synthetic data only. The one-time credential secret is never printed or captured.
"""
from urllib.parse import urlsplit

import pytest
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


@pytest.mark.parametrize("viewport_width", [1280, 375])
def test_documental_scopes_are_optional_exact_and_persisted(runtime, browser, viewport_width):
    runtime.start()
    context, page, errors = new_page(
        runtime, browser, viewport={"width": viewport_width, "height": 900}
    )
    try:
        legacy_defaults = ["jobs:read", "artifacts:read"]
        documental_labels = {
            "contexts:read": "Consultar contextos",
            "contexts:write": "Editar rascunhos de contextos",
            "metadata:read": "Consultar metadados",
            "metadata:write": "Salvar rascunhos de metadados",
        }
        expected_scopes = legacy_defaults + [
            "jobs:submit", "jobs:cancel", "artifacts:delete"
        ] + list(documental_labels)
        posted_scopes = []
        credential_lists = []

        def is_credentials(url):
            path = urlsplit(url).path
            return path.startswith(SERVICES + "/") and path.endswith("/credentials")

        def record_post_scopes(request):
            if request.method == "POST" and is_credentials(request.url):
                # Retain only selected scopes, never headers or issuance responses.
                posted_scopes.append(request.post_data_json["scopes"])

        def record_list_scopes(response):
            if response.request.method == "GET" and is_credentials(response.url):
                # Only GET listings are parsed; retain only these two safe fields.
                credential_lists.append([
                    {"name": item["name"], "scopes": item["scopes"]}
                    for item in response.json()
                ])

        def latest_scopes():
            if not credential_lists:
                return {}
            return {
                item["name"]: sorted(item["scopes"])
                for item in credential_lists[-1]
            }

        page.on("request", record_post_scopes)
        page.on("response", record_list_scopes)
        page.goto(runtime.origin + "/admin/integrations")
        expect(page.locator("#create-service-btn")).to_be_enabled()
        page.locator("#create-service-disclosure > summary").click()
        service_name = f"Serviço documental sintético {viewport_width}"
        page.locator("#service-name").fill(service_name)
        page.locator("#create-service-btn").click()
        expect(page.locator("#service-title")).to_have_text(service_name)
        expect(page.locator("#service-title")).to_be_focused()
        expect(page.locator("#issue-btn")).to_be_enabled()
        page.locator("#issue-disclosure > summary").click()
        form = page.locator("#issue-form")
        checkboxes = form.locator('input[type="checkbox"][name="scope"]')
        expect(checkboxes).to_have_count(9)
        assert checkboxes.evaluate_all("nodes => nodes.map(node => node.value)") == expected_scopes

        def selected_scopes():
            return form.locator('input[name="scope"]:checked').evaluate_all(
                "nodes => nodes.map(node => node.value)"
            )

        def assert_defaults():
            assert selected_scopes() == legacy_defaults
            for scope in expected_scopes:
                checkbox = form.locator(f'input[name="scope"][value="{scope}"]')
                if scope in legacy_defaults:
                    expect(checkbox).to_be_checked()
                else:
                    expect(checkbox).not_to_be_checked()

        assert_defaults()
        for scope in ("contexts:publish", "metadata:approve"):
            expect(form.locator(f'input[value="{scope}"]')).to_have_count(0)
        for scope, label_text in documental_labels.items():
            checkbox = form.locator(f'input[name="scope"][value="{scope}"]')
            expect(checkbox).to_be_visible()
            expect(checkbox).to_have_attribute("aria-describedby", "documental-scopes-help")
            label = checkbox.locator("..")
            expect(label).to_be_visible()
            expect(label).to_contain_text(label_text)
            expect(label.locator("code")).to_have_text(scope)

        help_text = page.locator("#documental-scopes-help")
        oauth_help = page.locator("#documental-oauth-help")
        expect(help_text).to_be_visible()
        expect(help_text).to_have_text(
            "As permissões documentais são opcionais. Serviços podem preparar contextos "
            "e tipos em rascunho, mas não publicar schemas nem aprovar metadados. "
            "Salvar metadados exige um tipo publicado do mesmo proprietário; "
            "um serviço novo não dispõe desse fluxo."
        )
        expect(oauth_help).to_be_visible()
        expect(oauth_help).to_have_text(
            "Para o fluxo completo de metadados e aprovação, use OAuth com uma conta "
            "de usuário e permissões explícitas. A configuração OAuth é feita "
            "separadamente desta tela."
        )

        def assert_scope_layout():
            # Measure the rendered controls, labels and guidance, including at 375px.
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            width = page.evaluate("window.innerWidth")
            for checkbox in checkboxes.all():
                for node in (checkbox, checkbox.locator("..")):
                    expect(node).to_be_visible()
                    box = node.bounding_box()
                    assert box is not None
                    assert 0 <= box["x"]
                    assert box["x"] + box["width"] <= width
            for node in (help_text, oauth_help):
                box = node.bounding_box()
                assert box is not None
                assert 0 <= box["x"]
                assert box["x"] + box["width"] <= width

        assert_scope_layout()
        legacy_name = f"Leitura legada sintética {viewport_width}"
        page.locator("#credential-name").fill(legacy_name)
        page.locator("#issue-btn").click()
        expect(page.locator("#secret-dialog")).to_be_visible()
        page.locator("#secret-done").click()
        expect(page.locator("#secret-dialog")).to_be_hidden()
        expect(page.locator("#issue-btn")).to_be_enabled()
        expect(page.locator("#credential-list > li")).to_have_count(1)
        wait_for(page, lambda: latest_scopes() == {legacy_name: sorted(legacy_defaults)})
        assert posted_scopes == [legacy_defaults]

        page.locator("#issue-disclosure > summary").click()
        assert_defaults()
        second_name = f"Metadados sintéticos {viewport_width}"
        page.locator("#credential-name").fill(second_name)
        form.locator('input[name="scope"][value="metadata:read"]').check()
        second_scopes = legacy_defaults + ["metadata:read"]
        assert selected_scopes() == second_scopes
        page.locator("#issue-btn").click()
        expect(page.locator("#secret-dialog")).to_be_visible()
        page.locator("#secret-done").click()
        expect(page.locator("#secret-dialog")).to_be_hidden()
        expect(page.locator("#issue-btn")).to_be_enabled()
        expect(page.locator("#credential-list > li")).to_have_count(2)
        wait_for(page, lambda: latest_scopes() == {
            legacy_name: sorted(legacy_defaults),
            second_name: sorted(second_scopes),
        })
        assert len(credential_lists[-1]) == 2
        assert posted_scopes == [legacy_defaults, second_scopes]
        second_row = page.locator("#credential-list > li").filter(has_text=second_name)
        expect(second_row).to_have_count(1)
        expect(second_row.locator(".entity__tags")).to_contain_text("Consultar metadados")

        page.locator("#issue-disclosure > summary").click()
        expect(page.locator("#credential-name")).to_have_value("")
        assert_defaults()
        assert_scope_layout()
        assert not errors
    finally:
        context.close()
