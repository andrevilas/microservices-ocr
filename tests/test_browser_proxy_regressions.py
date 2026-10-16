"""Browser-origin, initial failure recovery and panel sizing regressions."""
import json
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError
from playwright.sync_api import expect

from app.config import Settings, settings
from test_mcp_vertical import runtime
from test_upload_admission import pdf, system
from test_workspace_ui import browser, login, ready, screenshot


def test_public_origin_is_explicit_and_does_not_trust_forwarded_headers(system, monkeypatch):
    client = system.client
    client.cookies.set('access_token', system.tokens[0])
    headers = {'Origin': 'https://ocr.example.test', 'X-OCR-Browser': '1', 'Sec-Fetch-Site': 'same-origin'}
    monkeypatch.setattr(settings, 'browser_public_origin', None)
    assert client.post('/auth/browser-session', headers=headers).status_code == 403
    assert client.post('/auth/browser-session', headers={**headers, 'X-Forwarded-Proto': 'https', 'X-Forwarded-Host': 'ocr.example.test'}).status_code == 403
    monkeypatch.setattr(settings, 'browser_public_origin', 'https://ocr.example.test')
    response = client.post('/auth/browser-session', headers=headers)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert 'set-cookie' not in response.headers
    for origin in ['https://evil.test', 'http://ocr.example.test', 'https://ocr.example.test.evil.test', 'https://ocr.example.test:444', 'null']:
        assert client.post('/auth/browser-session', headers={**headers, 'Origin': origin}).status_code == 403
    assert client.post('/auth/browser-session', headers={**headers, 'Sec-Fetch-Site': 'cross-site'}).status_code == 403
    assert client.post('/auth/browser-session', headers={'Origin': headers['Origin']}).status_code == 403
    assert client.post('/auth/browser-session', headers={**headers, 'Origin': 'http://testserver'}).status_code == 200
    assert client.get('/api/v1/jobs').status_code == 401


@pytest.mark.parametrize('origin', ['http://ocr.test', 'https://u:p@ocr.test', 'https://ocr.test/path', 'https://ocr.test?q=1', 'https://ocr.test#x', 'https://*.test', 'https://ocr.test:0', ' https://ocr.test'])
def test_public_origin_config_rejects_unsafe_values(origin):
    with pytest.raises(ValidationError):
        Settings(browser_public_origin=origin)


def test_https_browser_origin_forwarded_to_http_backend(runtime, browser):
    public = 'https://ocr-proxy.example.test'
    runtime.env['BROWSER_PUBLIC_ORIGIN'] = public
    runtime.start()
    context = browser.new_context()
    login(context, runtime.origin)
    cookie = next(c for c in context.cookies() if c['name'] == 'access_token')
    context.add_cookies([{'name': 'access_token', 'value': cookie['value'], 'url': public, 'httpOnly': True, 'secure': True, 'sameSite': 'Lax'}])
    responses = []

    def proxy(route):
        # Browser sees HTTPS; ASGI receives HTTP. No forwarded-header trust.
        parsed = urlsplit(route.request.url)
        response = route.fetch(url=runtime.origin + parsed.path + ('?' + parsed.query if parsed.query else ''))
        if parsed.path == '/auth/browser-session':
            responses.append((route.request.headers.get('origin'), response.status))
        route.fulfill(response=response)

    context.route(public + '/**', proxy)
    page = context.new_page()
    ready(page, public)
    assert responses == [(public, 200)]
    expect(page.locator('#options-summary')).not_to_contain_text('Carregando')
    page.locator('#multi-file-input').set_input_files({'name': 'proxy-synthetic.pdf', 'mimeType': 'application/pdf', 'buffer': pdf(1)})
    page.locator('#queue-button').click()
    expect(page.locator('.file-card.status-completed')).to_have_count(1, timeout=20000)
    page.reload()
    expect(page.locator('.file-card.status-completed')).to_have_count(1, timeout=15000)
    page.get_by_role('link', name='Integrações', exact=True).click()
    expect(page.locator('#service-list > li')).to_have_count(2)
    expect(page.locator('#integrations-error')).to_be_hidden()
    context.close()


@pytest.mark.parametrize('failure', ['forbidden', 'network', 'invalid_json'])
def test_session_failure_is_explicit_and_retry_recovers(runtime, browser, failure):
    runtime.start()
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    login(context, runtime.origin)
    page = context.new_page()

    def fail(route):
        if failure == 'network':
            route.abort('failed')
        else:
            route.fulfill(status=403 if failure == 'forbidden' else 200, content_type='application/json', body='{}' if failure == 'forbidden' else '{')

    page.route('**/auth/browser-session', fail)
    page.goto(runtime.origin)
    expect(page.locator('#status-summary')).to_have_text('Histórico indisponível')
    expect(page.locator('#empty-state')).to_be_hidden()
    expect(page.locator('#options-summary')).to_have_text('Aguardando recuperação da sessão')
    expect(page.locator('#queue-button')).to_be_disabled()
    screenshot(page, 'session-error-' + failure + '.png')
    page.unroute('**/auth/browser-session')
    page.get_by_role('button', name='Tentar novamente', exact=True).click()
    expect(page.locator('#job-state')).to_have_text('Histórico sincronizado.', timeout=15000)
    expect(page.locator('#empty-state')).to_be_visible()
    expect(page.locator('#options-summary')).not_to_contain_text('Carregando')
    assert page.url == runtime.origin + '/'
    context.close()


def test_integration_session_failure_can_retry_without_logout(runtime, browser):
    runtime.start()
    context = browser.new_context()
    login(context, runtime.origin)
    page = context.new_page()
    page.route('**/auth/browser-session', lambda r: r.fulfill(status=403, content_type='application/json', body='{}'))
    page.goto(runtime.origin + '/admin/integrations')
    expect(page.locator('#integrations-error')).to_be_visible()
    expect(page.locator('#integrations-retry')).to_be_visible()
    page.unroute('**/auth/browser-session')
    page.locator('#integrations-retry').click()
    expect(page.locator('#service-list > li')).to_have_count(2)
    expect(page.locator('#integrations-error')).to_be_hidden()
    expect(page.locator('#integrations-retry')).to_be_hidden()
    context.close()


@pytest.mark.parametrize('width', [360, 390, 768, 1080, 1440, 2088])
def test_intake_has_no_horizontal_scroll_with_options_open(runtime, browser, width):
    runtime.start()
    context = browser.new_context(viewport={'width': width, 'height': 844})
    login(context, runtime.origin)
    page = context.new_page()
    ready(page, runtime.origin)
    for opened in (False, True):
        if opened:
            page.locator('#options-disclosure > summary').click()
        assert page.locator('.intake').evaluate('e => e.scrollWidth <= e.clientWidth + 1')
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
        assert page.locator('.dropzone__actions').evaluate('e => e.scrollWidth <= e.clientWidth + 1')
    page.locator('#more-filters > summary').click()
    assert page.locator('.history').evaluate('e => e.scrollWidth <= e.clientWidth + 1')
    screenshot(page, f'layout-{width}-expanded.png')
    context.close()
