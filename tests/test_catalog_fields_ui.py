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
    load(page, [original], [field('referencia_futura', type='enum', enum=[hostile])])
    open_details(page)
    expect(page.get_by_label('Opção da condição', exact=True)).to_be_visible()
    expect(page.get_by_label('Texto da condição', exact=True)).to_have_value(hostile)
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
    screenshot(page, f'catalog-fields-u1b-{width}.png')


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


def condition_ui(page, index=0):
    return page.locator('.catalog-fields__condition').nth(index)


def enable_condition(page, index=0):
    open_details(page)
    ui = condition_ui(page, index)
    ui.get_by_label('Ativar obrigatoriedade condicional', exact=True).check()
    return ui


@pytest.mark.parametrize('target_type,literal_type,literal', [
    ('boolean', 'boolean', False),
    ('integer', 'integer', 0),
    ('integer', 'integer', -9007199254740991),
    ('integer', 'integer', 9007199254740991),
    ('string', 'string', ''),
    ('decimal', 'string', '001.2300'),
    ('identifier', 'string', '000123'),
    ('date', 'string', '2026-10-16'),
    ('enum', 'string', 'primeira\nsegunda'),
])
def test_author_condition_with_guided_target(editor_page, target_type, literal_type, literal):
    page = editor_page
    target = field('origem', type=target_type, enum=['primeira\nsegunda', 'outra'] if target_type == 'enum' else None)
    original = field('dependente')
    load(page, [original, target])
    ui = enable_condition(page)
    suggestions = ui.get_by_label('Campo de referência sugerido', exact=True)
    assert suggestions.locator('option[value="dependente"]').count() == 0
    suggestions.select_option('origem')
    # Selecting a target never chooses or converts a literal for the author.
    assert value(page)[0]['required_when'] == {'field': 'origem', 'equals': ''}
    ui.get_by_label('Tipo do literal', exact=True).select_option(literal_type)
    if literal_type == 'boolean':
        ui.get_by_label('Sim ou não da condição', exact=True).select_option('true')
        ui.get_by_label('Sim ou não da condição', exact=True).select_option('false')
    elif literal_type == 'integer':
        ui.get_by_label('Inteiro da condição', exact=True).fill(str(literal))
    elif target_type == 'enum':
        ui.get_by_label('Opção da condição', exact=True).select_option(literal)
    else:
        ui.get_by_label('Texto da condição', exact=True).fill(literal)
    expected = copy.deepcopy(original)
    expected['required_when'] = {'field': 'origem', 'equals': literal}
    assert value(page) == [expected, target]
    assert type(value(page)[0]['required_when']['equals']) is type(literal)
    expect(ui.locator('.catalog-fields__condition-warning')).to_be_hidden()
    # Toggle off and back on without losing false, zero, empty or exact text.
    ui.get_by_label('Ativar obrigatoriedade condicional', exact=True).uncheck()
    assert value(page)[0]['required_when'] is None
    expect(ui.get_by_label('Chave do campo de referência', exact=True)).to_be_hidden()
    ui.get_by_label('Ativar obrigatoriedade condicional', exact=True).check()
    assert value(page) == [expected, target]


@pytest.mark.parametrize('kind,literal', [('boolean', False), ('integer', 0), ('string', '')])
def test_unknown_condition_can_be_authored_with_explicit_literal_type(editor_page, kind, literal):
    page = editor_page
    load(page, [field('comum')])
    ui = enable_condition(page)
    ui.get_by_label('Chave do campo de referência', exact=True).fill('especifico_futuro')
    ui.get_by_label('Tipo do literal', exact=True).select_option(kind)
    if kind == 'integer':
        ui.get_by_label('Inteiro da condição', exact=True).fill('0')
    expected = {'field': 'especifico_futuro', 'equals': literal}
    assert value(page)[0]['required_when'] == expected
    expect(ui.locator('.catalog-fields__condition-warning')).to_contain_text('desconhecido')
    assert type(value(page)[0]['required_when']['equals']) is type(literal)


def test_future_specific_and_inherited_union_resolve_without_mutating(editor_page):
    page = editor_page
    load(page, [field('comum')])
    ui = enable_condition(page)
    ui.get_by_label('Chave do campo de referência', exact=True).fill('especifico')
    ui.get_by_label('Texto da condição', exact=True).fill('001.2300')
    common = value(page)[0]
    expect(ui.locator('.catalog-fields__condition-warning')).to_contain_text('desconhecido')
    specific = field('especifico', type='decimal')
    load(page, [common, specific])
    open_details(page)
    expect(condition_ui(page).locator('.catalog-fields__condition-warning')).to_be_hidden()
    assert value(page) == [common, specific]
    # Type-local conditions can resolve inherited common fields. Inherited data
    # remains outside the editable list and outside the returned payload.
    local = field('especifico', required_when={'field': 'comum', 'equals': ''})
    page.evaluate('changes.length = 0')
    load(page, [local], [common])
    open_details(page)
    expect(condition_ui(page).locator('.catalog-fields__condition-warning')).to_be_hidden()
    assert value(page) == [local]
    assert page.locator('.catalog-fields__card').count() == 1
    assert page.evaluate('changes.length') == 0
    condition_ui(page).get_by_label('Campo de referência sugerido', exact=True).select_option('comum')
    assert value(page) == [local]
    assert common['required_when'] == {'field': 'especifico', 'equals': '001.2300'}


