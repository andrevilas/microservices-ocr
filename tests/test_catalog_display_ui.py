"""Rendered display previews, permanent locking, and editable-form compatibility."""
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from test_catalog_fields_ui import editor_page, field, load as load_fields, value
from test_catalog_ui import (
    browser, runtime, catalog_accounts, ui, private, record, seed, select,
)
from test_catalog_types_read_ui import seed_type, type_record, loaded_types, open_type
from test_catalog_versions_read_ui import page_for, publish, listed, open_version
from test_catalog_templates_ui import (
    catalog, load as load_templates, preview, readonly_fields,
)
from test_catalog_types_write_ui import choose
from test_workspace_ui import screenshot


def keyboard_open(summary):
    summary.focus()
    summary.press('Enter')
    expect(summary.locator('..')).to_have_attribute('open', '')
    assert summary.evaluate('node => document.activeElement === node')
    assert summary.evaluate('''node => {
        const style = getComputedStyle(node);
        return (style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0)
            || style.boxShadow !== 'none';
    }''')


def compact(page, container, fields):
    root = page.locator(container + ' .catalog-fields--display')
    expect(root).to_have_count(1)
    readonly_fields(page, container, fields)
    assert root.locator('button:visible').count() == 0
    cards = root.locator('.catalog-fields__card')
    for card, spec in zip(cards.all(), fields):
        expect(card.locator('.catalog-fields__display-label')).to_have_text(
            spec['name'] if spec['label'] is None else spec['label'])
        expect(card.locator('.catalog-fields__display-key')).to_have_text('Chave: ' + spec['name'])
        assert card.locator('.catalog-fields__display-description').text_content() == spec['description']
        expect(card.locator('.catalog-fields__display-type')).to_be_visible()
    return root


def readable(textarea):
    assert textarea.evaluate(r'''node => {
        const style = getComputedStyle(node);
        const rgb = value => value.match(/[\d.]+/g).slice(0, 3).map(Number);
        const luminance = value => rgb(value).map(c => {
            c /= 255;
            return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
        }).reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0);
        const foreground = luminance(style.color), background = luminance(style.backgroundColor);
        const contrast = (Math.max(foreground, background) + 0.05)
            / (Math.min(foreground, background) + 0.05);
        return contrast >= 4.5 && Number(style.opacity) === 1
            && style.webkitTextFillColor === style.color;
    }''')


def geometry(page):
    assert page.evaluate('''() => {
        const panels = [document.documentElement,
            ...document.querySelectorAll('.catalog-page .panel, .template-fields-grid')];
        return panels.every(node => node.scrollWidth <= node.clientWidth + 1);
    }''')
    targets = page.locator('.catalog-page main').locator(
        'button:visible:enabled, a:visible, summary:visible, '
        '.catalog-fields__check > label:visible, '
        '.catalog-fields__field:has(> input[type=checkbox]) > label:visible')
    assert targets.count() > 0
    for target in targets.all():
        assert target.bounding_box()['height'] >= 44


