"""Recovery proposals exercised against authenticated browser and persisted REST state."""
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from test_catalog_ui import ui, runtime, catalog_accounts, browser  # noqa: F401
from test_catalog_ui import seed, record, select, private
from test_catalog_fields_ui import field
from test_catalog_types_read_ui import seed_type, type_record, open_type, loaded_types
from test_catalog_types_write_ui import start, choose, locked, revision
from test_catalog_versions_read_ui import version_record, listed, open_version

TYPES = '/api/v1/document-types/'
UNCERTAIN = ('O pedido foi preservado e pode já ter sido registrado. Use Recuperar '
             'publicação para repetir exatamente o mesmo pedido, sem criar uma nova solicitação.')
UNCONFIRMED = ('A publicação foi recebida, mas não foi possível confirmar seus detalhes. '
               'Recuperar publicação apenas consulta a versão recebida.')
ABANDONED = ('Recuperação da publicação abandonada. Confira as versões antes de publicar novamente.')


def button(page, name):
    return page.get_by_role('button', name=name, exact=True)


def confirm(page, accept=True):
    if not accept:
        expect(page.get_by_role('dialog').locator('#confirm-cancel')).to_have_text('Voltar')
    choose(page, accept)


def status(page, text):
    expect(page.get_by_text(text, exact=True)).to_be_visible()


def pending(page, received=False):
    status(page, UNCONFIRMED if received else UNCERTAIN)
    expect(button(page, 'Recuperar publicação')).to_be_enabled()
    expect(page.locator('#version-publish-abandon')).to_be_enabled()
    locked(page)
    for selector in ('#context-name', '#context-description'):
        expect(page.locator(selector)).to_have_js_property('readOnly', True)


def snapshot(value):
    return value['schema'], value['schema_sha256']


def rows(ui, item):
    response = ui.api.get(TYPES + item['type_id'] + '/versions', params={'limit': 20})
    assert response.status_code == 200
    return response.json()['items']


class Recovery:
    def __init__(self, ui, modes, corrupt=None, detail_failure=False):
        self.ui, self.modes, self.corrupt = ui, modes, corrupt
        self.parent = seed(ui, fields=[field('common')])
        self.item = seed_type(ui, self.parent, 'Tipo recuperação', [field('specific')])
        self.page = start(ui, self.parent)
        open_type(self.page, self.item)
        expect(self.page.locator('#version-refresh')).to_be_enabled()
        expect(self.page.locator('#version-list button')).to_have_count(0)
        self.posts, self.gets, self.receipts, self.auth = [], [], [], []
        self.detail_failure = detail_failure
        self.base = TYPES + self.item['type_id'] + '/versions'
        self.post_pattern = re.compile(re.escape(ui.runtime.origin + self.base) + r'(?:\?.*)?$')
        self.get_pattern = re.compile(re.escape(ui.runtime.origin + self.base + '/1') + r'$')
        self.page.route(self.post_pattern, self.post)
        self.page.route(self.get_pattern, self.detail)
        self.page.on('request', self.watch_auth)

    def watch_auth(self, request):
        path = urlsplit(request.url).path
        if path == '/auth' or path.startswith('/auth/'):
            self.auth.append((request.method, path))

    @staticmethod
    def observed(request):
        raw = request.post_data
        return dict(method=request.method, path=urlsplit(request.url).path,
                    key=request.headers.get('idempotency-key'), raw=raw,
                    json=json.loads(raw) if raw else None)

    def post(self, route):
        if route.request.method != 'POST':
            response = route.fetch()
            route.fulfill(response=response)
            return
        self.posts.append(self.observed(route.request))
        index = len(self.posts) - 1
        assert index < len(self.modes), 'Unexpected additional publication POST'
        mode = self.modes[index]
        if mode == 'quota':
            route.fulfill(status=429, json={'error': {
                'code': 'type_version_quota_exceeded', 'message': 'Quota sintética'}})
            return
        response = route.fetch()
        self.posts[-1]['status'] = response.status
        assert response.status == (201 if index == 0 else 200)
        receipt = response.json()
        self.receipts.append(receipt)
        if mode == 'lost':
            route.abort('failed')
        elif mode == 'corrupt':
            altered = dict(receipt)
            self.corrupt(altered)
            route.fulfill(response=response, json=altered)
        else:
            route.fulfill(response=response)

    def detail(self, route):
        assert route.request.method == 'GET'
        self.gets.append(self.observed(route.request))
        if self.detail_failure and len(self.gets) == 1:
            route.fulfill(status=503, json={'error': {
                'code': 'synthetic_failure', 'message': 'Falha sintética de leitura'}})
            return
        response = route.fetch()
        self.gets[-1]['status'] = response.status
        assert response.status == 200
        self.gets[-1]['record'] = response.json()
        route.fulfill(response=response)

    def publish(self):
        self.page.get_by_role('button', name=re.compile(
            r'^Publicar versão(?: imutável)?$', re.I)).click()
        confirm(self.page)

    def recover(self, received=False):
        button(self.page, 'Recuperar publicação').click()
        pending(self.page, received)

    def confirmed(self, lifecycle='Publicada'):
        status(self.page, f'Versão 1 confirmada. Situação atual: {lifecycle}.')
        expect(button(self.page, 'Recuperar publicação')).to_be_hidden()
        listed(self.page, 1)

    def same_wire(self, count):
        assert len(self.posts) == count
        first = self.posts[0]
        assert first['key'] and first['raw']
        assert first['json'] == {
            'expected_revision': 1, 'expected_context_revision': 1}
        for request in self.posts:
            assert request['method'] == 'POST' and request['path'] == self.base
            assert (request['key'], request['raw'], request['json']) == (
                first['key'], first['raw'], first['json'])

    def stored(self):
        assert len(rows(self.ui, self.item)) == 1
        value = version_record(self.ui, self.item['type_id'], 1)
        for key in ('type_id', 'context_id', 'version', 'draft_revision',
                    'context_revision', 'schema_version', 'schema_sha256'):
            assert value[key] == self.receipts[0][key]
        assert value['draft_revision'] == value['context_revision'] == 1
        assert record(self.ui, self.parent['context_id']) == self.parent
        assert type_record(self.ui, self.item['type_id']) == self.item
        assert self.auth == []
        private(self.page)
        return value

    def close(self):
        self.page.unroute(self.post_pattern, self.post)
        self.page.unroute(self.get_pattern, self.detail)
        self.page.remove_listener('request', self.watch_auth)