def test_target_changes_and_removal_preserve_condition_and_stable_card(editor_page):
    page = editor_page
    dependent = field('dependente', required_when={'field': 'origem', 'equals': '001.2300'})
    target = field('origem', type='decimal')
    load(page, [dependent, target])
    open_details(page)
    first = page.locator('.catalog-fields__card').first
    dependent_key = first.get_attribute('data-key')
    target_card = page.locator('.catalog-fields__card').nth(1)
    ui = first.locator('.catalog-fields__condition')
    target_card.get_by_label('Chave do campo', exact=True).fill('renomeado')
    expect(ui.locator('.catalog-fields__condition-warning')).to_contain_text('desconhecido')
    assert value(page)[0] == dependent
    target_card.get_by_label('Chave do campo', exact=True).fill('origem')
    target_card.get_by_label('Tipo', exact=True).select_option('boolean')
    expect(ui.locator('.catalog-fields__condition-warning')).to_contain_text('não é compatível')
    assert value(page)[0] == dependent
    target_card.get_by_label('Quantidade de valores', exact=True).select_option('many')
    expect(ui.locator('.catalog-fields__condition-warning')).to_contain_text('vários valores')
    assert ui.get_by_label('Campo de referência sugerido', exact=True).locator('option[value="origem"]').count() == 0
    assert value(page)[0] == dependent
    page.get_by_role('button', name='Mover campo 1 para baixo', exact=True).click()
    assert value(page)[1] == dependent
    assert page.locator('.catalog-fields__card').nth(1).get_attribute('data-key') == dependent_key
    page.get_by_role('button', name='Remover campo 1', exact=True).click()
    assert value(page) == [dependent]
    expect(condition_ui(page).locator('.catalog-fields__condition-warning')).to_contain_text('desconhecido')


@pytest.mark.parametrize('literal', [False, 0, '', '001.2300'])
def test_loaded_semantic_mismatch_is_readable_and_survives_label_edit_and_readonly(editor_page, literal):
    page = editor_page
    target = field('origem', type='enum', enum=['permitida'])
    original = field('dependente', label='Antes', required_when={'field': 'origem', 'equals': literal})
    load(page, [original], [target])
    open_details(page)
    ui = condition_ui(page)
    expect(ui.locator('.catalog-fields__condition-warning')).to_contain_text('não é compatível')
    if type(literal) is bool:
        expect(ui.get_by_label('Sim ou não da condição', exact=True)).to_have_value('false')
    elif type(literal) is int:
        expect(ui.get_by_label('Inteiro da condição', exact=True)).to_have_value('0')
    else:
        expect(ui.get_by_label('Texto da condição', exact=True)).to_have_value(literal)
    page.get_by_label('Rótulo', exact=True).fill('Depois')
    edited = copy.deepcopy(original)
    edited['label'] = 'Depois'
    assert value(page) == [edited]
    before_changes = page.evaluate('changes.length')
    page.evaluate('editor.setReadonly(true)')
    assert page.locator('.catalog-fields input:enabled, .catalog-fields textarea:enabled, .catalog-fields select:enabled, .catalog-fields button:enabled').count() == 0
    assert value(page) == [edited]
    page.evaluate('editor.setReadonly(false)')
    assert value(page) == [edited]
    assert page.evaluate('changes.length') == before_changes


@pytest.mark.parametrize('raw', ['', '1.5', 'NaN', 'Infinity', '9007199254740992', '-9007199254740992', ' 0', '0 '])
def test_condition_integer_drafts_are_strict_and_focusable(editor_page, raw):
    page = editor_page
    load(page, [field(required_when={'field': 'futuro', 'equals': 0})])
    open_details(page)
    number = condition_ui(page).get_by_label('Inteiro da condição', exact=True)
    number.fill(raw)
    invalid(page)
    expect(number).to_have_value(raw)
    expect(number).to_have_attribute('aria-invalid', 'true')
    page.evaluate('editor.focusFirstError()')
    expect(number).to_be_focused()
    number.fill('0')
    assert value(page)[0]['required_when'] == {'field': 'futuro', 'equals': 0}
    assert number.get_attribute('aria-invalid') is None


