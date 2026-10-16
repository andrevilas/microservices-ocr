/* Shared page shell: logout, confirmation dialog, Portuguese messages and formatting. No secrets are stored. */
(() => {
  "use strict";

  // API v1 error codes (error.code). Unknown codes fall back to the server message.
  const ERRORS = {
    context_revision_conflict: "O contexto foi alterado por outra operação. Consulte a versão atual.",
    context_revision_limit: "Este contexto atingiu o limite de revisões.",
    context_quota_exceeded: "O limite de contextos foi atingido.",
    invalid_context_body: "Revise o nome, a descrição e os campos do contexto; o conteúdo também pode exceder o limite permitido.",
    invalid_context_query: "A consulta de contextos é inválida. Atualize a lista.",
    context_not_found: "Contexto não encontrado.",
    authentication_required: "Sua sessão expirou. Entre novamente.",
    insufficient_scope: "Esta identidade não tem permissão para a operação.",
    admin_required: "Operação restrita a administradores.",
    job_not_found: "Job não encontrado.",
    batch_not_found: "Lote não encontrado.",
    invalid_limit: "Limite de paginação inválido.",
    invalid_period: "Período inválido: a data inicial deve ser anterior à final.",
    invalid_filename_filter: "Filtro de nome inválido.",
    invalid_cursor: "A paginação ficou desatualizada; atualize o histórico.",
    result_not_ready: "O resultado ainda não está pronto.",
    result_unavailable: "O resultado não foi registrado ou não está mais disponível.",
    result_integrity_error: "O resultado armazenado falhou na verificação de integridade.",
    storage_unavailable: "Armazenamento indisponível no momento. Tente novamente.",
    invalid_text_range: "Página ou trecho inválido.",
    artifact_unavailable: "O PDF de resultado não está disponível.",
    artifacts_expired: "Os arquivos deste job expiraram.",
    artifacts_deleted: "Os arquivos deste job foram excluídos.",
    job_not_terminal: "Aguarde o job terminar.",
    input_expired: "O PDF original não está mais disponível; envie o arquivo novamente.",
    retry_options_required: "Este job antigo exige escolher as opções para reprocessar.",
    job_quota_exceeded: "Limite de jobs pendentes atingido; nada foi admitido. Aguarde a conclusão de alguns jobs.",
    idempotency_conflict: "Conflito com um pedido anterior. Atualize o histórico antes de tentar novamente.",
    invalid_idempotency_key: "Chave de repetição inválida.",
    upload_not_ready: "Um dos envios não terminou; nada foi admitido.",
    batch_limit_exceeded: "Quantidade de arquivos acima do limite por envio.",
    batch_too_large: "O tamanho total do envio excede o limite.",
    queue_unavailable: "A fila está indisponível. O pedido foi registrado; repita a ação para retomá-lo.",
    unsupported_option_combination: "Combinação de opções não suportada.",
    ocr_unavailable: "O motor de OCR está indisponível neste servidor.",
    language_unavailable: "Idioma de OCR indisponível.",
    fallback_unavailable: "O OCR alternativo está indisponível.",
    identity_not_found: "Serviço ou credencial não encontrado.",
    invalid_identity_request: "Dados inválidos para o serviço ou a credencial.",
    export_not_found: "Pedido de ZIP não encontrado.",
    export_expired: "O ZIP expirou; gere outro.",
    export_source_removed: "Os arquivos do ZIP não estão mais disponíveis.",
    export_unavailable: "O ZIP não está disponível.",
    export_not_ready: "O ZIP ainda está em preparação.",
    export_queue_full: "Há muitos ZIPs em preparação; aguarde e tente novamente.",
    export_too_large: "Os resultados escolhidos excedem o tamanho máximo do ZIP.",
    export_quota_exceeded: "Cota de armazenamento de ZIPs esgotada; tente mais tarde.",
    export_failed: "A preparação do ZIP falhou.",
    unsupported_media_type: "Tipo de conteúdo não suportado.",
    request_too_large: "Pedido grande demais.",
    headers_too_large: "Cabeçalhos do envio grandes demais.",
    upload_timeout: "O envio demorou demais; tente novamente.",
    upload_content_conflict: "O conteúdo difere do que já foi enviado para esta reserva.",
    file_too_large: "Arquivo acima do tamanho máximo.",
    invalid_multipart: "Envio de arquivo malformado.",
    invalid_filename: "Nome inválido: use um nome .pdf sem barras nem dois-pontos.",
    pdf_validation_limit: "O PDF excedeu os limites de validação.",
    validation_unavailable: "Validação de PDF indisponível no momento.",
    upload_not_found: "Reserva de envio não encontrada.",
    upload_expired: "A reserva de envio expirou; envie novamente.",
    storage_quota_exceeded: "Cota de armazenamento de envios esgotada.",
    upload_quota_exceeded: "Muitas reservas de envio ativas; aguarde.",
    upload_busy: "Limite de envios simultâneos atingido; tente em instantes.",
    upload_in_progress: "Este arquivo já está sendo enviado.",
    invalid_checksum: "Checksum inválido.",
    size_mismatch: "O tamanho recebido difere do informado.",
    checksum_mismatch: "O conteúdo recebido não confere com o checksum.",
    invalid_pdf: "O arquivo não é um PDF válido.",
    page_limit_exceeded: "O PDF tem mais páginas que o limite.",
    page_pixels_exceeded: "Uma página excede o limite de resolução.",
    validation_error: "Dados inválidos na solicitação.",
    processing_failed: "O processamento falhou.",
    job_canceled: "Job cancelado.",
    cancellation_recovery_required: "O cancelamento precisa de recuperação pelo operador.",
    process_termination_failed: "O processo não pôde ser interrompido; o cancelamento segue pendente.",
  };

  // Legacy /auth and /api/users routes answer with English or Portuguese `detail` strings.
  const LEGACY = {
    "Invalid email or password.": "E-mail ou senha inválidos.",
    "Email and password are required.": "Informe e-mail e senha.",
    "A user with this email already exists.": "Já existe um usuário com este e-mail.",
    "Fields name, email and password are required.": "Preencha nome, e-mail e senha.",
    "Admin access required.": "Operação restrita a administradores.",
    "Not authenticated.": "Sua sessão expirou. Entre novamente.",
  };

  const NETWORK = "Sem resposta do servidor. Verifique a conexão e tente novamente.";

  function errorMessage(code, status, fallback) {
    const known = code && ERRORS[code];
    const text = known || fallback || "Operação não concluída.";
    return `${text} (${code || status || "sem código"})`;
  }

  function legacyMessage(detail, fallback) {
    if (typeof detail === "string" && detail) return LEGACY[detail] || detail;
    return fallback;
  }

  const dateFormat = new Intl.DateTimeFormat("pt-BR", {dateStyle: "short", timeStyle: "short"});
  function date(value) {
    if (value == null || value === "") return "—";
    const parsed = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    return Number.isNaN(parsed.getTime()) ? "—" : dateFormat.format(parsed);
  }
  function time(value) {
    return value ? value.toLocaleTimeString("pt-BR", {hour: "2-digit", minute: "2-digit", second: "2-digit"}) : "—";
  }
  function bytes(value) {
    if (value == null) return "—";
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${Math.round(value / 1024)} KB`;
    return `${(value / (1024 * 1024)).toLocaleString("pt-BR", {maximumFractionDigits: 1})} MB`;
  }
  const count = (n, singular, plural = singular + "s") => `${n} ${n === 1 ? singular : plural}`;
  function duration(seconds) {
    if (!seconds) return "—";
    if (seconds % 86400 === 0) return count(seconds / 86400, "dia");
    if (seconds % 3600 === 0) return count(seconds / 3600, "hora");
    if (seconds % 60 === 0) return count(seconds / 60, "minuto");
    return count(seconds, "segundo");
  }

  /* Element helper: properties and text only; never assigns HTML from data. */
  function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value == null || value === false) continue;
      if (key === "text") node.textContent = value;
      else if (key === "className") node.className = value;
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key.startsWith("aria-") || key === "role" || key === "for") node.setAttribute(key, value === true ? "true" : value);
      else node[key] = value;
    }
    for (const child of [].concat(children)) {
      if (child == null || child === false) continue;
      node.append(child instanceof Node ? child : String(child));
    }
    return node;
  }

  /* Accessible confirmation through the native <dialog>; resolves true only on explicit confirmation. */
  function confirmAction({title, body, confirmLabel = "Confirmar", cancelLabel = "Voltar", danger = false}) {
    const dialog = document.getElementById("confirm-dialog");
    if (!dialog || typeof dialog.showModal !== "function") return Promise.resolve(window.confirm(`${title}\n\n${body}`));
    const opener = document.activeElement;
    document.getElementById("confirm-title").textContent = title;
    document.getElementById("confirm-body").textContent = body;
    const accept = document.getElementById("confirm-accept"), cancel = document.getElementById("confirm-cancel");
    accept.textContent = confirmLabel;
    cancel.textContent = cancelLabel;
    accept.className = danger ? "button button-danger-solid" : "button button-primary";
    dialog.returnValue = "";
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => {
        resolve(dialog.returnValue === "confirm");
        if (opener && opener.isConnected && typeof opener.focus === "function") opener.focus({preventScroll: true});
      }, {once: true});
      dialog.showModal();
      (danger ? cancel : accept).focus();
    });
  }

  /* Human browser session: Bearer kept only in memory by the caller. */
  async function browserToken() {
    let response;
    try { response = await fetch("/auth/browser-session", {method: "POST", credentials: "same-origin", headers: {"X-OCR-Browser": "1"}, signal: AbortSignal.timeout(15000)}); }
    catch (_) { throw new Error(NETWORK); }
    if (response.status === 401) { location.assign("/login"); throw new Error("Sessão encerrada."); }
    if (!response.ok) throw new Error("Não foi possível recuperar a sessão do navegador.");
    try {
      const session = await response.json();
      if (!session.access_token || !session.subject) throw new Error();
      return session;
    } catch (_) { throw new Error("A resposta da sessão ficou incompleta. Tente novamente."); }
  }

  /* Only same-origin /api/v1 paths after URL normalization. */
  function apiPath(path) {
    const url = new URL(path, location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith("/api/v1/")) throw new Error("Caminho de operação inválido.");
    return url.pathname + url.search;
  }

  // Closes open menus on outside click and with Escape, returning focus to the menu button.
  document.addEventListener("click", (event) => {
    document.querySelectorAll("details.menu[open]").forEach((menu) => {
      if (!menu.contains(event.target)) menu.open = false;
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const menu = event.target.closest?.("details.menu[open]") || document.querySelector("details.menu[open]");
    if (menu) {
      menu.open = false;
      menu.querySelector("summary")?.focus();
      event.stopPropagation();
    }
  }, true);

  const logout = document.getElementById("logout-button");
  if (logout) {
    logout.addEventListener("click", async () => {
      logout.disabled = true;
      try {
        const response = await fetch("/auth/logout", {method: "POST", credentials: "same-origin"});
        if (!response.ok) throw new Error();
        location.assign("/login");
      } catch (_) {
        logout.disabled = false;
        logout.textContent = "Falha ao sair — tentar novamente";
        document.dispatchEvent(new CustomEvent("ocr:logout-failed"));
      }
    });
  }

  window.OcrUi = {errorMessage, legacyMessage, NETWORK, date, time, bytes, count, duration, el, confirmAction, browserToken, apiPath};
})();
