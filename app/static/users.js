(() => {
  "use strict";
  const ui = window.OcrUi;
  const {el} = ui;
  const $ = (id) => document.getElementById(id);
  const status = $("users-status"), error = $("users-error"), tbody = $("users-tbody");
  let users = [], editTrigger = null;

  function show(node, message) {
    status.hidden = true; error.hidden = true;
    node.textContent = message; node.hidden = false;
  }
  async function request(path, options = {}) {
    let response;
    try { response = await fetch(path, {credentials: "same-origin", ...options}); }
    catch (_) { throw new Error(ui.NETWORK); }
    if (response.status === 401) { location.assign("/login"); throw new Error("Sessão encerrada."); }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(ui.legacyMessage(data.detail, "Operação não concluída."));
    return data;
  }
  const roleText = (role) => role === "admin" ? "Administrador" : "Usuário";

  function render() {
    const term = $("users-search").value.trim().toLocaleLowerCase("pt-BR");
    const list = users.filter((u) => `${u.name} ${u.email}`.toLocaleLowerCase("pt-BR").includes(term));
    if (!list.length) {
      tbody.replaceChildren(el("tr", {}, el("td", {colSpan: 5, className: "empty-cell", text: users.length ? "Nenhum usuário corresponde à busca." : "Nenhum usuário cadastrado."})));
      return;
    }
    tbody.replaceChildren(...list.map((u) => el("tr", {}, [
      el("td", {dataset: {label: "Nome"}}, el("strong", {text: u.name || "—"})),
      el("td", {dataset: {label: "E-mail"}, text: u.email || "—"}),
      el("td", {dataset: {label: "Papel"}}, el("span", {className: `badge badge-role-${u.role === "admin" ? "admin" : "user"}`, text: roleText(u.role)})),
      el("td", {dataset: {label: "Criado em"}, text: ui.date(u.created_at)}),
      el("td", {className: "cell-actions"}, el("button", {type: "button", className: "button button-small", dataset: {editUserId: String(u.id)}},
        ["Editar", el("span", {className: "visually-hidden", text: ` ${u.name || u.email}`})])),
    ])));
  }
  async function load() {
    try { users = await request("/api/users"); render(); }
    catch (e) { tbody.replaceChildren(el("tr", {}, el("td", {colSpan: 5, className: "empty-cell", text: "Não foi possível carregar os usuários."}))); show(error, e.message); }
  }

  $("users-search").addEventListener("input", render);

  $("create-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = $("new-name").value.trim(), email = $("new-email").value.trim();
    const password = $("new-password").value, confirmation = $("new-password-confirm").value;
    if (!name || !email || !password) return show(error, "Preencha nome, e-mail e senha inicial.");
    if (password !== confirmation) { show(error, "A confirmação não confere com a senha."); $("new-password-confirm").focus(); return; }
    const button = $("create-btn");
    button.disabled = true; button.textContent = "Criando…";
    try {
      await request("/api/users", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name, email, password})});
      $("create-form").reset();
      show(status, `Usuário ${name} criado.`);
      await load();
    } catch (e) { show(error, e.message); }
    finally { button.disabled = false; button.textContent = "Criar usuário"; }
  });

  function openEdit(user, trigger) {
    editTrigger = trigger;
    $("edit-id").value = user.id;
    $("edit-name").value = user.name || "";
    $("edit-email").value = user.email || "";
    $("edit-password").value = ""; $("edit-password-confirm").value = "";
    $("edit-title").textContent = `Editar ${user.name || user.email}`;
    $("edit-card").hidden = false;
    $("edit-card").scrollIntoView({block: "start", behavior: "smooth"});
    $("edit-title").focus({preventScroll: true});
  }
  function closeEdit() {
    $("edit-form").reset();
    $("edit-card").hidden = true;
    if (editTrigger?.isConnected) editTrigger.focus();
    editTrigger = null;
  }
  tbody.addEventListener("click", (event) => {
    const button = event.target.closest("[data-edit-user-id]");
    if (!button) return;
    const user = users.find((u) => String(u.id) === button.dataset.editUserId);
    if (user) openEdit(user, button);
  });
  $("edit-cancel-btn").addEventListener("click", closeEdit);
  $("edit-card").addEventListener("keydown", (event) => { if (event.key === "Escape") closeEdit(); });

  $("edit-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const id = $("edit-id").value, name = $("edit-name").value.trim(), email = $("edit-email").value.trim();
    const password = $("edit-password").value, confirmation = $("edit-password-confirm").value;
    if (!name || !email) return show(error, "Informe nome e e-mail de acesso.");
    if (password !== confirmation) { show(error, "A confirmação não confere com a nova senha."); $("edit-password-confirm").focus(); return; }
    if (password) {
      const ok = await ui.confirmAction({title: "Redefinir a senha?", body: `A senha de ${name} será substituída. Entregue a nova senha por um canal seguro. Sessões já abertas continuam válidas até expirar.`,
        confirmLabel: "Redefinir senha", cancelLabel: "Voltar"});
      if (!ok) return;
    }
    const button = $("edit-save-btn");
    button.disabled = true; button.textContent = "Salvando…";
    try {
      await request(`/api/users/${encodeURIComponent(id)}`, {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name, email, password})});
      show(status, `Usuário ${name} atualizado.`);
      closeEdit();
      await load();
    } catch (e) { show(error, e.message); }
    finally { button.disabled = false; button.textContent = "Salvar alterações"; }
  });

  load();
})();
