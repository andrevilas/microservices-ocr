"""Browser regressions using synthetic accounts and real catalog responses.

The pagehide case simulates the lifecycle boundary without navigating. These
cases do not establish global orphan-type absence; backend atomicity tests do.
"""

from contextlib import contextmanager
from copy import deepcopy
import re
from urllib.parse import urlsplit
from uuid import UUID

import pytest
from playwright.sync_api import expect

from test_catalog_ui import (
    ui, runtime, catalog_accounts, browser, private, record, seed, select,
)
from test_catalog_fields_ui import field
from test_catalog_types_write_ui import choose
from test_catalog_versions_read_ui import page_for
from test_catalog_templates_ui import (
    BASE, TEMPLATES, catalog, rows, preview, readonly_fields, field_description,
)


LOADED = 'Três modelos carregados.'
FAILED = 'Não foi possível carregar os modelos.'


def barrier(page):
    page.evaluate('''() => new Promise(resolve =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)))''')


def load(page):
    with page.expect_response(lambda response:
            urlsplit(response.url).path == TEMPLATES
            and response.request.method == 'GET') as pending:
        page.locator('#template-load').click()
    response = pending.value
    assert response.status == 200
    assert response.finished() is None
    expect(page.locator('#template-status')).to_contain_text(LOADED)


def setup(ui, dirty=False):
    original = seed(ui, 'Original sintético A', [field()])
    _, templates = catalog(ui)
    generic = [item for item in templates
               if re.search(r'gen[eé]ric', item['context']['name'], re.I)]
    assert len(generic) == 1
    page = page_for(ui)
    select(page, original['body']['name'])
    load(page)
    preview(page, generic[0])
    if dirty:
        page.locator('#context-name').fill('Rascunho sintético A')
        field_description(page, 'documento').fill('Descrição ainda não salva')
        expect(page.locator('#template-clone')).to_be_disabled()
    return page, original, templates, generic[0]


def original_form(page, original, dirty=False):
    expect(page.locator('#context-name')).to_have_value(
        'Rascunho sintético A' if dirty else original['body']['name'])
    expect(field_description(page, 'documento')).to_have_value(
        'Descrição ainda não salva' if dirty else 'Descrição sintética')
    expect(page.locator('#context-revision')).to_contain_text(
        re.compile(r'^Revisão 1\b'))


def unchanged_preview(page, template, templates):
    assert page.locator('#template-list button').evaluate_all(
        '(nodes) => nodes.map(node => node.dataset.templateId)'
    ) == [item['template_id'] for item in templates]
    expect(page.locator('#template-preview')).to_be_visible()
    expect(page.locator('#template-hash')).to_have_text(template['template_sha256'])
    expect(page.locator('#template-edition')).to_have_text(
        f"Edição {template['template_version']}")
    for prefix, body in [('context', template['context']),
                         ('type', template['types'][0]['body'])]:
        for key in ('name', 'description'):
            expect(page.locator(f'#template-{prefix}-{key}')).to_have_text(body[key])
    readonly_fields(page, '#template-common', template['context']['common_fields'])
    readonly_fields(page, '#template-specific',
                    template['types'][0]['body']['specific_fields'])


def observe(page):
    events = []
    # Never retain auth/me bodies, headers, or responses.
    def watch(request):
        path = urlsplit(request.url).path
        if path.startswith(('/api/', '/auth/')):
            events.append((request.method, path))
    page.on('request', watch)
    return events


def writes(events):
    return [event for event in events if event[0] not in ('GET', 'HEAD', 'OPTIONS')]


def listing(ui):
    response = ui.api.get(BASE, params={'limit': 100})
    assert response.status_code == 200
    return response.json()


def types(ui, context_id):
    assert re.fullmatch(r'[0-9a-f]{32}', context_id)
    response = ui.api.get(f'{BASE}/{context_id}/types')
    assert response.status_code == 200
    return response.json()['items']


def clone_path(template):
    return f"{TEMPLATES}/{template['template_id']}/clone"


def clone_request(request, template):
    assert request.method == 'POST'
    assert urlsplit(request.url).path == clone_path(template)
    assert request.post_data_json == {
        'expected_version': template['template_version'],
        'expected_sha256': template['template_sha256'],
    }
    key = request.header_value('Idempotency-Key')
    assert key and str(UUID(key)) == key.lower()
    return key


