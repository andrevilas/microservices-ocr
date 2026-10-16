"""CI2 acceptance: real authenticated catalog reads; no type write UI."""
from copy import deepcopy
import re
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.sync_api import expect
from test_catalog_ui import (
    ui, opened, ready, seed, record, select, private, field, runtime,
    catalog_accounts, browser,
)
from test_workspace_ui import confirm, screenshot
from test_catalog_mcp_boundaries import reduced_token

BASE = '/api/v1/contexts'


def type_record(ui, tid):
    response = ui.api.get('/api/v1/document-types/' + tid)
    assert response.status_code == 200
    result = response.json()
    assert result['type_id'] == tid
    assert result['state'] == 'draft'
    assert result['revision'] >= 1
    assert set(result['body']) == {'name', 'description', 'specific_fields'}
    return result


def seed_type(ui, parent, name, fields):
    response = ui.api.post(BASE + '/' + parent['context_id'] + '/types',
                           headers={'Idempotency-Key': uuid4().hex}, json={
                               'name': name, 'description': 'Descrição local sintética',
                               'specific_fields': fields,
                           })
    assert response.status_code == 201
    result = type_record(ui, response.json()['type_id'])
    assert result['context_id'] == parent['context_id']
    assert result['revision'] == 1
    assert result['body']['specific_fields'] == fields
    return result


def loaded_types(page, count):
    expect(page.locator('#type-refresh')).to_be_enabled()
    expect(page.locator('#type-list-status')).to_have_text(
        f'{count} tipo(s) de documento na lista.')
    expect(page.locator('#type-list button')).to_have_count(count)


def open_type(page, item):
    expect(page.locator('#type-refresh')).to_be_enabled()
    expect(page.locator('#type-list-status')).not_to_contain_text('Carregando')
    page.locator('#type-list').get_by_role(
        'button', name=item['body']['name'], exact=True).click()
    type_view(page, item)


def type_view(page, item):
    expect(page.locator('#context-form')).to_be_visible()
    expect(page.locator('#context-name')).to_have_value(item['body']['name'])
    expect(page.locator('#context-description')).to_have_value(item['body']['description'])
    expect(page.locator('#context-revision')).to_contain_text(
        re.compile(r'\bRevisão ' + str(item['revision']) + r'\b'))
    expect(page.locator('#context-status')).to_have_text(
        'Tipo de documento carregado. Consulta somente para leitura.')
    expect(page.locator('#context-save')).to_be_hidden()
    for selector in ('#context-name', '#context-description'):
        expect(page.locator(selector)).to_have_attribute('readonly', '')
    for selector in ('#context-fields', '#type-common-fields'):
        controls = page.locator(selector).locator('input, select, textarea, button')
        assert controls.count() > 0
        for control in controls.all():
            expect(control).to_be_disabled()


def page_for(ui):
    page = ui.context.new_page()
    page.goto(ui.runtime.origin + '/contexts')
    expect(page.locator('#context-refresh')).to_be_enabled()
    return page


def observe(page):
    writes, context_reads = [], []
    def watch(request):
        path = urlsplit(request.url).path
        if path.startswith('/api/v1/') and ('contexts' in path or 'document-types' in path):
            if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
                writes.append((request.method, path))
            if request.method == 'GET' and re.fullmatch(BASE + r'/[^/]+', path):
                context_reads.append(path)
    page.on('request', watch)
    return writes, context_reads


def discard(page, accept):
    dialog = page.get_by_role('dialog')
    expect(dialog).to_be_visible()
    dialog.locator('#confirm-accept' if accept else '#confirm-cancel').click()
    expect(dialog).to_be_hidden()


def keys(page, container, expected):
    cards = page.locator(container + ' .catalog-fields__card')
    expect(cards).to_have_count(len(expected))
    for card, name in zip(cards.all(), expected):
        expect(card.locator('input').first).to_have_value(name)


