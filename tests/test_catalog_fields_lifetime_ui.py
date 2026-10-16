import re
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Error, expect

from test_workspace_ui import browser
from test_catalog_fields_ui import editor_page, field, load, value, invalid, open_details
from test_catalog_ui import BASE, ui, runtime, catalog_accounts, seed, record, select, private
from test_catalog_types_read_ui import seed_type, type_record, open_type, loaded_types
from test_catalog_versions_read_ui import page_for
from test_catalog_types_write_ui import choose


def control(scope, text):
    for node in scope.locator('input, textarea').all():
        if node.input_value() == text:
            return node
    raise AssertionError(f'Controle sintético não encontrado: {text!r}')


def card(node):
    return node.locator('xpath=ancestor::article[contains(@class, "catalog-fields__card")][1]')


def same(node, saved):
    assert node.evaluate('(node, saved) => node === saved && node.isConnected', saved)


def listeners(page, node):
    return page.evaluate_handle('''node => registrations.filter(item =>
        item.node === node || node.contains(item.node))''', node.element_handle())


def signals(items, aborted, required=True):
    if required:
        assert items.evaluate('items => items.length') > 0
    assert items.evaluate('''(items, aborted) => items.every(item =>
        item.signal != null && item.signal.aborted === aborted)''', aborted)


def changes(page):
    return page.evaluate('changes.length')


def suggestion(page, name):
    return page.get_by_label('Campo de referência sugerido', exact=True).locator(
        f'option[value="{name}"]').first


def test_inherited_preserves_local_dom_validation_and_copy(editor_page):
    page = editor_page
    local = [field('local', required_when={'field': 'origem', 'equals': 'sim'}),
             field('comum')]
    inherited = [field('origem', type='enum', enum=['sim', 'nao']), field('comum')]
    load(page, local, inherited)
    open_details(page)
    name = control(page, 'local')
    cards = [(card(control(page, item['name'])),
              card(control(page, item['name'])).element_handle()) for item in local]
    saved_name = name.element_handle()
    invalid(page)
    name.focus()
    page.evaluate('''() => {
        window.incoming = [{name: 'origem', type: 'enum', enum: ['sim', 'nao']}];
        editor.setInherited(incoming);
    }''')
    # Inspect attempted validation before getValue can refresh the alerts itself.
    expect(page.locator('.catalog-fields [role="alert"]:visible')).to_have_count(0)
    for node, saved in cards:
        same(node, saved)
    same(name, saved_name)
    expect(name).to_be_focused()
    assert changes(page) == 0
    assert value(page) == local
    assert all(len(item) == 16 for item in local)
    page.evaluate('''() => {
        incoming[0].name = 'contaminado';
        incoming[0].enum[0] = 'contaminado';
        editor.setReadonly(true);
    }''')
    expect(suggestion(page, 'origem')).to_have_attribute('value', 'origem')
    expect(page.get_by_label('Campo de referência sugerido', exact=True).locator('option[value="contaminado"]')).to_have_count(0)
    enum_options = card(name).get_by_label('Opção da condição', exact=True)
    expect(enum_options.locator('option[value="sim"]')).to_have_count(1)
    expect(enum_options.locator('option[value="nao"]')).to_have_count(1)
    expect(enum_options.locator('option[value="contaminado"]')).to_have_count(0)
    preview = card(name).locator('pre[aria-label="Condição atual, somente leitura"]')
    expect(preview).to_contain_text('sim')
    expect(page.locator('.catalog-fields [role="alert"]:visible')).to_have_count(0)
    assert value(page) == local
    page.evaluate('editor.setInherited([{name: "origem", type: "enum", enum: ["sim", "nao"]}, {name: "extra", type: "string"}])')
    for node in page.locator('.catalog-fields input, .catalog-fields select, .catalog-fields textarea, .catalog-fields button').all():
        expect(node).to_be_disabled()
    details = card(name).locator('details.catalog-fields__details')
    details.locator('summary').click()
    expect(details).to_have_js_property('open', False)
    details.locator('summary').click()
    expect(details).to_have_js_property('open', True)
    for bad, message in [(None, 'devem ser listas'), ({}, 'devem ser listas'),
                         ([None], 'deve ser um objeto'), ([[]], 'deve ser um objeto')]:
        with pytest.raises(Error, match=message):
            page.evaluate('fields => editor.setInherited(fields)', bad)
        expect(suggestion(page, 'extra')).to_have_attribute('value', 'extra')
        assert value(page) == local
    for node, saved in cards:
        same(node, saved)
    assert changes(page) == 0


