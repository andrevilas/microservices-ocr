/* Read-only template catalog. Credentials remain in the calling closure. */
(() => {
  "use strict";
  function create({get, getState, object, clone, freeze, equal, forbidden, edgeWhitespace}) {
    const node = (id) => document.getElementById(`template-${id}`);
    const section = node("section"), status = node("status"), loadButton = node("load");
    const list = node("list"), preview = node("preview");
    const ids = ["generic", "real-estate-deed", "procurement-notice"];
    const fieldTypes = new Set(["string", "identifier", "integer", "decimal", "boolean", "date", "enum"]);
    let records = [], selected = null, loading = false, generation = 0, destroyed = false;
    let message = "Use Carregar modelos para consultar o catálogo.";
    const listeners = [];
    const commonEditor = window.OcrCatalogFields.create(node("common"), {idPrefix: "template-common-preview", readonly: true});
    const specificEditor = window.OcrCatalogFields.create(node("specific"), {idPrefix: "template-specific-preview", readonly: true});
    function keys(value, expected) {
      return object(value) && Object.keys(value).length === expected.length && expected.every((key) => Object.hasOwn(value, key));
    }
    function text(value, min, max, multiline) {
      return typeof value === "string" && Array.from(value).length >= min && Array.from(value).length <= max && !forbidden(value, multiline);
    }
    function nameOk(value) {
      return text(value, 1, 120, false) && !edgeWhitespace.test(value);
    }
    function keyOk(value) {
      return typeof value === "string" && value.length <= 64 && /^[a-z][a-z0-9_]*$/.test(value) && !value.includes("\n");
    }
    function fieldsOk(value) {
      return Array.isArray(value) && value.length <= 100 && value.every((field) =>
        object(field) && keyOk(field.name) && text(field.description, 1, 4000, true) && fieldTypes.has(field.type));
    }
    function bodyOk(value, fieldsKey) {
      return keys(value, ["name", "description", fieldsKey]) && nameOk(value.name) &&
        text(value.description, 0, 4000, true) && fieldsOk(value[fieldsKey]);
    }
    function recordOk(value, index) {
      if (!keys(value, ["template_id", "template_version", "context", "types", "template_sha256"]) ||
        value.template_id !== ids[index] || !Number.isSafeInteger(value.template_version) || value.template_version < 1 ||
        typeof value.template_sha256 !== "string" || value.template_sha256.length !== 64 || !/^[0-9a-f]{64}$/.test(value.template_sha256) ||
        !bodyOk(value.context, "common_fields") || !Array.isArray(value.types) || value.types.length !== 1) return false;
      const type = value.types[0];
      if (!keys(type, ["type_key", "body"]) || !keyOk(type.type_key) || !bodyOk(type.body, "specific_fields")) return false;
      const fields = [...value.context.common_fields, ...type.body.specific_fields];
      return fields.length <= 100 && new Set(fields.map((field) => field.name)).size === fields.length;
    }
    function catalogOk(value) {
      return keys(value, ["items"]) && Array.isArray(value.items) && value.items.length === ids.length && value.items.every(recordOk);
    }
    function allowed(state = getState()) {
      return !destroyed && state.active && !state.writing && !state.reading && !state.confirming && !state.departing;
    }
    function update() {
      if (destroyed) return;
      const state = getState(), enabled = allowed(state) && !loading;
      section.hidden = !state.active;
      section.setAttribute("aria-busy", String(loading));
      status.setAttribute("aria-busy", String(loading));
      loadButton.disabled = !enabled;
      loadButton.textContent = records.length ? "Atualizar modelos" : "Carregar modelos";
      list.querySelectorAll("button").forEach((button) => { button.disabled = !enabled; });
      status.textContent = !state.active
        ? "Sessão indisponível ou expirada. Os modelos consultados foram preservados. Entre com permissão de leitura para continuar."
        : state.departing ? "Consulta de modelos pausada durante a saída da página."
        : message;
    }
    function clearPreview() {
      selected = null;
      preview.hidden = true;
      for (const id of ["edition", "hash", "context-name", "context-description", "type-name", "type-description"]) node(id).textContent = "";
      commonEditor.setValue([], {inherited: []});
      specificEditor.setValue([], {inherited: []});
      commonEditor.setReadonly(true);
      specificEditor.setReadonly(true);
    }
    function show(record) {
      selected = record;
      const type = record.types[0].body;
      node("edition").textContent = `Edição ${record.template_version}`;
      node("hash").textContent = record.template_sha256;
      node("context-name").textContent = record.context.name;
      node("context-description").textContent = record.context.description;
      node("type-name").textContent = type.name;
      node("type-description").textContent = type.description;
      commonEditor.setValue(clone(record.context.common_fields), {inherited: clone(type.specific_fields)});
      specificEditor.setValue(clone(type.specific_fields), {inherited: clone(record.context.common_fields)});
      commonEditor.setReadonly(true);
      specificEditor.setReadonly(true);
      preview.hidden = false;
    }
    function renderList() {
      list.replaceChildren(...records.map((record) => {
        const item = document.createElement("li");
        item.className = "entity";
        const button = document.createElement("button");
        button.type = "button";
        button.className = "link-button entity__name";
        button.dataset.templateId = record.template_id;
        button.textContent = `Ver modelo: ${record.context.name}`;
        button.setAttribute("aria-controls", "template-preview");
        if (selected?.template_id === record.template_id) button.setAttribute("aria-current", "true");
        item.append(button);
        return item;
      }));
    }
    function select(event) {
      const button = event.target.closest?.("button[data-template-id]");
      if (!button || !list.contains(button) || !allowed() || loading) return;
      const record = records.find((entry) => entry.template_id === button.dataset.templateId);
      if (!record) return;
      if (!selected || !equal(selected, record)) show(record);
      message = `Modelo ${record.context.name}, edição ${record.template_version}, em consulta somente para leitura.`;
      renderList();
      update();
    }
    async function load() {
      if (!allowed() || loading) return;
      const ownGeneration = ++generation;
      loading = true;
      message = "Carregando modelos de contexto…";
      update();
      try {
        const page = await get("/api/v1/context-templates", {valid: catalogOk});
        if (destroyed || ownGeneration !== generation) return;
        if (!allowed()) {
          message = "Consulta interrompida. Use Atualizar modelos ou Carregar modelos para tentar novamente. Os dados anteriores foram preservados.";
          return;
        }
        // Validate the entire response before replacing any stored data or view.
        if (!catalogOk(page)) throw new Error("Invalid template catalog");
        const next = freeze(clone(page.items));
        let cleared = false;
        if (selected) {
          const match = next.find((record) => record.template_id === selected.template_id &&
            record.template_version === selected.template_version && record.template_sha256 === selected.template_sha256);
          if (!match) { clearPreview(); cleared = true; }
          // An unchanged edition/hash keeps the existing immutable preview.
        }
        records = next;
        renderList();
        message = cleared
          ? "Três modelos carregados. A edição do modelo selecionado mudou; selecione um modelo para consultar novamente."
          : "Três modelos carregados. Selecione um modelo para consultar seus campos.";
      } catch (_) {
        if (!destroyed && ownGeneration === generation) message = "Não foi possível carregar os modelos. Os dados anteriores foram preservados. Use Carregar modelos ou Atualizar modelos para tentar novamente.";
      } finally {
        if (!destroyed && ownGeneration === generation) { loading = false; update(); }
      }
    }
    function reset() {
      if (destroyed) return;
      ++generation;
      loading = false;
      records = [];
      clearPreview();
      list.replaceChildren();
      message = "Use Carregar modelos para consultar o catálogo.";
      update();
    }
    function listen(target, event, handler) {
      target.addEventListener(event, handler);
      listeners.push(() => target.removeEventListener(event, handler));
    }
    function destroy() {
      if (destroyed) return;
      destroyed = true;
      ++generation;
      loading = false;
      records = [];
      selected = null;
      listeners.forEach((remove) => remove());
      listeners.length = 0;
      commonEditor.destroy();
      specificEditor.destroy();
      section.hidden = true;
      section.setAttribute("aria-busy", "false");
      status.setAttribute("aria-busy", "false");
      loadButton.disabled = true;
      list.replaceChildren();
      preview.hidden = true;
    }
    listen(loadButton, "click", load);
    listen(list, "click", select);
    return Object.freeze({update, load, reset, destroy});
  }
  window.OcrCatalogTemplates = Object.freeze({create});
})();