def start_clone(page):
    page.locator('#template-clone').click()
    choose(page)


@contextmanager
def held_catalog(page):
    held = []
    def capture(route):
        assert route.request.method == 'GET'
        assert urlsplit(route.request.url).path == TEMPLATES
        assert not held
        held.append(route)
    def release():
        assert len(held) == 1
        route = held[0]
        response = route.fetch()
        assert response.status == 200
        with page.expect_event('requestfinished', predicate=lambda request:
                request.method == 'GET'
                and urlsplit(request.url).path == TEMPLATES):
            route.fulfill(response=response)
            held.clear()
        barrier(page)
    page.route('**' + TEMPLATES, capture)
    try:
        yield held, release
    finally:
        for route in held:
            route.abort()
        page.unroute('**' + TEMPLATES, capture)


def begin_held_load(page, held):
    with page.expect_request(lambda request:
            request.method == 'GET' and urlsplit(request.url).path == TEMPLATES):
        page.locator('#template-load').click()
    expect(page.locator('#template-section')).to_have_attribute('aria-busy', 'true')
    assert len(held) == 1


@pytest.mark.parametrize('fault', ['missing-third', 'duplicate-id', 'unknown-root'])
def test_invalid_catalog_preserves_dirty_draft_and_preview(ui, fault):
    page, original, templates, generic = setup(ui, dirty=True)
    before = listing(ui)
    events = observe(page)
    received = []
    def corrupt(route):
        response = route.fetch()
        assert response.status == 200
        data = deepcopy(response.json())
        items = rows(data)
        if fault == 'missing-third':
            del items[2]
        elif fault == 'duplicate-id':
            items[1]['template_id'] = items[0]['template_id']
        else:
            data['unexpected_synthetic_property'] = True
        received.append(data)
        route.fulfill(response=response, json=data)
    page.route('**' + TEMPLATES, corrupt)
    try:
        with page.expect_event('requestfinished', predicate=lambda request:
                urlsplit(request.url).path == TEMPLATES):
            page.locator('#template-load').click()
        expect(page.locator('#template-status')).to_contain_text(FAILED)
        barrier(page)
        assert len(received) == 1
        assert events == [('GET', TEMPLATES)]
        unchanged_preview(page, generic, templates)
        original_form(page, original, dirty=True)
        assert record(ui, original['context_id']) == original
        assert listing(ui) == before
    finally:
        page.unroute('**' + TEMPLATES, corrupt)
    load(page)
    unchanged_preview(page, generic, templates)
    original_form(page, original, dirty=True)
    expect(page.locator('#template-clone')).to_be_disabled()
    assert events == [('GET', TEMPLATES), ('GET', TEMPLATES)]
    assert record(ui, original['context_id']) == original
    private(page)


def test_pre_abandon_get_cannot_clear_refresh_gate(ui):
    page, original, templates, generic = setup(ui)
    before = listing(ui)
    events, receipts, keys = observe(page), [], []
    path = clone_path(generic)
    def commit_then_abort(route):
        keys.append(clone_request(route.request, generic))
        response = route.fetch()
        assert response.status == 201
        receipts.append(response.json())
        route.abort()
    page.route('**' + path, commit_then_abort)
    try:
        start_clone(page)
        expect(page.locator('#template-recover')).to_be_visible()
        expect(page.locator('#template-recover')).to_be_enabled()
        assert len(receipts) == len(keys) == 1
        receipt = receipts[0]
        assert receipt['template_sha256'] == generic['template_sha256']
        created_id = receipt['context_id']
        assert re.fullmatch(r'[0-9a-f]{32}', created_id)
        assert len(types(ui, created_id)) == 1
        with held_catalog(page) as (held, release):
            begin_held_load(page, held)
            count = len(events)
            page.locator('#template-abandon').click()
            choose(page)
            expect(page.locator('#template-recover')).to_be_hidden()
            assert len(events) == count
            release()
            expect(page.locator('#template-section')).to_have_attribute('aria-busy', 'false')
            expect(page.locator('#template-clone')).to_be_disabled()
            expect(page.locator('#template-recover')).to_be_hidden()
            unchanged_preview(page, generic, templates)
            assert events.count(('GET', TEMPLATES)) == 1
            assert writes(events) == [('POST', path)]
        load(page)
        expect(page.locator('#template-clone')).to_be_enabled()
        assert events.count(('GET', TEMPLATES)) == 2
        assert writes(events) == [('POST', path)]
        after = listing(ui)
        assert len(after['items']) == len(before['items']) + 1
        assert {item['context_id'] for item in after['items']} - {
            item['context_id'] for item in before['items']} == {created_id}
        assert len(types(ui, created_id)) == 1
        original_form(page, original)
        assert record(ui, original['context_id']) == original
        private(page)
    finally:
        page.unroute('**' + path, commit_then_abort)


