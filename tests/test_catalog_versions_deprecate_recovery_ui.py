"""Browser recovery against the real REST service and its SQLite-backed runtime."""
import re
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from playwright.sync_api import expect

from test_catalog_ui import (
    ui, runtime, catalog_accounts, browser, seed, record, select, private,
)
from test_catalog_fields_ui import field
from test_catalog_types_read_ui import (
    seed_type, type_record, open_type, loaded_types,
)
from test_catalog_types_write_ui import start, choose, locked, revision
from test_catalog_versions_read_ui import (
    publish, version_record, open_version,
)

from test_catalog_versions_publish_ui import listed
from test_catalog_versions_deprecate_ui import dirty_description

TYPES = '/api/v1/document-types/'
PENDING = ('O pedido de descontinuação da versão 1 foi preservado e a ação pode '
           'já ter ocorrido. Recuperar descontinuação repete exatamente o mesmo '
           'pedido, com a mesma chave. Você também pode abandonar a recuperação explicitamente.')
RECEIVED = ('A descontinuação da versão 1 foi recebida, mas seus detalhes ainda '
            'não foram confirmados. Recuperar descontinuação apenas consulta a versão recebida.')
CONFIRMED = 'Versão 1 descontinuada. A consulta histórica foi preservada.'
ABANDONED = ('Recuperação da descontinuação da versão 1 abandonada. '
             'Confira a situação da versão antes de tentar novamente.')


def status(s, text, pending=True):
    expect(s.page.locator('#version-deprecate-status')).to_have_text(text)
    recover = s.page.locator('#version-deprecate-recover')
    if pending:
        expect(recover).to_be_visible()
        expect(recover).to_be_enabled()
        locked(s.page)
    else:
        expect(recover).to_be_hidden()


def deprecate(s):
    s.page.locator('#version-deprecate').click()
    choose(s.page)


def recover(s):
    s.page.locator('#version-deprecate-recover').click()


def immutable(value):
    return {key: value[key] for key in (
        'type_id', 'version', 'context_id', 'draft_revision', 'context_revision',
        'schema_version', 'context_name', 'type_name', 'schema', 'schema_sha256', 'created_at',
    )}


@pytest.fixture
def recovery(ui):
    active = []

    def setup(mode='lost', count=1, other=False):
        parent = seed(ui, 'Recuperação de descontinuação', [field('comum')])
        item = seed_type(ui, parent, 'Documento original', [field('local')])
        views = [publish(ui, parent, item) for _ in range(count)]
        assert [v['version'] for v in views] == list(range(1, count + 1))
        for view in views:
            assert view['lifecycle']['state'] == 'published'
            assert view['lifecycle']['revision'] == 1
        alternate = seed(ui, 'Contexto de destino') if other else None
        alternate_type = seed_type(ui, alternate, 'Outro documento', [field('outro')]) if other else None
        page = start(ui, parent)
        open_type(page, item)
        loaded_types(page, 1)
        listed(page, count)
        open_version(page, views[0])
        revision(page, 1)
        expect(page.locator('#context-save')).to_be_disabled()
        expect(page.locator('#version-publish')).to_be_enabled()
        if other:
            page.locator('#context-refresh').click()
            expect(page.locator('#context-refresh')).to_be_enabled()
            expect(page.locator('#context-list').get_by_role(
                'button', name=alternate['body']['name'], exact=True)).to_be_visible()
        s = SimpleNamespace(page=page, parent=parent, item=item, views=views,
                            alternate=alternate, alternate_type=alternate_type,
                            posts=[], gets=[], receipts=[], auth=[], traffic=[],
                            routes=[], mode=mode)
        s.detail = TYPES + item['type_id'] + '/versions/1'
        s.post = s.detail + '/deprecate'

        def observe(request):
            path = urlsplit(request.url).path
            if path.startswith('/auth/'):
                s.auth.append((request.method, path))
            if path.startswith('/api/'):
                s.traffic.append((request.method, path))
        s.observe = observe
        page.on('request', observe)
        active.append(s)

        def post_route(route):
            request = route.request
            if request.method != 'POST':
                route.continue_()
                return
            wire = dict(method=request.method, path=urlsplit(request.url).path,
                        raw=request.post_data, json=request.post_data_json,
                        key=request.header_value('Idempotency-Key'), status=None)
            s.posts.append(wire)
            number = len(s.posts)
            if s.mode == 'chain' and number == 2:
                wire['status'] = 429
                route.fulfill(status=429, json={'error': {
                    'code': 'synthetic_rate_limit', 'message': 'Limite sintético'}})
                return
            response = route.fetch()
            wire['actual_status'] = response.status
            assert response.status == 200
            receipt = response.json()
            s.receipts.append(receipt)
            assert set(receipt) == {'type_id', 'version', 'schema_sha256', 'lifecycle'}
            assert receipt['type_id'] == item['type_id'] and receipt['version'] == 1
            assert receipt['schema_sha256'] == views[0]['schema_sha256']
            assert receipt['lifecycle']['state'] == 'deprecated'
            assert receipt['lifecycle']['revision'] == 2
            assert receipt['lifecycle']['changed_at']
            if number == 1 and s.mode in ('lost', 'chain'):
                wire['status'] = 'aborted-after-200'
                route.abort('failed')
            elif number == 1 and s.mode in ('hash', 'version', 'status201'):
                negative = dict(receipt)
                if s.mode == 'hash':
                    negative['schema_sha256'] = ('0' if receipt['schema_sha256'] != '0' * 64 else '1') * 64
                if s.mode == 'version':
                    negative['version'] = 2
                wire['status'] = 201 if s.mode == 'status201' else 200
                route.fulfill(status=wire['status'], json=negative)
            else:
                wire['status'] = response.status
                route.fulfill(response=response)

        def detail_route(route):
            if route.request.method != 'GET':
                route.continue_()
                return
            s.gets.append(dict(method='GET', path=s.detail, status=None))
            if s.mode == 'chain' and len(s.gets) == 1:
                s.gets[-1]['status'] = 503
                route.fulfill(status=503, json={'error': {
                    'code': 'synthetic_unavailable', 'message': 'Consulta indisponível'}})
                return
            response = route.fetch()
            s.gets[-1]['status'] = response.status
            assert response.status == 200
            route.fulfill(response=response)

        for path, handler in ((s.post, post_route), (s.detail, detail_route)):
            url = ui.runtime.origin + path
            page.route(url, handler)
            s.routes.append((url, handler))
        return s

    yield setup
    for s in reversed(active):
        s.page.remove_listener('request', s.observe)
        for url, handler in reversed(s.routes):
            s.page.unroute(url, handler)
        private(s.page)
        assert s.auth == []
        s.page.close()


