"""CI3 type draft writes against the real authenticated Runtime and SQLite."""
from copy import deepcopy
import json
import re
from uuid import uuid4

import pytest
from playwright.sync_api import expect
from test_catalog_ui import ui, runtime, catalog_accounts, browser, seed, record, select, private, field
from test_catalog_types_read_ui import seed_type, type_record, open_type, type_view, loaded_types, keys
from test_workspace_ui import confirm

BASE = '/api/v1/contexts'


def start(ui, parent):
    page = ui.context.new_page()
    page.goto(ui.runtime.origin + '/contexts')
    expect(page.locator('#context-refresh')).to_be_enabled()
    select(page, parent['body']['name'])
    loaded_types(page, 1)
    return page


def choose(page, accept=True):
    dialog = page.get_by_role('dialog')
    expect(dialog).to_be_visible()
    dialog.locator('#confirm-accept' if accept else '#confirm-cancel').click()
    expect(dialog).to_be_hidden()


def button(page, name):
    return page.get_by_role('button', name=name, exact=True)


def status(page, text):
    expect(page.locator('#context-status')).to_have_text(text)


def revision(page, number):
    expect(page.locator('#context-revision')).to_contain_text(
        re.compile(r'^Rascunho — tipo de documento · Revisão ' + str(number) + r'\b'))


def local(page, label):
    return page.locator('#context-fields').get_by_label(label, exact=True)


def locked(page):
    for selector in ('#context-name', '#context-description'):
        expect(page.locator(selector)).not_to_be_editable()
    for control in page.locator('#context-fields, #type-common-fields').locator('input, select, textarea, button').all():
        expect(control).to_be_disabled()
    expect(page.locator('#context-save')).to_be_disabled()


def unchanged(ui, parent, *pages):
    assert record(ui, parent['context_id']) == parent
    for page in pages:
        private(page)
    assert not ui.dialogs  # The ui fixture also asserts no pageerror on teardown.


def watch_writes(page):
    writes = []
    def watch(request):
        if '/api/v1/' in request.url and request.method in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            writes.append((request.method, request.url, request.post_data_json))
    page.on('request', watch)
    return writes


def listed(ui, parent):
    response = ui.api.get(BASE + '/' + parent['context_id'] + '/types', params={'limit': 100})
    assert response.status_code == 200
    return response.json()['items']


def test_create_uses_persisted_parent_and_reopens(ui):
    parent = seed(ui, 'Pai criação', [field('origem')])
    existing = seed_type(ui, parent, 'Tipo anterior', [field('anterior')])
    page = start(ui, parent)
    writes = watch_writes(page)
    page.locator('#context-name').fill('Pai não salvo')
    local(page, 'Chave do campo').fill('comum_nao_salvo')
    page.locator('#type-new').click()
    choose(page)
    status(page, 'Preencha o nome e os campos específicos. Os campos comuns são herdados do contexto.')
    keys(page, '#type-common-fields', ['origem'])
    page.locator('#context-name').fill('Tipo criado na UI')
    page.locator('#context-description').fill('Descrição criada na UI')
    page.locator('#context-fields').get_by_role('button', name='Adicionar campo', exact=True).click()
    local(page, 'Chave do campo').fill('numero')
    local(page, 'Descrição').fill('Número sintético')
    expect(page.locator('#type-field-count')).to_have_text('2 campo(s): 1 comuns herdados do contexto e 1 específicos do tipo.')
    for control in page.locator('#type-common-fields').locator('input, select, textarea, button').all():
        expect(control).to_be_disabled()
    with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/types')) as receipt:
        page.locator('#context-save').click()
    assert receipt.value.status == 201
    status(page, 'Tipo de documento criado. A versão atual foi recuperada.')
    created = [entry for entry in listed(ui, parent) if entry['type_id'] != existing['type_id']]
    assert len(created) == 1
    item = type_record(ui, created[0]['type_id'])
    assert item['revision'] == 1 and item['context_id'] == parent['context_id']
    assert set(item['body']) == {'name', 'description', 'specific_fields'}
    assert item['body'] == dict(name='Tipo criado na UI', description='Descrição criada na UI',
                                specific_fields=[field('numero', description='Número sintético')])
    assert len(writes) == 1 and writes[0][0] == 'POST'
    page.reload()
    expect(page.locator('#context-refresh')).to_be_enabled()
    select(page, parent['body']['name'])
    open_type(page, item)
    keys(page, '#context-fields', ['numero'])
    assert type_record(ui, item['type_id']) == item
    unchanged(ui, parent, page)


