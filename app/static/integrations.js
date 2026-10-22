/* Service identity administration over the existing /api/v1/service-principals contract.
   The one-time secret lives only in the dialog input while it is open; it is never stored, logged or copied automatically. */
(() => {
  "use strict";
  const ui = window.OcrUi;
  const {el} = ui;
  const $ = (id) => document.getElementById(id);
  const BASE = "/api/v1/service-principals";
  const SCOPES = {"jobs:read": "Consultar jobs", "artifacts:read": "Ler resultados", "jobs:submit": "Enviar documentos",
    "jobs:cancel": "Cancelar jobs", "artifacts:delete": "Excluir arquivos", "contexts:read": "Consultar contextos",
    "contexts:write": "Editar rascunhos de contextos", "metadata:read": "Consultar metadados", "metadata:write": "Salvar rascunhos de metadados"};
  const PAGE = 100;
  // Actions stay disabled until the session and first service page load (ready) and while any operation runs (busy),
  // so a click is never swallowed by the busy guard in run(). Text inputs stay editable; typed values are never reset on failure.
  const ACTIONS = ["create-service-btn", "issue-btn", "services-more", "credentials-more", "disable-service-btn"];
  const BUSY_REGIONS = ["create-service-form", "service-list", "service-detail"];
  let token = null, busy = false, ready = false;
  let services = [], servicesDone = false, current = null, credentials = [], credentialsDone = false;

  function syncControls() {
    const off = busy || !ready;
    for (const id of ACTIONS) $(id).disabled = off;
    document.querySelectorAll("#service-list [data-service-id], #credential-list [data-revoke-id]").forEach((button) => { button.disabled = off; });
    for (const id of BUSY_REGIONS) $(id).setAttribute("aria-busy", String(busy));
  }

  function show(kind, message) {
    $("integrations-status").hidden = true; $("integrations-error").hidden = true;
    const node = $(kind === "error" ? "integrations-error" : "integrations-status");
    node.textContent = message; node.hidden = false;
  }
  async function api(path, options = {}) {
    if (!token) throw new Error("Recupere a sessão usando Tentar novamente antes de continuar.");
    const target = ui.apiPath(path);
    let response;
    try {
      response = await fetch(target, {...options, credentials: "omit", cache: "no-store", headers: {...options.headers, Authorization: `Bearer ${token}`}});
    } catch (_) { throw new Error(ui.NETWORK); }
    if (response.status === 401) { location.assign("/login"); throw new Error("Sessão encerrada."); }
    if (!response.ok) {
      let error;
      try { error = (await response.json()).error; } catch (_) { /* sanitized fallback */ }
      throw Object.assign(new Error(ui.errorMessage(error?.code, response.status, error?.message)), {status: response.status});
    }
    return response.json();
  }
  const send = (path, method, body) => api(path, {method, headers: body ? {"Content-Type": "application/json"} : {}, body: body ? JSON.stringify(body) : undefined});
  const run = (operation) => async (...args) => {
    if (busy) return;                           // last-resort guard against duplicate requests; controls are disabled meanwhile
    busy = true;
    const origin = document.activeElement;
    syncControls();
    try { await operation(...args); } catch (e) { show("error", e.message); } finally {
      busy = false;
      syncControls();
      // Disabling the focused control drops focus to <body>; give it back when nothing else claimed it.
      const active = document.activeElement;
      if ((!active || active === document.body) && origin?.isConnected && !origin.disabled && origin !== document.body) origin.focus();
    }
  };

  // ------------------------------------------------------------ services
  async function loadServices(reset) {
    if (reset) { services = []; servicesDone = false; }
    const after = services.length ? services[services.length - 1].id : "";
    const page = await api(`${BASE}?${new URLSearchParams({limit: String(PAGE), after})}`);
    services.push(...page);
    servicesDone = page.length < PAGE;
    renderServices();
  }
  function serviceState(s) { return s.disabled_at ? `Desativado em ${ui.date(s.disabled_at)}` : "Ativo"; }
  function renderServices() {
    $("services-empty").hidden = services.length > 0;
    $("services-more").hidden = servicesDone;
    $("service-list").replaceChildren(...services.map((s) => el("li", {className: `entity${current?.id === s.id ? " is-current" : ""}`}, [
      el("div", {className: "entity__main"}, [
        el("span", {className: "entity__name", text: s.name}),
        el("span", {className: "entity__meta", text: `Criado em ${ui.date(s.created_at)} · ${serviceState(s)}`})]),
      el("button", {type: "button", className: "button button-small", dataset: {serviceId: s.id}, "aria-pressed": String(current?.id === s.id)},
        ["Gerenciar", el("span", {className: "visually-hidden", text: ` ${s.name}`})]),
    ])));
    syncControls();
  }
  // Resolve the clicked target before run() disables it, then pass only the id.
  const manageService = run(async (id) => {
    const service = services.find((s) => s.id === id);
    if (service) await openService(service);
  });
  $("service-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-service-id]");
    if (button && !button.disabled) manageService(button.dataset.serviceId);
  });
  $("services-more").onclick = run(() => loadServices(false));

  const onSubmit = (form, handler) => { const go = run(handler); $(form).addEventListener("submit", (event) => { event.preventDefault(); go(); }); };

  onSubmit("create-service-form", async () => {
    const name = $("service-name").value.trim();
    if (!name) { show("error", "Informe o nome do serviço."); $("service-name").focus(); return; }
    const created = await send(BASE, "POST", {name});
    $("create-service-form").reset();
    $("create-service-disclosure").open = false;
    show("success", `Serviço “${created.name}” criado. Emita uma credencial para usá-lo.`);
    await loadServices(true);
    const fresh = services.find((s) => s.id === created.id) || created;
    await openService(fresh);
  });

  // ------------------------------------------------------------ one service
  async function openService(service) {
    current = service;
    $("service-detail").hidden = false;
    $("service-title").textContent = service.name;
    renderServiceMeta();
    renderServices();
    await loadCredentials(true);
    $("service-title").focus();
  }
  function renderServiceMeta() {
    const s = current;
    $("service-meta").replaceChildren(...[["Identificador", s.id, "mono"], ["Criado em", ui.date(s.created_at)], ["Criado por", s.created_by, "mono"], ["Situação", serviceState(s)]]
      .map(([term, value, kind]) => el("div", {}, [el("dt", {text: term}), el("dd", {className: kind || "", text: value})])));
    const disabled = !!s.disabled_at;
    $("issue-disclosure").hidden = disabled;
    $("disable-zone").hidden = disabled;
  }
  $("service-close").onclick = () => {
    const id = current?.id;
    current = null;
    $("service-detail").hidden = true;
    renderServices();
    $("service-list").querySelector(`[data-service-id="${CSS.escape(id || "")}"]`)?.focus();
  };

  async function loadCredentials(reset) {
    if (reset) { credentials = []; credentialsDone = false; }
    const after = credentials.length ? credentials[credentials.length - 1].id : "";
    const page = await api(`${BASE}/${encodeURIComponent(current.id)}/credentials?${new URLSearchParams({limit: String(PAGE), after})}`);
    credentials.push(...page);
    credentialsDone = page.length < PAGE;
    renderCredentials();
  }
  function credentialState(c) {
    if (c.revoked_at) return {text: `Revogada em ${ui.date(c.revoked_at)}`, active: false};
    if (c.expires_at * 1000 <= Date.now()) return {text: `Expirada em ${ui.date(c.expires_at)}`, active: false};
    return {text: `Ativa até ${ui.date(c.expires_at)}`, active: true};
  }
  function renderCredentials() {
    $("credentials-empty").hidden = credentials.length > 0;
    $("credentials-more").hidden = credentialsDone;
    const serviceDisabled = !!current?.disabled_at;
    $("credential-list").replaceChildren(...credentials.map((c) => {
      const state = credentialState(c);
      return el("li", {className: `entity${state.active ? "" : " is-inactive"}`}, [
        el("div", {className: "entity__main"}, [
          el("span", {className: "entity__name", text: c.name}),
          el("span", {className: "entity__meta", text: `${state.text} · último uso: ${c.last_used_at ? ui.date(c.last_used_at) : "nunca"} · criada em ${ui.date(c.created_at)}`}),
          el("span", {className: "entity__tags"}, (c.scopes || []).map((scope) => el("span", {className: "tag", title: scope, text: SCOPES[scope] || scope})))]),
        !c.revoked_at && !serviceDisabled
          ? el("button", {type: "button", className: "button button-small button-danger", dataset: {revokeId: c.id}}, ["Revogar", el("span", {className: "visually-hidden", text: ` ${c.name}`})])
          : null,
      ]);
    }));
    syncControls();
  }
  $("credentials-more").onclick = run(() => loadCredentials(false));
  const revokeCredential = run(async (id) => {
    const credential = credentials.find((c) => c.id === id);
    const ok = await ui.confirmAction({title: "Revogar esta credencial?", danger: true,
      body: `“${credential?.name || ""}” deixará de autenticar na próxima requisição, inclusive em conexões MCP abertas. Pedidos já em andamento não são interrompidos. Não é possível desfazer.`,
      confirmLabel: "Revogar credencial", cancelLabel: "Voltar"});
    if (!ok) return;
    await send(`${BASE}/${encodeURIComponent(current.id)}/credentials/${encodeURIComponent(id)}`, "DELETE");
    show("success", "Credencial revogada.");
    await loadCredentials(true);
  });
  $("credential-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-revoke-id]");
    if (button && !button.disabled) revokeCredential(button.dataset.revokeId);
  });

  onSubmit("issue-form", async () => {
    const name = $("credential-name").value.trim();
    const scopes = [...document.querySelectorAll('#issue-form input[name="scope"]:checked')].map((i) => i.value);
    if (!name) { show("error", "Informe o nome da credencial."); $("credential-name").focus(); return; }
    if (!scopes.length) { show("error", "Escolha pelo menos uma permissão."); return; }
    if (scopes.includes("artifacts:read") && !scopes.includes("jobs:read")) { show("error", "Ler resultados também exige consultar jobs."); return; }
    const issued = await send(`${BASE}/${encodeURIComponent(current.id)}/credentials`, "POST",
      {name, scopes, expires_in_seconds: Number($("credential-expiry").value)});
    $("issue-form").reset();
    $("issue-disclosure").open = false;
    revealOnce(issued.token);
    await loadCredentials(true);
  });

  $("disable-service-btn").onclick = run(async () => {
    const ok = await ui.confirmAction({title: `Desativar “${current.name}”?`, danger: true,
      body: "Todas as credenciais deste serviço deixam de funcionar permanentemente. Não há reativação. Os documentos do serviço não são excluídos.",
      confirmLabel: "Desativar serviço", cancelLabel: "Voltar"});
    if (!ok) return;
    current = await send(`${BASE}/${encodeURIComponent(current.id)}/disable`, "POST");
    services = services.map((s) => s.id === current.id ? current : s);
    renderServiceMeta(); renderServices(); renderCredentials();
    show("success", "Serviço desativado.");
    $("service-title").focus();
  });

  // ------------------------------------------------------------ one-time secret
  const dialog = $("secret-dialog"), secret = $("secret-value");
  function revealOnce(value) {
    secret.value = value;                       // property only: not serialized into the HTML
    $("secret-copy-status").textContent = "";
    dialog.showModal();
    secret.focus(); secret.select();
  }
  function forget() {
    secret.value = "";
    secret.blur();
    $("secret-copy-status").textContent = "";
  }
  dialog.addEventListener("cancel", (event) => {
    // Escape would discard a secret that cannot be recovered; require the explicit button.
    event.preventDefault();
    $("secret-copy-status").textContent = "Use “Concluído” depois de guardar o segredo.";
  });
  dialog.addEventListener("close", forget);
  $("secret-done").onclick = () => { forget(); dialog.close(); show("success", "Credencial emitida. O segredo não pode ser exibido novamente."); $("service-title").focus(); };
  $("secret-copy").onclick = async () => {
    try { await navigator.clipboard.writeText(secret.value); $("secret-copy-status").textContent = "Copiado. Cole no gerenciador de segredos e conclua."; }
    catch (_) { secret.select(); $("secret-copy-status").textContent = "Não foi possível copiar automaticamente; o texto está selecionado para cópia manual."; }
  };

  const initialize = run(async () => {
    $("integrations-retry").disabled = true;
    $("integrations-error").hidden = true;
    $("integrations-loading").hidden = false;
    try {
      token = (await ui.browserToken()).access_token;
      await loadServices(true);
      ready = true;
      $("integrations-retry").hidden = true;
    } catch (e) {
      $("integrations-retry").hidden = false;
      show("error", e.message);
    } finally {
      $("integrations-loading").hidden = true;
      $("integrations-retry").disabled = false;
    }
  });
  $("integrations-retry").onclick = initialize;
  initialize();
})();
