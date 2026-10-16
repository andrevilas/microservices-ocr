"""Pure catalog component coverage; no application server or service mocks."""
import copy

import pytest
from playwright.sync_api import expect

from test_workspace_ui import browser, screenshot
from test_mcp_vertical import ROOT


@pytest.fixture
def editor_page(browser):
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    page = context.new_page()
    try:
        page.set_content('<main><div id="fields"></div></main>')
        page.add_style_tag(path=str(ROOT / 'app/static/style.css'))
        page.add_script_tag(path=str(ROOT / 'app/static/shell.js'))
        page.add_script_tag(path=str(ROOT / 'app/static/catalog-fields.js'))
        page.evaluate("""() => {
            window.changes = [];
            window.editor = OcrCatalogFields.create(document.querySelector('#fields'), {
                idPrefix: 'test', onChange: value => changes.push(value)
            });
        }""")
        yield page
    finally:
        context.close()


def field(name='documento', **overrides):
    result = dict(
        name=name, type='string', description='Descrição sintética', label=None,
        required=False, critical=False, evidence_required=False,
        sensitivity='internal', indexable=False, cardinality='one', max_items=20,
        normalization='type-default', destination='sidecar', examples=[],
        required_when=None, enum=None,
    )
    result.update(overrides)
    return result


def load(page, fields, inherited=None):
    page.evaluate('(value) => editor.setValue(value.fields, {inherited: value.inherited})',
                  {'fields': fields, 'inherited': inherited or []})


def value(page):
    return page.evaluate('editor.getValue()')


def invalid(page):
    result = page.evaluate("""() => {
        try { editor.getValue(); return ''; }
        catch (error) { return error.message; }
    }""")
    assert 'inválidos' in result
    expect(page.locator('.catalog-fields [role="alert"]:visible').first).to_be_visible()


def open_details(page):
    page.locator('.catalog-fields details').evaluate_all('(nodes) => nodes.forEach(node => node.open = true)')


@pytest.mark.parametrize('literal', [False, 0, '001.2300'])
def test_complete_roundtrip_and_label_only_edit(editor_page, literal):
    page = editor_page
    original = field(
        type='enum', description='Descrição\ncom Unicode 😀', label='Documento',
        required=True, critical=True, evidence_required=True, sensitivity='restricted',
        indexable=True, cardinality='many', max_items=73, destination='xmp',
        examples=['', '001.2300\nsegunda linha', '😀\ttexto'],
        required_when={'field': 'alvo_ainda_nao_resolvido', 'equals': literal},
        enum=['primeira\nsegunda', '<img src=x onerror=alert(1)>'],
    )
    assert len(original) == 16
    load(page, [original])
    assert value(page) == [original]
    assert page.evaluate('changes.length') == 0
    page.get_by_label('Rótulo', exact=True).fill('Novo rótulo')
    edited = copy.deepcopy(original)
    edited['label'] = 'Novo rótulo'
    assert value(page) == [edited]
    assert page.evaluate('changes.length') == 1
    open_details(page)
    expect(page.locator('.catalog-fields__condition pre')).to_contain_text('alvo_ainda_nao_resolvido')
    page.evaluate('editor.setReadonly(true)')
    assert value(page) == [edited]


def test_null_hidden_defaults_and_copy_isolation(editor_page):
    page = editor_page
    original = field(examples=['', 'linha um\nlinha dois'], required_when={'field': 'herdado', 'equals': False})
    inherited = field('herdado', cardinality='many')
    page.evaluate("""({local, inherited}) => {
        window.source = [local]; window.parentFields = [inherited];
        editor.setValue(source, {inherited: parentFields});
        source[0].examples[0] = 'alterado';
        source[0].required_when.equals = true;
        parentFields[0].name = 'alterado';
        const output = editor.getValue();
        output[0].examples.push('externo');
        output[0].required_when.equals = true;
    }""", {'local': original, 'inherited': inherited})
    assert value(page) == [original]
    expect(page.get_by_label('Máximo de itens (1 a 100)', exact=True)).to_be_hidden()
    expect(page.get_by_label('Usar chave como rótulo')).to_be_checked()
    expect(page.get_by_label('Rótulo', exact=True)).to_be_disabled()
    expect(page.locator('.catalog-fields__hint').first).to_contain_text('herdado')
    assert page.evaluate('changes.length') == 0
    page.get_by_label('Usar chave como rótulo').uncheck()
    page.get_by_label('Rótulo', exact=True).fill('Rótulo explícito')
    page.get_by_label('Usar chave como rótulo').check()
    assert value(page)[0]['label'] is None
    page.get_by_label('Usar chave como rótulo').uncheck()
    assert value(page)[0]['label'] == 'Rótulo explícito'