def test_writer_type_snapshot_and_responsive_readonly(ui):
    common = field('origem', type='enum', enum=['A', 'B'], label='Origem',
                   examples=['A'], required_when={'field': 'numero', 'equals': '0012'})
    local = field('numero', type='identifier', label=None, critical=True,
                  evidence_required=True, sensitivity='restricted', indexable=True,
                  cardinality='many', max_items=7, examples=['0012', 'linha\n2'],
                  destination='xmp')
    assert len(common) == len(local) == 16
    parent = seed(ui, 'Contexto sintético', [common])
    item = seed_type(ui, parent, 'Tipo sintético', [local])
    # Literal markup travels through the real context write API before observation.
    payload = deepcopy(parent['body'])
    payload['description'] = '<img src=synthetic onerror="window.syntheticXss=1">'
    response = ui.api.put(BASE + '/' + parent['context_id'],
                          json={'expected_revision': parent['revision'], 'body': payload},
                          headers={'Idempotency-Key': uuid4().hex})
    assert response.status_code == 200
    before = deepcopy(record(ui, parent['context_id']))
    type_before = deepcopy(type_record(ui, item['type_id']))
    page = page_for(ui)
    writes, _ = observe(page)
    page.locator('#context-list').get_by_role('button', name=parent['body']['name'], exact=True).click()
    expect(page.locator('#context-revision')).to_contain_text(re.compile(r'^Revisão 2\b'))
    open_type(page, item)
    keys(page, '#context-fields', ['numero'])
    keys(page, '#type-common-fields', ['origem'])
    expect(page.locator('#type-common-fields').get_by_label('Rótulo', exact=True)).to_have_value('Origem')
    expect(page.locator('#type-common-fields').get_by_label('Descrição', exact=True)).to_have_value(common['description'])
    expect(page.locator('#context-fields').get_by_label('Descrição', exact=True)).to_have_value(local['description'])
    expect(page.locator('#type-parent')).to_contain_text(parent['body']['name'])
    expect(page.locator('#context-revision')).to_contain_text(parent['body']['name'])
    expect(page.locator('#type-field-count')).to_have_text(
        '2 campo(s): 1 comuns herdados do contexto e 1 específicos do tipo.')
    for detail in page.locator('#context-form details').all():
        if not detail.get_attribute('open') == '':
            detail.locator('summary').click()
    for width, height in ((375, 812), (1440, 900)):
        page.set_viewport_size({'width': width, 'height': height})
        assert page.evaluate('''() => [document.documentElement,
            ...document.querySelectorAll('.panel, .catalog-fields')].every(
            el => el.scrollWidth <= el.clientWidth + 1)''')
        private(page)
        screenshot(page, f'types-readonly-{width}.png')
    page.locator('#type-edit-context').click()
    expect(page.locator('#context-name')).to_have_value(parent['body']['name'])
    expect(page.locator('#context-name')).to_be_editable()
    expect(page.locator('#context-description')).to_have_value(payload['description'])
    expect(page.locator('#context-form img')).to_have_count(0)
    assert page.evaluate('window.syntheticXss === undefined')
    keys(page, '#context-fields', ['origem'])
    expect(page.locator('#type-common-panel')).to_be_hidden()
    expect(page.locator('#context-fields').get_by_role('button', name='Adicionar campo')).to_be_enabled()
    page.reload()
    expect(page.locator('#context-refresh')).to_be_enabled()
    page.locator('#context-list').get_by_role('button', name=parent['body']['name'], exact=True).click()
    open_type(page, item)
    assert record(ui, parent['context_id']) == before
    assert type_record(ui, item['type_id']) == type_before
    assert not writes


def test_dirty_navigation_cancel_accept_and_context_isolation(ui):
    a = seed(ui, 'Contexto A', [field('comum_a')])
    b = seed(ui, 'Contexto B', [field('comum_b')])
    ta = seed_type(ui, a, 'Tipo A1', [field('local_a')])
    tb = seed_type(ui, b, 'Tipo B1', [field('local_b')])
    page = page_for(ui)
    writes, reads = observe(page)
    select(page, a['body']['name'])
    loaded_types(page, 1)
    page.locator('#context-name').fill('Rascunho A')
    button = page.locator('#type-list').get_by_role('button', name='Tipo A1', exact=True)
    button.click()
    discard(page, False)
    expect(page.locator('#context-name')).to_have_value('Rascunho A')
    keys(page, '#context-fields', ['comum_a'])
    expect(page.locator('#type-common-panel')).to_be_hidden()
    button.click()
    discard(page, True)
    type_view(page, ta)
    select(page, b['body']['name'])
    loaded_types(page, 1)
    expect(page.locator('#type-list')).not_to_contain_text('Tipo A1')
    open_type(page, tb)
    keys(page, '#context-fields', ['local_b'])
    keys(page, '#type-common-fields', ['comum_b'])
    select(page, a['body']['name'])
    assert record(ui, a['context_id']) == a
    assert record(ui, b['context_id']) == b
    assert not writes
    assert all(path.rsplit('/', 1)[-1] not in {ta['type_id'], tb['type_id']} for path in reads)


