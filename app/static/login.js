(() => {
  "use strict";
  const ui = window.OcrUi;
  const form = document.getElementById("login-form");
  const error = document.getElementById("login-error");
  const button = document.getElementById("login-btn");

  function showError(message, field) {
    error.textContent = message;
    error.hidden = false;
    if (field) field.focus();
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    const email = document.getElementById("email");
    const password = document.getElementById("password");
    if (!email.value.trim()) return showError("Informe o e-mail.", email);
    if (!password.value) return showError("Informe a senha.", password);
    button.disabled = true;
    button.textContent = "Entrando…";
    try {
      let response;
      try {
        response = await fetch("/auth/login", {method: "POST", credentials: "same-origin",
          headers: {"Content-Type": "application/json"}, body: JSON.stringify({email: email.value.trim(), password: password.value})});
      } catch (_) { throw new Error(ui.NETWORK); }
      if (!response.ok) {
        let detail;
        try { detail = (await response.json()).detail; } catch (_) { /* generic */ }
        throw new Error(ui.legacyMessage(detail, "Não foi possível entrar."));
      }
      location.assign("/");
    } catch (e) {
      password.value = "";
      showError(e.message, password);
      button.disabled = false;
      button.textContent = "Entrar";
    }
  });
})();