@pytest.fixture
def recovery(ui):
    active = []
    def make(*args, **kwargs):
        value = Recovery(ui, *args, **kwargs)
        active.append(value)
        return value
    yield make
    for value in reversed(active):
        value.close()


def test_lost_receipt_quota_replay_and_current_lifecycle(recovery):
    h = recovery(['lost', 'quota', 'real'], detail_failure=True)
    h.publish()
    pending(h.page)
    h.same_wire(1)
    h.page.locator('#logout-button').click()
    confirm(h.page, False)
    pending(h.page)
    expect(h.page).to_have_url(h.ui.runtime.origin + '/contexts')
    expect(h.page.locator('#context-name')).to_have_value(h.item['body']['name'])
    h.recover()
    h.same_wire(2)
    h.recover(received=True)
    h.same_wire(3)
    assert len(h.gets) == 1
    original = version_record(h.ui, h.item['type_id'], 1)
    response = h.ui.api.post(h.base + '/1/deprecate', headers={'Idempotency-Key': uuid4().hex},
                             json={'expected_lifecycle_revision': 1})
    assert response.status_code == 200
    deprecated = version_record(h.ui, h.item['type_id'], 1)
    assert deprecated['lifecycle']['state'] == 'deprecated'
    assert deprecated['lifecycle']['revision'] == 2
    assert deprecated['lifecycle']['changed_at']
    assert snapshot(deprecated) == snapshot(original)
    button(h.page, 'Recuperar publicação').click()
    h.confirmed('Descontinuada')
    h.same_wire(3)
    assert [request.get('status') for request in h.posts] == [201, None, 200]
    assert len(h.gets) == 2 and h.gets[-1]['status'] == 200
    assert h.gets[-1]['record']['lifecycle'] == deprecated['lifecycle']
    assert snapshot(h.stored()) == snapshot(original)