def test_advanced_fields_preserved_and_inherited_duplicate_blocked(ui):
    parent = seed(ui, 'Pai avançado', [field('origem', type='boolean')])
    specific = field('numero', type='enum', enum=['A', 'B'], label='Antes', required=True,
                     critical=True, evidence_required=True, sensitivity='restricted', indexable=True,
                     cardinality='many', max_items=7, examples=['0012', 'linha\n2'],
                     required_when={'field': 'origem', 'equals': False}, destination='xmp')
    assert len(specific) == 16
    item = seed_type(ui, parent, 'Tipo avançado', [specific])
    page = start(ui, parent)
    open_type(page, item)
    writes = watch_writes(page)
    local(page, 'Chave do campo').fill('origem')
    page.locator('#context-save').click()
    status(page, 'Revise os campos específicos indicados.')
    expect(page.locator('#context-fields [role="alert"]:visible').first).to_contain_text(
        re.compile(r'comum|herdad|duplic', re.I))
    expect(local(page, 'Chave do campo')).to_have_value('origem')
    assert not writes and type_record(ui, item['type_id']) == item
    local(page, 'Chave do campo').fill('numero')
    local(page, 'Rótulo').fill('Depois')
    page.locator('#context-save').click()
    status(page, 'Tipo de documento salvo.')
    revision(page, 2)
    expected = deepcopy(item['body'])
    expected['specific_fields'][0]['label'] = 'Depois'
    current = type_record(ui, item['type_id'])
    assert current['revision'] == 2 and current['body'] == expected
    assert len(current['body']['specific_fields'][0]) == 16
    assert current['body']['specific_fields'][0]['required_when']['equals'] is False
    assert len(writes) == 1 and writes[0][0] == 'PUT'
    unchanged(ui, parent, page)


def test_two_tabs_conflict_requires_explicit_adoption(ui):
    parent = seed(ui, 'Pai conflito', [field('origem')])
    item = seed_type(ui, parent, 'Tipo conflito', [field('numero', description='Específico original')])
    a, b = start(ui, parent), start(ui, parent)
    open_type(a, item)
    open_type(b, item)
    wa, wb = watch_writes(a), watch_writes(b)
    a.locator('#context-name').fill('Nome salvo A')
    a.locator('#context-save').click()
    status(a, 'Tipo de documento salvo.')
    revision(a, 2)
    b.locator('#context-description').fill('Rascunho B')
    with b.expect_response(lambda r: r.request.method == 'PUT') as conflict:
        b.locator('#context-save').click()
    assert conflict.value.status == 409
    assert conflict.value.json()['error']['code'] == 'type_revision_conflict'
    expect(button(b, 'Consultar versão atual')).to_be_enabled()
    expect(b.locator('#context-description')).to_have_value('Rascunho B')
    revision(b, 1)
    button(b, 'Consultar versão atual').click()
    status(b, 'Versão atual consultada. Seu rascunho e sua revisão original permanecem no formulário.')
    revision(b, 1)
    expect(b.locator('#context-current')).to_contain_text('Específico original')
    expect(b.locator('#context-current')).to_contain_text('Campos específicos')
    button(b, 'Usar versão atual').click()
    choose(b, False)
    expect(b.locator('#context-description')).to_have_value('Rascunho B')
    revision(b, 1)
    button(b, 'Usar versão atual').click()
    choose(b)
    status(b, 'Versão consultada carregada. Novas alterações usarão esta revisão.')
    revision(b, 2)
    expect(b.locator('#context-name')).to_have_value('Nome salvo A')
    b.locator('#context-description').fill('Descrição final B')
    b.locator('#context-save').click()
    status(b, 'Tipo de documento salvo.')
    revision(b, 3)
    expected = dict(item['body'], name='Nome salvo A', description='Descrição final B')
    current = type_record(ui, item['type_id'])
    assert current['revision'] == 3 and current['body'] == expected
    assert [entry[2]['expected_revision'] for entry in wa + wb] == [1, 1, 2]
    assert all(method == 'PUT' and url.endswith('/document-types/' + item['type_id']) for method, url, _ in wa + wb)
    unchanged(ui, parent, a, b)