def test_actions_stable_cards_caret_and_readonly(editor_page):
    page = editor_page
    load(page, [field('primeiro'), field('segundo')])
    card = page.locator('.catalog-fields__card').first
    key = card.get_attribute('data-key')
    control = card.get_by_label('Chave do campo', exact=True)
    control.fill('ab')
    control.evaluate('(node) => { window.savedInput = node; node.setSelectionRange(1, 1); }')
    control.press_sequentially('cd')
    assert control.input_value() == 'acdb'
    assert control.evaluate('(node) => node === savedInput && node.selectionStart === 3 && document.activeElement === node')
    page.get_by_role('button', name='Mover campo 1 para baixo', exact=True).click()
    assert [item['name'] for item in value(page)] == ['segundo', 'acdb']
    assert page.locator('.catalog-fields__card').nth(1).get_attribute('data-key') == key
    page.get_by_role('button', name='Mover campo 2 para cima', exact=True).click()
    page.get_by_role('button', name='Adicionar campo', exact=True).click()
    last = page.locator('.catalog-fields__card').last
    last.get_by_label('Chave do campo', exact=True).fill('terceiro')
    last.get_by_label('Descrição', exact=True).fill('Terceiro campo')
    assert len(value(page)) == 3
    page.get_by_role('button', name='Remover campo 2', exact=True).click()
    before = value(page)
    page.evaluate('editor.setReadonly(true)')
    assert value(page) == before
    assert page.locator('.catalog-fields input:enabled, .catalog-fields textarea:enabled, .catalog-fields select:enabled, .catalog-fields button:enabled').count() == 0
    summary = page.locator('.catalog-fields summary').first
    summary.focus()
    summary.press('Enter')
    expect(page.locator('.catalog-fields details').first).to_have_attribute('open', '')
    page.evaluate('editor.setReadonly(false)')
    expect(page.get_by_role('button', name='Adicionar campo', exact=True)).to_be_enabled()


def test_enum_confirmation_accept_and_dismiss(editor_page):
    page = editor_page
    original = field(type='enum', enum=['um\ndois', 'três'])
    load(page, [original])
    page.once('dialog', lambda dialog: dialog.dismiss())
    page.get_by_label('Tipo', exact=True).select_option('string')
    expect(page.get_by_label('Tipo', exact=True)).to_be_enabled()
    expect(page.get_by_label('Tipo', exact=True)).to_have_value('enum')
    assert value(page) == [original]
    assert page.evaluate('changes.length') == 0
    page.once('dialog', lambda dialog: dialog.accept())
    page.get_by_label('Tipo', exact=True).select_option('identifier')
    expect(page.get_by_label('Tipo', exact=True)).to_have_value('identifier')
    assert value(page)[0]['enum'] is None
    page.get_by_label('Tipo', exact=True).select_option('enum')
    open_details(page)
    expect(page.get_by_label('Opção 1', exact=True)).to_have_value('')
    invalid(page)
    page.get_by_label('Opção 1', exact=True).fill('opção\ncom quebra')
    assert value(page)[0]['enum'] == ['opção\ncom quebra']


@pytest.mark.parametrize('interruption', ['destroy', 'setValue', 'readonly'])
def test_pending_confirmation_cannot_mutate_stale_state(editor_page, interruption):
    page = editor_page
    original = field(type='enum', enum=['preservada'])
    load(page, [original])
    page.evaluate("""() => {
        window.OcrUi = {...window.OcrUi, confirmAction: () => new Promise(resolve => {
            window.finishConfirmation = resolve;
        })};
    }""")
    page.get_by_label('Tipo', exact=True).select_option('string')
    expect(page.get_by_label('Tipo', exact=True)).to_be_disabled()
    if interruption == 'destroy':
        page.evaluate('editor.destroy()')
    elif interruption == 'setValue':
        load(page, [field('substituto')])
    else:
        page.evaluate('editor.setReadonly(true); editor.setReadonly(false)')
    page.evaluate('async () => { finishConfirmation(true); await Promise.resolve(); }')
    assert page.evaluate('changes.length') == 0
    if interruption == 'destroy':
        expect(page.locator('#fields')).to_be_empty()
    elif interruption == 'setValue':
        assert value(page) == [field('substituto')]
    else:
        assert value(page) == [original]


