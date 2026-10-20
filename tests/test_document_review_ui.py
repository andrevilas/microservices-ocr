"""C7a: read-only document review page in real Chromium against the real runtime.

Synthetic data only; no fetch mocks and no dependency overrides. Jobs, catalog entries and
metadata revisions are seeded through the real HTTP API with the owner's JWT.
"""
import os
import re
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from test_catalog_ui import ui, runtime, catalog_accounts, browser, private  # noqa: F401  (fixtures)
from test_catalog_mcp_boundaries import reduced_token
from test_catalog_http import legacy_token
from test_upload_admission import pdf
from test_workspace_ui import screenshot

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Real MetadataFiles requires POSIX')


def key(tag):
    return {'Idempotency-Key': f'review-{tag}-{uuid4().hex[:12]}'}


def seed_job(ui):
    data = pdf(2)
    receipt = ui.api.post('/api/v1/upload-sessions', json={'filename': 'synthetic-review.pdf', 'size_bytes': len(data)})
    assert receipt.status_code == 201
    upload = receipt.json()
    assert ui.api.put(upload['content_url'], content=data, headers={'Content-Type': 'application/pdf'}).status_code == 200
    admitted = ui.api.post('/api/v1/jobs', headers=key('job'), json={
        'upload_id': upload['upload_id'], 'options': {'processing_mode': 'extract_text'}})
    assert admitted.status_code == 202
    job_id = admitted.json()['job_id']
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        status = ui.api.get(f'/api/v1/jobs/{job_id}').json()['status']
        assert status != 'failed'
        if status == 'completed':
            return job_id
        time.sleep(0.2)
    raise AssertionError('job did not complete within 30 seconds')


def seed_type(ui):
    context = ui.api.post('/api/v1/contexts', headers=key('context'), json={'name': 'Contexto de revisão', 'common_fields': []})
    assert context.status_code == 201
    typ = ui.api.post(f"/api/v1/contexts/{context.json()['context_id']}/types", headers=key('type'), json={
        'name': 'Tipo de revisão', 'specific_fields': [{'name': 'titulo', 'type': 'string', 'description': 'Título sintético.'}]})
    assert typ.status_code == 201
    type_id = typ.json()['type_id']
    published = ui.api.post(f'/api/v1/document-types/{type_id}/versions', headers=key('publish'),
                            json={'expected_revision': 1, 'expected_context_revision': 1})
    assert published.status_code == 201
    return type_id, published.json()['version']


def evidence(ui, job_id, page):
    chunk = ui.api.get(f'/api/v1/jobs/{job_id}/canonical/pages', params={'page': page, 'offset': 0, 'max_chars': 64}).json()
    quote = chunk['text'].strip()[:20].strip()
    start = chunk['offset'] + chunk['text'].index(quote)
    return {'document_id': job_id, 'artifact_version': 1, 'text_version': 1, 'page': page,
            'text_sha256': chunk['text_sha256'], 'start': start, 'end': start + len(quote), 'quote': quote}


def segment(label, first, last, type_id, version, proof):
    return {'client_key': label, 'page_start': first, 'page_end': last, 'type_id': type_id, 'type_version': version,
            'fields': [{'field_name': 'titulo', 'raw': proof['quote'], 'origin': 'manual', 'evidence': [proof]}]}


def save(ui, job_id, segments, previous=None):
    response = ui.api.put(f'/api/v1/jobs/{job_id}/metadata', headers=key('save'), json={
        'expected_head': previous and previous['revision_id'], 'expected_sha256': previous and previous['sha256'], 'segments': segments})
    assert response.status_code == 201
    return response.json()


def seeded(ui):
    job_id, (type_id, version) = seed_job(ui), seed_type(ui)
    one, two = evidence(ui, job_id, 1), evidence(ui, job_id, 2)
    first = save(ui, job_id, [segment('a', 1, 1, type_id, version, one), segment('b', 2, 2, type_id, version, two)])
    second = save(ui, job_id, [segment('main', 1, 2, type_id, version, one)], first)
    assert first['revision_id'] != second['revision_id']
    return SimpleNamespace(job_id=job_id, first=first, second=second, quotes=(one['quote'], two['quote']))


def loaded(page, text='Documento carregado'):
    expect(page.locator('#rv-status')).to_contain_text(text, timeout=15000)


