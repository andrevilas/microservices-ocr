/* Template preview and explicit clone recovery. Credentials remain in the calling closure. */
(() => {
  "use strict";
  function create({get, post, getState, beginWrite, confirm, onStateChange, refreshContexts, openContext,
    entityRecordOk, object, clone, freeze, equal, forbidden, edgeWhitespace}) {
    const node = (id) => document.getElementById(`template-${id}`);
    const section = node("section"), status = node("status"), loadButton = node("load");
    const list = node("list"), preview = node("preview");
    const ids = ["generic", "real-estate-deed", "procurement-notice"];
    const fieldTypes = new Set(["string", "identifier", "integer", "decimal", "boolean", "date", "enum"]);
    let records = [], selected = null, loading = false, generation = 0, destroyed = false;
    let message = "Use Carregar modelos para consultar o catálogo.";
    let cloneGeneration = 0, cloneIntent = null, cloneSuccess = null, cloneConfirm = false;
    let requiresTemplateRefresh = false, refreshMinimum = 0;
    let cloneMessage = "";
    const protectedClone = () => !!cloneIntent || cloneConfirm;
    const identity = (value) => typeof value === "string" && value.length === 32 && /^[0-9a-f]{32}$/.test(value);
    const utc = (value) => typeof value === "string" && value.includes("T") &&
      /(?:Z|\+00:00)$/.test(value) && Number.isFinite(Date.parse(value));
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
      const writable = state.active && state.canWrite && !state.expired && !state.departing;
      const busy = state.writing || state.reading || state.confirming || cloneConfirm;
      node("clone").hidden = !writable || !selected;
      node("clone").disabled = !canStartClone(state);
      node("recover").hidden = !cloneIntent;
      node("recover").disabled = !writable || busy || state.blocked;
      node("abandon").hidden = !cloneIntent;
      node("abandon").disabled = !writable || busy;
      node("open").hidden = !cloneSuccess;
      node("open").disabled = !state.active || busy || state.departing;
      node("clone-status").textContent = cloneIntent && state.expired
        ? "Sessão expirada. O pedido de clonagem e sua recuperação foram preservados. Entre novamente para continuar."
        : cloneMessage || (state.dirty
          ? "Salve ou descarte as alterações do rascunho antes de criar outro contexto."
          : requiresTemplateRefresh ? "Atualize os modelos antes de criar outro contexto." : "");
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
        if (requiresTemplateRefresh && ownGeneration >= refreshMinimum) requiresTemplateRefresh = false;
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
    function requireRefresh() {
      requiresTemplateRefresh = true;
      refreshMinimum = generation + 1;
    }
    function resetClone() {
      if (cloneIntent) requireRefresh();
      ++cloneGeneration;
      cloneIntent = null;
      cloneSuccess = null;
      cloneMessage = "";
      if (!destroyed) update();
    }
    function writableIdle(state = getState()) {
      return !destroyed && state.active && state.canWrite && !state.expired && !state.departing &&
        !state.writing && !state.reading && !state.confirming && !cloneConfirm;
    }
    function canStartClone(state = getState()) {
      return writableIdle(state) && !state.blocked && !loading && !cloneIntent &&
        !requiresTemplateRefresh && !!selected && recordOk(selected, ids.indexOf(selected.template_id));
    }
    function receiptOk(intent, value) {
      const template = intent.request.template;
      if (!keys(value, ["template_id", "template_version", "template_sha256", "context_id", "context_revision", "types", "created_at"]) ||
        value.template_id !== template.template_id || value.template_version !== template.template_version ||
        value.template_sha256 !== template.template_sha256 || !identity(value.context_id) ||
        value.context_revision !== 1 || !utc(value.created_at) || !Array.isArray(value.types) ||
        value.types.length !== template.types.length) return false;
      const seen = new Set([value.context_id]);
      return value.types.every((type, index) => {
        if (!keys(type, ["type_key", "type_id", "revision"]) ||
          type.type_key !== template.types[index].type_key || !identity(type.type_id) ||
          type.revision !== 1 || seen.has(type.type_id)) return false;
        seen.add(type.type_id);
        return true;
      });
    }
    function currentRecordOk(intent, kind, index, value) {
      const receipt = intent.receipt, template = intent.request.template;
      const id = kind === "context" ? receipt.context_id : receipt.types[index].type_id;
      const body = kind === "context" ? template.context : template.types[index].body;
      return entityRecordOk(kind, kind === "context" ? null : receipt.context_id, value) &&
        value[kind === "context" ? "context_id" : "type_id"] === id &&
        Date.parse(value.created_at) === Date.parse(receipt.created_at) &&
        (value.revision > 1 || equal(value.body, body));
    }
    function cloneCurrent(intent, ownGeneration, lease) {
      const state = getState();
      return !destroyed && cloneIntent === intent && cloneGeneration === ownGeneration &&
        lease.current() && state.active && state.canWrite && !state.expired && !state.departing;
    }
    function changed() { update(); onStateChange(); }
    async function startClone() {
      if (!canStartClone()) return;
      const source = selected, ownGeneration = cloneGeneration;
      const template = freeze(clone(source));
      let accepted = false;
      cloneConfirm = true; changed();
      try {
        accepted = await confirm({title: "Criar contexto a partir deste modelo?",
          body: "Será criado um novo contexto com um tipo de documento, ambos como rascunhos na revisão 1, sem publicação automática. O modelo é um ponto de partida e precisa de revisão. O formulário atual permanecerá inalterado.",
          confirmLabel: "Criar contexto"});
      } catch (_) {
        cloneMessage = "Não foi possível confirmar a clonagem. Tente novamente.";
      } finally { cloneConfirm = false; if (!destroyed) changed(); }
      if (!accepted || ownGeneration !== cloneGeneration || selected !== source || !canStartClone()) return;
      let key;
      try { key = crypto.randomUUID(); }
      catch (_) { cloneMessage = "Não foi possível preparar a clonagem. Use um navegador com conexão segura."; changed(); return; }
      cloneSuccess = null;
      cloneIntent = {request: freeze({template, templateId: template.template_id,
        body: JSON.stringify({expected_version: template.template_version, expected_sha256: template.template_sha256}), key}),
        everAmbiguous: false, receipt: null};
      await recoverClone();
    }
    async function recoverClone() {
      if (!cloneIntent || !writableIdle() || getState().blocked) return;
      const intent = cloneIntent, ownGeneration = cloneGeneration;
      const lease = beginWrite();
      if (!lease) return;
      let confirmed = false;
      cloneMessage = intent.receipt ? "Consultando o contexto e o tipo criados…" : "Criando contexto a partir do modelo…";
      changed();
      try {
        if (!intent.receipt) {
          const receipt = await post(intent.request.templateId, {body: intent.request.body,
            key: intent.request.key, valid: (value) => receiptOk(intent, value)});
          if (!receiptOk(intent, receipt)) throw {ambiguous: true};
          if (!cloneCurrent(intent, ownGeneration, lease)) return;
          intent.receipt = freeze(clone(receipt));
        }
        if (!cloneCurrent(intent, ownGeneration, lease)) return;
        const receipt = intent.receipt;
        const [context, ...types] = await Promise.all([
          get(`/api/v1/contexts/${receipt.context_id}`, {valid: (value) => currentRecordOk(intent, "context", 0, value)}),
          ...receipt.types.map((type, index) => get(`/api/v1/document-types/${type.type_id}`, {
            valid: (value) => currentRecordOk(intent, "type", index, value),
          })),
        ]);
        if (!currentRecordOk(intent, "context", 0, context) ||
          !types.every((type, index) => currentRecordOk(intent, "type", index, type))) throw {ambiguous: true};
        if (!cloneCurrent(intent, ownGeneration, lease)) return;
        cloneSuccess = freeze({receipt: clone(receipt), context: clone(context), types: clone(types)});
        cloneIntent = null;
        cloneMessage = `Clonagem confirmada. Contexto e tipo criados inicialmente na revisão 1. Revisões atuais consultadas: contexto ${context.revision}; tipo ${types[0].revision}. O formulário atual foi preservado.`;
        confirmed = true;
      } catch (error) {
        if (error.ambiguous) intent.everAmbiguous = true;
        if (!cloneCurrent(intent, ownGeneration, lease)) return;
        const preAdmission = !intent.receipt && !intent.everAmbiguous && (
          (error.status === 409 && error.code === "template_version_conflict") ||
          (error.status === 404 && error.code === "context_template_not_found") ||
          (error.status === 429 && ["context_quota_exceeded", "type_quota_exceeded"].includes(error.code)) ||
          (error.status === 422 && error.code === "validation_error"));
        if (preAdmission) {
          cloneIntent = null;
          if (error.status === 409) requireRefresh();
          cloneMessage = error.status === 409
            ? "A edição selecionada mudou. Use Atualizar modelos e revise o modelo antes de criar outro contexto."
            : error.status === 404 ? "O modelo não está disponível. Atualize os modelos antes de tentar novamente."
            : error.status === 429 ? "O limite de contextos ou tipos foi atingido. O formulário foi preservado."
            : "A clonagem não foi admitida. Revise o modelo antes de tentar novamente.";
        } else cloneMessage = intent.receipt
          ? "A criação foi recebida, mas a consulta atual ainda não foi confirmada. Recuperar clonagem apenas consulta o contexto e o tipo recebidos."
          : "O pedido foi preservado e o contexto pode já ter sido criado. Recuperar clonagem repete exatamente o mesmo pedido, com a mesma chave. Não há nova tentativa automática.";
      } finally {
        lease.release();
        if (!destroyed) changed();
      }
      if (confirmed && !destroyed && ownGeneration === cloneGeneration && cloneSuccess &&
        getState().active && !getState().departing) void refreshContexts();
    }
    async function abandonClone() {
      if (!cloneIntent || !writableIdle()) return;
      const intent = cloneIntent, ownGeneration = cloneGeneration;
      let accepted = false;
      cloneConfirm = true; changed();
      try {
        accepted = await confirm({title: "Abandonar recuperação da clonagem?",
          body: "O contexto pode já ter sido criado. Seu formulário e a consulta do modelo serão preservados. Confira a lista de contextos e depois use Atualizar modelos antes de criar outro contexto. Nenhum pedido será enviado.",
          confirmLabel: "Abandonar recuperação", danger: true});
      } catch (_) {
        cloneMessage = "Não foi possível confirmar o abandono. A recuperação foi preservada.";
      } finally { cloneConfirm = false; if (!destroyed) changed(); }
      if (!accepted || cloneIntent !== intent || ownGeneration !== cloneGeneration || !writableIdle()) return;
      resetClone();
      cloneMessage = "Recuperação abandonada. O contexto pode já ter sido criado. Confira a lista de contextos e depois use Atualizar modelos antes de criar outro contexto. O formulário foi preservado.";
      changed();
    }
    function openCreated() {
      const state = getState();
      if (!cloneSuccess || !state.active || state.writing || state.reading || state.confirming || state.departing || destroyed) return;
      void openContext(cloneSuccess.receipt.context_id);
    }
    function reset() {
      if (destroyed) return;
      resetClone();
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
      ++cloneGeneration;
      cloneIntent = null;
      cloneSuccess = null;
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
    listen(node("clone"), "click", startClone);
    listen(node("recover"), "click", recoverClone);
    listen(node("abandon"), "click", abandonClone);
    listen(node("open"), "click", openCreated);
    return Object.freeze({update, load, reset, resetClone, protected: protectedClone, destroy});
  }
  window.OcrCatalogTemplates = Object.freeze({create});
})();