@pytest.mark.parametrize('invalid', ['type_id', 'draft_revision'])
def test_invalid_receipt_preserves_intent_until_valid_replay(recovery, invalid):
    def corrupt(receipt):
        receipt[invalid] = (('0' * 32 if receipt['type_id'] != '0' * 32 else '1' * 32)
                            if invalid == 'type_id' else 2)
    h = recovery(['corrupt', 'real'], corrupt=corrupt)
    h.publish()
    pending(h.page)
    assert h.gets == []
    expect(h.page.get_by_text(re.compile(r'^Versão 1 confirmada\.', re.I))).to_have_count(0)
    h.same_wire(1)
    button(h.page, 'Recuperar publicação').click()
    h.confirmed()
    h.same_wire(2)
    assert [request['status'] for request in h.posts] == [201, 200]
    assert len(h.gets) == 1 and h.gets[0]['status'] == 200
    assert h.receipts[0] == h.receipts[1]
    assert snapshot(h.stored()) == snapshot(h.gets[0]['record'])


def test_contradictory_hash_requires_explicit_safe_abandon(recovery):
    def corrupt(receipt):
        receipt['schema_sha256'] = '0' * 64 if receipt['schema_sha256'] != '0' * 64 else '1' * 64
    h = recovery(['corrupt'], corrupt=corrupt)
    h.publish()
    pending(h.page, received=True)
    original = h.stored()
    h.recover(received=True)
    h.same_wire(1)
    assert len(h.gets) == 2
    assert all(request['status'] == 200 for request in h.gets)
    assert all(snapshot(request['record']) == snapshot(original) for request in h.gets)
    expect(h.page.get_by_text(re.compile(r'^Versão 1 confirmada\.', re.I))).to_have_count(0)
    h.page.locator('#version-publish-abandon').click()
    confirm(h.page, False)
    pending(h.page, received=True)
    h.same_wire(1)
    assert len(h.gets) == 2
    mutations = []
    def observe_mutations(request):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            mutations.append((request.method, urlsplit(request.url).path))
    h.page.on('request', observe_mutations)
    try:
        h.page.locator('#version-publish-abandon').click()
        confirm(h.page)
        status(h.page, ABANDONED)
        expect(button(h.page, 'Recuperar publicação')).to_be_hidden()
        expect(h.page.locator('#context-name')).to_be_editable()
        expect(h.page.locator('#context-description')).to_be_editable()
        assert mutations == []
    finally:
        h.page.remove_listener('request', observe_mutations)
    h.page.locator('#version-refresh').click()
    listed(h.page, 1)
    open_version(h.page, original)
    assert len(h.gets) == 3 and h.gets[-1]['status'] == 200
    assert snapshot(h.gets[-1]['record']) == snapshot(original)
    h.same_wire(1)
    assert snapshot(h.stored()) == snapshot(original)


def test_failed_context_navigation_keeps_old_publication_recoverable(recovery):
    h = recovery(['lost', 'real'])
    other = seed(h.ui, name='Contexto B persistido')
    other_type = seed_type(h.ui, other, 'Tipo B persistido', [field('other')])
    assert rows(h.ui, other_type) == []
    h.page.locator('#context-refresh').click()
    expect(h.page.locator('#context-refresh')).to_be_enabled()
    target = h.page.locator('#context-list').get_by_role(
        'button', name=other['body']['name'], exact=True)
    expect(target).to_be_visible()
    h.publish()
    pending(h.page)
    failures = []
    pattern = re.compile(re.escape(h.ui.runtime.origin + '/api/v1/contexts/' +
                                   other['context_id']) + r'(?:\?.*)?$')
    def fail_context(route):
        assert route.request.method == 'GET'
        failures.append(h.observed(route.request))
        route.fulfill(status=503, json={'error': {
            'code': 'synthetic_failure', 'message': 'Falha sintética ao abrir contexto'}})
    h.page.route(pattern, fail_context)
    try:
        target.click()
        confirm(h.page)
        expect(h.page.locator('#context-status')).to_contain_text(
            'Não foi possível abrir o contexto. Seu formulário foi preservado.')
        pending(h.page)
        expect(h.page.locator('#context-name')).to_have_value(h.item['body']['name'])
        revision(h.page, 1)
        assert len(failures) == 1
    finally:
        h.page.unroute(pattern, fail_context)
    button(h.page, 'Recuperar publicação').click()
    h.confirmed()
    h.same_wire(2)
    assert [request['status'] for request in h.posts] == [201, 200]
    assert len(h.gets) == 1 and h.gets[0]['status'] == 200
    h.stored()
    assert rows(h.ui, other_type) == []
