"""U2a-T: real Chromium, runtime, authentication and catalog persistence.

Synthetic data only. Routes inject failures, never successful service responses.
The persisted lifecycle event test exercises reload handling, not proof of BFCache.
Coordinator must run the Docker browser suite; this proposal was not executed.
"""
import copy
import json
import re
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import httpx2
import pytest
from playwright.sync_api import expect

from test_mcp_vertical import ROOT, runtime
from test_catalog_mcp_vertical import catalog_accounts
from test_workspace_ui import browser, screenshot, confirm
from test_catalog_fields_ui import field
from test_catalog_mcp_boundaries import reduced_token
from test_catalog_http import legacy_token

BASE = '/api/v1/contexts'
PASSWORD = 'synthetic-test-user-password'


@pytest.fixture
def ui(browser, runtime, catalog_accounts):
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    errors, dialogs = [], []
    def watch(page):
        page.on('pageerror', lambda error: errors.append(str(error)))
        def dismiss_native(dialog):
            dialogs.append(dialog.type)
            dialog.accept()
        page.on('dialog', dismiss_native)
    context.on('page', watch)
    context.set_default_timeout(15000)
    try:
        response = context.request.post(runtime.origin + '/auth/login', data={
            'email': 'catalog-owner@example.com', 'password': PASSWORD,
        }, timeout=15000)
        assert response.status == 200
        with httpx2.Client(base_url=runtime.origin, timeout=15, trust_env=False,
                           headers={'Authorization': 'Bearer ' + catalog_accounts.owner}) as api:
            yield SimpleNamespace(context=context, api=api, runtime=runtime,
                                  accounts=catalog_accounts, dialogs=dialogs)
    finally:
        context.close()
        assert len(errors) == 0
        assert len(dialogs) == 0


def ready(page):
    expect(page.locator('#context-new')).to_be_enabled(timeout=15000)
    expect(page.locator('#context-list-status')).to_contain_text(
        re.compile(r'\d+ contexto\(s\) na lista\.|Você ainda não tem contextos\.'), timeout=15000)


def opened(ui):
    page = ui.context.new_page()
    page.goto(ui.runtime.origin + '/contexts')
    ready(page)
    return page


def draft(page, name='Contexto sintético', description='Descrição sintética'):
    page.locator('#context-new').click()
    expect(page.locator('#context-form')).to_be_visible()
    page.locator('#context-name').fill(name)
    page.locator('#context-description').fill(description)


def revision(page, number):
    expect(page.locator('#context-revision')).to_contain_text(re.compile(rf'\b{number}\b'))


def locked(page):
    for selector in ('#context-name', '#context-description'):
        expect(page.locator(selector)).to_have_attribute('readonly', '')
    expect(page.locator('#context-save')).to_be_disabled()
    expect(page.get_by_role('button', name='Adicionar campo', exact=True)).to_be_disabled()


def private(page):
    assert page.evaluate('''async () => {
        const names = ['access_token', 'accessToken', 'authToken', 'token',
                       'ocrToken', '__OCR_TOKEN__', '__OCR_ACCESS_TOKEN__'];
        return localStorage.length === 0 && sessionStorage.length === 0 &&
            (await indexedDB.databases()).length === 0 &&
            names.every(name => window[name] == null) && location.search === '';
    }''') is True


def record(ui, context_id):
    response = ui.api.get(BASE + '/' + context_id)
    assert response.status_code == 200
    return response.json()


def records(ui):
    result, cursor = [], None
    while True:
        response = ui.api.get(BASE, params={'limit': 25, **({'after': cursor} if cursor else {})})
        assert response.status_code == 200
        payload = response.json()
        result.extend(payload['items'])
        cursor = payload['next_cursor']
        if cursor is None:
            return result


def seed(ui, name='Contexto inicial', fields=None):
    response = ui.api.post(BASE, headers={'Idempotency-Key': 'synthetic-seed-' + uuid4().hex}, json={
        'name': name, 'description': 'Descrição sintética', 'common_fields': fields or [],
    })
    assert response.status_code == 201
    return record(ui, response.json()['context_id'])