@pytest.mark.parametrize('condition', [
    {'field': 'futuro', 'equals': 9007199254740992},
    {'field': 'futuro', 'equals': 1.5},
    {'field': 'futuro', 'equals': None},
    {'field': 'futuro', 'equals': []},
    {'field': 'futuro', 'equals': {}, 'extra': True},
    {'field': 'futuro', 'equals': '', 'extra': True},
    {'field': 'futuro'},
    {'field': 'futuro', 'equals': '😀' * 4001},
    {'field': 'futuro', 'equals': '\x00'},
    {'field': 'futuro', 'equals': '\ud800'},
    {'field': ' Chave', 'equals': ''},
    {'field': 'á', 'equals': ''},
    {'field': 'a' * 65, 'equals': ''},
    {'field': 'documento', 'equals': False},
])
def test_condition_structural_validation_does_not_repair_loaded_data(editor_page, condition):
    page = editor_page
    original = field(label='Antes', required_when=condition)
    load(page, [original])
    open_details(page)
    invalid(page)
    page.get_by_label('Rótulo', exact=True).fill('Depois')
    invalid(page)
    # onChange receives the actual draft, including extra keys and invalid
    # literals; unrelated edits must not silently repair or discard it.
    assert page.evaluate('changes.at(-1)[0].required_when') == condition


@pytest.mark.parametrize('expression', ['NaN', 'Infinity', '-Infinity'])
def test_loaded_nonfinite_condition_is_invalid_and_not_coerced(editor_page, expression):
    page = editor_page
    page.evaluate(f"""() => editor.setValue([{{
        name: 'documento', description: 'Descrição', label: 'Antes',
        required_when: {{field: 'futuro', equals: {expression}}}
    }}])""")
    open_details(page)
    invalid(page)
    page.get_by_label('Rótulo', exact=True).fill('Depois')
    assert page.evaluate(f'Object.is(changes.at(-1)[0].required_when.equals, {expression})')
    expect(condition_ui(page).get_by_label('Inteiro da condição', exact=True)).to_have_value(expression)
    page.evaluate('editor.focusFirstError()')
    expect(condition_ui(page).get_by_label('Inteiro da condição', exact=True)).to_be_focused()


def test_condition_authoring_validation_caret_and_copy_isolation(editor_page):
    page = editor_page
    load(page, [field()])
    ui = enable_condition(page)
    key = ui.get_by_label('Chave do campo de referência', exact=True)
    key.fill('documento')
    invalid(page)
    expect(ui.locator('.catalog-fields__condition-warning')).to_contain_text('próprio campo')
    page.evaluate('editor.focusFirstError()')
    expect(key).to_be_focused()
    key.fill(' Chave')
    invalid(page)
    expect(key).to_have_value(' Chave')
    key.fill('ab')
    key.evaluate('(node) => { window.conditionKey = node; node.setSelectionRange(1, 1); }')
    key.press_sequentially('cd')
    expect(key).to_have_value('acdb')
    assert key.evaluate('(node) => node === conditionKey && node.selectionStart === 3 && document.activeElement === node')
    text = ui.get_by_label('Texto da condição', exact=True)
    text.fill('ab')
    text.evaluate('(node) => { window.conditionText = node; node.setSelectionRange(1, 1); }')
    text.press_sequentially('cd')
    expect(text).to_have_value('acdb')
    assert text.evaluate('(node) => node === conditionText && node.selectionStart === 3 && document.activeElement === node')
    text.fill('😀' * 4001)
    invalid(page)
    page.evaluate('editor.focusFirstError()')
    expect(text).to_be_focused()
    expect(text).to_have_attribute('aria-invalid', 'true')
    text.fill('😀' * 4000)
    assert value(page)[0]['required_when']['equals'] == '😀' * 4000
    page.evaluate("""() => {
        const output = editor.getValue();
        output[0].required_when.field = 'externo';
        output[0].required_when.equals = false;
        changes.at(-1)[0].required_when.equals = 'externo';
    }""")
    assert value(page)[0]['required_when'] == {'field': 'acdb', 'equals': '😀' * 4000}


def test_condition_reference_edit_does_not_convert_literal(editor_page):
    page = editor_page
    target = field('booleano', type='boolean')
    load(page, [field(required_when={'field': 'futuro', 'equals': '001.2300'})], [target])
    open_details(page)
    ui = condition_ui(page)
    ui.get_by_label('Campo de referência sugerido', exact=True).select_option('booleano')
    assert value(page)[0]['required_when'] == {'field': 'booleano', 'equals': '001.2300'}
    expect(ui.get_by_label('Tipo do literal', exact=True)).to_have_value('string')
    expect(ui.get_by_label('Texto da condição', exact=True)).to_have_value('001.2300')
    expect(ui.locator('.catalog-fields__condition-warning')).to_contain_text('não é compatível')
    ui.get_by_label('Tipo do literal', exact=True).select_option('boolean')
    assert value(page)[0]['required_when'] == {'field': 'booleano', 'equals': False}
    expect(ui.locator('.catalog-fields__condition-warning')).to_be_hidden()
