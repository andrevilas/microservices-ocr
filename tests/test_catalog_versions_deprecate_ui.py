"""DT-A: real deprecation, scope enforcement and lifecycle CAS; DT-B follows."""
import json
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from test_catalog_ui import ui, runtime, catalog_accounts, browser
from test_catalog_ui import seed, record, select, private
from test_catalog_fields_ui import field
from test_catalog_types_read_ui import seed_type, type_record, open_type, loaded_types
from test_catalog_types_write_ui import start, choose, locked, revision
from test_catalog_versions_read_ui import publish, version_record, open_version, listed, page_for
from test_catalog_versions_publish_ui import credentials
from test_workspace_ui import screenshot


def published(ui):
    parent = seed(ui, fields=[field('common')])
    item = seed_type(ui, parent, 'Tipo sintético', [field('specific')])
    view = publish(ui, parent, item)
    assert view['version'] == 1
    assert view['lifecycle']['state'] == 'published'
    assert view['lifecycle']['revision'] == 1
    return parent, item, view


def opened(ui, parent, item, view, readonly=False):
    page = page_for(ui) if readonly else start(ui, parent)
    if readonly:
        select(page, parent['body']['name'])
        loaded_types(page, 1)
    open_type(page, item, readonly=readonly)
    listed(page, 1)
    open_version(page, view)
    expect(page.locator('#version-deprecate')).to_be_enabled()
    return page


def observe(page):
    events = []

    def forward(route):
        request = route.request
        path = urlsplit(request.url).path
        response = route.fetch()
        if path.startswith(('/api/v1/contexts', '/api/v1/document-types')):
            raw = request.post_data
            events.append(dict(method=request.method, path=path,
                               key=request.header_value('Idempotency-Key'),
                               raw=raw, body=json.loads(raw) if raw else None,
                               status=response.status, response=response.json()))
        route.fulfill(response=response)

    page.route('**/api/v1/**', forward)
    return events


def writes(events):
    return [event for event in events if event['method'] in ('POST', 'PUT', 'PATCH', 'DELETE')]


def deprecate(page, path, status=200):
    page.locator('#version-deprecate').click()
    dialog = page.locator('#confirm-dialog')
    expect(dialog).to_be_visible()
    for text in ('Descontinuar versão 1', 'consulta histórica', 'não libera quota',
                 'não permite reativação', 'rascunho não será salvo nem alterado'):
        expect(dialog).to_contain_text(text)
    with page.expect_response(lambda response: urlsplit(response.url).path == path
                              and response.request.method == 'POST'
                              and response.status == status):
        choose(page)
    message = ('Versão 1 descontinuada. A consulta histórica foi preservada.' if status == 200
               else 'A situação da versão mudou. Abra a versão novamente antes de continuar.')
    expect(page.locator('#version-deprecate-status')).to_have_text(message)
    expect(page.locator('#version-deprecate-recover')).to_be_hidden()
    expect(page.locator('#version-deprecate-abandon')).to_be_hidden()
    expect(page.locator('#version-deprecate')).to_be_disabled()
    expect(page.locator('#version-refresh')).to_be_enabled()
    listed(page, 1)


def unchanged(ui, parent, item, before):
    assert record(ui, parent['context_id']) == parent
    assert type_record(ui, item['type_id']) == item
    after = version_record(ui, item['type_id'], 1)
    assert {k: v for k, v in after.items() if k != 'lifecycle'} == {
        k: v for k, v in before.items() if k != 'lifecycle'}
    lifecycle = after['lifecycle']
    assert lifecycle['state'] == 'deprecated' and lifecycle['revision'] == 2
    assert lifecycle['changed_at'] != before['lifecycle']['changed_at']
    changed = datetime.fromisoformat(lifecycle['changed_at'].replace('Z', '+00:00'))
    assert changed.utcoffset() == timedelta(0)
    response = ui.api.get(f"/api/v1/document-types/{item['type_id']}/versions")
    assert response.status_code == 200
    listing = response.json()
    assert len(listing['items']) == 1 and listing['next_cursor'] is None
    assert listing['items'][0]['version'] == 1
    assert listing['items'][0]['lifecycle'] == lifecycle
    return after


def operation(events, path, status):
    mutations = writes(events)
    assert len(mutations) == 1
    event = mutations[0]
    assert (event['method'], event['path'], event['status']) == ('POST', path, status)
    assert event['key'] and event['raw']
    assert event['body'] == {'expected_lifecycle_revision': 1}
    return event