def test_listener_disposal_and_failed_replacement_are_atomic(editor_page):
    page = editor_page
    page.evaluate('editor.destroy()')
    page.evaluate('''() => {
        window.registrations = [];
        window.originalAdd = EventTarget.prototype.addEventListener;
        EventTarget.prototype.addEventListener = function(type, callback, options) {
            if (this instanceof Element)
                registrations.push({node: this, signal: options && options.signal});
            return originalAdd.call(this, type, callback, options);
        };
    }''')
    try:
        page.evaluate('''() => {
            editor = OcrCatalogFields.create(document.querySelector('#fields'), {
                idPrefix: 'lifetime', onChange: value => changes.push(value)
            });
        }''')
        add = page.get_by_role('button', name=re.compile(r'^Adicionar campo$', re.I))
        root_signals = listeners(page, add)
        load(page, [field('antigo')])
        old_signals = listeners(page, card(control(page, 'antigo')))
        load(page, [field('opcoes', type='enum', enum=['sim', 'nao'],
                          examples=['exemplo-um', 'exemplo-dois']), field('removivel')])
        signals(old_signals, True)
        signals(root_signals, False)
        current = card(control(page, 'opcoes'))
        live = listeners(page, current)
        signals(live, False)
        description = control(current, 'Descrição sintética')
        description.fill('Descrição editada')
        assert changes(page) > 0
        before, count = value(page), changes(page)
        saved_card = current.element_handle()
        page.evaluate('window.failureStart = registrations.length')
        with pytest.raises(Error):
            page.evaluate('''fields => {
                fields[1].examples = [() => {}];
                editor.setValue(fields);
            }''', [field('parcial'), field('nao-clonavel')])
        partial = page.evaluate_handle('registrations.slice(failureStart)')
        signals(partial, True, required=False)
        same(current, saved_card)
        signals(live, False)
        assert value(page) == before
        assert changes(page) == count
        description.fill('Continua operacional')
        assert value(page)[0]['description'] == 'Continua operacional'
        open_details(page)
        for addition, first, second in [('Adicionar exemplo', 'exemplo-um', 'exemplo-dois'),
                                         ('Adicionar (?:opção|valor)', 'sim', 'nao')]:
            row = control(current, first).locator('xpath=ancestor::*[button][1]')
            old_row = listeners(page, row)
            current.get_by_role('button', name=re.compile(addition, re.I)).click()
            signals(old_row, True)
            row = control(current, first).locator('xpath=ancestor::*[button][1]')
            replaced_row = listeners(page, row)
            signals(replaced_row, False)
            row.locator('button').click()
            signals(replaced_row, True)
            signals(listeners(page, control(current, second).locator(
                'xpath=ancestor::*[button][1]')), False)
            signals(root_signals, False)
        removed = card(control(page, 'removivel'))
        removed_signals = listeners(page, removed)
        detached = control(removed, 'Descrição sintética').element_handle()
        removed.get_by_role('button', name='Remover campo 2', exact=True).click()
        signals(removed_signals, True)
        count = changes(page)
        detached.evaluate('node => { node.value = "fantasma"; node.dispatchEvent(new Event("input", {bubbles: true})); }')
        assert changes(page) == count
        signals(listeners(page, current), False)
        last = control(page, 'opcoes').element_handle()
        page.evaluate('() => { editor.destroy(); editor.destroy(); }')
        expect(page.locator('#fields > *')).to_have_count(0)
        signals(root_signals, True)
        signals(page.evaluate_handle('registrations'), True)
        assert last.evaluate('node => !node.isConnected')
        last.evaluate('node => { node.value = "tardio"; node.dispatchEvent(new Event("input", {bubbles: true})); }')
        assert changes(page) == count
    finally:
        page.evaluate('() => { EventTarget.prototype.addEventListener = originalAdd; }')


def test_inherited_update_preserves_pending_enum_confirmation(editor_page):
    page = editor_page
    load(page, [field('escolha', type='enum', enum=['sim', 'nao'])], [field('base')])
    page.evaluate('''() => {
        window.originalConfirm = OcrUi.confirmAction;
        window.confirmCalls = 0;
        OcrUi.confirmAction = () => {
            confirmCalls++;
            window.confirmation = new Promise(resolve => window.finishConfirm = resolve);
            return confirmation;
        };
    }''')
    try:
        current = card(control(page, 'escolha'))
        saved = current.element_handle()
        kind = current.locator('select:has(option[value="enum"])')
        saved_select = kind.element_handle()
        kind.select_option('string')
        expect(kind).to_be_disabled()
        assert page.evaluate('confirmCalls') == 1
        page.evaluate('editor.setInherited([{name: "base_nova", type: "string"}])')
        same(current, saved)
        same(kind, saved_select)
        expect(kind).to_be_disabled()
        assert changes(page) == 0
        page.evaluate('async () => { finishConfirm(true); await confirmation; }')
        expect(kind).to_have_value('string')
        expect(kind).to_be_enabled()
        same(current, saved)
        same(kind, saved_select)
        expect(suggestion(page, 'base_nova')).to_have_attribute('value', 'base_nova')
        assert value(page) == [field('escolha')]
        assert changes(page) == 1
        load(page, [field('antiga', type='enum', enum=['sim', 'nao'])])
        pending = card(control(page, 'antiga')).locator('select:has(option[value="enum"])')
        pending.select_option('string')
        expect(pending).to_be_disabled()
        assert page.evaluate('confirmCalls') == 2
        replacement = [field('substituta', description='Nova descrição')]
        load(page, replacement)
        new_name = control(page, 'substituta')
        saved_new = new_name.element_handle()
        page.evaluate('async () => { finishConfirm(true); await confirmation; }')
        expect(new_name).to_have_value('substituta')
        expect(card(new_name).locator('select:has(option[value="enum"])')).to_be_enabled()
        same(new_name, saved_new)
        assert value(page) == replacement
        assert changes(page) == 1
    finally:
        page.evaluate('() => { OcrUi.confirmAction = originalConfirm; }')


