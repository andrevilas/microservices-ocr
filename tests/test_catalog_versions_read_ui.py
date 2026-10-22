"""Real REST publication and browser-only immutable-version consultation."""
from contextlib import contextmanager
from copy import deepcopy
import json
import re
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from playwright.sync_api import expect
from test_catalog_ui import (ui, runtime, catalog_accounts, browser, seed, record,
                             select, private, field)
from test_catalog_types_read_ui import seed_type, type_record, open_type, loaded_types, keys
from test_workspace_ui import confirm, screenshot
from test_catalog_mcp_boundaries import reduced_token

CONTEXTS = '/api/v1/contexts'
TYPES = '/api/v1/document-types/'
DETAIL_ERROR = ('Não foi possível abrir a versão. Nenhum detalhe está selecionado. '
                'Selecione uma versão para tentar novamente.')
LIST_ERROR = ('Não foi possível carregar as versões. A lista anterior foi preservada. '
              'Tente novamente.')


def page_for(ui):
    page = ui.context.new_page()
    page.goto(ui.runtime.origin + '/contexts')
    expect(page.locator('#context-refresh')).to_be_enabled()
    return page


def version_record(ui, tid, version):
    response = ui.api.get(f'{TYPES}{tid}/versions/{version}')
    assert response.status_code == 200
    value = response.json()
    assert value['type_id'] == tid and value['version'] == version
    assert re.fullmatch(r'[0-9a-f]{64}', value['schema_sha256'])
    assert value['draft_revision'] >= 1 and value['context_revision'] >= 1
    assert isinstance(value['schema_version'], str) and value['schema_version']
    assert value['lifecycle']['state'] in ('published', 'deprecated')
    assert value['lifecycle']['revision'] >= 1
    assert len(value['schema']['fields']) == sum(
        len(value['schema'][key]) for key in ('common_fields', 'specific_fields'))
    return value


def publish(ui, parent, item):
    response = ui.api.post(f"{TYPES}{item['type_id']}/versions",
                          headers={'Idempotency-Key': uuid4().hex}, json={
                              'expected_revision': item['revision'],
                              'expected_context_revision': parent['revision']})
    assert response.status_code == 201
    receipt = response.json()
    assert 'lifecycle' not in receipt
    value = version_record(ui, item['type_id'], receipt['version'])
    for key, expected in (('context_id', parent['context_id']),
                          ('draft_revision', item['revision']),
                          ('context_revision', parent['revision']),
                          ('context_name', parent['body']['name']),
                          ('type_name', item['body']['name'])):
        assert value[key] == expected
    assert value['lifecycle']['state'] == 'published'
    return value


def listed(page, count):
    expect(page.locator('#version-refresh')).to_be_enabled()
    expect(page.locator('#version-list-status')).to_have_text(
        f'{count} versão(ões) na lista. Situação consultada ao carregar a lista.')
    expect(page.locator('#version-list button')).to_have_count(count)


def enter(page, parent, item, readonly=False):
    if parent['revision'] == 1 and not readonly:
        select(page, parent['body']['name'])
    else:
        page.locator('#context-list').get_by_role(
            'button', name=parent['body']['name'], exact=True).click()
        expect(page.locator('#context-name')).to_have_value(parent['body']['name'])
        expect(page.locator('#context-revision')).to_contain_text(
            re.compile(r'Revisão ' + str(parent['revision']) + r'\b'))
    loaded_types(page, 1)
    open_type(page, item, readonly=readonly)


def shown(page, value):
    expect(page.locator('#version-detail-status')).to_have_text(
        f"Versão {value['version']} carregada para consulta.")
    expect(page.locator('#version-detail-status')).to_have_attribute('aria-busy', 'false')
    expect(page.locator('#version-detail')).to_be_visible()
    expect(page.locator('#version-reference')).to_have_text(
        f"Tipo: {value['type_id']} · Versão: {value['version']} · SHA-256: {value['schema_sha256']}")
    expect(page.locator('#version-meta')).to_contain_text(
        f"Contexto: {value['context_name']} · Tipo: {value['type_name']} · "
        f"draft_revision: {value['draft_revision']} · context_revision: {value['context_revision']} · "
        f"schema_version: {value['schema_version']} · Criada em")
    lifecycle = value['lifecycle']
    expect(page.locator('#version-lifecycle')).to_contain_text(
        'Publicada' if lifecycle['state'] == 'published' else 'Descontinuada')
    expect(page.locator('#version-lifecycle')).to_contain_text(f"· Revisão {lifecycle['revision']} ·")
    keys(page, '#version-fields', [entry['name'] for entry in value['schema']['fields']])
    for control in page.locator('#version-fields').locator('input, select, textarea, button').all():
        expect(control).to_be_disabled()