def test_post_uncertainty_replay_and_receipt_get_recovery(ui):
    parent = seed(ui, 'Pai recuperação POST', [field('origem')])
    seed_type(ui, parent, 'Tipo anterior', [field('anterior')])
    page = start(ui, parent)
    path = BASE + '/' + parent['context_id'] + '/types'
    posts, gets, receipts, wires = [], [], [], []
    def recover_read(route):
        gets.append(route.request.url)
        if len(gets) == 1:
            route.fulfill(status=503, content_type='application/json', body=json.dumps(
                {'error': {'code': 'synthetic_failure', 'message': 'Falha sintética de leitura'}}))
        else:
            route.fulfill(response=route.fetch())
    def create(route):
        wires.append(route.request.post_data)
        posts.append((route.request.url, route.request.headers.get('idempotency-key'), route.request.post_data_json))
        if len(posts) == 2:
            route.fulfill(status=429, content_type='application/json', body=json.dumps(
                {'error': {'code': 'type_quota_exceeded', 'message': 'Cota sintética temporária'}}))
            return
        response = route.fetch()
        receipts.append(response.json())
        assert response.status == (201 if len(posts) == 1 else 200)
        if len(posts) == 1:
            page.route('**/api/v1/document-types/' + receipts[0]['type_id'], recover_read)
            route.abort()
        else:
            route.fulfill(response=response)
    page.route('**' + path, create)
    page.locator('#type-new').click()
    status(page, 'Preencha o nome e os campos específicos. Os campos comuns são herdados do contexto.')
    page.locator('#context-name').fill('Tipo de pedido congelado')
    page.locator('#context-fields').get_by_role('button', name='Adicionar campo', exact=True).click()
    local(page, 'Chave do campo').fill('numero')
    local(page, 'Descrição').fill('Descrição congelada')
    page.locator('#context-save').click()
    recover = page.locator('#context-recover-create')
    expect(recover).to_be_enabled()
    locked(page)
    assert len(posts) == 1
    recover.click()
    expect(recover).to_be_enabled()
    locked(page)
    assert len(posts) == 2
    recover.click()
    expect(recover).to_be_enabled()
    locked(page)
    assert len(posts) == 3 and len(gets) == 1
    recover.click()
    status(page, 'Tipo de documento criado. A versão atual foi recuperada.')
    expect(page.locator('#context-name')).to_be_editable()
    assert len(posts) == 3 and len(gets) == 2
    assert posts[0] == posts[1] == posts[2] and posts[0][1]
    assert len(wires) == 3 and wires[0] == wires[1] == wires[2]
    assert all(url.endswith(path) for url, _, _ in posts)
    assert receipts[0]['type_id'] == receipts[1]['type_id']
    item = type_record(ui, receipts[0]['type_id'])
    assert item['revision'] == 1 and item['body'] == posts[0][2]
    entries = listed(ui, parent)
    assert len(entries) == 2 and sum(entry['type_id'] == item['type_id'] for entry in entries) == 1
    unchanged(ui, parent, page)


