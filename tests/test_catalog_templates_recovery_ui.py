"""Recovery against the real browser, REST service and runtime SQLite store."""
import copy
import json
import re
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from playwright.sync_api import expect

from test_catalog_ui import browser, runtime, catalog_accounts, ui
from test_catalog_ui import private, record, seed, select, revision
from test_catalog_fields_ui import field
from test_catalog_types_read_ui import type_record
from test_catalog_types_write_ui import choose
from test_catalog_versions_read_ui import page_for
from test_catalog_templates_ui import field_description

BASE = '/api/v1/contexts'
TEMPLATES = '/api/v1/context-templates'
TYPES = '/api/v1/document-types/'
PRESERVED = 'O pedido foi preservado'
RECEIVED = 'A criação foi recebida'


class Recovery:
    def __init__(self, ui, other=False, stale=False):
        self.ui, self.stale = ui, stale
        self.a = seed(ui, fields=[field('original')])
        self.b = seed(ui, 'Contexto B', [field('outro')]) if other else None
        self.originals = [self.a] + ([self.b] if other else [])
        self.page = page_for(ui)
        select(self.page, self.a['body']['name'])
        private(self.page)
        self.form = (self.page.locator('#context-name').input_value(),
                     self.page.locator('#context-description').input_value())
        self.field_description = field_description(self.page, 'original').input_value()
        self.rows, self.requests, self.actions = [], {}, []
        self.type_fault, self.navigation_fault = None, False
        self.page.on('request', self.observe)
        self.page.route('**/api/v1/**', self.route)

    def observe(self, request):
        path = urlsplit(request.url).path
        if not path.startswith(('/api/', '/auth')):
            return
        raw = request.post_data
        row = dict(method=request.method, path=path, rawbody=raw,
                   json=json.loads(raw) if raw else None,
                   key=request.headers.get('idempotency-key'), status=None, receipt=None)
        self.rows.append(row)
        self.requests[request] = row

    def route(self, route):
        row = self.requests[route.request]
        path, method = row['path'], row['method']
        action = self.actions.pop(0) if self.actions and method == 'POST' and path.endswith('/clone') else None
        fault = self.type_fault if method == 'GET' and path.startswith(TYPES) else None
        if fault:
            self.type_fault = None
        navigation = self.navigation_fault and method == 'GET' and path == BASE + '/' + self.b['context_id']
        if navigation:
            self.navigation_fault = False
        if action == '429' or fault == '503' or navigation:
            row['status'] = 429 if action == '429' else 503
            route.fulfill(status=row['status'], content_type='application/json',
                          body=json.dumps({'error': {'code': 'temporarily_unavailable', 'message': 'Teste negativo'}}))
            return
        response = route.fetch()
        row['status'] = response.status
        if response.status >= 400:
            row['error_code'] = response.json().get('error', {}).get('code')
        if method == 'GET' and path == TEMPLATES:
            catalog = response.json()
            generic, = [item for item in catalog['items'] if item['template_id'] == 'generic']
            self.edition = copy.deepcopy(generic)
            if self.stale:
                assert response.status == 200
                generic['template_version'] += 1
                self.stale = False
                route.fulfill(response=response, json=catalog)
                return
        if method == 'POST' and path.endswith('/clone') and response.status in (200, 201):
            row['receipt'] = response.json()
            if action == 'lost':
                assert response.status == 201
                route.abort('failed')
                return
            if action in ('template_sha256', 'type_key'):
                assert response.status == 201
                bad = copy.deepcopy(row['receipt'])
                if action == 'template_sha256':
                    bad['template_sha256'] = '0' * 64 if bad['template_sha256'] != '0' * 64 else '1' * 64
                else:
                    bad['types'][0]['type_key'] += '_wrong'
                self.corrupted = bad
                route.fulfill(response=response, json=bad)
                return
        if fault == 'parent':
            assert response.status == 200
            bad = response.json()
            bad['context_id'] = '0' * 32 if bad['context_id'] != '0' * 32 else '1' * 32
            route.fulfill(response=response, json=bad)
            return
        route.fulfill(response=response)

    def load(self):
        with self.page.expect_response(lambda r: urlsplit(r.url).path == TEMPLATES) as response:
            self.page.locator('#template-load').click()
        assert response.value.status == 200
        self.page.locator('#template-list button[data-template-id="generic"]').click()
        expect(self.page.locator('#template-preview')).to_be_visible()
        expect(self.page.locator('#template-clone')).to_be_enabled()

    def clone(self):
        self.page.locator('#template-clone').click()
        choose(self.page)

    def posts(self):
        return [r for r in self.rows if r['method'] == 'POST' and r['path'].endswith('/clone')]

    def ids(self):
        receipt = next(row['receipt'] for row in self.posts() if row['receipt'] is not None)
        return receipt['context_id'], receipt['types'][0]['type_id']

    def gets(self):
        cid, tid = self.ids()
        return [r for r in self.rows if r['method'] == 'GET' and r['path'] in (BASE + '/' + cid, TYPES + tid)]

    @contextmanager
    def current(self, type_status=200):
        # For an initial successful POST the receipt is not available until the route fetch completes.
        def matches(response, kind):
            if not any(row['receipt'] is not None for row in self.posts()):
                return False
            cid, tid = self.ids()
            return response.request.method == 'GET' and urlsplit(response.url).path == (BASE + '/' + cid if kind == 'context' else TYPES + tid)
        with self.page.expect_response(lambda r: matches(r, 'context')) as context, self.page.expect_response(lambda r: matches(r, 'type')) as typ:
            yield
        assert (context.value.status, typ.value.status) == (200, type_status)
        assert context.value.finished() is None
        assert typ.value.finished() is None

    def pending(self, received=False):
        expect(self.page.locator('#template-clone-status')).to_contain_text(RECEIVED if received else PRESERVED)
        expect(self.page.locator('#template-recover')).to_be_enabled()
        expect(self.page.locator('#template-clone')).to_be_disabled()
        expect(self.page.locator('#context-save')).to_be_disabled()
        expect(self.page.locator('#template-open')).not_to_be_visible()

    def unchanged_form(self):
        expect(self.page.locator('#context-name')).to_have_value(self.form[0])
        expect(self.page.locator('#context-description')).to_have_value(self.form[1])
        expect(field_description(self.page, 'original')).to_have_value(self.field_description)
        revision(self.page, 1)

    def same_request(self, count):
        posts = self.posts()
        assert len(posts) == count
        original = {k: posts[0][k] for k in ('method', 'path', 'key', 'rawbody', 'json')}
        assert original['method'] == 'POST' and original['path'] == TEMPLATES + '/generic/clone'
        assert str(UUID(original['key'])) == original['key']
        assert UUID(original['key']).version == 4
        assert json.loads(original['rawbody']) == original['json']
        assert all({k: r[k] for k in original} == original for r in posts)

    def success(self, number=1):
        expect(self.page.locator('#template-clone-status')).to_have_text(re.compile(
            rf'^Clonagem confirmada\. Contexto e tipo criados inicialmente na revisão 1\. Revisões atuais consultadas: contexto {number}; tipo {number}\. O formulário atual foi preservado\.$', re.I))
        expect(self.page.locator('#template-open')).to_be_enabled()
        expect(self.page.locator('#template-recover')).not_to_be_visible()
        self.unchanged_form()
        private(self.page)
        cid, tid = self.ids()
        context, typ = record(self.ui, cid), type_record(self.ui, tid)
        assert context['revision'] == typ['revision'] == number
        assert typ['context_id'] == cid and typ['state'] == 'draft'
        receipt = next(row['receipt'] for row in self.posts() if row['receipt'] is not None)
        assert receipt['context_revision'] == receipt['types'][0]['revision'] == 1
        assert all(r['receipt'] == receipt for r in self.posts() if r['receipt'] is not None)
        self.inventory([cid])
        return context, typ

    def inventory(self, created=()):
        mutations = [r for r in self.rows if r['method'] in ('POST', 'PUT', 'PATCH', 'DELETE')]
        assert mutations == self.posts()
        response = self.ui.api.get(BASE)
        assert response.status_code == 200
        contexts = response.json()['items']
        expected = {r['context_id'] for r in self.originals} | set(created)
        assert len(contexts) == len(expected)
        assert {r['context_id'] for r in contexts} == expected
        types = []
        for cid in expected:
            response = self.ui.api.get(BASE + '/' + cid + '/types')
            assert response.status_code == 200
            types.extend(response.json()['items'])
        assert len(types) == len(created)
        if created:
            assert [t['type_id'] for t in types] == [self.ids()[1]]
            versions = self.ui.api.get(TYPES + self.ids()[1] + '/versions')
            assert versions.status_code == 200 and versions.json()['items'] == []
        for original in self.originals:
            assert record(self.ui, original['context_id']) == original
        assert not any(r['path'].startswith('/auth') for r in self.rows)