def test_display_fidelity_and_permanent_readonly(editor_page):
    page = editor_page
    hostile = '<img src=x onerror="window.injected=true"> Ω'
    fields = [
        field('opcao', type='enum', label=hostile, description=hostile + '\nDescrição 😀',
              enum=['001', 'linha\nΩ'], examples=['', 'Exemplo\n漢字'], required=True,
              critical=True, evidence_required=True, sensitivity='restricted', indexable=True,
              destination='xmp', max_items=7, required_when={'field': 'ativo', 'equals': False}),
        field('ativo', type='boolean', required_when={'field': 'numero', 'equals': 0}),
        field('numero', type='integer', cardinality='many', max_items=9,
              sensitivity='public', required_when={'field': 'opcao', 'equals': '001'}),
    ]
    page.evaluate('''() => {
        editor.destroy();
        window.changes = [];
        window.editor = OcrCatalogFields.create(document.querySelector('#fields'), {
            displayOnly: true, readonly: false, onChange: value => changes.push(value)
        });
    }''')
    load_fields(page, fields)
    root = compact(page, '#fields', fields)
    cards = root.locator('.catalog-fields__card')
    expect(root.locator('details[open]')).to_have_count(0)
    for card, spec, human_type in zip(cards.all(), fields, ['Lista de opções', 'Sim ou não', 'Inteiro']):
        expect(card.locator('.catalog-fields__display-type')).to_have_text(human_type)
        expect(card.locator('.catalog-fields__display-label')).to_be_visible()
        expect(card.locator('.catalog-fields__display-key')).to_be_visible()
        expect(card.locator('.catalog-fields__display-description')).to_be_visible()
        expect(card.locator('details')).to_have_count(1)
        keyboard_open(card.locator('summary'))
        expect(card.get_by_label('Chave do campo', exact=True)).to_have_value(spec['name'])
        expect(card.get_by_label('Rótulo', exact=True)).to_have_value(spec['label'] or '')
        expect(card.get_by_label('Campo de referência sugerido', exact=True)).to_be_visible()
        assert card.locator('pre').evaluate('node => JSON.parse(node.textContent)') == spec['required_when']
        expect(card.get_by_text('Estas opções registram intenção.', exact=False)).to_be_visible()
        for prop, label in [('type', 'Tipo'), ('sensitivity', 'Sensibilidade'),
                            ('cardinality', 'Quantidade de valores'),
                            ('normalization', 'Normalização prevista'),
                            ('destination', 'Destino previsto dos metadados')]:
            expect(card.get_by_label(label, exact=True)).to_be_visible()
            expect(card.get_by_label(label, exact=True)).to_have_value(spec[prop])
        for index, example in enumerate(spec['examples'], 1):
            expect(card.get_by_label(f'Exemplo descritivo {index}', exact=True)).to_have_value(example)
        for index, option in enumerate(spec['enum'] or [], 1):
            expect(card.get_by_label(f'Opção {index}', exact=True)).to_have_value(option)
    maximum = cards.first.get_by_label('Limite para listas — max_items (mesmo com um valor)', exact=True)
    expect(maximum).to_be_visible()
    expect(maximum).to_have_value('7')
    summary = cards.first.locator('summary')
    summary.focus()
    summary.press('Space')
    expect(cards.first.locator('details')).not_to_have_attribute('open', '')
    keyboard_open(summary)
    page.evaluate('''() => {
        window.originalCard = document.querySelector('.catalog-fields__card');
        editor.setInherited([{name: 'herdado', type: 'string'}]);
        editor.setReadonly(false);
        document.querySelectorAll('#fields input, #fields textarea, #fields select').forEach(node => {
            node.dispatchEvent(new Event('input', {bubbles: true}));
            node.dispatchEvent(new Event('change', {bubbles: true}));
        });
    }''')
    assert page.evaluate("originalCard === document.querySelector('.catalog-fields__card')")
    expect(cards.first.locator('details')).to_have_attribute('open', '')
    readonly_fields(page, '#fields', fields)
    assert value(page) == fields
    assert page.evaluate('changes') == []
    assert page.evaluate('window.injected === undefined')
    expect(root.locator('img, script')).to_have_count(0)
    expect(cards.nth(1).get_by_text('Opções da lista: null (sem lista)', exact=True)).to_be_visible()
    readable(cards.first.get_by_label('Descrição', exact=True))


def test_default_form_retains_temporary_readonly_layout(editor_page):
    page = editor_page
    load_fields(page, [field()])
    root = page.locator('#fields .catalog-fields')
    assert root.evaluate("node => !node.classList.contains('catalog-fields--display')")
    expect(root.get_by_label('Chave do campo', exact=True)).to_be_visible()
    expect(root.get_by_label('Descrição', exact=True)).to_be_editable()
    for name in ['Adicionar campo', 'Mover campo 1 para cima', 'Mover campo 1 para baixo', 'Remover campo 1']:
        expect(root.get_by_role('button', name=name, exact=True)).to_be_visible()
    keyboard_open(root.locator('summary'))
    expect(root.get_by_role('button', name='Adicionar exemplo', exact=True)).to_be_visible()
    page.evaluate('editor.setReadonly(true)')
    for control in root.locator('input, textarea, select, button').all():
        expect(control).to_be_disabled()
    expect(root.get_by_label('Descrição', exact=True)).to_be_visible()
    expect(root.get_by_role('button', name='Adicionar campo', exact=True)).to_be_visible()
    page.evaluate('editor.setReadonly(false)')
    expect(root.get_by_label('Descrição', exact=True)).to_be_editable()
    expect(root.get_by_role('button', name='Adicionar campo', exact=True)).to_be_enabled()
    expect(root.get_by_role('button', name='Adicionar exemplo', exact=True)).to_be_enabled()
    root.get_by_label('Descrição', exact=True).fill('Descrição alterada')
    assert value(page)[0]['description'] == 'Descrição alterada'