def put(ui, current, body):
    response = ui.api.put(BASE + '/' + current['context_id'], json={
        'body': body, 'expected_revision': current['revision'],
    })
    assert response.status_code == 200
    return response.json()


def select(page, name):
    page.locator('#context-list').get_by_role('button', name=name, exact=True).click()
    expect(page.locator('#context-name')).to_have_value(name)
    revision(page, 1)


def capture_posts(page):
    requests = []
    def observe(request):
        if request.method == 'POST' and urlsplit(request.url).path == BASE:
            requests.append((request.post_data, request.header_value('idempotency-key')))
    page.on('request', observe)
    return requests


def advanced():
    return field(type='enum', label='Documento', required=True, critical=True, evidence_required=True,
                 indexable=True, max_items=7, examples=['A'], enum=['A', 'B'])


@pytest.mark.parametrize('width', [375, 1440])
def test_common_user_create_reload_update_and_safe_literal(ui, width):
    page = ui.context.new_page()
    page.set_viewport_size({'width': width, 'height': 1000})
    page.goto(ui.runtime.origin + '/contexts')
    ready(page)
    posts = capture_posts(page)
    literal = '<img src=synthetic-xss onerror="window.syntheticXss=true">'
    draft(page, ' Contexto inválido ', literal)
    page.get_by_role('button', name='Adicionar campo', exact=True).click()
    card = page.locator('.catalog-fields__card')
    card.get_by_label('Chave do campo', exact=True).fill('documento')
    card.get_by_label('Descrição', exact=True).fill('Descrição sintética')
    card.get_by_label('Usar chave como rótulo', exact=True).uncheck()
    card.get_by_label('Rótulo', exact=True).fill('Documento')
    page.locator('#context-save').click()
    expect(page.locator('#context-name-error')).to_be_visible()
    assert len(posts) == 0
    name = '\ufeffContexto sintético\ufeff'
    page.locator('#context-name').fill(name)
    page.locator('#context-save').click()
    expect(page.locator('#context-status')).to_contain_text('Contexto criado. A versão atual foi recuperada.')
    revision(page, 1)
    current = record(ui, records(ui)[0]['context_id'])
    expected = {'name': name, 'description': literal,
                'common_fields': [field(label='Documento')]}
    assert current['body'] == expected
    assert len(current['body']['common_fields'][0]) == 16
    expect(page.locator('#context-description')).to_have_value(literal)
    expect(page.locator('img[src="synthetic-xss"]')).to_have_count(0)
    assert page.evaluate('window.syntheticXss !== true') is True
    page.reload()
    ready(page)
    select(page, name)
    expect(page.locator('#context-description')).to_have_value(literal)
    expect(page.locator('.catalog-fields__card')).to_have_count(1)
    updated = '\ufeffContexto atualizado\ufeff'
    page.locator('#context-name').fill(updated)
    page.locator('#context-save').click()
    expect(page.locator('#context-status')).to_contain_text('Contexto salvo.')
    revision(page, 2)
    expected['name'] = updated
    assert record(ui, current['context_id'])['body'] == expected
    assert record(ui, current['context_id'])['revision'] == 2
    assert len(posts) == 1
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth') is True
    private(page)
    screenshot(page, f'catalog-common-user-{width}.png')


def test_pagination_and_refresh_preserve_draft(ui):
    seeded = [seed(ui, f'Contexto {index:02d}') for index in range(26)]
    page = opened(ui)
    expect(page.locator('#context-list li.entity')).to_have_count(25)
    draft(page, 'Rascunho preservado')
    page.locator('#context-more').click()
    expect(page.locator('#context-list li.entity')).to_have_count(26)
    names = page.locator('#context-list button').all_text_contents()
    assert len(names) == len(set(names)) == 26
    assert set(names) == {item['body']['name'] for item in seeded}
    expect(page.locator('#context-more')).to_be_hidden()
    page.locator('#context-refresh').click()
    expect(page.locator('#context-list li.entity')).to_have_count(25)
    expect(page.locator('#context-name')).to_have_value('Rascunho preservado')
    expect(page.locator('#context-name')).to_be_editable()