@pytest.fixture
def recovery(ui):
    active = []
    def make(other=False, stale=False):
        h = Recovery(ui, other, stale)
        active.append(h)
        h.load()
        return h
    try:
        yield make
    finally:
        for h in reversed(active):
            h.page.unroute('**/api/v1/**', h.route)
            h.page.remove_listener('request', h.observe)


def test_historical_replay_then_get_only_recovery(recovery):
    h = recovery()
    h.actions = ['lost', '429', None]
    h.clone(); h.pending()
    initial = copy.deepcopy(h.posts()[0]['receipt'])
    cid, tid = h.ids()
    for path, current in ((BASE + '/' + cid, record(h.ui, cid)), (TYPES + tid, type_record(h.ui, tid))):
        body = copy.deepcopy(current['body']); body['description'] = 'Alterado na revisão dois'
        response = h.ui.api.put(path, json={'body': body, 'expected_revision': 1})
        assert response.status_code == 200
    h.page.locator('#template-recover').click(); h.pending()
    h.same_request(2)
    assert [r['status'] for r in h.posts()] == [201, 429]
    assert h.gets() == []
    h.type_fault = '503'
    with h.current(503):
        h.page.locator('#template-recover').click()
    h.pending(received=True)
    other = h.page.locator('#template-list button[data-template-id]:not([data-template-id="generic"])')
    assert other.count() > 0
    other.first.click(); h.pending(received=True)
    with h.current():
        h.page.locator('#template-recover').click()
    context, typ = h.success(2)
    assert context['body']['description'] == typ['body']['description'] == 'Alterado na revisão dois'
    h.same_request(3)
    assert [r['status'] for r in h.posts()] == [201, 429, 200]
    assert h.posts()[2]['receipt'] == initial
    assert sorted((r['path'], r['status']) for r in h.gets()) == sorted([(BASE + '/' + cid, 200)] * 2 + [(TYPES + tid, 503), (TYPES + tid, 200)])
    h.page.locator('#template-open').click()
    expect(h.page.locator('#context-name')).to_have_value(context['body']['name'])
    expect(h.page.locator('#context-description')).to_have_value(context['body']['description'])
    revision(h.page, 2); h.same_request(3); h.inventory([cid]); private(h.page)