def open_version(page, view):
    button = page.locator('#version-list').get_by_role(
        'button', name=f"Versão {view['version']}", exact=True)
    expect(button).to_be_enabled()
    button.click()
    shown(page, view)


@contextmanager
def held_detail(page, value):
    path = f"{TYPES}{value['type_id']}/versions/{value['version']}"
    pending = {}
    def hold(route):
        assert not pending
        pending['route'] = route
    def release():
        route = pending['route']
        response = route.fetch()
        assert response.status == 200 and response.json() == value
        with page.expect_event('requestfinished', predicate=lambda request: urlsplit(request.url).path == path):
            route.fulfill(response=response)
            pending.clear()
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
    page.route('**' + path, hold)
    try:
        yield release
    finally:
        if pending:
            pending['route'].abort()
        page.unroute('**' + path, hold)


def test_historical_snapshot_preserves_dirty_draft(ui, runtime, catalog_accounts, browser):
    parent = seed(ui, 'Origem histórica', [field('origem', type='boolean')])
    item = seed_type(ui, parent, 'Tipo histórico', [field(
        'numero', type='identifier', required_when={'field': 'origem', 'equals': False}, examples=['0012'])])
    original = deepcopy(publish(ui, parent, item))
    body = deepcopy(parent['body'])
    body.update(name='Contexto atual', description='Descrição atual sintética')
    body['common_fields'][0]['description'] = 'Descrição comum atual'
    assert ui.api.put(CONTEXTS + '/' + parent['context_id'], json={
        'body': body, 'expected_revision': 1}).status_code == 200
    body = deepcopy(item['body'])
    body['name'] = 'Tipo atual'
    body['specific_fields'][0]['label'] = 'Número atual'
    assert ui.api.put(TYPES + item['type_id'], json={'body': body, 'expected_revision': 1}).status_code == 200
    parent, item = record(ui, parent['context_id']), type_record(ui, item['type_id'])
    assert parent['revision'] == item['revision'] == 2
    page, requests = page_for(ui), []
    def observe(request):
        requests.append((request.method, urlsplit(request.url).path))
    page.on('request', observe)
    enter(page, parent, item)
    listed(page, 1)
    page.locator('#context-name').fill('UNSAVED')
    open_version(page, original)
    for details in page.locator('#version-fields details').all():
        if details.get_attribute('open') is None:
            details.locator('summary').click()
    cards = page.locator('#version-fields .catalog-fields__card')
    expect(cards.nth(0).get_by_label('Descrição', exact=True)).to_have_value(
        original['schema']['common_fields'][0]['description'])
    expect(page.locator('#type-common-fields').get_by_label('Descrição', exact=True)).to_have_value(
        'Descrição comum atual')
    expect(cards.nth(1).get_by_label('Rótulo', exact=True)).to_have_value('numero')
    assert json.loads(cards.nth(1).locator('.catalog-fields__condition pre').inner_text()) == {
        'field': 'origem', 'equals': False}
    expect(page.locator('#version-count')).to_have_text(
        '2 campo(s) no esquema histórico: 1 comuns e 1 específicos. Independente do rascunho atual.')
    def dirty():
        expect(page.locator('#context-name')).to_have_value('UNSAVED')
        expect(page.locator('#context-name')).to_be_editable()
        expect(page.locator('#context-save')).to_be_enabled()
        expect(page.locator('#context-revision')).to_contain_text(
            re.compile(r'^Rascunho — tipo de documento · Revisão 2\b'))
        keys(page, '#context-fields', ['numero'])
        keys(page, '#type-common-fields', ['origem'])
    dirty()
    page.locator('#version-close').click()
    expect(page.locator('#version-detail')).to_be_hidden()
    open_version(page, original)
    dirty()
    for width, height in ((375, 812), (1440, 900)):
        page.set_viewport_size({'width': width, 'height': height})
        page.locator('#version-detail').scroll_into_view_if_needed()
        assert page.evaluate('''() => [document.documentElement,
            ...document.querySelectorAll('#version-section, #version-detail, #version-fields')]
            .every(node => node.scrollWidth <= node.clientWidth + 1)''')
        screenshot(page, f'catalog-version-{width}.png')
    private(page)
    assert record(ui, parent['context_id']) == parent
    assert type_record(ui, item['type_id']) == item
    assert version_record(ui, item['type_id'], 1) == original
    assert not any(method in {'POST', 'PUT', 'PATCH', 'DELETE'} for method, path in requests)