@pytest.mark.parametrize('mode', ['read', 'legacy'])
def test_real_reduced_and_legacy_permissions(ui, mode):
    current = seed(ui, fields=[advanced()])
    secret = ui.runtime.env['JWT_SECRET']
    token = (reduced_token(ui.accounts.owner, ['contexts:read'], secret) if mode == 'read'
             else legacy_token(ui.accounts.owner_id, secret))
    ui.context.add_cookies([{'name': 'access_token', 'value': token,
                            'url': ui.runtime.origin, 'httpOnly': True, 'sameSite': 'Lax'}])
    page = ui.context.new_page()
    posts = capture_posts(page)
    page.goto(ui.runtime.origin + '/contexts')
    if mode == 'read':
        expect(page.locator('#context-list')).to_contain_text(current['body']['name'])
        select(page, current['body']['name'])
        expect(page.locator('#context-save')).to_be_disabled()
        for selector in ('#context-name', '#context-description', '#context-fields input',
                         '#context-fields textarea', '#context-fields select', '#context-fields button'):
            for control in page.locator(selector).all():
                assert control.evaluate('(el) => el.disabled || el.readOnly') is True
        expect(page.get_by_role('button', name='Adicionar campo', exact=True)).to_be_disabled()
    else:
        expect(page.locator('#context-status')).to_contain_text('contexts:read')
        expect(page.locator('#context-login')).to_be_visible()
        expect(page.locator('#context-list button')).to_have_count(0)
    expect(page.locator('#context-new')).to_be_disabled()
    response = ui.context.request.get(ui.runtime.origin + '/api/v1/me',
                                     headers={'Authorization': 'Bearer ' + token}, timeout=15000)
    assert response.status == 200
    assert set(response.json()['scopes']) == ({'contexts:read'} if mode == 'read' else {
        'jobs:read', 'artifacts:read', 'jobs:submit', 'jobs:cancel', 'artifacts:delete'})
    assert len(posts) == 0
    private(page)


def test_two_tabs_conflict_requires_explicit_adoption(ui):
    initial = seed(ui, fields=[advanced()])
    a, b = opened(ui), opened(ui)
    for page in (a, b):
        select(page, initial['body']['name'])
    writes = []
    b.on('request', lambda request: writes.append(request.post_data)
         if request.method == 'PUT' and urlsplit(request.url).path == BASE + '/' + initial['context_id'] else None)
    a.locator('#context-name').fill('Versão A')
    a.locator('#context-save').click()
    expect(a.locator('#context-status')).to_contain_text('Contexto salvo.')
    saved = record(ui, initial['context_id'])
    expected = copy.deepcopy(initial['body'])
    expected['name'] = 'Versão A'
    assert saved['body'] == expected and saved['revision'] == 2
    b.locator('#context-name').fill('Rascunho B')
    b.locator('.catalog-fields__card').get_by_label('Descrição', exact=True).fill('Campo B')
    with b.expect_response(lambda response: response.request.method == 'PUT' and response.status == 409) as conflict:
        b.locator('#context-save').click()
    assert conflict.value.json()['error']['code'] == 'context_revision_conflict'
    expect(b.locator('#context-consult')).to_be_visible()
    b.locator('#context-consult').click()
    expect(b.locator('#context-comparison')).to_be_visible()
    expect(b.locator('#context-current')).to_contain_text('Versão A')
    expect(b.locator('#context-name')).to_have_value('Rascunho B')
    expect(b.locator('.catalog-fields__card').get_by_label('Descrição', exact=True)).to_have_value('Campo B')
    revision(b, 1)
    b.locator('#context-use-current').click()
    expect(b.locator('#confirm-dialog')).to_be_visible()
    b.locator('#confirm-cancel').click()
    expect(b.locator('#confirm-dialog')).to_be_hidden()
    expect(b.locator('#context-name')).to_have_value('Rascunho B')
    revision(b, 1)
    b.locator('#context-use-current').click()
    confirm(b)
    expect(b.locator('#context-name')).to_have_value('Versão A')
    revision(b, 2)
    assert len(writes) == 1
    assert record(ui, initial['context_id']) == saved


