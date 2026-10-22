"""TT-A: browser/REST template reads, scope enforcement and real clones."""
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import pytest
from playwright.sync_api import expect

from test_catalog_ui import (
    ui, runtime, catalog_accounts, browser, private, record, revision, seed, select,
)
from test_catalog_fields_ui import field
from test_catalog_types_read_ui import keys, loaded_types, open_type, type_record
from test_catalog_types_write_ui import choose
from test_catalog_versions_read_ui import page_for
from test_catalog_versions_publish_ui import credentials, listed
from test_workspace_ui import screenshot

BASE = '/api/v1/contexts'
TEMPLATES = '/api/v1/context-templates'
IDS = ['generic', 'real-estate-deed', 'procurement-notice']


def rows(data):
    if isinstance(data, list):
        return data
    arrays = [value for value in data.values() if isinstance(value, list)]
    assert len(arrays) == 1
    return arrays[0]


def catalog(ui):
    response = ui.api.get(TEMPLATES)
    assert response.status_code == 200
    data = response.json()
    templates = rows(data)
    assert [item['template_id'] for item in templates] == IDS
    return data, templates


def contexts(ui):
    response = ui.api.get(BASE)
    assert response.status_code == 200
    return response.json()


def observe(page):
    events = []

    def route_request(route):
        request = route.request
        path = urlsplit(request.url).path
        if not path.startswith(('/api/', '/auth/')):
            route.continue_()
            return
        response = route.fetch()
        data = None
        if path.startswith('/api/v1/context') or path.startswith('/api/v1/document-types'):
            try:
                data = response.json()
            except ValueError:
                pass
        events.append(dict(method=request.method, path=path,
                           body=request.post_data_json if request.post_data and not path.startswith('/auth/') else None,
                           key=request.header_value('idempotency-key'),
                           status=response.status, data=data))
        route.fulfill(response=response)

    page.route('**/*', route_request)
    return events


def writes(events):
    return [event for event in events if event['path'].startswith('/api/')
            and event['method'] in {'POST', 'PUT', 'PATCH', 'DELETE'}]


def no_auth(events):
    assert not [event for event in events
                if event['path'].startswith('/auth/') or event['path'] == '/api/v1/me']


def load(page, events):
    assert not [event for event in events if event['path'] == TEMPLATES]
    page.locator('#template-load').click()
    expect(page.locator('#template-list button')).to_have_count(3)
    expect(page.locator('#template-load')).to_be_enabled()
    expect(page.locator('#template-status')).not_to_contain_text('Carregando')
    requests = [event for event in events if event['path'] == TEMPLATES]
    assert len(requests) == 1
    assert requests[0]['method'] == 'GET' and requests[0]['status'] == 200


def readonly_fields(page, container, fields):
    keys(page, container, [item['name'] for item in fields])
    cards = page.locator(container + ' .catalog-fields__card')
    for card, spec in zip(cards.all(), fields):
        controls = card.locator('input, select, textarea, button')
        for control in controls.all():
            expect(control).to_be_disabled()
        values = card.locator('input:not([type=checkbox]), textarea').evaluate_all(
            '(nodes) => nodes.map(node => node.value)')
        assert spec['name'] in values
        assert spec['description'] in values
        assert (spec.get('label') or '') in values
        selections = card.locator('select').evaluate_all('(nodes) => nodes.map(node => node.value)')
        for key in ('type', 'sensitivity', 'cardinality', 'normalization', 'destination'):
            assert spec[key] in selections
        for key, label in [('required', 'Obrigatório'), ('critical', 'Crítico'),
                           ('evidence_required', 'Exigir evidência'),
                           ('indexable', 'Previsto para indexação (intenção de metadados)')]:
            expect(card.get_by_label(label, exact=True)).to_be_checked(checked=spec[key])
        assert str(spec['max_items']) in values