def open_review(ui, job_id):
    page = ui.context.new_page()
    page.goto(f'{ui.runtime.origin}/documents/{job_id}/review')
    return page


class Reads:
    """Records document GETs (job canonical/metadata only).

    Listeners never make protocol calls (no header_value): they only keep the request object and read its cached
    method/url. 'requestfinished' may be delivered before 'request', so activity is started minus ended. The Bearer
    check runs at assertion time and yields only a boolean; no credential is printed or stored.
    """

    def __init__(self, page, job_id):
        self.marker = f'/api/v1/jobs/{job_id}/'
        self.requests, self.started, self.ended, self.peak = [], set(), set(), 0
        page.on('request', self.begin)
        page.on('requestfinished', self.end)
        page.on('requestfailed', self.end)

    @property
    def urls(self):
        return [request.url for request in self.requests]

    def counts(self, request):
        return self.marker in request.url and ('/canonical/' in request.url or '/metadata' in request.url)

    def begin(self, request):
        if not self.counts(request):
            return
        self.requests.append(request)
        self.started.add(request)
        self.peak = max(self.peak, len(self.started - self.ended))

    def end(self, request):
        if self.counts(request):
            self.ended.add(request)

    def reset(self):
        """Start counting afresh (after the initial serial check, before unrelated user actions)."""
        self.requests.clear()
        self.peak = len(self.started - self.ended)

    def assert_serial(self):
        assert self.requests
        assert all(request.method == 'GET' for request in self.requests)
        assert all((request.header_value('authorization') or '').startswith('Bearer ') for request in self.requests)
        assert self.peak == 1, f'document reads overlapped (peak {self.peak})'


def fits(page):
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth') is True


def test_workspace_link_reads_canonical_current_history_and_pinned_revision(ui):
    data = seeded(ui)
    page = ui.context.new_page()
    calls = []
    page.on('request', calls.append)  # request objects only; headers are inspected after collection
    reads = Reads(page, data.job_id)
    page.goto(ui.runtime.origin + '/')
    card = page.locator(f'[data-card-id="{data.job_id}"]')
    expect(card).to_have_class(re.compile('status-completed'), timeout=20000)
    card.locator('[data-action="detail"]').click()
    link = page.get_by_role('link', name='Revisar metadados')
    expect(link).to_have_attribute('href', f'/documents/{data.job_id}/review')
    link.click()
    loaded(page)
    expect(page).to_have_url(f'{ui.runtime.origin}/documents/{data.job_id}/review')
    expect(page.locator('#rv-job')).to_have_text(data.job_id)
    expect(page.locator('#rv-manifest')).to_contain_text('2 páginas')
    expect(page.locator('#rv-text')).to_contain_text('Synthetic upload page 0')
    expect(page.locator('#rv-next')).to_have_text('Próxima página')
    reads.assert_serial()  # initialization: chunk, then HEAD, then revisions, one at a time
    assert [u.split('?')[0].rsplit(data.job_id, 1)[1] for u in reads.urls] == [
        '/canonical/manifest', '/canonical/pages', '/metadata', '/metadata/revisions']
    reads.reset()
    page.locator('#rv-next').click()
    expect(page.locator('#rv-chunk-meta')).to_contain_text('Página 2 de 2')
    expect(page.locator('#rv-text')).to_contain_text('Synthetic upload page 1')
    expect(page.locator('#rv-page')).to_have_value('2')
    expect(page.locator('#rv-next')).to_be_disabled()
    page.locator('#rv-page').select_option('1')
    expect(page.locator('#rv-text')).to_contain_text('Synthetic upload page 0')
    # HEAD is the second revision: one segment over both pages.
    expect(page.locator('#rv-meta')).to_contain_text(data.second['revision_id'])
    expect(page.locator('#rv-meta')).to_contain_text(data.second['sha256'])
    expect(page.locator('#rv-meta-note')).to_contain_text('Versão atual (HEAD)')
    expect(page.locator('#rv-segments li')).to_have_count(1)
    expect(page.locator('#rv-segments')).to_contain_text('Páginas 1–2')
    expect(page.locator('#rv-revs li')).to_have_count(2)
    page.locator(f'#rv-revs button[data-revision="{data.first["revision_id"]}"]').click()
    expect(page.locator('#rv-meta')).to_contain_text(data.first['revision_id'])
    expect(page.locator('#rv-meta-note')).to_contain_text('Revisão histórica')
    expect(page.locator('#rv-segments li')).to_have_count(2)
    expect(page.locator('#rv-segments')).to_contain_text('Páginas 2–2')
    page.locator('#rv-head').click()
    expect(page.locator('#rv-meta')).to_contain_text(data.second['revision_id'])
    expect(page.locator('#rv-meta-note')).to_contain_text('Versão atual (HEAD)')
    page.reload()
    loaded(page)
    expect(page.locator('#rv-meta')).to_contain_text(data.second['sha256'])
    expect(page.locator('#rv-text')).to_contain_text('Synthetic upload page 0')
    pinned = f'/metadata/revisions/{data.first["revision_id"]}?sha256={data.first["sha256"]}'
    assert any(request.url.endswith(pinned) for request in calls)
    api = [request for request in calls if '/api/' in request.url]
    assert api and all(request.method == 'GET' for request in api)
    assert all((request.header_value('authorization') or '').startswith('Bearer ') for request in api)
    reads.assert_serial()  # user actions after the initial load were serial too
    assert not any(ui.accounts.owner in request.url or 'access_token' in request.url for request in calls)
    assert 'PDF/A' not in page.locator('#rv-root').inner_text()
    fits(page)
    screenshot(page, 'document-review-desktop.png')
    page.set_viewport_size({'width': 390, 'height': 844})
    fits(page)
    screenshot(page, 'document-review-mobile.png')
    private(page)


