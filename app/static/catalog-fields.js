/* Pure, local FieldSpec editor. Publication validation remains the backend's job. */
(() => {
  'use strict';
  let instanceSequence = 0;
  const TYPES = {string: 'Texto', identifier: 'Identificador', integer: 'Inteiro', decimal: 'Decimal', boolean: 'Sim ou não', date: 'Data', enum: 'Lista de opções'};
  const DEFAULTS = {
    name: '', type: 'string', description: '', label: null,
    required: false, critical: false, evidence_required: false,
    sensitivity: 'internal', indexable: false, cardinality: 'one', max_items: 20,
    normalization: 'type-default', destination: 'sidecar', examples: [],
    required_when: null, enum: null,
  };
  const copy = value => structuredClone(value);
  function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (key === 'text') node.textContent = value;
      else if (key === 'className') node.className = value;
      else if (key.startsWith('aria-') || key === 'role' || key === 'for') node.setAttribute(key, String(value));
      else node[key] = value;
    }
    node.append(...children);
    return node;
  }
  function textOK(value, min, max, multiline = true) {
    if (typeof value !== 'string') return false;
    const points = Array.from(value);
    if (points.length < min || points.length > max) return false;
    return points.every(ch => {
      const n = ch.codePointAt(0);
      if (n >= 0xd800 && n <= 0xdfff) return false;
      return !(n <= 31 || (n >= 127 && n <= 159)) || (multiline && (n === 9 || n === 10));
    });
  }
  const validName = value => textOK(value, 1, 64, false) && /^[a-z][a-z0-9_]{0,63}$/.test(value);
  // Python's str.strip whitespace, without silently changing the label.
  const edgeWhitespace = /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]$/u;

  function create(container, {idPrefix = 'catalog', readonly = false, onChange} = {}) {
    const prefix = `${idPrefix}-${++instanceSequence}`;
    const controller = new AbortController();
    let records = [], inherited = [], sequence = 0, generation = 0;
    let dead = false, locked = Boolean(readonly), attempted = false, firstError = null;
    const root = el('section', {className: 'catalog-fields', 'aria-label': 'Editor de campos do catálogo'});
    const status = el('p', {className: 'catalog-fields__status', role: 'status', 'aria-live': 'polite'});
    const inheritedInfo = el('p', {className: 'catalog-fields__hint'});
    const globalError = el('p', {className: 'catalog-fields__error', role: 'alert', tabIndex: -1, hidden: true});
    const list = el('div', {className: 'catalog-fields__list'});
    const add = el('button', {type: 'button', text: 'Adicionar campo'});
    root.append(status, inheritedInfo, globalError, list, add);
    container.append(root);

    function listen(node, event, fn) {
      node.addEventListener(event, fn, {signal: controller.signal});
    }
    function editable(record) {
      return !dead && !locked && (!record || records.includes(record));
    }
    function changed() {
      if (dead) return;
      refresh();
      if (attempted) validate();
      if (typeof onChange === 'function') onChange(copy(records.map(record => record.data)));
    }
    function refresh() {
      status.textContent = `${records.length} de 100 campos locais${locked ? ' — somente leitura' : ''}.`;
      inheritedInfo.hidden = inherited.length === 0;
      inheritedInfo.textContent = inherited.length ? `Chaves herdadas, apenas para referência: ${inherited.map(field => field.name).join(', ')}.` : '';
      add.disabled = locked || records.length >= 100;
      records.forEach((record, index) => {
        const name = typeof record.data.name === 'string' && record.data.name ? record.data.name : 'sem chave';
        record.title.textContent = `Campo ${index + 1}: ${name}`;
        for (const control of record.card.querySelectorAll('input, textarea, select, button')) control.disabled = locked;
        record.up.disabled = locked || index === 0;
        record.down.disabled = locked || index === records.length - 1;
        record.up.setAttribute('aria-label', `Mover campo ${index + 1} para cima`);
        record.down.setAttribute('aria-label', `Mover campo ${index + 1} para baixo`);
        record.remove.setAttribute('aria-label', `Remover campo ${index + 1}`);
        record.controls.label.disabled = locked || record.data.label === null;
        record.controls.type.disabled = locked || record.pending;
        record.maxWrap.hidden = record.data.cardinality !== 'many';
        record.enumSection.hidden = record.data.type !== 'enum';
        record.exampleAdd.disabled = locked || (Array.isArray(record.data.examples) && record.data.examples.length >= 3);
        record.enumAdd.disabled = locked || (Array.isArray(record.data.enum) && record.data.enum.length >= 100);
      });
    }
    function makeRecord(value) {
      const data = {...copy(DEFAULTS), ...copy(value)};
      const key = `${prefix}-field-${++sequence}`;
      const record = {key, data, controls: {}, pending: false, request: 0};
      const title = el('h3', {id: `${key}-title`});
      const card = el('article', {className: 'catalog-fields__card', 'aria-labelledby': title.id});
      card.dataset.key = key;
      const grid = el('div', {className: 'catalog-fields__grid'});
      const details = el('details', {className: 'catalog-fields__details'}, [el('summary', {text: 'Opções avançadas'})]);
      const advanced = el('div', {className: 'catalog-fields__advanced'});
      const error = el('p', {id: `${key}-error`, className: 'catalog-fields__error', role: 'alert', hidden: true});
      record.card = card;
      record.title = title;
      record.details = details;
      record.error = error;

      function field(property, label, parent, kind = 'text', choices = null) {
        const id = `${key}-${property}`;
        const control = el(kind === 'textarea' ? 'textarea' : kind === 'select' ? 'select' : 'input', {id});
        if (kind === 'text') control.type = 'text';
        if (kind === 'checkbox') control.type = 'checkbox';
        if (choices) for (const [value, text] of Object.entries(choices)) control.append(el('option', {value, text}));
        if (kind === 'checkbox') control.checked = data[property] === true;
        else control.value = data[property] == null ? '' : String(data[property]);
        const wrap = el('div', {className: 'catalog-fields__field'}, [el('label', {htmlFor: id, text: label}), control]);
        parent.append(wrap);
        record.controls[property] = control;
        if (property !== 'type') listen(control, kind === 'select' || kind === 'checkbox' ? 'change' : 'input', () => {
          if (!editable(record)) return;
          if (kind === 'checkbox') data[property] = control.checked;
          else if (property === 'max_items') data[property] = /^[0-9]+$/.test(control.value) ? Number(control.value) : control.value;
          else data[property] = control.value;
          changed();
        });
        return wrap;
      }
      field('name', 'Chave do campo', grid);
      field('type', 'Tipo', grid, 'select', TYPES);
      const useName = el('input', {type: 'checkbox', id: `${key}-use-name`, checked: data.label === null});
      const useNameWrap = el('div', {className: 'catalog-fields__check'}, [useName, el('label', {htmlFor: useName.id, text: 'Usar chave como rótulo'})]);
      grid.append(useNameWrap);
      field('label', 'Rótulo', grid);
      field('description', 'Descrição', grid, 'textarea');
      let savedLabel = typeof data.label === 'string' ? data.label : '';
      listen(useName, 'change', () => {
        if (!editable(record)) return;
        if (useName.checked) {
          savedLabel = record.controls.label.value;
          data.label = null;
        } else {
          data.label = savedLabel;
          record.controls.label.value = savedLabel;
        }
        changed();
      });
      field('required', 'Obrigatório', advanced, 'checkbox');
      field('critical', 'Crítico', advanced, 'checkbox');
      field('evidence_required', 'Exigir evidência', advanced, 'checkbox');
      field('sensitivity', 'Sensibilidade', advanced, 'select', {public: 'Público', internal: 'Interno', restricted: 'Restrito'});
      field('indexable', 'Previsto para indexação (intenção de metadados)', advanced, 'checkbox');
      field('cardinality', 'Quantidade de valores', advanced, 'select', {one: 'Um valor', many: 'Vários valores'});
      record.maxWrap = field('max_items', 'Máximo de itens (1 a 100)', advanced);
      record.controls.max_items.inputMode = 'numeric';
      field('normalization', 'Normalização prevista', advanced, 'select', {'type-default': 'Padrão do tipo'});
      field('destination', 'Destino previsto dos metadados', advanced, 'select', {sidecar: 'Arquivo auxiliar (sidecar)', xmp: 'Metadados XMP'});
      advanced.append(el('p', {className: 'catalog-fields__hint', text: 'Estas opções registram intenção. Não confirmam gravação de metadados nem indexação.'}));
      const condition = el('div', {className: 'catalog-fields__condition'}, [el('h4', {text: 'Obrigatoriedade condicional'})]);
      condition.append(el('p', {className: 'catalog-fields__hint', text: 'Condição preservada; a edição de condições será disponibilizada em uma próxima etapa.'}));
      condition.append(el('pre', {text: data.required_when === null ? 'Sem condição.' : JSON.stringify(data.required_when, null, 2)}));
      advanced.append(condition);
      record.controls.required_when = condition;
      condition.tabIndex = -1;
      function arrayEditor(property, heading, limit) {
        const section = el('section', {className: 'catalog-fields__values', 'aria-labelledby': `${key}-${property}-heading`});
        section.append(el('h4', {id: `${key}-${property}-heading`, text: heading}));
        const rows = el('div', {className: 'catalog-fields__value-list'});
        const button = el('button', {type: 'button', text: property === 'examples' ? 'Adicionar exemplo' : 'Adicionar opção'});
        section.append(rows, button);
        advanced.append(section);
        function render() {
          for (const name of Object.keys(record.controls)) if (name.startsWith(`${property}.`)) delete record.controls[name];
          rows.replaceChildren();
          const values = Array.isArray(data[property]) ? data[property] : [];
          values.forEach((value, index) => {
            const id = `${key}-${property}-${index}`;
            const control = el('textarea', {id, value: typeof value === 'string' ? value : String(value)});
            const remove = el('button', {type: 'button', text: 'Remover', 'aria-label': `Remover ${property === 'examples' ? 'exemplo' : 'opção'} ${index + 1} do campo ${key}`});
            const row = el('div', {className: 'catalog-fields__value'}, [el('label', {htmlFor: id, text: `${property === 'examples' ? 'Exemplo descritivo' : 'Opção'} ${index + 1}`}), control, remove]);
            record.controls[`${property}.${index}`] = control;
            listen(control, 'input', () => {
              if (!editable(record)) return;
              data[property][index] = control.value;
              changed();
            });
            listen(remove, 'click', () => {
              if (!editable(record)) return;
              data[property].splice(index, 1);
              render();
              changed();
              (rows.querySelector('textarea') || button).focus();
            });
            rows.append(row);
          });
        }
        listen(button, 'click', () => {
          if (!editable(record)) return;
          if (!Array.isArray(data[property])) data[property] = [];
          if (data[property].length >= limit) return;
          data[property].push('');
          render();
          changed();
          rows.lastElementChild.querySelector('textarea').focus();
        });
        render();
        return {section, button, render};
      }
      const examples = arrayEditor('examples', 'Exemplos descritivos (até 3)', 3);
      record.exampleAdd = examples.button;
      record.controls.examples = examples.button;
      const options = arrayEditor('enum', 'Opções da lista (1 a 100)', 100);
      record.enumSection = options.section;
      record.enumAdd = options.button;
      record.controls.enum = options.button;
      listen(record.controls.type, 'change', async () => {
        if (!editable(record) || record.pending) return;
        const next = record.controls.type.value;
        const previous = data.type;
        if (next === previous) return;
        if (previous === 'enum') {
          record.controls.type.value = previous;
          record.pending = true;
          const epoch = generation, request = ++record.request;
          refresh();
          let accepted = false;
          try {
            const message = {title: 'Descartar opções da lista?', body: 'Ao mudar o tipo, as opções deste campo serão removidas.', confirmLabel: 'Descartar opções', danger: true};
            accepted = window.OcrUi && typeof window.OcrUi.confirmAction === 'function'
              ? await window.OcrUi.confirmAction(message)
              : await Promise.resolve(window.confirm(`${message.title}\n\n${message.body}`));
          } catch (_) {
            accepted = false;
          }
          if (!editable(record) || generation !== epoch || record.request !== request) return;
          record.pending = false;
          if (!accepted) {
            refresh();
            return;
          }
        }
        if (!editable(record)) return;
        data.type = next;
        data.enum = next === 'enum' ? [''] : null;
        record.controls.type.value = next;
        options.render();
        changed();
      });
      const actions = el('div', {className: 'catalog-fields__actions'});
      record.up = el('button', {type: 'button', text: 'Subir'});
      record.down = el('button', {type: 'button', text: 'Descer'});
      record.remove = el('button', {type: 'button', text: 'Remover campo'});
      actions.append(record.up, record.down, record.remove);
      function move(delta) {
        if (!editable(record)) return;
        const index = records.indexOf(record), target = index + delta;
        if (target < 0 || target >= records.length) return;
        const focused = document.activeElement;
        [records[index], records[target]] = [records[target], records[index]];
        if (delta < 0) list.insertBefore(record.card, records[index].card);
        else list.insertBefore(records[index].card, record.card);
        changed();
        if (focused && focused.isConnected && !focused.disabled) focused.focus();
      }
      listen(record.up, 'click', () => move(-1));
      listen(record.down, 'click', () => move(1));
      listen(record.remove, 'click', () => {
        if (!editable(record)) return;
        const index = records.indexOf(record);
        records.splice(index, 1);
        record.request++;
        card.remove();
        changed();
        (records[Math.min(index, records.length - 1)]?.controls.name || add).focus();
      });
      details.append(advanced);
      card.append(title, grid, details, error, actions);
      return record;
    }

    function validate() {
      firstError = null;
      globalError.hidden = true;
      globalError.textContent = '';
      const names = new Map();
      for (const record of records) names.set(record.data.name, (names.get(record.data.name) || 0) + 1);
      const inheritedNames = new Set(inherited.map(field => field.name));
      if (records.length > 100) {
        globalError.textContent = 'Use no máximo 100 campos locais.';
        globalError.hidden = false;
        firstError = {node: globalError};
      }
      records.forEach((record, index) => {
        const f = record.data, errors = [];
        const fail = (property, message) => errors.push({property, message});
        for (const node of Object.values(record.controls)) {
          node.removeAttribute('aria-invalid');
          node.removeAttribute('aria-describedby');
        }
        if (Object.keys(f).some(key => !Object.hasOwn(DEFAULTS, key))) fail('name', 'O campo contém propriedades não reconhecidas.');
        if (!validName(f.name)) fail('name', 'Chave: use uma letra minúscula seguida de letras minúsculas, números ou sublinhados, até 64 caracteres.');
        if (names.get(f.name) > 1 || inheritedNames.has(f.name)) fail('name', 'A chave já existe nos campos locais ou herdados.');
        if (!Object.hasOwn(TYPES, f.type)) fail('type', 'Escolha um tipo válido.');
        if (!textOK(f.description, 1, 4000)) fail('description', 'Descrição: use de 1 a 4000 caracteres, sem controles inválidos.');
        if (f.label !== null && (!textOK(f.label, 1, 120, false) || edgeWhitespace.test(f.label))) fail('label', 'Rótulo: use de 1 a 120 caracteres, sem espaços nas extremidades nem controles.');
        for (const key of ['required', 'critical', 'evidence_required', 'indexable']) if (typeof f[key] !== 'boolean') fail(key, 'Selecione um valor booleano válido.');
        for (const [key, values] of Object.entries({sensitivity: ['public', 'internal', 'restricted'], cardinality: ['one', 'many'], normalization: ['type-default'], destination: ['sidecar', 'xmp']})) {
          if (!values.includes(f[key])) fail(key, 'Selecione uma opção válida.');
        }
        if (!Number.isInteger(f.max_items) || f.max_items < 1 || f.max_items > 100) fail('max_items', 'Máximo de itens: use um inteiro de 1 a 100.');
        if (!Array.isArray(f.examples) || f.examples.length > 3) fail('examples', 'Use no máximo 3 exemplos descritivos.');
        if (Array.isArray(f.examples)) f.examples.forEach((value, i) => {
          if (!textOK(value, 0, 4000)) fail(`examples.${i}`, 'Exemplo: use texto de até 4000 caracteres, sem controles inválidos.');
        });
        if (f.type === 'enum') {
          if (!Array.isArray(f.enum) || f.enum.length < 1 || f.enum.length > 100) fail('enum', 'A lista precisa de 1 a 100 opções.');
          if (Array.isArray(f.enum)) {
            const seen = new Set();
            f.enum.forEach((value, i) => {
              if (!textOK(value, 1, 4000)) fail(`enum.${i}`, 'Opção: use de 1 a 4000 caracteres, sem controles inválidos.');
              if (seen.has(value)) fail(`enum.${i}`, 'As opções da lista não podem se repetir.');
              seen.add(value);
            });
          }
        } else if (f.enum !== null) fail('type', 'Opções de lista são permitidas apenas para o tipo lista de opções.');
        if (f.required_when !== null) {
          const condition = f.required_when;
          if (typeof condition !== 'object' || Array.isArray(condition) || Object.keys(condition).length !== 2 || !Object.hasOwn(condition, 'field') || !Object.hasOwn(condition, 'equals') || !validName(condition.field)) {
            fail('required_when', 'A condição preservada precisa de uma chave válida e um literal.');
          } else {
            if (condition.field === f.name) fail('required_when', 'A condição não pode referenciar o próprio campo.');
            const value = condition.equals;
            if (!(typeof value === 'boolean' || (typeof value === 'number' && Number.isSafeInteger(value)) || textOK(value, 0, 4000))) fail('required_when', 'O literal da condição deve ser texto, booleano ou inteiro seguro.');
          }
        }
        record.error.hidden = errors.length === 0;
        record.error.textContent = errors.length ? `Campo ${index + 1}: ${errors.map(error => error.message).join(' ')}` : '';
        for (const error of errors) {
          const node = record.controls[error.property] || record.controls.name;
          node.setAttribute('aria-invalid', 'true');
          node.setAttribute('aria-describedby', record.error.id);
          if (!firstError) firstError = {node, record};
        }
      });
      return firstError === null;
    }
    listen(add, 'click', () => {
      if (!editable() || records.length >= 100) return;
      const record = makeRecord(DEFAULTS);
      records.push(record);
      list.append(record.card);
      changed();
      record.controls.name.focus();
    });
    refresh();
    return {
      setValue(fields, {inherited: inheritedFields = []} = {}) {
        if (dead) return;
        if (!Array.isArray(fields) || !Array.isArray(inheritedFields)) throw new Error('Campos locais e herdados devem ser listas.');
        if (fields.some(field => !field || typeof field !== 'object' || Array.isArray(field)) || inheritedFields.some(field => !field || typeof field !== 'object' || Array.isArray(field))) throw new Error('Cada campo deve ser um objeto.');
        const next = fields.map(makeRecord);
        const nextInherited = copy(inheritedFields);
        generation++;
        records = next;
        inherited = nextInherited;
        attempted = false;
        firstError = null;
        globalError.hidden = true;
        list.replaceChildren(...records.map(record => record.card));
        refresh();
      },
      getValue() {
        if (dead) throw new Error('O editor de campos foi encerrado.');
        attempted = true;
        if (!validate()) throw new Error('Campos inválidos. Revise os avisos do editor antes de continuar.');
        return copy(records.map(record => record.data));
      },
      setReadonly(value) {
        if (dead) return;
        locked = Boolean(value);
        generation++;
        records.forEach(record => { record.pending = false; record.request++; });
        refresh();
      },
      focusFirstError() {
        if (dead || !firstError) return;
        const {node, record} = firstError;
        if (record) {
          if (record.details.contains(node)) record.details.open = true;
          if (node === record.controls.max_items) record.maxWrap.hidden = false;
          if (node.disabled) {
            record.error.tabIndex = -1;
            record.error.focus();
            return;
          }
        }
        node.focus();
      },
      destroy() {
        if (dead) return;
        dead = true;
        generation++;
        controller.abort();
        root.remove();
        records = [];
        inherited = [];
        firstError = null;
        onChange = undefined;
      },
    };
  }
  window.OcrCatalogFields = Object.freeze({create});
})();