def same_request(s, count):
    assert len(s.posts) == count
    first = s.posts[0]
    assert UUID(first['key']).int != 0
    assert first['json'] == {'expected_lifecycle_revision': 1}
    for post in s.posts:
        assert (post['method'], post['path'], post['key'], post['raw'], post['json']) == (
            'POST', s.post, first['key'], first['raw'], first['json'])
    assert all(receipt == s.receipts[0] for receipt in s.receipts)


def server_unchanged(ui, s):
    assert record(ui, s.parent['context_id']) == s.parent
    assert type_record(ui, s.item['type_id']) == s.item
    final = version_record(ui, s.item['type_id'], 1)
    assert immutable(final) == immutable(s.views[0])
    assert final['lifecycle'] == s.receipts[0]['lifecycle']
    assert final['lifecycle']['state'] == 'deprecated'
    assert final['lifecycle']['revision'] == 2
    listing = ui.api.get(TYPES + s.item['type_id'] + '/versions')
    assert listing.status_code == 200
    assert len(listing.json()['items']) == len(s.views)
    assert listing.json()['next_cursor'] is None
    return final


def terminal(s, count):
    status(s, CONFIRMED, pending=False)
    expect(s.page.locator('#version-detail')).to_be_visible()
    expect(s.page.locator('#version-reference')).to_have_text(
        f"Tipo: {s.item['type_id']} · Versão: 1 · SHA-256: {s.views[0]['schema_sha256']}")
    expect(s.page.locator('#version-lifecycle')).to_contain_text('Descontinuada')
    expect(s.page.locator('#version-lifecycle')).to_contain_text('· Revisão 2 ·')
    listed(s.page, count)


def test_lost_reply_replay_and_get_only_recovery_remain_pinned(ui, recovery):
    s = recovery(mode='chain', count=2)
    deprecate(s)
    status(s, PENDING)
    assert len(s.posts) == 1 and not s.gets
    expect(s.page.locator('#version-publish')).to_be_disabled()
    s.page.locator('#logout-button').click()
    choose(s.page, accept=False)
    status(s, PENDING)
    expect(s.page).to_have_url(ui.runtime.origin + '/contexts')
    assert not s.auth
    recover(s)
    status(s, PENDING)
    assert len(s.posts) == 2 and not s.gets
    recover(s)
    status(s, RECEIVED)
    assert len(s.posts) == 3 and len(s.gets) == 1
    open_version(s.page, s.views[1])
    listed(s.page, 2)
    expect(s.page.locator('#version-deprecate')).to_be_visible()
    expect(s.page.locator('#version-deprecate')).to_be_disabled()
    expect(s.page.locator('#version-publish')).to_be_disabled()
    status(s, RECEIVED)
    recover(s)
    terminal(s, 2)
    same_request(s, 3)
    assert [p['status'] for p in s.posts] == ['aborted-after-200', 429, 200]
    assert [g['status'] for g in s.gets] == [503, 200]
    final = server_unchanged(ui, s)
    open_version(s.page, final)
    assert len(s.posts) == 3
    assert version_record(ui, s.item['type_id'], 2) == s.views[1]


