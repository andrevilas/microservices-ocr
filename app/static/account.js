(() => {
  "use strict";
  const ui = window.OcrUi;
  const $ = (id) => document.getElementById(id);
  const status = $("account-status"), error = $("account-error"), save = $("account-save-btn");

  function show(node, message) {
    status.hidden = true; error.hidden = true;
    node.textContent = message; node.hidden = false;
  }

  $("account-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = $("account-name").value.trim(), email = $("account-email").value.trim();
    const password = $("account-password").value, confirmation = $("account-password-confirm").value;
    const current = $("account-current-password").value;
    if (!name || !email) return show(error, "Informe nome e e-mail de acesso.");
    if (password !== confirmation) { show(error, "A confirmação não confere com a nova senha."); $("account-password-confirm").focus(); return; }
    if ((password || email !== $("profile-email").textContent) && !current) {
      show(error, "Informe a senha atual para trocar o e-mail ou a senha."); $("account-current-password").focus(); return;
    }
    save.disabled = true; save.textContent = "Salvando…";
    try {
      let response;
      try {
        response = await fetch("/api/me", {method: "PUT", credentials: "same-origin", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({name, email, current_password: current, password})});
      } catch (_) { throw new Error(ui.NETWORK); }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(ui.legacyMessage(data.detail, "Não foi possível atualizar a conta."));
      $("account-name").value = data.name; $("account-email").value = data.email;
      $("profile-name").textContent = data.name; $("profile-email").textContent = data.email;
      for (const id of ["account-current-password", "account-password", "account-password-confirm"]) $(id).value = "";
      show(status, "Conta atualizada.");
    } catch (e) {
      show(error, e.message);
    } finally {
      save.disabled = false; save.textContent = "Salvar alterações";
    }
  });
})();