def test_pagination_lifecycle_and_invalid_refresh(ui, runtime, catalog_accounts, browser):
    parent = seed(ui, 'Paginação sintética')
    item = seed_type(ui, parent, 'Onze versões', [field('numero')])
    versions = [publish(ui, parent, item) for _ in range(11)]
    path = f"{TYPES}{item['type_id']}/versions"
    response = ui.api.post(path + '/1/deprecate', headers={'Idempotency-Key': uuid4().hex},
                           json={'expected_lifecycle_revision': 1})
    assert response.status_code == 200
    deprecated = version_record(ui, item['type_id'], 1)
    assert deprecated['lifecycle']['state'] == 'deprecated' and deprecated['lifecycle']['revision'] == 2
    assert all(deprecated[key] == versions[0][key] for key in ('schema', 'schema_sha256'))
    first = ui.api.get(path, params={'limit': 10})
    assert first.status_code == 200
    first = first.json()
    cursor = first['next_cursor']
    assert type(cursor) is int and len(first['items']) == 10
    tail = ui.api.get(path, params={'after': cursor})
    assert tail.status_code == 200
    assert [entry['version'] for entry in first['items'] + tail.json()['items']] == list(range(1, 12))
    page = page_for(ui)
    enter(page, parent, item)
    listed(page, 10)
    with page.expect_response(lambda response: urlsplit(response.url).path == path and
                              parse_qs(urlsplit(response.url).query).get('after') == [str(cursor)]) as fetched:
        page.locator('#version-more').click()
    assert fetched.value.status == 200
    listed(page, 11)
    expect(page.locator('#version-more')).to_be_hidden()
    expect(page.locator('#version-list button')).to_have_text([f'Versão {n}' for n in range(1, 12)])
    open_version(page, deprecated)
    def invalid_list(route):
        response = route.fetch()
        assert response.status == 200
        payload = response.json()
        del payload['items'][0]['lifecycle']
        route.fulfill(response=response, json=payload)
    page.route('**' + path + '?*', invalid_list)
    try:
        page.locator('#version-refresh').click()
        expect(page.locator('#version-list-status')).to_have_text(LIST_ERROR)
        expect(page.locator('#version-refresh')).to_be_enabled()
        expect(page.locator('#version-list button')).to_have_count(11)
        expect(page.locator('#version-more')).to_be_hidden()
    finally:
        page.unroute('**' + path + '?*', invalid_list)
    page.locator('#version-refresh').click()
    listed(page, 10)
    page.locator('#version-more').click()
    listed(page, 11)
    open_version(page, deprecated)
    private(page)