def test_real_type_keystrokes_and_same_parent_revision_refresh(ui):
    a = seed(ui, 'U3 lifetime A', [field('comum_a')])
    b = seed(ui, 'U3 lifetime B', [field('comum_b')])
    ta = seed_type(ui, a, 'Tipo lifetime A', [field('local_a')])
    tb = seed_type(ui, b, 'Tipo lifetime B', [field('local_b')])
    page = page_for(ui)
    requests = []
    page.on('request', lambda request: requests.append(
        (request.method, urlsplit(request.url).path)))
    select(page, a['body']['name'])
    loaded_types(page, 1)
    open_type(page, ta)
    inherited = page.locator('#type-common-fields')
    inherited_name = control(inherited, 'comum_a')
    inherited_card = card(inherited_name)
    saved_card, saved_input = inherited_card.element_handle(), inherited_name.element_handle()
    details = inherited_card.locator('details.catalog-fields__details')
    details.locator('summary').click()
    expect(details).to_have_js_property('open', True)
    expect(inherited_name).to_be_disabled()
    local_name = control(page, 'local_a')
    description = control(card(local_name), 'Descrição sintética')
    for text in ['Descrição local primeira', 'Descrição local segunda', 'Descrição local terceira']:
        description.fill(text)
        expect(description).to_be_focused()
        same(inherited_card, saved_card)
        same(inherited_name, saved_input)
        expect(details).to_have_js_property('open', True)
    local_name.fill('local_a_novo')
    expect(local_name).to_be_focused()
    same(inherited_card, saved_card)
    same(inherited_name, saved_input)
    expect(details).to_have_js_property('open', True)
    expect(suggestion(page, 'local_a_novo')).to_have_attribute('value', 'local_a_novo')
    expect(page.get_by_label('Campo de referência sugerido', exact=True).locator('option[value="local_a"]')).to_have_count(0)
    expect(page.locator('#context-save')).to_be_enabled()
    assert record(ui, a['context_id'])['body'] == a['body']
    assert record(ui, a['context_id'])['revision'] == 1
    assert type_record(ui, ta['type_id'])['body'] == ta['body']
    assert type_record(ui, ta['type_id'])['revision'] == 1
    page.locator('#context-list').get_by_role('button', name=b['body']['name'], exact=True).click()
    choose(page)
    expect(page.locator('#context-name')).to_have_value(b['body']['name'])
    loaded_types(page, 1)
    open_type(page, tb)
    expect(control(inherited, 'comum_b')).to_be_disabled()
    assert saved_input.evaluate('node => !node.isConnected')
    assert not inherited.locator('input').evaluate_all('nodes => nodes.some(node => node.value === "comum_a")')
    select(page, a['body']['name'])
    loaded_types(page, 1)
    open_type(page, ta)
    expect(control(inherited, 'comum_a')).to_be_disabled()
    expect(control(page, 'local_a')).to_have_value('local_a')
    returned_card = card(control(inherited, 'comum_a')).element_handle()
    returned_input = control(inherited, 'comum_a').element_handle()
    fresh_fields = [field('comum_a_revisao2', description='Descrição comum atualizada')]
    response = ui.api.put(BASE + '/' + a['context_id'], json={
        'body': {**a['body'], 'common_fields': fresh_fields}, 'expected_revision': 1})
    assert response.status_code == 200
    fresh = record(ui, a['context_id'])
    assert fresh['context_id'] == a['context_id'] and fresh['revision'] == 2
    assert fresh['body']['common_fields'] == fresh_fields
    refresh_start = len(requests)
    page.locator('#version-refresh-draft').click()
    expect(page.locator('#context-status')).to_have_text(
        'Rascunho atualizado. Revise as revisões do tipo e do contexto antes de publicar.')
    expect(page.get_by_role('dialog')).to_be_hidden()
    expect(control(inherited, 'comum_a_revisao2')).to_be_disabled()
    expect(control(inherited, 'Descrição comum atualizada')).to_be_disabled()
    expect(page.locator('#type-parent')).to_have_text(a['body']['name'] + ' · Revisão 2')
    expect(page.locator('#context-revision')).to_have_text(
        'Rascunho — tipo de documento · Revisão 1 · Contexto: ' + a['body']['name'] + ' · Revisão 2')
    assert returned_card.evaluate('node => !node.isConnected')
    assert returned_input.evaluate('node => !node.isConnected')
    assert ('GET', BASE + '/' + a['context_id']) in requests[refresh_start:]
    stored_type = type_record(ui, ta['type_id'])
    assert stored_type['revision'] == 1 and stored_type['body'] == ta['body']
    assert not [(method, path) for method, path in requests
                if method in {'POST', 'PUT', 'PATCH', 'DELETE'} and path.startswith('/api/')]
    private(page)
