"""Browser checks for PDF/A in the interface: capabilities, retained failed reports and result controls.

Synthetic data only: history, reports and capabilities are mocked HTTP responses; no document is processed.
"""
from playwright.sync_api import expect

from test_mcp_vertical import runtime
from test_ui_design import RETENTION, job, mock_history, new_page
from test_workspace_ui import browser, ready

FAILED_ID, COMPLETED_ID = "f" * 32, "c" * 32
EXPIRED = {"state": "expired", "expires_at": "2026-10-15T12:00:00Z", "unavailable_at": "2026-10-15T12:00:00Z",
           "purged_at": None, "purge_pending": True}
PDFA_OPTIONS = {"processing_mode": "preserve_existing_text", "languages": ["eng", "por"], "output_format": "pdfa",
                "compression_profile": "balanced", "fallback_policy": "disabled"}
REPORT = {"job_id": FAILED_ID, "status": "failed", "generated_at": "2026-10-15T12:00:00Z", "duration_ms": 1200, "page_count": 1,
          "options_effective": PDFA_OPTIONS,
          "output_validation": {"requested_format": "pdfa", "pdf_readable": True, "page_count_matches": True,
                                "pdfa_conformance": "failed", "pdfa_profile": "PDF/A-2B", "pdfa_validator": "verapdf",
                                "pdfa_validator_version": "1.30.2", "pdfa_reason_codes": ["pdfa_rule_failed"]},
          "warnings": []}
UNAVAILABLE = {"error": {"code": "result_unavailable", "message": "Synthetic result unavailable."}}


def failed(retention=RETENTION):
    return job("f", "failed", retention=retention, error_code="pdfa_validation_failed",
               options_requested=PDFA_OPTIONS)


def capabilities(pdfa):
    verapdf = ({"available": True, "version": "1.30.2", "reasons": []} if pdfa
               else {"available": False, "version": None, "reasons": ["validator_not_configured"]})
    return {"schema_version": "1.0",
            "engines": {"ocrmypdf": {"available": True, "version": "16.13.0", "reasons": []}, "verapdf": verapdf},
            "languages": ["eng", "por"], "processing_modes": ["extract_text", "preserve_existing_text"],
            "output_formats": (["pdfa", "pdf_searchable"] if pdfa else ["pdf_searchable"]),
            "compression_profiles": ["none", "balanced", "economic", "preservation"],
            "fallback_policies": ["disabled"], "fallback_language_sets": [],
            "defaults": PDFA_OPTIONS, "limits": {"text_response_max_chars": 20000},
            "probe_cache_seconds": 60, "warnings": []}


def track(page):
    """Record result requests and refuse text/PDF content: a failed job must never ask for them."""
    calls = []
    page.on("request", lambda request: calls.append(request.url))
    unavailable = lambda route: route.fulfill(status=409, json=UNAVAILABLE)
    page.route(f"**/api/v1/jobs/{FAILED_ID}/text*", unavailable)
    page.route(f"**/api/v1/jobs/{FAILED_ID}/artifact/content", unavailable)
    return calls


def published(calls):
    return [url for url in calls if FAILED_ID in url and ("/text" in url or "/artifact/content" in url)]


def open_detail(page, char):
    page.locator(".file-card", has_text=f"synthetic-{char}.pdf").locator('[data-action="detail"]').first.click()
    expect(page.locator("#detail-drawer")).to_be_visible()


def open_options(page):
    if not page.locator("#options-disclosure").evaluate("(d) => d.open"):
        page.locator("#options-disclosure > summary").click()


def test_capabilities_without_validator_do_not_offer_pdfa(runtime, browser):
    runtime.start()
    context, page, errors = new_page(runtime, browser)
    page.route("**/api/v1/capabilities", lambda route: route.fulfill(json=capabilities(False)))
    mock_history(page, [])
    ready(page, runtime.origin)
    open_options(page)
    expect(page.locator("#output-format option[value=pdfa]")).to_have_count(0)
    expect(page.locator("#output-format")).to_have_value("pdf_searchable")  # not the API default "pdfa"
    expect(page.locator("#ocr-language")).to_have_value("eng+por")  # defaults.languages, not the "eng" fallback
    expect(page.locator("#compression-profile")).to_have_value("balanced")
    expect(page.locator("#capability-note")).to_contain_text("Saída PDF/A indisponível neste servidor")
    expect(page.locator("#capability-note")).to_contain_text("não é oferecida")
    assert not errors
    context.close()


