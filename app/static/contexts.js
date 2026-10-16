/* Context CRUD only. Session credentials and drafts live in this closure. */
(() => {
  "use strict";
  const ui = window.OcrUi;
  const $ = (id) => document.getElementById(`context-${id}`);
  const form = $("form"), name = $("name"), description = $("description");
  const clone = (value) => structuredClone(value);
  const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const idOk = (value) => typeof value === "string" && /^[0-9a-f]{32}$/.test(value);
  const revisionOk = (value) => Number.isSafeInteger(value) && value >= 1;
  const dateOk = (value) => typeof value === "string" && Number.isFinite(Date.parse(value));
  const fieldsOk = (value) => Array.isArray(value) && value.every(object);
  const contextKind = "context";
  const entities = freeze({
    context: {
      idKey: "context_id", fieldsKey: "common_fields",
      collection: () => "/api/v1/contexts",
      detail: (id) => `/api/v1/contexts/${id}`,
      invalidBody: "invalid_context_body", revisionConflict: "context_revision_conflict", quotaExceeded: "context_quota_exceeded",
    },
    type: {
      idKey: "type_id", fieldsKey: "specific_fields",
      collection: (parentId) => `/api/v1/contexts/${parentId}/types`,
      detail: (id) => `/api/v1/document-types/${id}`,
      invalidBody: "invalid_type_body", revisionConflict: "type_revision_conflict", quotaExceeded: "type_quota_exceeded",
    },
  });
  function entityPath(kind, parentId, id = null) {
    if (kind === "type" && !idOk(parentId)) throw new Error("Invalid parent identity");
    return id === null ? entities[kind].collection(parentId) : entities[kind].detail(id);
  }
  function entityBody(kind, name, description, fields) {
    return {name, description, [entities[kind].fieldsKey]: fields};
  }
  const bodyOk = (kind, value) => object(value) && typeof value.name === "string" && typeof value.description === "string" && fieldsOk(value[entities[kind].fieldsKey]);
  const summaryOk = (value) => object(value) && idOk(value.context_id) && typeof value.name === "string" && revisionOk(value.revision) && dateOk(value.created_at) && dateOk(value.updated_at);
  function identityOk(kind, parentId, value) {
    return object(value) && idOk(value[entities[kind].idKey]) &&
      (kind === "context" || (idOk(parentId) && idOk(value.context_id) && value.context_id === parentId));
  }
  function entityRecordOk(kind, parentId, value) {
    return identityOk(kind, parentId, value) && revisionOk(value.revision) && dateOk(value.created_at) && dateOk(value.updated_at) &&
      bodyOk(kind, value.body) && (kind === "context" || value.state === "draft");
  }
  function entityReceiptOk(kind, parentId, value) {
    return identityOk(kind, parentId, value) && value.revision === 1 && dateOk(value.created_at);
  }
  const recordOk = (value) => entityRecordOk(contextKind, null, value);
  const pageOk = (value) => object(value) && Array.isArray(value.items) && value.items.every(summaryOk) && (value.next_cursor === null || typeof value.next_cursor === "string");
  function equal(a, b) {
    if (a === b) return true;
    if (Array.isArray(a) || Array.isArray(b)) return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((v, i) => equal(v, b[i]));
    if (!object(a) || !object(b)) return false;
    const keys = Object.keys(a);
    return keys.length === Object.keys(b).length && keys.every((key) => Object.hasOwn(b, key) && equal(a[key], b[key]));
  }
  function freeze(value) {
    if (value && typeof value === "object") { Object.values(value).forEach(freeze); Object.freeze(value); }
    return value;
  }
  let token = null, subject = null, ready = false, expired = false;
  let canRead = false, canWrite = false, booting = false, writing = false, listing = false, reading = false;
  let selected = null, originalRevision = null, savedSnapshot = null, dirty = false, editing = false;
  let selectionGeneration = 0, listGeneration = 0, summaries = [], cursor = null;
  let pendingCreate = null, uncertainUpdate = null, conflict = false, current = null;
  let programmatic = false, confirming = false, logoutBypass = false;
  let approvedDeparture = false, pageInactive = false;
  let activeKind = contextKind, parentContext = null;
  let typeGeneration = 0, typeListing = false, typeSummaries = [], typeCursor = null;
  const typeSummaryOk = (parentId, value) => identityOk("type", parentId, value) && value.state === "draft" && typeof value.name === "string" && revisionOk(value.revision) && dateOk(value.created_at) && dateOk(value.updated_at);
  const typePageOk = (parentId, value) => object(value) && Array.isArray(value.items) && value.items.every((item) => typeSummaryOk(parentId, item)) && (value.next_cursor === null || typeof value.next_cursor === "string");
  const edgeWhitespace = /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]$/u;
  const status = (text) => { $("status").textContent = text; };
  const listStatus = (text) => { $("list-status").textContent = text; };
  const protectedState = () => dirty || !!pendingCreate || !!uncertainUpdate || writing;
  const active = () => !pageInactive && !approvedDeparture && ready && !expired && canRead;
  const mutable = () => activeKind === contextKind && active() && canWrite && !writing && !reading && !pendingCreate && !uncertainUpdate;
  const editor = window.OcrCatalogFields.create($("fields"), {
    idPrefix: "context-common", readonly: true,
    onChange: () => { if (!programmatic && editing) { dirty = true; update(); } },
  });
  const inheritedEditor = window.OcrCatalogFields.create(document.getElementById("type-common-fields"), {
    idPrefix: "type-common", readonly: true,
  });
  function update() {
    if (pageInactive) return;
    const available = active();
    $("new").disabled = !available || !canWrite || writing || reading;
    $("refresh").disabled = !available || listing;
    $("more").hidden = !cursor;
    $("more").disabled = !available || listing;
    $("bootstrap").disabled = booting;
    $("login").hidden = !(expired || (ready && !canRead));
    $("session-logout").hidden = !(expired || (ready && !canRead));
    $("session-logout").disabled = writing || approvedDeparture;
    const readonly = !editing || !mutable();
    name.disabled = !editing; description.disabled = !editing;
    name.readOnly = readonly; description.readOnly = readonly;
    editor.setReadonly(readonly);
    $("save").hidden = activeKind === "type";
    $("save").disabled = readonly || (!dirty && !!selected);
    const typeBusy = !available || writing || reading;
    document.getElementById("type-section").hidden = !parentContext;
    document.getElementById("type-refresh").disabled = typeBusy || typeListing || !parentContext;
    document.getElementById("type-more").hidden = !typeCursor;
    document.getElementById("type-more").disabled = typeBusy || typeListing || !parentContext;
    document.getElementById("type-edit-context").hidden = activeKind !== "type";
    document.getElementById("type-edit-context").disabled = typeBusy || !parentContext;
    document.getElementById("type-edit-context").textContent = canWrite ? "Editar contexto" : "Ver contexto";
    document.querySelectorAll("#type-list button").forEach((button) => { button.disabled = typeBusy; });
    $("recover-create").hidden = !pendingCreate;
    $("recover-create").disabled = !available || !canWrite || writing || reading;
    $("consult").hidden = !(conflict || uncertainUpdate);
    $("consult").disabled = !available || writing || reading;
    $("use-current").hidden = !current || !(conflict || uncertainUpdate);
    $("use-current").disabled = !available || writing || reading;
    $("abandon").hidden = !pendingCreate;
    $("abandon").disabled = writing || reading || approvedDeparture;
    form.setAttribute("aria-busy", String(writing || reading));
    document.querySelectorAll("#context-list button").forEach((button) => { button.disabled = !available || writing || reading; });
  }
  class RequestError extends Error {
    constructor(statusCode, code, ambiguous = false) {
      super("Context request failed"); this.status = statusCode; this.code = code; this.ambiguous = ambiguous;
    }
  }
  function failMessage(error) {
    return ui.errorMessage(error.code, error.status, "Não foi possível concluir a operação.");
  }
  function invalidateSession() {
    expired = true;
    status("Sua sessão expirou. Seu rascunho foi preservado nesta página. Entre novamente para continuar.");
    update();
  }
  async function request(path, {method = "GET", body, key, valid} = {}) {
    if (pageInactive) throw new RequestError(0, null, true);
    const headers = {Authorization: `Bearer ${token}`, Accept: "application/json"};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (key) headers["Idempotency-Key"] = key;
    let response, data;
    try {
      const signal = AbortSignal.timeout(15000);
      response = await fetch(ui.apiPath(path), {method, headers, body, credentials: "omit", cache: "no-store", signal, redirect: "error"});
      try { data = await response.json(); }
      catch (_) { throw new RequestError(response.status, null, true); }
      signal.throwIfAborted();
      if (pageInactive) throw new RequestError(response.status, null, true);
    } catch (error) {
      if (response?.status === 401) invalidateSession();
      if (error instanceof RequestError) throw error;
      throw new RequestError(response?.status || 0, null, true);
    }
    if (response.status === 401) invalidateSession();
    if (!response.ok) {
      const code = typeof data?.error?.code === "string" ? data.error.code : null;
      throw new RequestError(response.status, code, response.status === 408 || response.status >= 500);
    }
    if ((method === "POST" && response.status !== 200 && response.status !== 201) || (method !== "POST" && response.status !== 200) || (valid && !valid(data))) throw new RequestError(response.status, null, true);
    return data;
  }
  function showError(error, prefix = "") {
    if (expired) { invalidateSession(); return; }
    status(prefix + failMessage(error));
  }
  function resetErrors() {
    for (const field of ["name", "description"]) { $(field).removeAttribute("aria-invalid"); $(`${field}-error`).hidden = true; $(`${field}-error`).textContent = ""; }
  }
  function clearComparison() {
    current = null; $("comparison").hidden = true; $("current").replaceChildren();
  }
  function setParent(record) {
    if (parentContext?.context_id !== record?.context_id) {
      ++typeGeneration; typeListing = false; typeSummaries = []; typeCursor = null;
      document.getElementById("type-list").replaceChildren();
      document.getElementById("type-list-status").textContent = "";
    }
    parentContext = record ? freeze(clone(record)) : null;
    document.getElementById("type-parent").textContent = record ? `${record.body.name} · Revisão ${record.revision}` : "";
  }
  function renderTypes() {
    document.getElementById("type-list").replaceChildren(...typeSummaries.map((item) => {
      const button = ui.el("button", {type: "button", className: "link-button entity__name", text: item.name, "aria-current": activeKind === "type" && selected === item.type_id ? "true" : null});
      button.addEventListener("click", () => selectType(item.type_id));
      return ui.el("li", {className: `entity${activeKind === "type" && selected === item.type_id ? " is-current" : ""}`}, [ui.el("div", {className: "entity__main"}, [button, ui.el("p", {className: "entity__meta", text: `Rascunho · Revisão ${item.revision} · Criado em ${ui.date(item.created_at)} · Atualizado em ${ui.date(item.updated_at)}`})])]);
    }));
    update();
  }
  async function loadTypes(append = false) {
    if (!active() || !parentContext || typeListing) return;
    const parentId = parentContext.context_id, generation = ++typeGeneration;
    const after = append ? typeCursor : null;
    const stillCurrent = () => active() && generation === typeGeneration && parentContext?.context_id === parentId;
    typeListing = true;
    document.getElementById("type-list-status").textContent = "Carregando tipos de documento…"; update();
    try {
      const query = new URLSearchParams({limit: "25"});
      if (after !== null) query.set("after", after);
      const page = await request(`${entityPath("type", parentId)}?${query}`, {valid: (value) => typePageOk(parentId, value)});
      if (!stillCurrent()) return;
      const next = append ? typeSummaries.slice() : [], seen = new Set(next.map((item) => item.type_id));
      for (const item of page.items) if (!seen.has(item.type_id)) { seen.add(item.type_id); next.push(item); }
      typeSummaries = next; typeCursor = page.next_cursor; renderTypes();
      document.getElementById("type-list-status").textContent = next.length ? `${next.length} tipo(s) de documento na lista.` : "Este contexto ainda não tem tipos de documento.";
    } catch (_) {
      if (stillCurrent()) document.getElementById("type-list-status").textContent = "Não foi possível carregar os tipos de documento. A lista anterior foi preservada. Tente novamente.";
    } finally {
      if (generation === typeGeneration) { typeListing = false; update(); }
    }
  }
  function contextView() {
    activeKind = contextKind;
    $("detail-title").textContent = "Detalhes do contexto";
    $("fields-title").textContent = "Campos comuns";
    document.getElementById("type-common-panel").hidden = true;
    document.getElementById("type-field-count").textContent = "";
    inheritedEditor.setValue([], {inherited: []});
  }
  function hydrate(record) {
    contextView(); setParent(record);
    programmatic = true;
    try {
      editor.setValue(clone(record.body[entities[contextKind].fieldsKey]), {inherited: []});
      name.value = record.body.name; description.value = record.body.description;
    } finally { programmatic = false; }
    selected = record[entities[contextKind].idKey]; originalRevision = record.revision; savedSnapshot = clone(record.body);
    editing = true; dirty = false; conflict = false; uncertainUpdate = null;
    resetErrors(); clearComparison();
    form.hidden = false; $("empty").hidden = true;
    $("revision").textContent = `Revisão ${originalRevision} · Atualizado em ${ui.date(record.updated_at)}`;
    renderList(); renderTypes(); update();
    void loadTypes();
  }
  async function selectType(id) {
    if (!active() || writing || reading || !parentContext || !idOk(id)) return;
    if (activeKind === "type" && selected === id) return;
    const parent = parentContext, before = selectionGeneration;
    if (!await confirmDeparture() || before !== selectionGeneration || parent !== parentContext || writing || reading || !active()) return;
    const generation = ++selectionGeneration, parentId = parent.context_id;
    reading = true; status("Carregando tipo de documento…"); update();
    try {
      const [authoritativeParent, record] = await Promise.all([
        request(entityPath(contextKind, null, parentId), {valid: (value) => recordOk(value) && value.context_id === parentId}),
        request(entityPath("type", parentId, id), {valid: (value) => entityRecordOk("type", parentId, value) && value.type_id === id}),
      ]);
      if (generation !== selectionGeneration || parentContext !== parent || !active()) return;
      abandonState(); setParent(authoritativeParent); activeKind = "type";
      programmatic = true;
      try {
        inheritedEditor.setValue(clone(authoritativeParent.body.common_fields), {inherited: clone(record.body.specific_fields)});
        editor.setValue(clone(record.body.specific_fields), {inherited: clone(authoritativeParent.body.common_fields)});
        name.value = record.body.name; description.value = record.body.description;
      } finally { programmatic = false; }
      selected = record.type_id; originalRevision = record.revision; savedSnapshot = clone(record.body);
      editing = true; dirty = false; resetErrors();
      form.hidden = false; $("empty").hidden = true;
      $("detail-title").textContent = "Detalhes do tipo";
      $("fields-title").textContent = "Campos específicos";
      $("revision").textContent = `Rascunho — tipo de documento · Revisão ${record.revision} · Contexto: ${authoritativeParent.body.name} · Revisão ${authoritativeParent.revision}`;
      document.getElementById("type-common-panel").hidden = false;
      const commonCount = authoritativeParent.body.common_fields.length, specificCount = record.body.specific_fields.length;
      document.getElementById("type-field-count").textContent = `${commonCount + specificCount} campo(s): ${commonCount} comuns herdados do contexto e ${specificCount} específicos do tipo.`;
      renderList(); renderTypes(); update();
      status("Tipo de documento carregado. Consulta somente para leitura.");
    } catch (_) {
      if (generation === selectionGeneration && active()) status("Não foi possível abrir o tipo de documento. O formulário anterior foi preservado. Selecione o tipo para tentar novamente.");
    } finally {
      if (generation === selectionGeneration) { reading = false; update(); }
    }
  }
  function renderList() {
    const listedContextId = activeKind === "type" ? parentContext?.context_id : selected;
    $("list").replaceChildren(...summaries.map((item) => {
      const button = ui.el("button", {type: "button", className: "link-button entity__name", text: item.name, "aria-current": item.context_id === listedContextId ? "true" : null});
      button.addEventListener("click", () => select(item.context_id));
      return ui.el("li", {className: `entity${item.context_id === listedContextId ? " is-current" : ""}`}, [ui.el("div", {className: "entity__main"}, [button, ui.el("p", {className: "entity__meta", text: `Revisão ${item.revision} · Criado em ${ui.date(item.created_at)} · Atualizado em ${ui.date(item.updated_at)}`})])]);
    }));
    update();
  }
  async function loadList(append = false) {
    if (!active() || listing) return;
    listing = true; const generation = ++listGeneration;
    const after = append ? cursor : null;
    listStatus(append ? "Carregando mais contextos…" : "Carregando contextos…"); update();
    try {
      const query = new URLSearchParams({limit: "25"});
      if (after !== null) query.set("after", after);
      const page = await request(`/api/v1/contexts?${query}`, {valid: pageOk});
      if (generation !== listGeneration || expired) return;
      const next = append ? summaries.slice() : [], seen = new Set(next.map((item) => item.context_id));
      for (const item of page.items) if (!seen.has(item.context_id)) { seen.add(item.context_id); next.push(item); }
      summaries = next; cursor = page.next_cursor;
      renderList(); listStatus(summaries.length ? `${summaries.length} contexto(s) na lista.` : "Você ainda não tem contextos.");
    } catch (error) {
      if (generation === listGeneration) listStatus(`${failMessage(error)} Use ${append ? "Carregar mais" : "Atualizar lista"} para tentar novamente. A lista anterior foi preservada.`);
    } finally { if (generation === listGeneration) { listing = false; update(); } }
  }
  async function confirmDeparture(force = false) {
    if (confirming || approvedDeparture || pageInactive) return false;
    if (!force && !protectedState()) return true;
    confirming = true;
    try {
      return await ui.confirmAction({title: "Sair deste rascunho?", body: pendingCreate || uncertainUpdate || writing ? "Existe uma operação pendente ou com resultado incerto. Ela pode ter sido gravada no servidor. Sair abandona a recuperação e as alterações locais; confira a lista antes de criar novamente." : "As alterações não salvas serão descartadas.", confirmLabel: "Descartar e continuar", danger: true});
    } finally { confirming = false; }
  }
  function abandonState() {
    ++selectionGeneration; reading = false;
    pendingCreate = null; uncertainUpdate = null; conflict = false; dirty = false; clearComparison();
  }
  async function select(id) {
    if (!active() || writing || reading || !idOk(id)) return;
    if (activeKind === contextKind && id === selected && !pendingCreate) return;
    const before = selectionGeneration;
    if (!await confirmDeparture() || before !== selectionGeneration || writing || reading || !active()) return;
    const generation = ++selectionGeneration;
    reading = true; status("Carregando contexto…"); update();
    try {
      const record = await request(entityPath(contextKind, null, id), {valid: (value) => recordOk(value) && value[entities[contextKind].idKey] === id});
      if (generation !== selectionGeneration || !active()) return;
      abandonState(); hydrate(record); status(canWrite ? "Contexto carregado." : "Contexto carregado. Você tem permissão somente para leitura.");
    } catch (error) { if (generation === selectionGeneration) { showError(error, "Não foi possível abrir o contexto. Seu formulário foi preservado. "); } }
    finally { if (generation === selectionGeneration) { reading = false; update(); } }
  }
  async function newContext() {
    if (!active() || !canWrite || writing || reading) return;
    const before = selectionGeneration;
    if (!await confirmDeparture() || before !== selectionGeneration || writing || !active()) return;
    abandonState(); contextView(); setParent(null); selected = null; originalRevision = null; savedSnapshot = null;
    programmatic = true;
    try { editor.setValue([], {inherited: []}); name.value = ""; description.value = ""; }
    finally { programmatic = false; }
    editing = true; dirty = false; resetErrors(); form.hidden = false; $("empty").hidden = true;
    $("revision").textContent = "Novo contexto";
    status("Preencha o nome e os campos comuns."); renderList(); update(); name.focus();
  }
  function inputChanged() {
    if (!programmatic && editing) { dirty = true; update(); }
  }
  name.addEventListener("input", inputChanged); description.addEventListener("input", inputChanged);
  function draft() {
    if (activeKind !== contextKind) return null;
    resetErrors(); let first = null;
    function invalid(field, text) { $(field).setAttribute("aria-invalid", "true"); $(`${field}-error`).textContent = text; $(`${field}-error`).hidden = false; first ||= $(field); }
    // Iterate code points, rejecting isolated UTF-16 surrogates, not valid pairs.
    const forbidden = (text, multiline) => Array.from(text).some((char) => {
      const code = char.codePointAt(0);
      return (code >= 0xd800 && code <= 0xdfff) || ((code < 32 || (code >= 127 && code <= 159)) && !(multiline && (code === 9 || code === 10)));
    });
    const length = Array.from(name.value).length;
    if (length < 1 || length > 120 || edgeWhitespace.test(name.value) || forbidden(name.value, false)) invalid("name", "Use de 1 a 120 caracteres, sem espaços nas bordas nem caracteres de controle.");
    if (Array.from(description.value).length > 4000 || forbidden(description.value, true)) invalid("description", "Use até 4.000 caracteres. Tabulação e quebra de linha são permitidas; outros controles não são.");
    let fields;
    try { fields = editor.getValue(); }
    catch (_) { if (!first) editor.focusFirstError(); status("Revise os campos comuns indicados."); }
    if (first) { first.focus(); status("Revise os dados indicados."); }
    if (first || !fields) return null;
    return clone(entityBody(contextKind, name.value, description.value, fields));
  }
  async function recoverCreated(intent) {
    const receipt = intent.receipt;
    const {kind, parentId} = intent.request;
    const id = receipt[entities[kind].idKey];
    const record = await request(entityPath(kind, parentId, id), {valid: (value) => entityRecordOk(kind, parentId, value) && value[entities[kind].idKey] === id});
    if (expired || pendingCreate !== intent) return false;
    hydrate(record); pendingCreate = null; update();
    status("Contexto criado. A versão atual foi recuperada.");
    return true;
  }
  async function sendCreate() {
    if (!pendingCreate || writing || reading || !active() || !canWrite) return;
    const intent = pendingCreate;
    writing = true; status(intent.receipt ? "Recuperando contexto criado…" : "Salvando contexto…"); update();
    let saved = false;
    try {
      if (!intent.receipt) {
        const {kind, parentId} = intent.request;
        const receipt = await request(intent.request.path, {method: "POST", body: intent.request.body, key: intent.request.key, valid: (value) => entityReceiptOk(kind, parentId, value)});
        intent.receipt = receipt;
      }
      saved = await recoverCreated(intent);
    } catch (error) {
      if (error.ambiguous) intent.everAmbiguous = true;
      const entity = entities[intent.request.kind];
      const preAdmission = !intent.receipt && !intent.everAmbiguous && ((error.status === 422 && [entity.invalidBody, "validation_error"].includes(error.code)) || (error.status === 413 && error.code === "request_too_large") || (error.status === 429 && error.code === entity.quotaExceeded));
      if (preAdmission) { pendingCreate = null; showError(error, "O contexto não foi admitido. Corrija o rascunho e salve novamente. "); }
      else showError(error, intent.receipt ? "A criação foi recebida, mas a versão atual ainda não foi recuperada. Use Tentar novamente. " : "O pedido foi preservado e o formulário permanece bloqueado. Tentar novamente reutiliza exatamente o mesmo pedido. ");
    } finally { writing = false; update(); }
    if (saved && active()) await loadList();
  }
  async function save(event) {
    event.preventDefault();
    if (!mutable() || !editing) return;
    const body = draft(); if (!body) return;
    if (!selected) {
      let key;
      try { key = crypto.randomUUID(); }
      catch (_) { status("Não foi possível preparar o pedido. Use um navegador com conexão segura."); return; }
      pendingCreate = {request: freeze({kind: contextKind, parentId: null, path: entityPath(contextKind, null), body: JSON.stringify(body), key}), everAmbiguous: false, receipt: null};
      update(); await sendCreate(); return;
    }
    const intent = freeze({kind: contextKind, parentId: null, path: entityPath(contextKind, null, selected), id: selected, expectedRevision: originalRevision, body: clone(body), wire: JSON.stringify({body, expected_revision: originalRevision})});
    writing = true; status("Salvando contexto…"); update();
    let saved = false;
    try {
      const record = await request(intent.path, {method: "PUT", body: intent.wire, valid: (value) => entityRecordOk(intent.kind, intent.parentId, value) && value[entities[intent.kind].idKey] === intent.id && value.revision === intent.expectedRevision + 1 && equal(value.body, intent.body)});
      if (expired) return;
      hydrate(record); status("Contexto salvo."); saved = true;
    } catch (error) {
      if (error.ambiguous) { uncertainUpdate = intent; conflict = false; clearComparison(); showError(error, "O resultado da gravação é incerto. Novas gravações estão bloqueadas. Consulte a versão atual para verificar. "); }
      else if (error.status === 409 && error.code === entities[intent.kind].revisionConflict) { conflict = true; clearComparison(); showError(error, "Seu rascunho e sua revisão original foram preservados. Consulte a versão atual antes de decidir. "); }
      else showError(error);
    } finally { writing = false; update(); }
    if (saved && active()) await loadList();
  }
  // Read-only comparison, rendered as text and semantic lists, never as editable JSON.
  function valueNode(value) {
    if (Array.isArray(value)) return ui.el("ol", {}, value.map((item) => ui.el("li", {}, [valueNode(item)])));
    if (object(value)) {
      const list = ui.el("dl", {className: "detail-list"});
      for (const [key, item] of Object.entries(value)) list.append(ui.el("div", {}, [ui.el("dt", {text: key}), ui.el("dd", {}, [valueNode(item)])]));
      return list;
    }
    return ui.el("span", {text: value === null ? "Não definido" : value === true ? "Sim" : value === false ? "Não" : String(value)});
  }
  function showCurrent(record) {
    current = clone(record); $("comparison").hidden = false;
    $("current").replaceChildren(ui.el("p", {text: `Revisão ${record.revision} · ${record.body.name}`}), ui.el("p", {className: "catalog-description", text: record.body.description || "Sem descrição."}), ui.el("h3", {text: "Campos comuns"}), valueNode(record.body.common_fields));
  }
  async function consult() {
    if (!active() || writing || reading || !(uncertainUpdate || conflict) || !selected) return;
    const generation = selectionGeneration, intent = uncertainUpdate;
    const kind = intent ? intent.kind : contextKind;
    const parentId = intent ? intent.parentId : null;
    const id = intent ? intent.id : selected;
    const path = intent ? intent.path : entityPath(kind, parentId, id);
    reading = true; status("Consultando versão atual…"); update();
    try {
      const record = await request(path, {valid: (value) => entityRecordOk(kind, parentId, value) && value[entities[kind].idKey] === id});
      if (generation !== selectionGeneration || expired) return;
      if (intent && record.revision === intent.expectedRevision + 1 && equal(record.body, intent.body)) { hydrate(record); status("A gravação foi confirmada."); }
      else { showCurrent(record); status(intent ? "A consulta não confirmou a gravação esperada. Compare a versão atual com seu rascunho. Para resolver, escolha explicitamente Usar versão atual ou abandone este rascunho ao navegar." : "Versão atual consultada. Seu rascunho e sua revisão original permanecem no formulário."); }
    } catch (error) { if (generation === selectionGeneration) showError(error, "Não foi possível consultar. A recuperação permanece disponível. "); }
    finally { if (generation === selectionGeneration) { reading = false; update(); } }
  }
  async function useCurrent() {
    if (!current || !active() || writing || reading || confirming) return;
    const candidate = current, generation = selectionGeneration;
    confirming = true;
    let accepted;
    try { accepted = await ui.confirmAction({title: "Usar versão atual?", body: "Seu rascunho será descartado e substituído pela versão consultada. Uma operação com resultado incerto pode já ter sido gravada. Nenhuma gravação será repetida.", confirmLabel: "Usar versão atual", danger: true}); }
    finally { confirming = false; }
    if (accepted && generation === selectionGeneration && candidate === current && !writing && !reading && active()) { hydrate(candidate); status("Versão consultada carregada. Novas alterações usarão esta revisão."); }
  }
  async function abandonCreate() {
    if (!pendingCreate || writing || reading || approvedDeparture || pageInactive) return;
    const generation = selectionGeneration;
    if (!await confirmDeparture(true) || generation !== selectionGeneration || writing || reading || approvedDeparture || pageInactive) return;
    abandonState(); dirty = editing;
    status("Recuperação abandonada. O pedido pode ter sido gravado. Consulte a lista antes de criar novamente."); update();
  }
  async function bootstrap() {
    if (booting || protectedState() || editing || ready || approvedDeparture || pageInactive) return;
    booting = true; $("bootstrap").hidden = true; status("Carregando sessão…"); update();
    try {
      if (!token) {
        const session = await ui.browserToken();
        if (pageInactive) return;
        token = session.access_token; subject = session.subject;
      }
      const me = await request("/api/v1/me", {valid: (value) => object(value) && Array.isArray(value.scopes) && value.scopes.every((scope) => typeof scope === "string")});
      if (expired) return;
      canRead = me.scopes.includes("contexts:read"); canWrite = canRead && me.scopes.includes("contexts:write"); ready = true;
      if (!canRead) { status("Sua sessão não tem a permissão contexts:read para consultar contextos. Saia e entre novamente para atualizar uma sessão antiga. As permissões continuam sendo definidas pelo servidor."); listStatus("Sem permissão para consultar contextos."); }
      else { status(canWrite ? "Selecione um contexto ou crie um novo." : "Você tem permissão somente para leitura. Para editar, são necessárias contexts:read e contexts:write."); await loadList(); }
    } catch (error) {
      if (!expired) { status(error instanceof RequestError ? failMessage(error) : "Não foi possível recuperar a sessão. Verifique a conexão e tente novamente."); $("bootstrap").hidden = false; listStatus("A sessão não foi carregada."); }
    } finally { booting = false; update(); }
  }
  document.getElementById("type-refresh").addEventListener("click", () => loadTypes());
  document.getElementById("type-more").addEventListener("click", () => loadTypes(true));
  document.getElementById("type-edit-context").addEventListener("click", () => {
    if (parentContext && activeKind === "type") void select(parentContext.context_id);
  });
  $("new").addEventListener("click", newContext);
  $("refresh").addEventListener("click", () => loadList());
  $("more").addEventListener("click", () => loadList(true));
  $("bootstrap").addEventListener("click", bootstrap);
  $("recover-create").addEventListener("click", sendCreate);
  $("consult").addEventListener("click", consult);
  $("use-current").addEventListener("click", useCurrent);
  $("abandon").addEventListener("click", abandonCreate);
  $("session-logout").addEventListener("click", () => document.getElementById("logout-button").click());
  form.addEventListener("submit", save);
  window.addEventListener("beforeunload", (event) => { if (!approvedDeparture && !pageInactive && protectedState()) { event.preventDefault(); event.returnValue = ""; } });
  function resetDeparture() {
    if (pageInactive) return;
    approvedDeparture = false;
    update();
  }
  document.addEventListener("ocr:logout-failed", resetDeparture);
  // Capture precedes the existing shell logout listener. Re-dispatch only once after consent.
  document.addEventListener("click", async (event) => {
    const target = event.target.closest?.("#logout-button, a[href]");
    if (!target || pageInactive) return;
    const logout = target.id === "logout-button";
    if (logout && logoutBypass) { logoutBypass = false; return; }
    let destination;
    if (!logout) {
      const browsingTarget = target.getAttribute("target") ?? document.querySelector("base[target]")?.getAttribute("target") ?? "";
      if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || target.hasAttribute("download") || (browsingTarget && browsingTarget.toLowerCase() !== "_self")) return;
      try { destination = new URL(target.href, location.href); } catch (_) { return; }
      if (!["http:", "https:"].includes(destination.protocol)) return;
      if (destination.origin === location.origin && destination.pathname === location.pathname && destination.search === location.search && (destination.hash || location.hash || target.getAttribute("href")?.startsWith("#"))) return;
    }
    if (!protectedState() && !confirming && !approvedDeparture) return;
    event.preventDefault(); event.stopImmediatePropagation();
    if (confirming || approvedDeparture) return;
    const generation = selectionGeneration;
    if (!await confirmDeparture() || generation !== selectionGeneration || pageInactive || approvedDeparture) return;
    approvedDeparture = true;
    update();
    if (logout) {
      // The shell owns the single POST and reports failure so protection can resume.
      logoutBypass = true;
      try { target.click(); }
      catch (_) { resetDeparture(); }
      finally { logoutBypass = false; }
    } else {
      try { window.location.assign(destination.href); }
      catch (_) { resetDeparture(); }
    }
  }, true);
  window.addEventListener("pagehide", () => {
    pageInactive = true;
    token = null; subject = null; editor.destroy(); inheritedEditor.destroy();
  }, {once: true});
  window.addEventListener("pageshow", (event) => {
    approvedDeparture = false;
    if (event.persisted) {
      // Keep the old closure inert, including its unload prompt, until fresh bootstrap.
      pageInactive = true;
      window.location.reload();
      return;
    }
    if (!pageInactive) update();
  });
  update(); bootstrap();
})();