def test_failed_read_preserves_dirty_context_and_retry_confirms_again(ui):
    parent = seed(ui, 'Falha sintética', [field('comum_falha')])
    item = seed_type(ui, parent, 'Tipo recuperável', [field('local_falha')])
    page = page_for(ui)
    writes, _ = observe(page)
    select(page, parent['body']['name'])
    loaded_types(page, 1)
    page.locator('#context-name').fill('Rascunho preservado')
    attempts = []
    def read(route):
        attempts.append(route.request.method)
        if len(attempts) == 1:
            route.fulfill(status=503, content_type='application/json',
                          body='{"detail":"Falha sintética temporária"}')
        else:
            route.fulfill(response=route.fetch())
    page.route('**/api/v1/document-types/' + item['type_id'], read)
    button = page.locator('#type-list').get_by_role('button', name=item['body']['name'], exact=True)
    button.click()
    discard(page, True)
    expect(page.locator('#context-status')).to_contain_text('Não foi possível abrir')
    expect(page.locator('#context-name')).to_be_editable()
    expect(page.locator('#context-name')).to_have_value('Rascunho preservado')
    keys(page, '#context-fields', ['comum_falha'])
    assert record(ui, parent['context_id']) == parent
    button.click()
    discard(page, True)
    type_view(page, item)
    assert attempts == ['GET', 'GET']
    assert not writes


def test_real_pagination_refresh_and_stale_list_isolation(ui):
    a = seed(ui, 'Paginação A', [field('comum_a')])
    b = seed(ui, 'Paginação B', [field('comum_b')])
    items = [seed_type(ui, a, f'Tipo {i:02d}', [field('local')]) for i in range(26)]
    other = seed_type(ui, b, 'Tipo exclusivo B', [field('local_b')])
    page = page_for(ui)
    writes, _ = observe(page)
    select(page, a['body']['name'])
    loaded_types(page, 25)
    page.locator('#type-more').click()
    loaded_types(page, 26)
    names = page.locator('#type-list button').all_text_contents()
    assert len(set(names)) == 26
    assert set(names) == {item['body']['name'] for item in items}
    expect(page.locator('#type-more')).to_be_hidden()
    page.locator('#context-name').fill('Rascunho paginação')
    page.locator('#type-refresh').click()
    loaded_types(page, 25)
    expect(page.locator('#context-name')).to_have_value('Rascunho paginação')
    page.locator('#type-list button').first.click()
    discard(page, True)
    expect(page.locator('#context-status')).to_have_text('Tipo de documento carregado. Consulta somente para leitura.')
    active = next(item for item in items if item['body']['name'] == page.locator('#context-name').input_value())
    type_view(page, active)
    page.locator('#type-refresh').click()
    loaded_types(page, 25)
    type_view(page, active)
    held = []
    def hold(route):
        held.append(route)
    pattern = '**/api/v1/contexts/' + a['context_id'] + '/types?*'
    page.route(pattern, hold)
    try:
        page.locator('#type-refresh').click()
        expect(page.locator('#type-list-status')).to_contain_text('Carregando')
        target = page.locator('#context-list').get_by_role('button', name=b['body']['name'], exact=True)
        expect(target).to_be_enabled()
        target.click()
        loaded_types(page, 1)
        assert held
        route = held.pop()
        with page.expect_request_finished(lambda request: request.url == route.request.url):
            route.fulfill(response=route.fetch())
        # Let the completed fetch and its DOM work reach the next browser paint.
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        loaded_types(page, 1)
        expect(page.locator('#type-list button')).to_have_text(other['body']['name'])
        open_type(page, other)
        keys(page, '#type-common-fields', ['comum_b'])
    finally:
        for route in held:
            route.abort()
        page.unroute(pattern, hold)
    assert not writes


def test_reduced_read_scope_browses_without_upgrading(ui):
    secret = ui.runtime.env['JWT_SECRET']
    parent = seed(ui, 'Leitura restrita', [field('comum_leitura')])
    item = seed_type(ui, parent, 'Tipo leitura', [field('local_leitura')])
    token = reduced_token(ui.accounts.owner, {'contexts:read'}, secret)
    ui.context.add_cookies([{'name': 'access_token', 'value': token,
                            'url': ui.runtime.origin, 'httpOnly': True, 'sameSite': 'Lax'}])
    page = page_for(ui)
    writes, _ = observe(page)
    me = ui.context.request.get(ui.runtime.origin + '/api/v1/me',
                                headers={'Authorization': 'Bearer ' + token})
    assert me.status == 200
    assert set(me.json()['scopes']) == {'contexts:read'}
    expect(page.locator('#context-new')).to_be_disabled()
    select(page, parent['body']['name'])
    open_type(page, item)
    keys(page, '#context-fields', ['local_leitura'])
    keys(page, '#type-common-fields', ['comum_leitura'])
    page.locator('#type-edit-context').click()
    expect(page.locator('#context-name')).to_have_value(parent['body']['name'])
    expect(page.locator('#context-name')).not_to_be_editable()
    expect(page.locator('#context-save')).not_to_be_enabled()
    private(page)
    assert record(ui, parent['context_id']) == parent
    assert type_record(ui, item['type_id']) == item
    assert not writes