def test_capabilities_with_validator_offer_neutral_pdfa_label(runtime, browser):
    runtime.start()
    context, page, errors = new_page(runtime, browser)
    page.route("**/api/v1/capabilities", lambda route: route.fulfill(json=capabilities(True)))
    mock_history(page, [])
    ready(page, runtime.origin)
    open_options(page)
    expect(page.locator("#output-format")).to_have_value("pdfa")
    label = page.locator("#output-format option[value=pdfa]")
    expect(label).to_have_text("PDF/A-2b (validação obrigatória)")
    assert "aprovado" not in label.inner_text().lower()  # offering PDF/A does not promise conformance
    expect(page.locator("#capability-note")).not_to_contain_text("Saída PDF/A indisponível")
    assert not errors
    context.close()


def test_retained_failed_job_shows_report_without_text_or_download(runtime, browser):
    runtime.start()
    context, page, errors = new_page(runtime, browser)
    calls = track(page)
    page.route(f"**/api/v1/jobs/{FAILED_ID}/report", lambda route: route.fulfill(json=REPORT))
    mock_history(page, [failed()])
    ready(page, runtime.origin)
    open_detail(page, "f")
    expect(page.locator("#results-switch")).to_be_visible()
    expect(page.locator("#text-button")).to_be_hidden()
    expect(page.locator('#detail-actions [data-action="download"]')).to_have_count(0)
    expect(page.locator("#results-unavailable")).to_contain_text("texto e PDF não são publicados")
    page.locator("#report-button").click()
    content = page.locator("#result-content")
    expect(content).to_contain_text("PDF/A-2b reprovado (veraPDF 1.30.2)")
    expect(content).to_contain_text("pdfa_rule_failed")
    expect(page.locator("#report-button")).to_have_attribute("aria-pressed", "true")
    assert not published(calls)
    assert not errors
    context.close()


def test_failed_job_without_report_shows_conflict_and_clears_previous_report(runtime, browser):
    runtime.start()
    context, page, errors = new_page(runtime, browser)
    calls = track(page)
    replies = [REPORT]
    def report(route):
        if replies:
            route.fulfill(json=replies.pop())
        else:
            route.fulfill(status=409, json=UNAVAILABLE)
    page.route(f"**/api/v1/jobs/{FAILED_ID}/report", report)
    mock_history(page, [failed()])
    ready(page, runtime.origin)
    open_detail(page, "f")
    page.locator("#report-button").click()
    expect(page.locator("#result-content")).to_contain_text("PDF/A-2b reprovado")
    page.locator("#report-button").click()  # now answered with 409 result_unavailable
    expect(page.locator("#detail-notice")).to_contain_text("O resultado não foi registrado ou não está mais disponível.")
    expect(page.locator("#result-content")).to_be_empty()  # no stale report remains
    expect(page.locator("#report-button")).to_have_attribute("aria-pressed", "false")
    assert not published(calls)
    assert not errors
    context.close()


def test_expired_failed_job_removes_report_access(runtime, browser):
    runtime.start()
    context, page, errors = new_page(runtime, browser)
    calls = track(page)
    page.route(f"**/api/v1/jobs/{FAILED_ID}/report", lambda route: route.fulfill(json=REPORT))
    state = {"retention": RETENTION}
    mock_history(page, lambda: [failed(state["retention"])])
    ready(page, runtime.origin)
    open_detail(page, "f")
    page.locator("#report-button").click()
    expect(page.locator("#result-content")).to_contain_text("PDF/A-2b reprovado")
    state["retention"] = EXPIRED
    expect(page.locator("#results-switch")).to_be_hidden(timeout=15000)  # next poll sees the expiry
    expect(page.locator("#result-content")).to_be_empty()
    expect(page.locator("#results-unavailable")).to_have_text("Os arquivos deste job não estão mais disponíveis.")
    expect(page.locator("#detail-danger")).to_be_hidden()
    assert not published(calls)
    assert not errors
    context.close()


def test_switching_failed_and_completed_jobs_keeps_matching_controls(runtime, browser):
    runtime.start()
    context, page, errors = new_page(runtime, browser)
    calls = track(page)
    page.route(f"**/api/v1/jobs/{FAILED_ID}/report", lambda route: route.fulfill(json=REPORT))
    mock_history(page, [failed(), job("c")])
    ready(page, runtime.origin)
    download = page.locator('#detail-actions [data-action="download"]')
    for _ in range(2):
        open_detail(page, "f")
        expect(page.locator("#text-button")).to_be_hidden()
        expect(download).to_have_count(0)
        page.locator("#report-button").click()
        expect(page.locator("#result-content")).to_contain_text("PDF/A-2b reprovado")
        page.locator("#detail-close-button").click()
        open_detail(page, "c")
        expect(page.locator("#result-content")).to_be_empty()  # the failed job's report does not carry over
        expect(page.locator("#text-button")).to_be_visible()
        expect(download).to_have_count(1)
        expect(page.locator("#results-unavailable")).to_be_hidden()
        page.locator("#detail-close-button").click()
    assert not published(calls)
    assert not errors
    context.close()
