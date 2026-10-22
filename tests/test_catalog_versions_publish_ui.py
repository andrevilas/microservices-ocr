"""PT-A: real publication, reduced scopes, revision conflicts and schema rejection.

PT-B recovery, receipt failures and abandonment remain a separate required gate.
"""
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from test_catalog_ui import ui, runtime, catalog_accounts, browser, seed, record, select, private
from test_catalog_fields_ui import field
from test_catalog_types_read_ui import seed_type, type_record, open_type, loaded_types, keys
from test_catalog_types_write_ui import start, choose, locked, revision
from test_catalog_versions_read_ui import version_record, page_for, enter, listed as version_listed, open_version
from test_catalog_mcp_boundaries import reduced_token
from test_workspace_ui import screenshot

TYPES = '/api/v1/document-types/'
CONFIRMED = 'Versão 1 confirmada. Situação atual: Publicada.'
CONFLICT = ('A revisão do tipo ou do contexto mudou. Use Atualizar rascunho e '
            'revise as revisões antes de publicar novamente.')
INVALID = ('A publicação não foi admitida. Revise as condições, os nomes duplicados '
           'e a composição dos campos do tipo e do contexto. O rascunho foi preservado.')


def credentials(ui, scopes):
    token = reduced_token(ui.accounts.owner, scopes, ui.runtime.env['JWT_SECRET'])
    headers = {'Authorization': 'Bearer ' + token}
    response = ui.api.get('/api/v1/me', headers=headers)
    assert response.status_code == 200
    assert set(response.json()['scopes']) == set(scopes)
    ui.context.add_cookies([{'name': 'access_token', 'value': token,
                            'url': ui.runtime.origin, 'httpOnly': True,
                            'sameSite': 'Lax'}])

    def request(method, path, **kwargs):
        return ui.api.request(method, path, headers={**headers, **kwargs.pop('headers', {})}, **kwargs)
    return request


def observe(page):
    writes = []

    def intercept(route):
        request = route.request
        if request.method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
            route.continue_()
            return
        actual = route.fetch()
        writes.append(dict(method=request.method, path=urlsplit(request.url).path,
                           raw=request.post_data, body=request.post_data_json,
                           key=request.header_value('Idempotency-Key'),
                           status=actual.status, response=actual.json()))
        route.fulfill(response=actual)
    page.route('**/api/v1/**', intercept)
    return writes


def setup(ui, fields=None):
    parent = seed(ui, fields=[field('common')])
    item = seed_type(ui, parent, 'Tipo sintético', fields or [field('specific')])
    return parent, item


def opened(ui, parent, item, readonly=False):
    page = page_for(ui)
    writes = observe(page)
    enter(page, parent, item, readonly=readonly)
    listed(page, 0)
    return page, writes


def listed(page, count):
    if count:
        version_listed(page, count)
    else:
        expect(page.locator('#version-refresh')).to_be_enabled()
        expect(page.locator('#version-list-status')).to_have_text(
            'Este tipo ainda não tem versões imutáveis.')
        expect(page.locator('#version-list button')).to_have_count(0)


def submit(page, item, type_revision=1, context_revision=1):
    path = TYPES + item['type_id'] + '/versions'
    page.locator('#version-publish').click()
    dialog = page.locator('#confirm-dialog')
    expect(dialog).to_be_visible()
    for label, number in (('tipo', type_revision), ('contexto', context_revision)):
        expect(dialog).to_contain_text(f'revisão {number} do {label}')
    with page.expect_response(lambda response: urlsplit(response.url).path == path
                              and response.request.method == 'POST'):
        choose(page)


def terminal(page, text, count):
    expect(page.locator('#version-publish-status')).to_have_text(text)
    expect(page.locator('#version-refresh-draft')).to_be_enabled()
    expect(page.locator('#version-publish-recover')).to_be_disabled()
    listed(page, count)
    private(page)


def attempt(value, item, status, type_revision=1, context_revision=1):
    assert value['method'] == 'POST'
    assert value['path'] == TYPES + item['type_id'] + '/versions'
    assert value['status'] == status
    assert value['key']
    expected = dict(expected_revision=type_revision,
                    expected_context_revision=context_revision)
    assert value['body'] == expected
    assert json.loads(value['raw']) == expected


def error_code(value):
    if isinstance(value, dict):
        if 'code' in value:
            return value['code']
        for child in value.values():
            found = error_code(child)
            if found:
                return found
    return None


def versions(ui, item):
    response = ui.api.get(TYPES + item['type_id'] + '/versions', params={'limit': 20})
    assert response.status_code == 200
    return response.json()


def update(ui, path, value, **changes):
    body = dict(value['body'], **changes)
    response = ui.api.put(path, json={'body': body, 'expected_revision': value['revision']})
    assert response.status_code == 200
    return body