def preview(page, template):
    page.locator('#template-list button[data-template-id="' + template['template_id'] + '"]').click()
    expect(page.locator('#template-preview')).to_be_visible()
    expect(page.locator('#template-status')).to_have_text(
        f"Modelo {template['context']['name']}, edição {template['template_version']}, "
        'em consulta somente para leitura.')
    expect(page.locator('#template-edition')).to_have_text(f"Edição {template['template_version']}")
    expect(page.locator('#template-hash')).to_have_text(template['template_sha256'])
    assert re.fullmatch(r'[0-9a-f]{64}', template['template_sha256'])
    for prefix, body in [('context', template['context']), ('type', template['types'][0]['body'])]:
        for key in ('name', 'description'):
            expect(page.locator(f'#template-{prefix}-{key}')).to_have_text(body[key])
    readonly_fields(page, '#template-common', template['context']['common_fields'])
    readonly_fields(page, '#template-specific', template['types'][0]['body']['specific_fields'])


def unavailable(locator):
    if locator.is_visible():
        expect(locator).to_be_disabled()
    else:
        expect(locator).to_be_hidden()


def field_description(page, name):
    cards = page.locator('#context-fields .catalog-fields__card')
    matches = [card for card in cards.all() if card.locator('input').first.input_value() == name]
    assert len(matches) == 1
    return matches[0].get_by_label(re.compile(r'^Descrição', re.I))


def denied(request, template):
    response = request('POST', TEMPLATES + '/' + template['template_id'] + '/clone',
                       headers={'Idempotency-Key': uuid4().hex},
                       json={'expected_version': template['template_version'],
                             'expected_sha256': template['template_sha256']})
    assert response.status_code == 403


def test_writer_previews_preserve_dirty_draft(ui):
    original = seed(ui, fields=[field('local_original')])
    original_catalog, templates = catalog(ui)
    before = contexts(ui)
    page = page_for(ui)
    select(page, original['body']['name'])
    page.locator('#context-name').fill('UNSAVED')
    description = field_description(page, 'local_original')
    description.fill('synthetic-unsaved')
    private(page)
    events = observe(page)
    load(page, events)
    for template in templates:
        preview(page, template)
        unavailable(page.locator('#template-clone'))
    preview(page, templates[0])
    for width, height in [(375, 812), (1440, 900)]:
        page.set_viewport_size({'width': width, 'height': height})
        expect(page.locator('#template-context-name')).to_have_text(templates[0]['context']['name'])
        expect(page.locator('#template-section')).to_be_visible()
        assert page.evaluate('''() => {
            const root = document.documentElement;
            const panel = document.querySelector('#template-section');
            return root.scrollWidth <= root.clientWidth && panel.scrollWidth <= panel.clientWidth;
        }''')
        screenshot(page, f'template-preview-{width}x{height}.png')
    expect(page.locator('#context-name')).to_have_value('UNSAVED')
    expect(description).to_have_value('synthetic-unsaved')
    revision(page, 1)
    expect(page.locator('#context-save')).to_be_enabled()
    assert record(ui, original['context_id']) == original
    assert contexts(ui) == before
    assert catalog(ui)[0] == original_catalog
    assert writes(events) == []
    no_auth(events)
    private(page)


def test_read_only_scope_can_preview_but_cannot_clone(ui):
    _, templates = catalog(ui)
    before = contexts(ui)
    request = credentials(ui, ['contexts:read'])
    page = page_for(ui)
    private(page)
    events = observe(page)
    load(page, events)
    for template in templates:
        preview(page, template)
        unavailable(page.locator('#template-clone'))
    denied(request, templates[0])
    assert contexts(ui) == before
    assert writes(events) == []
    no_auth(events)
    private(page)


def test_write_without_read_cannot_access_templates(ui):
    _, templates = catalog(ui)
    before = contexts(ui)
    request = credentials(ui, ['contexts:write'])
    page = ui.context.new_page()
    events = observe(page)
    page.goto(ui.runtime.origin + '/contexts')
    expect(page.locator('#context-status')).to_contain_text('contexts:read')
    expect(page.locator('#template-section')).to_be_hidden()
    expect(page.locator('#template-load')).to_be_disabled()
    assert not [event for event in events if event['path'] == TEMPLATES]
    assert request('GET', TEMPLATES).status_code == 403
    denied(request, templates[0])
    assert contexts(ui) == before
    assert writes(events) == []
    private(page)