def confirmed(events, path, before, after):
    event = operation(events, path, 200)
    assert event['response'] == {
        'type_id': before['type_id'], 'version': 1,
        'schema_sha256': before['schema_sha256'], 'lifecycle': after['lifecycle']}
    position = events.index(event)
    assert any(e['method'] == 'GET' and e['path'] == path.removesuffix('/deprecate')
               and e['status'] == 200 for e in events[position + 1:])
    return event


def dirty_description(page):
    for control in page.locator('#context-fields').locator('input, textarea').all():
        if control.is_editable() and control.input_value() == 'Descrição sintética':
            control.fill('synthetic-unsaved')
            return control
    raise AssertionError('Editable specific-field description was not rendered')


@pytest.mark.parametrize('actor', ['writer-dirty', 'publisher-no-write'])
def test_deprecate_preserves_draft_and_snapshot(ui, actor):
    parent, item, before = published(ui)
    readonly = actor == 'publisher-no-write'
    if readonly:
        credentials(ui, ['contexts:read', 'contexts:publish'])
    page = opened(ui, parent, item, before, readonly=readonly)
    description = None
    if readonly:
        locked(page)
    else:
        page.locator('#context-name').fill('UNSAVED')
        description = dirty_description(page)
        revision(page, 1)
        expect(page.locator('#context-save')).to_be_enabled()
    draft_revision = page.locator('#context-revision').inner_text()
    events = observe(page)
    path = f"/api/v1/document-types/{item['type_id']}/versions/1/deprecate"
    deprecate(page, path)
    expect(page.locator('#version-lifecycle')).to_contain_text('Descontinuada')
    expect(page.locator('#version-lifecycle')).to_contain_text('· Revisão 2 ·')
    expect(page.locator('#version-list')).to_contain_text(re.compile('Descontinuada', re.I))
    after = unchanged(ui, parent, item, before)
    confirmed(events, path, before, after)
    expect(page.locator('#context-revision')).to_have_text(draft_revision)
    if readonly:
        locked(page)
    else:
        expect(page.locator('#context-name')).to_have_value('UNSAVED')
        expect(description).to_have_value('synthetic-unsaved')
        revision(page, 1)
        expect(page.locator('#context-save')).to_be_enabled()
        for width, height in ((375, 812), (1440, 900)):
            page.set_viewport_size({'width': width, 'height': height})
            assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
            assert page.locator('#version-detail').evaluate('(el) => el.scrollWidth <= el.clientWidth')
            screenshot(page, f'catalog-deprecate-dirty-{width}.png')
    private(page)
    operation(events, path, 200)


def test_deprecate_two_tabs_real_lifecycle_conflict_preserves_dirty_draft(ui):
    parent, item, before = published(ui)
    a = opened(ui, parent, item, before)
    b = opened(ui, parent, item, before)
    b.locator('#context-name').fill('DIRTY_B')
    revision(b, 1)
    expect(b.locator('#context-save')).to_be_enabled()
    events_a, events_b = observe(a), observe(b)
    path = f"/api/v1/document-types/{item['type_id']}/versions/1/deprecate"
    deprecate(a, path)
    after = unchanged(ui, parent, item, before)
    accepted = confirmed(events_a, path, before, after)
    expect(b.locator('#version-lifecycle')).to_contain_text('Publicada')
    expect(b.locator('#version-lifecycle')).to_contain_text('· Revisão 1 ·')
    assert events_b == []
    deprecate(b, path, 409)
    rejected = operation(events_b, path, 409)
    assert rejected['response']['error']['code'] == 'type_lifecycle_revision_conflict'
    assert accepted['key'] != rejected['key']
    assert len(events_b) == 1
    expect(b.locator('#version-detail')).to_be_hidden()
    expect(b.locator('#context-name')).to_have_value('DIRTY_B')
    revision(b, 1)
    expect(b.locator('#context-save')).to_be_enabled()
    open_version(b, after)
    expect(b.locator('#version-deprecate')).to_be_disabled()
    expect(b.locator('#context-name')).to_have_value('DIRTY_B')
    revision(b, 1)
    expect(b.locator('#context-save')).to_be_enabled()
    assert unchanged(ui, parent, item, before) == after
    operation(events_a, path, 200)
    operation(events_b, path, 409)
    private(a)
    private(b)