def test_post_commit_loss_quota_then_real_replay(ui):
    page = opened(ui)
    attempts, statuses = capture_posts(page), []
    def intercept(route):
        if route.request.method != 'POST':
            return route.continue_()
        if len(attempts) == 2:
            return route.fulfill(status=429, content_type='application/json',
                                 body=json.dumps({'error': {'code': 'context_quota_exceeded'}}))
        response = route.fetch(timeout=15000)
        statuses.append(response.status)
        if len(attempts) == 1:
            assert response.status == 201
            return route.abort('connectionreset')
        assert response.status == 200
        route.fulfill(response=response)
    page.route('**/api/v1/contexts', intercept)
    draft(page, 'Criação recuperável')
    page.locator('#context-save').click()
    expect(page.locator('#context-recover-create')).to_be_visible()
    locked(page)
    committed = records(ui)
    assert len(committed) == 1 and statuses == [201]
    context_id = committed[0]['context_id']
    with page.expect_response(lambda response: response.status == 429):
        page.locator('#context-recover-create').click()
    expect(page.locator('#context-recover-create')).to_be_enabled()
    locked(page)
    expect(page.locator('#context-name')).to_have_value('Criação recuperável')
    page.locator('#context-recover-create').click()
    expect(page.locator('#context-status')).to_contain_text('Contexto criado. A versão atual foi recuperada.')
    assert statuses == [201, 200]
    assert len(attempts) == 3 and attempts[0] == attempts[1] == attempts[2]
    assert attempts[0][1] and 8 <= len(attempts[0][1]) <= 128
    assert attempts[0][1].isascii()
    assert [item['context_id'] for item in records(ui)] == [context_id]
    current = record(ui, context_id)
    assert current['revision'] == 1
    assert current['body'] == json.loads(attempts[0][0])
    revision(page, 1)


@pytest.mark.parametrize('divergent', [False, True])
def test_put_commit_loss_consults_without_retry(ui, divergent):
    initial = seed(ui, fields=[advanced()])
    page = opened(ui)
    select(page, initial['body']['name'])
    writes = []
    def intercept(route):
        if route.request.method != 'PUT':
            return route.continue_()
        writes.append(route.request.post_data)
        response = route.fetch(timeout=15000)
        assert response.status == 200
        route.abort('connectionreset')
    page.route('**/api/v1/contexts/*', intercept)
    page.locator('#context-name').fill('Gravação ambígua')
    page.locator('#context-save').click()
    expect(page.locator('#context-consult')).to_be_visible()
    locked(page)
    saved = record(ui, initial['context_id'])
    expected = copy.deepcopy(initial['body'])
    expected['name'] = 'Gravação ambígua'
    assert saved['body'] == expected and saved['revision'] == 2
    if divergent:
        body = copy.deepcopy(saved['body'])
        body['name'] = 'Terceira versão'
        saved = put(ui, saved, body)
        assert saved['revision'] == 3
    page.locator('#context-consult').click()
    if divergent:
        expect(page.locator('#context-current')).to_contain_text('Terceira versão')
        expect(page.locator('#context-name')).to_have_value('Gravação ambígua')
        revision(page, 1)
        page.locator('#context-use-current').click()
        confirm(page)
        expect(page.locator('#context-name')).to_have_value('Terceira versão')
        revision(page, 3)
    else:
        expect(page.locator('#context-status')).to_contain_text('A gravação foi confirmada.')
        revision(page, 2)
    expect(page.locator('#context-name')).to_be_editable()
    assert len(writes) == 1
    assert json.loads(writes[0]) == {'body': expected, 'expected_revision': 1}
    assert record(ui, initial['context_id']) == saved