def test_job_without_metadata_stays_honest_and_other_owner_sees_nothing(ui):
    job_id = seed_job(ui)
    page = ui.context.new_page()
    reads = Reads(page, job_id)
    page.goto(f'{ui.runtime.origin}/documents/{job_id}/review')
    loaded(page)
    reads.assert_serial()
    expect(page.locator('#rv-text')).to_contain_text('Synthetic upload page 0')
    expect(page.locator('#rv-meta-note')).to_contain_text('Nenhum metadado salvo')
    expect(page.locator('#rv-revs li')).to_have_count(0)
    page.locator('#rv-head').click()
    expect(page.locator('#rv-meta-note')).to_contain_text('Nenhum metadado salvo')
    data = seeded(ui)
    ui.context.add_cookies([{'name': 'access_token', 'value': ui.accounts.other, 'url': ui.runtime.origin,
                            'httpOnly': True, 'sameSite': 'Lax'}])
    stranger = open_review(ui, data.job_id)
    expect(stranger.locator('#rv-status')).to_contain_text('não encontrado', timeout=15000)
    expect(stranger.locator('#rv-text')).to_be_empty()
    expect(stranger.locator('#rv-next')).to_be_disabled()
    visible = stranger.locator('body').inner_text()
    for leaked in (*data.quotes, 'Synthetic upload page', data.second['revision_id'], data.second['sha256']):
        assert leaked not in visible
    fits(stranger)
    private(stranger)


@pytest.mark.parametrize('mode', ['reduced', 'legacy'])
def test_token_without_metadata_scope_reads_text_but_never_calls_metadata(ui, mode):
    data = seeded(ui)
    secret = ui.runtime.env['JWT_SECRET']
    token = (reduced_token(ui.accounts.owner, ['jobs:read', 'artifacts:read'], secret) if mode == 'reduced'
             else legacy_token(ui.accounts.owner_id, secret))
    ui.context.add_cookies([{'name': 'access_token', 'value': token, 'url': ui.runtime.origin,
                            'httpOnly': True, 'sameSite': 'Lax'}])
    page = ui.context.new_page()
    paths = []
    page.on('request', lambda request: paths.append(request.url))
    reads = Reads(page, data.job_id)
    page.goto(f'{ui.runtime.origin}/documents/{data.job_id}/review')
    loaded(page)
    reads.assert_serial()
    expect(page.locator('#rv-text')).to_contain_text('Synthetic upload page 0')
    expect(page.locator('#rv-meta-note')).to_contain_text('metadata:read')
    expect(page.locator('#rv-revs li')).to_have_count(0)
    expect(page.locator('#rv-head')).to_be_disabled()
    assert not any('/metadata' in url for url in paths)
    assert data.second['sha256'] not in page.locator('#rv-root').inner_text()
    private(page)


