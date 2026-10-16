"""Independent review regressions: uncertain receipts and unavailable metrics."""
import json

from playwright.sync_api import expect

from test_mcp_vertical import runtime
from test_upload_admission import pdf
from test_workspace_ui import browser, login, ready


def test_truncated_admission_receipt_replays_same_request(runtime, browser):
    runtime.start()
    context = browser.new_context()
    login(context, runtime.origin)
    page = context.new_page()
    ready(page, runtime.origin)
    requests = []

    def admission(route):
        request = route.request
        if request.method != "POST":
            return route.continue_()
        requests.append((request.headers.get("idempotency-key"), request.post_data_json))
        response = route.fetch()
        assert response.ok
        if len(requests) == 1:
            # The backend admitted the batch; the browser cannot decode its receipt.
            route.fulfill(status=response.status, content_type="application/json", body='{"batch_id":')
        else:
            route.fulfill(response=response)

    page.route("**/api/v1/batches", admission)
    page.locator("#multi-file-input").set_input_files({
        "name": "uncertain-receipt.pdf", "mimeType": "application/pdf", "buffer": pdf(1)})
    page.locator("#queue-button").click()
    expect(page.locator("#queue-button")).to_have_text("Retomar envio anterior")
    page.locator("#queue-button").click()
    expect(page.locator(".file-card.status-completed")).to_have_count(1, timeout=20000)
    page.locator('.file-card [data-action="detail"]').click()
    expect(page.locator("#detail-status")).to_have_text("Concluído")
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert requests[0][0]
    context.close()


def test_metrics_failure_does_not_report_empty_history(runtime, browser):
    runtime.start()
    context = browser.new_context()
    login(context, runtime.origin)
    page = context.new_page()
    page.route("**/api/v1/metrics", lambda route: route.fulfill(
        status=503, content_type="application/json",
        body=json.dumps({"error": {"code": "temporarily_unavailable", "message": "Synthetic failure"}})))
    ready(page, runtime.origin)
    page.locator("#multi-file-input").set_input_files({
        "name": "metrics-outage.pdf", "mimeType": "application/pdf", "buffer": pdf(1)})
    page.locator("#queue-button").click()
    expect(page.locator(".file-card.status-completed")).to_have_count(1, timeout=20000)
    expect(page.locator("#status-summary")).not_to_contain_text("Nenhum documento")
    context.close()


def test_mobile_detail_keeps_focus_on_visible_controls(runtime, browser):
    runtime.start()
    context = browser.new_context(viewport={"width": 390, "height": 844})
    login(context, runtime.origin)
    page = context.new_page()
    ready(page, runtime.origin)
    page.locator("#multi-file-input").set_input_files({
        "name": "mobile-focus.pdf", "mimeType": "application/pdf", "buffer": pdf(1)})
    page.locator("#queue-button").click()
    expect(page.locator(".file-card.status-completed")).to_have_count(1, timeout=20000)
    opener = page.locator('.file-card [data-action="detail"]')
    opener.click()
    drawer = page.locator("#detail-drawer")
    expect(drawer).to_have_attribute("aria-modal", "true")
    page.route("**/api/v1/jobs/*/text", lambda route: route.fulfill(
        status=503, content_type="application/json",
        body=json.dumps({"error": {"code": "result_unavailable", "message": "Synthetic result failure"}})))
    page.locator("#text-button").click()
    expect(drawer.get_by_role("alert")).to_be_visible()
    expect(drawer.get_by_role("alert")).to_contain_text("result_unavailable")
    for _ in range(20):
        page.keyboard.press("Tab")
        assert page.evaluate("document.querySelector('#detail-drawer').contains(document.activeElement)")
    page.keyboard.press("Shift+Tab")
    assert page.evaluate("document.querySelector('#detail-drawer').contains(document.activeElement)")
    page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()
    expect(opener).to_be_focused()
    assert not page.locator(".topbar").evaluate("el => el.inert")
    context.close()