@pytest.mark.parametrize('binding', ['template_sha256', 'type_key'])
def test_invalid_receipt_binding_recovers_original_request(recovery, binding):
    h = recovery(); h.actions = [binding]
    h.clone(); h.pending()
    assert h.corrupted != h.posts()[0]['receipt']
    assert h.posts()[0]['status'] == 201 and h.gets() == []
    with h.current():
        h.page.locator('#template-recover').click()
    h.success(); h.same_request(2)
    assert [r['status'] for r in h.posts()] == [201, 200]
    assert len(h.gets()) == 2


def test_abandon_cancel_accept_and_refresh_gate(recovery):
    h = recovery(other=True); h.actions = ['lost']
    h.clone(); h.pending()
    created = record(h.ui, h.ids()[0]); typ = type_record(h.ui, h.ids()[1])
    def initiated():
        return [(r['method'], r['path'], r['rawbody'], r['key']) for r in h.rows]
    snapshot = initiated()
    h.page.locator('#template-abandon').click(); choose(h.page, False)
    h.pending(); h.same_request(1)
    assert initiated() == snapshot
    h.page.locator('#template-abandon').click(); choose(h.page)
    expect(h.page.locator('#template-clone-status')).to_contain_text('Recuperação abandonada.')
    expect(h.page.locator('#template-recover')).not_to_be_visible()
    assert initiated() == snapshot
    select(h.page, h.b['body']['name'])
    expect(h.page.locator('#template-clone')).to_be_disabled()
    h.page.locator('#context-new').click()
    expect(h.page.locator('#context-name')).to_have_value('')
    expect(h.page.locator('#template-clone')).to_be_disabled()
    select(h.page, h.b['body']['name'])
    expect(h.page.locator('#template-clone')).to_be_disabled()
    h.load(); h.same_request(1)
    assert record(h.ui, h.ids()[0]) == created and type_record(h.ui, h.ids()[1]) == typ
    h.inventory([h.ids()[0]]); private(h.page)