@pytest.mark.parametrize('negative', ['hash', 'version', 'status201'])
def test_invalid_receipt_retains_intent_until_real_200_replay(ui, recovery, negative):
    s = recovery(mode=negative)
    deprecate(s)
    status(s, PENDING)
    assert len(s.posts) == 1 and not s.gets
    assert s.posts[0]['actual_status'] == 200
    assert s.posts[0]['status'] == (201 if negative == 'status201' else 200)
    expect(s.page.locator('#version-deprecate-status')).not_to_have_text(CONFIRMED)
    recover(s)
    terminal(s, 1)
    same_request(s, 2)
    assert s.posts[1]['actual_status'] == s.posts[1]['status'] == 200
    assert [g['status'] for g in s.gets] == [200]
    server_unchanged(ui, s)


def test_explicit_abandon_preserves_dirty_draft_without_requests(ui, recovery):
    s = recovery()
    s.page.locator('#context-name').fill('UNSAVED')
    field_description = dirty_description(s.page)
    s.page.locator('#context-description').fill('Descrição específica não salva')
    deprecate(s)
    status(s, PENDING)
    s.page.locator('#version-deprecate-abandon').click()
    choose(s.page, accept=False)
    status(s, PENDING)
    same_request(s, 1)
    s.page.locator('#version-deprecate-abandon').click()
    before = list(s.traffic)
    choose(s.page)
    status(s, ABANDONED, pending=False)
    assert s.traffic == before
    expect(s.page.locator('#context-name')).to_have_value('UNSAVED')
    expect(field_description).to_have_value('synthetic-unsaved')
    expect(s.page.locator('#context-description')).to_have_value('Descrição específica não salva')
    revision(s.page, 1)
    expect(s.page.locator('#context-save')).to_be_enabled()
    assert not s.gets
    final = server_unchanged(ui, s)
    s.page.locator('#version-refresh').click()
    listed(s.page, 1)
    open_version(s.page, final)
    expect(s.page.locator('#version-deprecate')).to_be_hidden()
    same_request(s, 1)
    assert [g['status'] for g in s.gets] == [200]


def test_failed_context_navigation_preserves_clean_pending_intent(ui, recovery):
    s = recovery(other=True)
    target = ui.runtime.origin + '/api/v1/contexts/' + s.alternate['context_id']
    faults = []

    def unavailable(route):
        faults.append((route.request.method, urlsplit(route.request.url).path))
        assert route.request.method == 'GET'
        route.fulfill(status=503, json={'error': {
            'code': 'synthetic_unavailable', 'message': 'Contexto indisponível'}})

    s.page.route(target, unavailable)
    s.routes.append((target, unavailable))
    deprecate(s)
    status(s, PENDING)
    s.page.locator('#context-list').get_by_role(
        'button', name=s.alternate['body']['name'], exact=True).click()
    choose(s.page)
    expect(s.page.locator('#context-status')).to_contain_text(
        'Não foi possível abrir o contexto. Seu formulário foi preservado.')
    expect(s.page.locator('#context-name')).to_have_value(s.item['body']['name'])
    revision(s.page, 1)
    status(s, PENDING)
    assert faults == [('GET', urlsplit(target).path)]
    assert len(s.posts) == 1 and not s.gets
    s.page.unroute(target, unavailable)
    s.routes.remove((target, unavailable))
    recover(s)
    terminal(s, 1)
    same_request(s, 2)
    assert [g['status'] for g in s.gets] == [200]
    server_unchanged(ui, s)
    assert record(ui, s.alternate['context_id']) == s.alternate
    assert type_record(ui, s.alternate_type['type_id']) == s.alternate_type
    select(s.page, s.alternate['body']['name'])
    loaded_types(s.page, 1)
    open_type(s.page, s.alternate_type)
    listed(s.page, 0)
    assert not any(method == 'POST' and s.alternate_type['type_id'] in path
                   for method, path in s.traffic)
    expect(s.page.locator('#version-deprecate-status')).to_have_text('')
    assert re.fullmatch(r'[0-9a-f]{64}', s.views[0]['schema_sha256'], re.I)