@pytest.mark.parametrize('template_id', IDS)
def test_clone_creates_drafts_and_requires_explicit_open(ui, template_id):
    original = seed(ui, name='Contexto anterior', fields=[field('anterior')])
    _, templates = catalog(ui)
    template = next(item for item in templates if item['template_id'] == template_id)
    credentials(ui, ['contexts:read', 'contexts:write'])
    page = page_for(ui)
    select(page, original['body']['name'])
    original_description = page.locator('#context-description').input_value()
    original_field = field_description(page, 'anterior').input_value()
    selected = page.locator('#context-list button').filter(has_text='Contexto anterior')
    expect(selected).to_have_count(1)
    private(page)
    events = observe(page)
    load(page, events)
    preview(page, template)
    expect(page.locator('#template-clone')).to_be_enabled()
    page.locator('#template-clone').click()
    dialog = page.locator('#confirm-dialog')
    expect(dialog).to_contain_text('Criar contexto a partir deste modelo?')
    expect(dialog).to_contain_text('ambos como rascunhos na revisão 1, sem publicação automática')
    expect(dialog).to_contain_text('O formulário atual permanecerá inalterado.')
    choose(page)
    expect(page.locator('#template-clone-status')).to_contain_text('Clonagem confirmada.')
    expect(page.locator('#template-recover')).to_be_hidden()
    expect(page.locator('#template-open')).to_be_enabled()
    expect(page.locator('#context-refresh')).to_be_enabled()
    expect(page.locator('#context-list button')).to_have_count(2)
    mutations = writes(events)
    assert len(mutations) == 1
    post = mutations[0]
    assert (post['method'], post['path'], post['status']) == (
        'POST', TEMPLATES + '/' + template_id + '/clone', 201)
    assert UUID(post['key']).version == 4
    assert post['body'] == {'expected_version': template['template_version'],
                            'expected_sha256': template['template_sha256']}
    receipt = post['data']
    assert set(receipt) == {'template_id', 'template_version', 'template_sha256',
                            'context_id', 'context_revision', 'types', 'created_at'}
    for key in ('template_id', 'template_version', 'template_sha256'):
        assert receipt[key] == template[key]
    assert receipt['context_revision'] == 1 and len(receipt['types']) == 1
    entry = receipt['types'][0]
    assert set(entry) == {'type_key', 'type_id', 'revision'}
    assert entry['revision'] == 1
    assert entry['type_key'] == template['types'][0]['type_key']
    cid, tid = receipt['context_id'], entry['type_id']
    assert cid != tid and cid != original['context_id']
    assert all(re.fullmatch(r'[0-9a-f]{32}', value) for value in (cid, tid))
    assert datetime.fromisoformat(receipt['created_at'].replace('Z', '+00:00')).utcoffset() == timedelta(0)
    created, document_type = record(ui, cid), type_record(ui, tid)
    assert created['body'] == template['context'] and created['revision'] == 1
    assert document_type['state'] == 'draft'
    assert document_type['body'] == template['types'][0]['body']
    assert document_type['context_id'] == cid and document_type['revision'] == 1
    versions = ui.api.get('/api/v1/document-types/' + tid + '/versions')
    assert versions.status_code == 200 and rows(versions.json()) == []
    after_post = events[events.index(post) + 1:]
    for path in (BASE + '/' + cid, '/api/v1/document-types/' + tid):
        assert any(event['method'] == 'GET' and event['path'] == path
                   and event['status'] == 200 for event in after_post)
    expect(page.locator('#context-name')).to_have_value(original['body']['name'])
    expect(page.locator('#context-description')).to_have_value(original_description)
    expect(field_description(page, 'anterior')).to_have_value(original_field)
    revision(page, 1)
    expect(selected).to_have_count(1)
    expect(page.locator('#context-list').get_by_role('button', name=created['body']['name'], exact=True)).to_have_count(1)
    assert record(ui, original['context_id']) == original
    page.locator('#template-open').click()
    expect(page.locator('#context-name')).to_have_value(created['body']['name'])
    revision(page, 1)
    for spec in template['context']['common_fields']:
        expect(field_description(page, spec['name'])).to_have_value(spec['description'])
    loaded_types(page, 1)
    open_type(page, document_type)
    listed(page, 0)
    unavailable(page.locator('#version-publish'))
    assert len(rows(contexts(ui))) == 2
    types = ui.api.get(BASE + '/' + cid + '/types')
    assert types.status_code == 200
    assert [item['type_id'] for item in rows(types.json())] == [tid]
    assert record(ui, original['context_id']) == original
    assert writes(events) == [post]
    no_auth(events)
    private(page)