@pytest.mark.parametrize('concurrent', [False, True])
def test_put_uncertainty_consults_without_repeating_write(ui, concurrent):
    parent = seed(ui, 'Pai recuperação PUT', [field('origem')])
    item = seed_type(ui, parent, 'Tipo PUT', [field('numero')])
    page = start(ui, parent)
    open_type(page, item)
    puts = []
    def lose_response(route):
        puts.append(route.request.post_data_json)
        response = route.fetch()
        assert response.status == 200
        route.abort()
    page.route('**/api/v1/document-types/' + item['type_id'],
               lambda route: lose_response(route) if route.request.method == 'PUT' else route.continue_())
    page.locator('#context-description').fill('Gravação incerta')
    page.locator('#context-save').click()
    consult = button(page, 'Consultar versão atual')
    expect(consult).to_be_enabled()
    locked(page)
    assert len(puts) == 1 and puts[0]['expected_revision'] == 1
    expected = dict(item['body'], description='Gravação incerta')
    assert type_record(ui, item['type_id'])['body'] == expected
    if concurrent:
        expected = dict(expected, description='Atualização concorrente sintética')
        response = ui.api.put('/api/v1/document-types/' + item['type_id'],
                             headers={'Idempotency-Key': uuid4().hex},
                             json={'expected_revision': 2, 'body': expected})
        assert response.status_code == 200
    consult.click()
    if concurrent:
        status(page, 'A consulta não confirmou a gravação esperada. Compare a versão atual com seu rascunho. Para resolver, escolha explicitamente Usar versão atual ou abandone este rascunho ao navegar.')
        revision(page, 1)
        expect(page.locator('#context-description')).to_have_value('Gravação incerta')
        button(page, 'Usar versão atual').click()
        choose(page, False)
        revision(page, 1)
        expect(page.locator('#context-description')).to_have_value('Gravação incerta')
        button(page, 'Usar versão atual').click()
        choose(page)
        status(page, 'Versão consultada carregada. Novas alterações usarão esta revisão.')
    else:
        status(page, 'A gravação foi confirmada.')
    revision(page, 3 if concurrent else 2)
    expect(page.locator('#context-name')).to_be_editable()
    expect(page.locator('#context-description')).to_have_value(expected['description'])
    expect(local(page, 'Chave do campo')).to_be_editable()
    expect(page.locator('#context-save')).to_be_disabled()
    current = type_record(ui, item['type_id'])
    assert current['revision'] == (3 if concurrent else 2) and current['body'] == expected
    assert len(puts) == 1
    unchanged(ui, parent, page)


@pytest.mark.parametrize('failure', ['parent_mismatch', 'expired'])
def test_negative_type_read_preserves_previous_dirty_context(ui, failure):
    parent = seed(ui, 'Pai leitura negativa', [field('origem')])
    other = seed(ui, 'Outro pai real', [field('outro')])
    item = seed_type(ui, parent, 'Tipo leitura negativa', [field('numero')])
    page = start(ui, parent)
    writes, attempts = watch_writes(page), []
    page.locator('#context-name').fill('Rascunho anterior')
    def negative_read(route):
        attempts.append(route.request.method)
        if len(attempts) > 1:
            route.fulfill(response=route.fetch())
        elif failure == 'expired':
            route.fulfill(status=401, content_type='application/json', body=json.dumps(
                {'error': {'code': 'session_expired', 'message': 'Sessão sintética expirada'}}))
        else:
            response = route.fetch()
            assert response.status == 200
            invalid = response.json()
            invalid['context_id'] = other['context_id']  # Deliberately invalid data, not a success fixture.
            route.fulfill(response=response, body=json.dumps(invalid))
    page.route('**/api/v1/document-types/' + item['type_id'], negative_read)
    target = page.locator('#type-list').get_by_role('button', name=item['body']['name'], exact=True)
    target.click()
    choose(page)
    if failure == 'expired':
        status(page, 'Sua sessão expirou. Seu rascunho foi preservado nesta página. Entre novamente para continuar.')
        expect(page.get_by_role('link', name='Entrar novamente', exact=True)).to_be_visible()
        locked(page)
        assert page.url == ui.runtime.origin + '/contexts'
    else:
        status(page, 'Não foi possível abrir o tipo de documento. O formulário anterior foi preservado. Selecione o tipo para tentar novamente.')
        expect(page.locator('#context-name')).to_be_editable()
    expect(page.locator('#context-name')).to_have_value('Rascunho anterior')
    keys(page, '#context-fields', ['origem'])
    expect(page.locator('#type-common-panel')).to_be_hidden()
    assert attempts == ['GET'] and not writes
    assert type_record(ui, item['type_id']) == item
    if failure == 'parent_mismatch':
        target.click()
        choose(page, False)
        expect(page.locator('#context-name')).to_have_value('Rascunho anterior')
        assert attempts == ['GET']
        target.click()
        choose(page)
        type_view(page, item)
        assert attempts == ['GET', 'GET']
    assert not writes and record(ui, other['context_id']) == other
    unchanged(ui, parent, page)