def test_pagehide_invalidates_late_catalog_render(ui):
    page, original, _, _ = setup(ui, dirty=True)
    before = listing(ui)
    events = observe(page)
    with held_catalog(page) as (held, release):
        begin_held_load(page, held)
        # Lifecycle simulation only: no navigation and no native beforeunload.
        page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pagehide'))")
        expect(page.locator('#template-section')).to_be_hidden()
        expect(page.locator('#template-list')).to_be_empty()
        expect(page.locator('#template-preview')).to_be_hidden()
        release()
        expect(page.locator('#template-section')).to_be_hidden()
        expect(page.locator('#template-list')).to_be_empty()
        expect(page.locator('#template-preview')).to_be_hidden()
        assert events == [('GET', TEMPLATES)]
        assert record(ui, original['context_id']) == original
        assert listing(ui) == before
        assert ui.dialogs == []
        private(page)
    # The main editor was destroyed; its old field DOM is not asserted.


def test_clone_401_preserves_pending_intent_without_retry(ui):
    page, original, _, generic = setup(ui)
    before, events, keys = listing(ui), observe(page), []
    path = clone_path(generic)
    def expire(route):
        keys.append(clone_request(route.request, generic))
        # Synthetic rejection before any server commit.
        route.fulfill(status=401, json={'detail': 'Session expired'})
    page.route('**' + path, expire)
    try:
        start_clone(page)
        expect(page.locator('#context-status')).to_contain_text(
            re.compile(r'expir|entre novamente', re.I))
        expect(page.locator('#context-login')).to_be_visible()
        expect(page.locator('#template-section')).to_be_hidden()
        expect(page.locator('#template-clone-status')).to_contain_text(
            'Sessão expirada. O pedido de clonagem e sua recuperação foram preservados.')
        expect(page.locator('#template-recover')).to_be_hidden()
        expect(page.locator('#template-recover')).to_be_disabled()
        barrier(page)
        assert len(keys) == 1
        assert events == [('POST', path)]
        original_form(page, original)
        assert record(ui, original['context_id']) == original
        assert listing(ui) == before
        assert types(ui, original['context_id']) == []
        private(page)
    finally:
        page.unroute('**' + path, expire)


def test_real_context_quota_rejects_clone_before_admission(ui):
    original = seed(ui, 'Original sintético A', [field()])
    for number in range(99):
        seed(ui, f'Contexto sintético de cota {number:03d}')
    before = listing(ui)
    assert len(before['items']) == 100
    _, templates = catalog(ui)
    generic = [item for item in templates
               if re.search(r'gen[eé]ric', item['context']['name'], re.I)]
    assert len(generic) == 1
    generic = generic[0]
    page = page_for(ui)
    target = page.locator('#context-list').get_by_role(
        'button', name=original['body']['name'], exact=True)
    expect(page.locator('#context-list button').first).to_be_visible()
    while not target.count():
        more = page.locator('#context-more')
        expect(more).to_be_visible()
        previous = page.locator('#context-list button').count()
        more.click()
        expect(page.locator('#context-list button')).not_to_have_count(previous)
    select(page, original['body']['name'])
    load(page)
    preview(page, generic)
    events = observe(page)
    path = clone_path(generic)
    with page.expect_response(lambda response:
            response.request.method == 'POST'
            and urlsplit(response.url).path == path) as pending:
        start_clone(page)
    response = pending.value
    assert response.status == 429
    assert response.finished() is None
    clone_request(response.request, generic)
    assert response.json()['error']['code'] == 'context_quota_exceeded'
    expect(page.locator('#template-clone-status')).to_contain_text(
        re.compile(r'(limite|cota).*contextos', re.I))
    expect(page.locator('#template-recover')).to_be_hidden()
    barrier(page)
    assert events == [('POST', path)]
    original_form(page, original)
    assert record(ui, original['context_id']) == original
    assert listing(ui) == before
    assert types(ui, original['context_id']) == []
    private(page)