def test_controls_stay_disabled_and_actions_cannot_overlap_while_a_real_read_is_held(ui):
    data = seeded(ui)
    page = ui.context.new_page()
    reads = Reads(page, data.job_id)
    page.goto(f'{ui.runtime.origin}/documents/{data.job_id}/review')
    loaded(page)
    reads.assert_serial()
    reads.reset()
    held = []

    def hold(route):
        # Hold only the next chunk request; the real authenticated response is fetched at release time.
        if held:
            route.continue_()
            return
        held.append(route)
        page.evaluate("document.body.dataset.held = '1'")

    page.route('**/canonical/pages*', hold)
    page.locator('#rv-next').click()
    expect(page.locator('body')).to_have_attribute('data-held', '1')
    expect(page.locator('#rv-chunk-meta')).to_have_text('Carregando texto…')
    for control in ('#rv-page', '#rv-next', '#rv-head', '#rv-retry'):
        expect(page.locator(control)).to_be_disabled()
    expect(page.locator('#rv-revs button').first).to_be_disabled()
    # Rapid/programmatic actions invoke the handlers directly (a disabled control would not dispatch them).
    page.evaluate("""() => {
        const $ = (id) => document.getElementById(id);
        $('rv-page').value = '2'; $('rv-page').onchange();
        $('rv-next').onclick(); $('rv-head').onclick(); $('rv-more').onclick(); $('rv-retry').onclick();
        for (const button of $('rv-revs').querySelectorAll('button')) button.click();
    }""")
    assert len(reads.requests) == 1 and '/canonical/pages' in reads.requests[0].url
    assert reads.peak == 1
    held[0].fulfill(response=held[0].fetch())
    expect(page.locator('#rv-chunk-meta')).to_contain_text('Página 2 de 2')
    expect(page.locator('#rv-page')).to_be_enabled()
    expect(page.locator('#rv-head')).to_be_enabled()
    expect(page.locator('#rv-next')).to_be_disabled()  # last page
    assert len(reads.requests) == 1
    reads.assert_serial()
    page.unroute('**/canonical/pages*')
    private(page)


@pytest.mark.parametrize('lie', ['canonical_sha256', 'page_count', 'segment_bounds'])
def test_metadata_view_that_disagrees_with_the_manifest_is_rejected(ui, lie):
    data = seeded(ui)
    page = ui.context.new_page()

    def fault_injection(route):
        # FAULT INJECTION (negative only): the real response is mutated so the client must reject it.
        response = route.fetch()
        body = response.json()
        if lie == 'canonical_sha256':
            body['canonical_sha256'] = '0' * 64
        elif lie == 'page_count':
            body['page_count'] += 1
        else:
            body['segments'][0]['page_end'] = body['page_count'] + 1
        headers = {k: v for k, v in response.headers.items() if k.lower() not in ('content-length', 'content-encoding')}
        route.fulfill(response=response, headers=headers, json=body)

    page.route(re.compile(r'/api/v1/jobs/[0-9a-f]{32}/metadata$'), fault_injection)
    page.goto(f'{ui.runtime.origin}/documents/{data.job_id}/review')
    expect(page.locator('#rv-status')).to_contain_text('não corresponde', timeout=15000)
    expect(page.locator('#rv-meta')).to_be_empty()
    expect(page.locator('#rv-text')).to_be_empty()
    expect(page.locator('#rv-next')).to_be_disabled()
    visible = page.locator('body').inner_text()
    for leaked in (data.second['revision_id'], data.second['sha256']):
        assert leaked not in visible
    private(page)


def test_lost_access_after_load_clears_everything_on_the_next_explicit_request(ui):
    data = seeded(ui)
    page = open_review(ui, data.job_id)
    loaded(page)
    expect(page.locator('#rv-text')).to_contain_text('Synthetic upload page 0')
    assert ui.api.delete(f'/api/v1/jobs/{data.job_id}/artifacts').status_code == 200
    page.locator('#rv-next').click()
    expect(page.locator('#rv-status')).to_contain_text('expiraram ou foram excluídos', timeout=15000)
    expect(page.locator('#rv-text')).to_be_empty()
    expect(page.locator('#rv-meta')).to_be_empty()
    expect(page.locator('#rv-revs li')).to_have_count(0)
    expect(page.locator('#rv-next')).to_be_disabled()
    expect(page.locator('#rv-page')).to_be_disabled()
    visible = page.locator('body').inner_text()
    for leaked in (*data.quotes, 'Synthetic upload page', data.second['revision_id'], data.second['sha256']):
        assert leaked not in visible
    private(page)