def test_bootstrap_retry_then_401_does_not_recover_implicitly(ui):
    page = ui.context.new_page()
    calls = []
    def bootstrap(route):
        if route.request.method != 'POST':
            return route.continue_()
        calls.append(True)
        if len(calls) == 1:
            return route.abort('connectionreset')
        response = route.fetch(timeout=15000)
        assert response.status == 200
        route.fulfill(response=response)
    page.route('**/auth/browser-session', bootstrap)
    page.goto(ui.runtime.origin + '/contexts')
    expect(page.locator('#context-bootstrap')).to_be_visible()
    expect(page.locator('#context-bootstrap')).to_be_enabled()
    page.locator('#context-bootstrap').click()
    ready(page)
    assert len(calls) == 2
    draft(page, 'Rascunho após bootstrap')
    def unauthorized(route):
        if route.request.method == 'GET' and urlsplit(route.request.url).path == BASE:
            return route.fulfill(status=401, content_type='application/json',
                                 body=json.dumps({'error': {'code': 'unauthorized'}}))
        route.continue_()
    page.route('**/api/v1/contexts*', unauthorized)
    page.locator('#context-refresh').click()
    expect(page.locator('#context-login')).to_be_visible()
    expect(page.locator('#context-name')).to_have_value('Rascunho após bootstrap')
    page.locator('#context-login').click()
    expect(page.locator('#confirm-dialog')).to_be_visible()
    page.locator('#confirm-cancel').click()
    expect(page.locator('#confirm-dialog')).to_be_hidden()
    expect(page).to_have_url(ui.runtime.origin + '/contexts')
    expect(page.locator('#context-name')).to_have_value('Rascunho após bootstrap')
    assert len(calls) == 2
    private(page)


def test_logout_cancel_failure_and_real_success(ui):
    page = opened(ui)
    draft(page, 'Rascunho antes de sair')
    attempts, committed = [], []
    def logout(route):
        if route.request.method != 'POST':
            return route.continue_()
        attempts.append(True)
        if len(attempts) == 1:
            return route.fulfill(status=503, content_type='application/json', body='{"error":{"code":"unavailable"}}')
        response = route.fetch(timeout=15000)
        assert response.status == 200
        committed.append(True)
        route.fulfill(response=response)
    page.route('**/auth/logout', logout)
    page.locator('#logout-button').click()
    expect(page.locator('#confirm-dialog')).to_be_visible()
    page.locator('#confirm-cancel').click()
    expect(page.locator('#confirm-dialog')).to_be_hidden()
    assert len(attempts) == 0
    response = ui.context.request.post(ui.runtime.origin + '/auth/browser-session',
        headers={'X-OCR-Browser': '1', 'Origin': ui.runtime.origin}, timeout=15000)
    assert response.status == 200
    expect(page.locator('#context-name')).to_have_value('Rascunho antes de sair')
    page.locator('#logout-button').click()
    with page.expect_response(lambda response: urlsplit(response.url).path == '/auth/logout' and response.status == 503):
        confirm(page)
    expect(page.locator('#context-name')).to_be_editable()
    expect(page.locator('#context-name')).to_have_value('Rascunho antes de sair')
    assert len(attempts) == 1 and len(committed) == 0
    page.locator('#logout-button').click()
    confirm(page)
    expect(page).to_have_url(re.compile(r'/login(?:\?.*)?$'))
    assert len(attempts) == 2 and len(committed) == 1
    assert len(ui.dialogs) == 0


def test_guarded_navigation_back_and_persisted_lifecycle_reload(ui):
    page = opened(ui)
    draft(page, 'Rascunho de navegação')
    page.locator('.topnav a[href="/"]').click()
    confirm(page)
    expect(page).to_have_url(ui.runtime.origin + '/')
    assert len(ui.dialogs) == 0
    page.go_back()
    ready(page)
    draft(page, 'Salvo após voltar')
    page.locator('#context-save').click()
    expect(page.locator('#context-status')).to_contain_text('Contexto criado. A versão atual foi recuperada.')
    assert record(ui, records(ui)[0]['context_id'])['body']['name'] == 'Salvo após voltar'
    # Deterministic lifecycle branch injection; ordinary back above makes no BFCache claim.
    with page.expect_navigation(wait_until='domcontentloaded', timeout=15000):
        page.evaluate('''() => {
            dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
            dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
        }''')
    ready(page)
    draft(page, 'Salvo após lifecycle')
    page.locator('#context-save').click()
    expect(page.locator('#context-status')).to_contain_text('Contexto criado. A versão atual foi recuperada.')
    persisted = [record(ui, item['context_id']) for item in records(ui)]
    assert {item['body']['name'] for item in persisted} == {'Salvo após voltar', 'Salvo após lifecycle'}
    assert all(item['revision'] == 1 for item in persisted)
    private(page)