@pytest.mark.parametrize('publisher', [True, False], ids=['publish-without-write', 'read-only'])
def test_publication_scopes_and_immutable_snapshot(ui, publisher):
    parent, item = setup(ui)
    scopes = ['contexts:read'] + (['contexts:publish'] if publisher else [])
    request = credentials(ui, scopes)
    page, writes = opened(ui, parent, item, readonly=True)
    locked(page)
    if not publisher:
        expect(page.locator('#version-publish')).to_be_hidden()
        response = request(
            'POST', TYPES + item['type_id'] + '/versions',
            headers={'Idempotency-Key': uuid4().hex},
            json={'expected_revision': 1, 'expected_context_revision': 1})
        assert response.status_code == 403
        assert writes == []
        listed(page, 0)
        assert type_record(ui, item['type_id']) == item
        private(page)
        return
    submit(page, item)
    terminal(page, CONFIRMED, 1)
    assert len(writes) == 1
    attempt(writes[0], item, 201)
    assert 'lifecycle' not in writes[0]['response']
    value = version_record(ui, item['type_id'], 1)
    assert value['draft_revision'] == value['context_revision'] == 1
    assert value['context_id'] == parent['context_id']
    assert value['context_name'] == parent['body']['name']
    assert value['type_name'] == item['body']['name']
    assert value['lifecycle']['state'] == 'published'
    assert record(ui, parent['context_id']) == parent
    assert type_record(ui, item['type_id']) == item
    expect(page.locator('#version-reference')).to_contain_text(value['schema_sha256'])
    expect(page.locator('#version-meta')).to_contain_text('schema_version: ' + value['schema_version'])
    expect(page.locator('#version-lifecycle')).to_contain_text('Publicada · Revisão 1')
    open_version(page, value)
    for width in (375, 1440):
        page.set_viewport_size({'width': width, 'height': 1000})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        screenshot(page, f'publication-{width}.png')
    update(ui, TYPES + item['type_id'], item, name='Tipo renomeado',
           specific_fields=[field('specific', label='Rótulo novo')])
    update(ui, '/api/v1/contexts/' + parent['context_id'], parent,
           description='Descrição posterior')
    assert type_record(ui, item['type_id'])['revision'] == 2
    assert record(ui, parent['context_id'])['revision'] == 2
    unchanged = version_record(ui, item['type_id'], 1)
    assert unchanged == value
    assert unchanged['schema_sha256'] == value['schema_sha256']
    expect(page.locator('#context-name')).to_have_value(item['body']['name'])
    revision(page, 1)
    page.locator('#version-refresh-draft').click()
    expect(page.locator('#context-name')).to_have_value('Tipo renomeado')
    revision(page, 2)
    listed(page, 1)
    assert len(writes) == 1
    private(page)


@pytest.mark.parametrize('target', ['type', 'context'])
def test_publication_revision_conflict_requires_explicit_refresh(ui, target):
    parent, item = setup(ui)
    page, writes = opened(ui, parent, item)
    if target == 'type':
        update(ui, TYPES + item['type_id'], item, name='Tipo atualizado')
    else:
        update(ui, '/api/v1/contexts/' + parent['context_id'], parent,
               description='Contexto atualizado')
    empty = versions(ui, item)
    submit(page, item)
    terminal(page, CONFLICT, 0)
    assert len(writes) == 1
    attempt(writes[0], item, 409)
    assert error_code(writes[0]['response']) == (
        'type_revision_conflict' if target == 'type' else 'context_revision_conflict')
    assert versions(ui, item) == empty
    expect(page.locator('#context-name')).to_have_value(item['body']['name'])
    revision(page, 1)
    expect(page.locator('#version-publish')).to_be_disabled()
    page.locator('#version-refresh-draft').click()
    current = type_record(ui, item['type_id'])
    expect(page.locator('#context-name')).to_have_value(current['body']['name'])
    revision(page, current['revision'])
    expect(page.locator('#version-publish')).to_be_enabled()
    assert len(writes) == 1
    tr, cr = (2, 1) if target == 'type' else (1, 2)
    submit(page, item, tr, cr)
    terminal(page, CONFIRMED, 1)
    assert len(writes) == 2
    attempt(writes[1], item, 201, tr, cr)
    assert writes[0]['key'] != writes[1]['key']
    value = version_record(ui, item['type_id'], 1)
    assert (value['draft_revision'], value['context_revision']) == (tr, cr)
    assert value['lifecycle']['state'] == 'published'


def test_publication_rejects_unresolved_field_reference(ui):
    parent, item = setup(ui, [field('dependent', required_when={
        'field': 'missing', 'equals': False})])
    page, writes = opened(ui, parent, item)
    empty = versions(ui, item)
    submit(page, item)
    terminal(page, INVALID, 0)
    assert len(writes) == 1
    attempt(writes[0], item, 422)
    assert error_code(writes[0]['response']) == 'invalid_type_schema'
    assert versions(ui, item) == empty
    assert type_record(ui, item['type_id']) == item
    assert record(ui, parent['context_id']) == parent
    revision(page, 1)
    keys(page, '#context-fields', ['dependent'])
    expect(page.locator('#version-publish')).to_be_enabled()