@pytest.mark.parametrize('width', [375, 1440])
def test_application_display_previews(ui, width):
    common = [field('origem', description='Origem\nDocumento Ω', examples=['Exemplo'])]
    specific = [field('numero', type='integer', description='Número sintético')]
    parent = seed(ui, name=f'Contexto visual {width}', fields=common)
    item = seed_type(ui, parent, f'Tipo visual {width}', specific)
    historical = publish(ui, parent, item)
    _, templates = catalog(ui)
    page = page_for(ui)
    page.set_viewport_size({'width': width, 'height': 900})
    events, writes = [], []
    # Observe metadata only: never retain headers, credentials, or response bodies.
    page.on('response', lambda response: events.append({
        'path': urlsplit(response.url).path,
        'method': response.request.method, 'status': response.status}))
    page.on('request', lambda request: writes.append((request.method, urlsplit(request.url).path))
            if request.method not in ('GET', 'HEAD', 'OPTIONS') else None)
    select(page, parent['body']['name'])
    loaded_types(page, 1)
    open_type(page, item)
    listed(page, 1)
    expect(page.locator('#context-fields .catalog-fields--display')).to_have_count(0)
    inherited = compact(page, '#type-common-fields', common)
    keyboard_open(inherited.locator('summary').first)
    assert inherited.locator('summary').first.bounding_box()['height'] >= 44
    readable(inherited.get_by_label('Descrição', exact=True).first)
    page.locator('#context-description').fill('Rascunho local preservado Ω')
    load_templates(page, events)
    for template in templates:
        preview(page, template)
        expect(page.locator('#context-description')).to_have_value('Rascunho local preservado Ω')
        common_preview = compact(page, '#template-common', template['context']['common_fields'])
        compact(page, '#template-specific', template['types'][0]['body']['specific_fields'])
        selected = page.locator('#template-list button[aria-current="true"]')
        expect(selected).to_have_count(1)
        expect(selected).to_have_attribute('data-template-id', template['template_id'])
        assert selected.locator('..').evaluate(
            "node => getComputedStyle(node, '::after').content") == '"Selecionado"'
        reference = page.locator('#template-preview > details')
        expect(reference).not_to_have_attribute('open', '')
        expect(reference.locator('summary')).to_be_visible()
        keyboard_open(reference.locator('summary'))
        expect(page.locator('#template-edition')).to_be_visible()
        expect(page.locator('#template-hash')).to_have_text(template['template_sha256'])
        reference.locator('summary').press('Space')
    keyboard_open(common_preview.locator('summary').first)
    readable(common_preview.get_by_label('Descrição', exact=True).first)
    groups = page.locator('.template-fields-grid > section')
    boxes = [group.bounding_box() for group in groups.all()]
    assert len(boxes) == 2
    assert (abs(boxes[0]['y'] - boxes[1]['y']) <= 1) == (width >= 720)
    geometry(page)
    private(page)
    screenshot(page, f'catalog-display-templates-{width}.png')
    page.locator('#type-edit-context').click()
    choose(page)
    expect(page.locator('#context-name')).to_have_value(parent['body']['name'])
    expect(page.locator('#context-description')).to_have_value(parent['body']['description'])
    open_type(page, item)
    listed(page, 1)
    open_version(page, historical)
    snapshot = compact(page, '#version-fields', historical['schema']['fields'])
    keyboard_open(snapshot.locator('summary').first)
    readable(snapshot.get_by_label('Descrição', exact=True).first)
    for control in ['version-publish', 'version-deprecate', 'version-refresh']:
        expect(page.locator('#' + control)).to_be_visible()
        expect(page.locator('#' + control)).to_be_enabled()
    expect(page.locator('#context-description')).to_be_editable()
    geometry(page)
    private(page)
    screenshot(page, f'catalog-display-version-{width}.png')
    assert record(ui, parent['context_id']) == parent
    assert type_record(ui, item['type_id']) == item
    assert writes == []
    assert ui.dialogs == []
    page.close()