def test_reduced_read_scope(ui, runtime, catalog_accounts, browser):
    parent = seed(ui, 'Consulta sintética')
    item = seed_type(ui, parent, 'Tipo consultável', [field('numero')])
    view = publish(ui, parent, item)
    authorization = ui.api.headers['Authorization']
    assert authorization.startswith('Bearer ')
    token = reduced_token(authorization[7:], {'contexts:read'}, ui.runtime.env['JWT_SECRET'])
    headers = {'Authorization': 'Bearer ' + token}
    response = ui.api.get('/api/v1/me', headers=headers)
    assert response.status_code == 200
    assert set(response.json()['scopes']) == {'contexts:read'}
    ui.context.add_cookies([{'name': 'access_token', 'value': token,
                            'url': ui.runtime.origin, 'httpOnly': True, 'sameSite': 'Lax'}])
    page = page_for(ui)
    enter(page, parent, item, readonly=True)
    listed(page, 1)
    open_version(page, view)
    private(page)
    denied = ui.api.post(f"{TYPES}{item['type_id']}/versions", headers={
        **headers, 'Idempotency-Key': uuid4().hex}, json={
            'expected_revision': 1, 'expected_context_revision': 1})
    assert denied.status_code == 403


def test_stale_cross_version_and_cross_type_responses(ui, runtime, catalog_accounts, browser):
    a, b = seed(ui, 'Contexto A'), seed(ui, 'Contexto B')
    ta = seed_type(ui, a, 'Tipo A', [field('campo_a')])
    tb = seed_type(ui, b, 'Tipo B', [field('campo_b')])
    a1, a2, b1 = publish(ui, a, ta), publish(ui, a, ta), publish(ui, b, tb)
    page = page_for(ui)
    enter(page, a, ta)
    listed(page, 2)
    with held_detail(page, a1) as release:
        page.locator('#version-list').get_by_role('button', name='Versão 1', exact=True).click()
        expect(page.locator('#version-detail-status')).to_have_text('Carregando versão 1…')
        expect(page.locator('#version-list').get_by_role('button', name='Versão 2', exact=True)).to_be_enabled()
        open_version(page, a2)
        release()
        shown(page, a2)
    with held_detail(page, a1) as release:
        page.locator('#version-list').get_by_role('button', name='Versão 1', exact=True).click()
        expect(page.locator('#version-detail-status')).to_have_text('Carregando versão 1…')
        expect(page.locator('#context-list').get_by_role('button', name=b['body']['name'], exact=True)).to_be_enabled()
        enter(page, b, tb)
        listed(page, 1)
        open_version(page, b1)
        release()
        shown(page, b1)
        expect(page.locator('#context-name')).to_have_value(tb['body']['name'])
    private(page)


@pytest.mark.parametrize('fault', ['missing_lifecycle', 'invalid_hash', 'invalid_revision'])
def test_invalid_detail_fails_closed_and_retries(ui, runtime, catalog_accounts, browser, fault):
    parent = seed(ui, 'Detalhe negativo sintético')
    item = seed_type(ui, parent, 'Validação estrutural', [field('numero')])
    first, second = publish(ui, parent, item), publish(ui, parent, item)
    page = page_for(ui)
    enter(page, parent, item)
    listed(page, 2)
    open_version(page, first)
    page.locator('#context-name').fill('UNSAVED')
    path = f"{TYPES}{item['type_id']}/versions/2"
    def invalid_detail(route):
        response = route.fetch()
        assert response.status == 200 and response.json() == second
        payload = response.json()
        if fault == 'missing_lifecycle':
            del payload['lifecycle']
        else:
            payload['schema_sha256' if fault == 'invalid_hash' else 'draft_revision'] = (
                ('0' if second['schema_sha256'][0] != '0' else '1') + second['schema_sha256'][1:]
                if fault == 'invalid_hash' else second['draft_revision'] + 1)
        route.fulfill(response=response, json=payload)
    page.route('**' + path, invalid_detail)
    try:
        page.locator('#version-list').get_by_role('button', name='Versão 2', exact=True).click()
        expect(page.locator('#version-detail-status')).to_have_text(DETAIL_ERROR)
        expect(page.locator('#version-detail-status')).to_have_attribute('aria-busy', 'false')
        expect(page.locator('#version-detail')).to_be_hidden()
        expect(page.locator('#version-reference')).to_have_text('')
    finally:
        page.unroute('**' + path, invalid_detail)
    open_version(page, second)
    expect(page.locator('#context-name')).to_have_value('UNSAVED')
    expect(page.locator('#context-save')).to_be_enabled()
    assert type_record(ui, item['type_id']) == item
    assert version_record(ui, item['type_id'], 2) == second
    private(page)