def test_failed_navigation_preserves_pending_and_form(recovery):
    h = recovery(other=True); h.actions = ['lost']
    h.clone(); h.pending(); h.navigation_fault = True
    with h.page.expect_response(lambda r: urlsplit(r.url).path == BASE + '/' + h.b['context_id']) as response:
        h.page.locator('#context-list').get_by_role('button', name=h.b['body']['name'], exact=True).click()
        choose(h.page)
    assert response.value.status == 503
    expect(h.page.locator('#context-status')).to_contain_text('Não foi possível abrir o contexto. Seu formulário foi preservado.')
    h.unchanged_form(); h.pending(); h.same_request(1)
    with h.current():
        h.page.locator('#template-recover').click()
    h.success(); h.same_request(2)
    assert [r['status'] for r in h.posts()] == [201, 200] and len(h.gets()) == 2


def test_real_template_edition_conflict_requires_refresh(recovery):
    h = recovery(other=True, stale=True)
    real_version, real_sha = h.edition['template_version'], h.edition['template_sha256']
    h.clone()
    expect(h.page.locator('#template-clone-status')).to_contain_text('A edição selecionada mudou.')
    expect(h.page.locator('#template-recover')).not_to_be_visible()
    expect(h.page.locator('#template-clone')).to_be_disabled()
    assert h.posts()[0]['status'] == 409
    assert h.posts()[0]['error_code'] == 'template_version_conflict'
    h.inventory()
    select(h.page, h.b['body']['name']); select(h.page, h.a['body']['name'])
    expect(h.page.locator('#template-clone')).to_be_disabled()
    h.load()
    with h.current():
        h.clone()
    h.success()
    first, second = h.posts()
    assert [first['status'], second['status']] == [409, 201]
    assert first['path'] == second['path'] == TEMPLATES + '/generic/clone'
    assert first['method'] == second['method'] == 'POST'
    assert first['key'] != second['key']
    for attempt in (first, second):
        assert str(UUID(attempt['key'])) == attempt['key'] and UUID(attempt['key']).version == 4
        assert json.loads(attempt['rawbody']) == attempt['json']
        assert attempt['json']['expected_sha256'] == real_sha
    assert first['json'] == dict(second['json'], expected_version=real_version + 1)
    assert second['json']['expected_version'] == real_version and len(h.gets()) == 2


def test_wrong_current_type_parent_retains_receipt_and_get_only_recovery(recovery):
    h = recovery(); h.type_fault = 'parent'
    with h.current():
        h.clone()
    h.pending(received=True); h.unchanged_form()
    receipt = copy.deepcopy(h.posts()[0]['receipt'])
    assert h.posts()[0]['status'] == 201 and len(h.gets()) == 2
    with h.current():
        h.page.locator('#template-recover').click()
    context, typ = h.success(); h.same_request(1)
    assert h.posts()[0]['receipt'] == receipt
    assert context['context_id'] == typ['context_id'] == receipt['context_id']
    assert typ['type_id'] == receipt['types'][0]['type_id']
    assert len(h.gets()) == 4 and all(r['status'] == 200 for r in h.gets())