def test_duplicates_limits_and_error_focus(editor_page):
    page = editor_page
    load(page, [field('igual'), field('igual')])
    invalid(page)
    page.evaluate('editor.focusFirstError()')
    expect(page.get_by_label('Chave do campo', exact=True).first).to_be_focused()
    expect(page.get_by_label('Chave do campo', exact=True).first).to_have_attribute('aria-invalid', 'true')
    load(page, [field('igual')], [field('igual')])
    invalid(page)
    load(page, [field(description='😀' * 4000, label='😀' * 120)])
    assert len(value(page)[0]['description']) == 4000
    page.get_by_label('Descrição', exact=True).fill('😀' * 4001)
    invalid(page)
    page.evaluate('editor.focusFirstError()')
    expect(page.get_by_label('Descrição', exact=True)).to_be_focused()
    page.get_by_label('Descrição', exact=True).fill('válida')
    assert value(page)[0]['description'] == 'válida'
    load(page, [field(max_items=101)])
    invalid(page)
    page.evaluate('editor.focusFirstError()')
    expect(page.get_by_label('Máximo de itens (1 a 100)', exact=True)).to_be_focused()
    page.get_by_label('Máximo de itens (1 a 100)', exact=True).fill('20')
    assert value(page)[0]['max_items'] == 20
    load(page, [field(f'campo_{i}') for i in range(100)])
    expect(page.get_by_role('button', name='Adicionar campo', exact=True)).to_be_disabled()
    assert len(value(page)) == 100
    load(page, [field(f'campo_{i}') for i in range(101)])
    invalid(page)
    page.evaluate('editor.focusFirstError()')
    expect(page.locator('.catalog-fields > [role="alert"]')).to_be_focused()


def test_dto_validation_and_independent_text_rows(editor_page):
    page = editor_page
    # Representative DTO failures, rather than a combinatorial matrix.
    bad_fields = [
        field(name=' Chave'), field(label=' rótulo'), field(description=''),
        field(description='controle\x00'), field(label='\ud800'),
        field(examples=['um', 'dois', 'três', 'quatro']),
        field(type='enum', enum=['igual', 'igual']),
        field(type='enum', enum=['']), field(enum=['indevida']),
        field(required_when={'field': 'documento', 'equals': 0}),
        field(required_when={'field': 'outra', 'equals': 1.5}),
        field(required='true'), field(normalization='outra'),
    ]
    for bad in bad_fields:
        load(page, [bad])
        invalid(page)
    load(page, [field(examples=['', '001.2300\nlinha'])])
    open_details(page)
    page.get_by_role('button', name='Adicionar exemplo', exact=True).click()
    page.get_by_label('Exemplo descritivo 3', exact=True).fill('😀\nnova linha')
    assert value(page)[0]['examples'] == ['', '001.2300\nlinha', '😀\nnova linha']
    expect(page.get_by_role('button', name='Adicionar exemplo', exact=True)).to_be_disabled()
    page.get_by_role('button', name='Remover exemplo 1 do campo', exact=False).click()
    assert value(page)[0]['examples'] == ['001.2300\nlinha', '😀\nnova linha']


@pytest.mark.parametrize('width,height', [(375, 812), (1440, 900)])
def test_hostile_text_and_layout(editor_page, width, height):
    page = editor_page
    page.set_viewport_size({'width': width, 'height': height})
    hostile = '<img src=x onerror="window.executed=true">' + '😀' * 60
    original = field(
        label='R' * 120, description=hostile + '\n' + 'x' * 4000,
        type='enum', enum=[hostile + '\ncontinuação'], examples=[hostile],
        required_when={'field': 'referencia_futura', 'equals': hostile},
        cardinality='many', max_items=100,
    )
    # Keep the hostile description within the DTO's code-point limit.
    original['description'] = original['description'][:4000]
    load(page, [original])
    open_details(page)
    assert value(page) == [original]
    assert page.locator('.catalog-fields img, .catalog-fields script').count() == 0
    assert page.evaluate('window.executed === undefined')
    assert page.evaluate('document.documentElement.scrollWidth <= document.documentElement.clientWidth')
    controls = page.locator('.catalog-fields input:visible, .catalog-fields textarea:visible, .catalog-fields select:visible, .catalog-fields button:visible, .catalog-fields summary:visible')
    assert controls.count() > 15
    for control in controls.all():
        control.scroll_into_view_if_needed()
        assert control.evaluate("""node => {
            const r = node.getBoundingClientRect();
            const width = document.documentElement.clientWidth;
            const center = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
            return r.width > 0 && r.height > 0 && r.left >= 0 && r.right <= width
                && (center === node || node.contains(center));
        }""")
    screenshot(page, f'catalog-fields-u1a-{width}.png')


def test_destroy_removes_listeners_and_isolates_instances(editor_page):
    page = editor_page
    load(page, [field()])
    page.evaluate("""() => {
        window.oldAdd = document.querySelector('.catalog-fields > button');
        const second = document.createElement('div');
        document.body.append(second);
        window.secondEditor = OcrCatalogFields.create(second, {idPrefix: 'test'});
        secondEditor.setValue([{name: 'outro', type: 'string', description: 'Outro'}]);
    }""")
    assert page.evaluate("""() => {
        const ids = [...document.querySelectorAll('[id]')].map(node => node.id);
        return ids.length === new Set(ids).size;
    }""")
    page.evaluate('editor.destroy(); oldAdd.click(); editor.setValue([]); editor.setReadonly(false); editor.destroy()')
    expect(page.locator('#fields')).to_be_empty()
    assert page.evaluate('changes.length') == 0
    assert page.evaluate('secondEditor.getValue()[0].name') == 'outro'
