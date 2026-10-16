/* The backend owns job history. Only display preferences are stored locally. */
(() => {
  "use strict";
  const ui = window.OcrUi;
  const {el} = ui;
  const $ = (id) => document.getElementById(id);

  const ACTIVE = new Set(["queued", "processing", "cancel_requested"]);
  const TERMINAL = new Set(["completed", "failed", "canceled"]);
  const STATUS = {queued: "Na fila", processing: "Processando", cancel_requested: "Cancelamento solicitado",
    completed: "Concluído", failed: "Falhou", canceled: "Cancelado"};
  const PRIORITY = {failed: 0, processing: 1, cancel_requested: 2, queued: 3, completed: 4, canceled: 5};
  const FILTERS = {all: () => true, active: (j) => ACTIVE.has(j.status), completed: (j) => j.status === "completed",
    failed: (j) => j.status === "failed", canceled: (j) => j.status === "canceled"};
  const OPTION_LABELS = {extract_text: "Extrair texto existente", preserve_existing_text: "OCR preservando texto existente",
    eng: "Inglês", por: "Português", "eng+por": "Português e inglês", pdf_searchable: "PDF pesquisável",
    pdfa: "PDF/A (conformidade não validada)", none: "Sem compactação", balanced: "Compactação equilibrada",
    disabled: "Desativado", if_low_quality: "Usar se a qualidade for baixa"};
  const WARNINGS = {
    quality_is_heuristic_not_accuracy: "A qualidade é uma heurística; não mede precisão.",
    pdfa_conformance_not_validated: "A conformidade PDF/A não é validada.",
    primary_ocr_unavailable: "OCR principal indisponível; foi usado apenas o texto existente.",
    fallback_ocr_unavailable: "OCR alternativo indisponível.",
    fallback_disabled: "OCR alternativo desativado nas opções.",
    no_text_extracted_blankness_not_determined: "Nenhum texto extraído; não foi possível determinar se o documento está em branco.",
    text_extraction_only: "Somente o texto existente no PDF foi extraído.",
    tesseract_version_not_captured: "A versão do Tesseract não foi registrada.",
    poppler_version_not_captured: "A versão do Poppler não foi registrada.",
    insufficient_text: "Pouco texto reconhecido.",
    overlay_geometry_approximate_text_may_be_clipped: "Posição do texto aproximada; trechos podem aparecer cortados.",
    probe_is_not_ocr_homologation: "A verificação dos motores não é uma homologação do OCR.",
    defaults_may_be_unavailable: "Os padrões do servidor podem não estar disponíveis.",
    extract_text_does_not_recognize_scans: "Extrair texto não reconhece páginas digitalizadas.",
  };
  const REASONS = {missing_or_failed_probe: "não encontrado ou falhou na verificação", unsupported_engine_version: "versão não suportada",
    ocrmypdf_16_4_to_16_x_required: "requer OCRmyPDF 16.4 a 16.x", tesseract_unavailable: "Tesseract indisponível",
    ghostscript_unavailable: "Ghostscript indisponível", no_supported_language_installed: "nenhum idioma suportado instalado",
    dependencies_or_local_pt_en_models_unavailable: "dependências ou modelos locais pt/en ausentes"};
  const SOURCES = {pdf_text: "texto existente do PDF", ocrmypdf: "OCR (OCRmyPDF)", easyocr: "OCR alternativo (EasyOCR)"};
  const OUTCOMES = {canceled: "cancelado(s)", requested: "com cancelamento solicitado", already_terminal: "já encerrado(s)", not_found: "não encontrado(s)"};
  const EXCLUDED = {job_not_completed: "não concluído", artifact_unavailable: "arquivos indisponíveis"};
  // Definitive rejections: the server persisted nothing for the request. 429 codes on these
  // endpoints (job_quota_exceeded, export_queue_full, export_quota_exceeded) are raised before
  // persistence. Network failures, 408 and 5xx stay ambiguous and keep the same key and body.
  const DEFINITIVE = new Set([400, 403, 404, 409, 410, 413, 415, 422, 429]);
  const definitive = (e) => DEFINITIVE.has(e.status);
  const PAGE = 50, FULL_SYNC_MS = 60000, POLL_MS = 5000;

  const cards = new Map();   // server jobs by job_id, enriched with session-only path/size/batch
  const staged = new Map();  // local files not yet admitted
  const rows = new Map();    // job_id -> row element (keyed rendering keeps focus)
  const hidden = new Set(), selected = new Set();
  let token = null, subject = null, capabilities = null, metrics = null;
  let busy = false, refreshing = false, lastSync = null, lastFull = 0, syncError = null;
  let pendingBatch = null, exportPending = null, resultView = null, progressText = "";
  let detailId = null, detailTrigger = null, filter = "all", limit = PAGE;
  const batches = new Map(); let batchesLoaded = false;

  // ---------------------------------------------------------------- messages
  function notify(message, tone = "info") {
    $("job-state").textContent = message;
    $("notice").dataset.tone = tone;
    $("notice").setAttribute("role", tone === "error" ? "alert" : "status");
    $("notice").hidden = false;
    $("detail-notice").hidden = !(detailId && tone === "error");
    $("detail-notice").textContent = detailId && tone === "error" ? message : "";
  }
  $("notice-close").onclick = () => { $("notice").hidden = true; $("page-title").focus?.(); };
  const fail = (message) => Object.assign(new Error(message), {status: undefined});

  // ---------------------------------------------------------------- API
  async function api(path, options = {}) {
    const target = ui.apiPath(path);
    let response;
    try {
      response = await fetch(target, {...options, credentials: "omit", headers: {...options.headers, Authorization: `Bearer ${token}`}});
    } catch (_) {
      throw Object.assign(new Error(ui.NETWORK), {status: 0});
    }
    if (response.status === 401) {
      token = null;
      notify("Sua sessão expirou. Redirecionando para o login…", "error");
      location.assign("/login");
      throw Object.assign(new Error("Sessão encerrada."), {status: 401});
    }
    if (!response.ok) {
      let error;
      try { error = (await response.json()).error; } catch (_) { /* sanitized fallback */ }
      throw Object.assign(new Error(ui.errorMessage(error?.code, response.status, error?.message)), {status: response.status, code: error?.code});
    }
    return response;
  }
  const json = async (path, options) => {
    const response = await api(path, options);
    try { return await response.json(); }
    catch (_) {
      // A mutation may already be committed even if its receipt is truncated.
      throw Object.assign(new Error("A resposta do servidor ficou incompleta. Retome a operação para consultar o mesmo pedido."), {status: 0});
    }
  };
  const post = (path, body, key) => json(path, {method: "POST", headers: {"Content-Type": "application/json", ...(key ? {"Idempotency-Key": key} : {})}, body: JSON.stringify(body)});

  const act = (operation) => async (...args) => {
    if (busy) { notify("Aguarde a operação em andamento terminar.", "info"); return; }
    busy = true; document.body.classList.add("is-busy"); $("conteudo").setAttribute("aria-busy", "true"); render();
    try { await operation(...args); } catch (e) { notify(e.message, "error"); }
    finally { busy = false; document.body.classList.remove("is-busy"); $("conteudo").removeAttribute("aria-busy"); render(); }
  };

  // ---------------------------------------------------------------- derived state
  const gone = (j) => ["expired", "deleted"].includes(j.retention?.state);
  const available = (j) => j.status === "completed" && !gone(j) && !!j.download_url;
  const values = () => [...cards.values()].filter((j) => !hidden.has(j.job_id));
  const activeCount = () => [...cards.values()].filter((j) => ACTIVE.has(j.status)).length;
  const batchJobs = () => { const b = batches.get($("batch-filter").value); return b ? new Set(b.items.map((i) => i.job_id)) : null; };
  function dayBound(value, end) {
    if (!value) return null;
    const [y, m, d] = value.split("-").map(Number);
    return end ? new Date(y, m - 1, d, 23, 59, 59, 999) : new Date(y, m - 1, d);
  }
  function visible() {
    const search = $("search-input").value.trim().toLocaleLowerCase("pt-BR");
    const from = dayBound($("created-from").value, false), to = dayBound($("created-to").value, true);
    const quality = $("quality-filter").value, inBatch = batchJobs();
    const result = values().filter((j) => FILTERS[filter](j)
      && `${j.filename} ${j.relativePath || ""}`.toLocaleLowerCase("pt-BR").includes(search)
      && (!quality || j.quality === quality)
      && (!inBatch || inBatch.has(j.job_id))
      && (!from || new Date(j.created_at) >= from) && (!to || new Date(j.created_at) <= to));
    const order = $("sort-select").value;
    const byCreated = (a, b) => (a.created_at || "").localeCompare(b.created_at || "") || a.job_id.localeCompare(b.job_id);
    const bySize = (a, b) => a.size == null ? (b.size == null ? byCreated(b, a) : 1)
      : b.size == null ? -1 : (a.size - b.size) * (order === "size_desc" ? -1 : 1) || byCreated(b, a);
    return result.sort((a, b) => order === "name" ? a.filename.localeCompare(b.filename, "pt-BR") || byCreated(b, a)
      : order === "oldest" ? byCreated(a, b)
      : order.startsWith("size_") ? bySize(a, b)
      : order === "status" ? (PRIORITY[a.status] ?? 9) - (PRIORITY[b.status] ?? 9) || byCreated(b, a)
      : byCreated(b, a));
  }
  function allowance() {
    if (!capabilities) return 0;
    const limits = capabilities.limits || {};
    const perBatch = limits.batch_max_items || 25;
    const pending = limits.pending_jobs_per_owner;
    return Math.max(0, Math.min(perBatch, pending == null ? perBatch : pending - activeCount()));
  }
  const sendable = () => [...staged.values()].filter((s) => s.state !== "rejected").map((s) => s.id);
  const selectedJobs = () => [...selected].map((id) => cards.get(id)).filter(Boolean);

  // ---------------------------------------------------------------- formatting
  const optionText = (value) => OPTION_LABELS[value] || value;
  function formatOptions(o) {
    if (!o) return "—";
    if (o.processing_mode === "extract_text") return optionText("extract_text");
    const langs = (o.languages || []).length ? [...o.languages].sort().join("+") : null;
    return [optionText(o.processing_mode), langs && optionText(langs), o.output_format && optionText(o.output_format),
      o.compression_profile && optionText(o.compression_profile),
      o.fallback_policy && `OCR alternativo: ${optionText(o.fallback_policy).toLocaleLowerCase("pt-BR")}`].filter(Boolean).join(" · ");
  }
  function retentionText(j) {
    const r = j.retention;
    if (!r) return ACTIVE.has(j.status) ? "Em processamento" : "—";
    const purge = r.purge_pending ? " · remoção física pendente" : "";
    if (r.state === "deleted") return `Arquivos excluídos${r.unavailable_at ? " em " + ui.date(r.unavailable_at) : ""}${purge}`;
    if (r.state === "expired") return `Arquivos expirados${r.unavailable_at ? " em " + ui.date(r.unavailable_at) : ""}${purge}`;
    if (r.state === "active") return "Retidos enquanto o job está em andamento";
    return r.expires_at ? `Disponíveis até ${ui.date(r.expires_at)}` : "Disponíveis";
  }
  function errorText(j) {
    if (!j.error_code && !j.error) return null;
    const known = j.error_code ? ui.errorMessage(j.error_code, null, null) : null;
    return [known, j.error].filter(Boolean).join(" — ");
  }
  const qualityText = (q) => q === "HIGH" ? "Alta" : q === "LOW" ? "Baixa" : "—";

  // ---------------------------------------------------------------- rendering: history rows
  function createRow(id) {
    const check = el("input", {type: "checkbox", className: "row-check", dataset: {select: id}});
    const title = el("button", {type: "button", className: "row-title link-button", dataset: {action: "detail", id}});
    const row = el("article", {className: "file-card", tabIndex: -1, dataset: {cardId: id}}, [
      el("div", {className: "row-select"}, check),
      el("div", {className: "row-main"}, [el("h3", {className: "row-heading"}, title), el("p", {className: "row-path"}), el("p", {className: "row-meta"})]),
      el("div", {className: "row-state"}, [el("span", {className: "badge"}),
        el("div", {className: "meter", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100"}, el("span", {className: "meter__fill"}))]),
      el("div", {className: "row-actions"}),
    ]);
    return row;
  }
  function actionButton(action, id, label, {className = "button button-small", context, hint} = {}) {
    const b = el("button", {type: "button", className, dataset: {action, id}}, [label]);
    if (context) b.append(el("span", {className: "visually-hidden", text: ` — ${context}`}));
    if (hint) b.title = hint;
    return b;
  }
  function rowActions(j) {
    const id = j.job_id, name = j.filename, items = [];
    const terminalKept = TERMINAL.has(j.status) && !gone(j);
    if (available(j)) items.push(actionButton("download", id, "Baixar PDF", {className: "button button-small button-primary-soft", context: name}));
    else if (ACTIVE.has(j.status)) items.push(actionButton("cancel", id, "Cancelar", {context: name}));
    else if (terminalKept) items.push(actionButton("retry", id, "Reprocessar", {context: name}));
    const menuItems = [];
    if (available(j) && terminalKept) menuItems.push(actionButton("retry", id, j.options_requested ? "Reprocessar com as mesmas opções" : "Reprocessar", {className: "menu__item"}));
    if (terminalKept && j.options_requested) menuItems.push(actionButton("retry-current", id, "Reprocessar com as opções atuais", {className: "menu__item"}));
    menuItems.push(actionButton("hide", id, "Ocultar desta lista", {className: "menu__item"}));
    if (terminalKept) menuItems.push(actionButton("delete", id, "Excluir arquivos…", {className: "menu__item menu__item-danger"}));
    items.push(el("details", {className: "menu menu-end row-menu"}, [
      el("summary", {className: "button button-small button-quiet"}, ["Mais ações", el("span", {className: "visually-hidden", text: ` — ${name}`})]),
      el("div", {className: "menu__panel"}, menuItems)]));
    return items;
  }
  function updateRow(row, j) {
    const id = j.job_id;
    row.className = `file-card status-${j.status}${selected.has(id) ? " selected" : ""}`;
    const check = row.querySelector(".row-check");
    check.checked = selected.has(id);
    check.setAttribute("aria-label", `Selecionar ${j.filename}`);
    row.querySelector(".row-title").textContent = j.filename;
    row.querySelector(".row-path").textContent = j.relativePath || "";
    row.querySelector(".row-path").hidden = !j.relativePath;
    const meta = [`Criado em ${ui.date(j.created_at)}`];
    if (j.size != null) meta.push(ui.bytes(j.size));
    if (TERMINAL.has(j.status)) meta.push(retentionText(j));
    row.querySelector(".row-meta").textContent = meta.join(" · ");
    row.querySelector(".badge").textContent = STATUS[j.status] || j.status;
    const meter = row.querySelector(".meter"), percent = Math.min(100, Math.max(0, Number(j.progress_percent || 0)));
    meter.hidden = !ACTIVE.has(j.status);
    meter.setAttribute("aria-valuenow", String(percent));
    meter.setAttribute("aria-label", `Progresso de ${j.filename}`);
    meter.firstChild.style.width = `${percent}%`;
    const signature = [j.status, available(j), gone(j), !!j.options_requested].join("|");
    if (row.dataset.signature !== signature) {
      row.dataset.signature = signature;
      row.querySelector(".row-actions").replaceChildren(...rowActions(j));
    }
  }

  function renderList() {
    const grid = $("cards-grid"), all = values(), list = visible(), shown = list.slice(0, limit);
    const focused = document.activeElement, focusedRow = focused?.closest?.(".file-card");
    for (const [id, row] of rows) if (!cards.has(id)) { row.remove(); rows.delete(id); }
    const wanted = new Set(shown.map((j) => j.job_id));
    for (const [id, row] of rows) if (!wanted.has(id) && row.isConnected) row.remove();
    let ref = grid.firstElementChild;
    for (const j of shown) {
      let row = rows.get(j.job_id);
      if (!row) { row = createRow(j.job_id); rows.set(j.job_id, row); }
      updateRow(row, j);
      if (row !== ref) grid.insertBefore(row, ref); else ref = ref.nextElementSibling;
    }
    // Moving or removing a node drops focus; restore it, or fall back to the list heading.
    if (focused && focused !== document.activeElement) {
      if (focused.isConnected) focused.focus({preventScroll: true});
      else if (focusedRow) (focusedRow.isConnected ? focusedRow : $("history-title")).focus({preventScroll: true});
    }
    grid.setAttribute("aria-busy", busy ? "true" : "false");
    $("empty-state").hidden = cards.size > 0;
    $("filter-empty").hidden = !(cards.size > 0 && list.length === 0);
    $("filter-empty-text").textContent = all.length === 0 ? `Todos os ${cards.size} documento(s) estão ocultos nesta aba. Use Visualização › Mostrar ocultos.` : "Nenhum documento corresponde aos filtros.";
    const hiddenNote = hidden.size ? ` · ${hidden.size} oculto(s) nesta aba` : "";
    $("results-summary").textContent = `Mostrando ${shown.length} de ${ui.count(list.length, "documento")}${list.length !== all.length ? ` (${all.length} na lista)` : ""}${hiddenNote}.`;
    $("show-more-button").hidden = list.length <= limit;
    $("show-more-button").textContent = `Mostrar mais (${list.length - limit} restantes)`;
    $("select-visible-button").hidden = shown.length === 0;
  }

  function renderSelection() {
    for (const id of [...selected]) if (!cards.has(id) || hidden.has(id)) selected.delete(id);
    const jobs = selectedJobs(), zip = jobs.filter(available), cancelable = jobs.filter((j) => ACTIVE.has(j.status));
    const maxZip = capabilities?.limits?.export_max_items || 25, maxCancel = capabilities?.limits?.cancel_max_items || 25;
    $("selection-bar").hidden = jobs.length === 0 && !exportPending;
    $("selection-count").textContent = ui.count(jobs.length, "selecionado");
    $("download-all-button").textContent = exportPending ? "Retomar ZIP pendente" : `Baixar ZIP (${zip.length})`;
    $("download-all-button").disabled = !exportPending && (zip.length === 0 || zip.length > maxZip);
    $("clear-queue-button").textContent = `Cancelar selecionados (${cancelable.length})`;
    $("clear-queue-button").disabled = cancelable.length === 0 || cancelable.length > maxCancel;
    $("discard-export-button").hidden = !exportPending;
    const hints = [];
    if (zip.length > maxZip) hints.push(`O ZIP aceita até ${maxZip} resultados.`);
    if (cancelable.length > maxCancel) hints.push(`O cancelamento aceita até ${maxCancel} jobs por vez.`);
    if (jobs.length && !zip.length && !exportPending) hints.push("O ZIP inclui apenas resultados concluídos e ainda disponíveis.");
    $("selection-hint").textContent = hints.join(" ");
  }

  function renderStaging() {
    const list = $("staging-list"), items = [...staged.values()];
    $("staging").hidden = items.length === 0;
    $("staging-title").textContent = `Arquivos a enviar (${items.length})`;
    const locked = new Set(pendingBatch?.ids || []);
    const uncertain = !!pendingBatch?.body, sending = busy && !!pendingBatch && !uncertain;
    // Rebuild only when the list changed, so polling never steals focus from "Remover".
    const key = JSON.stringify(items.map((s) => [s.id, s.state, s.error, locked.has(s.id)]));
    if (list.dataset.key !== key) {
      const focusedId = list.contains(document.activeElement) ? document.activeElement.dataset.stagedRemove : null;
      list.dataset.key = key;
      list.replaceChildren(...items.map((s) => {
        const state = s.state === "rejected" ? `Rejeitado: ${s.error}` : s.state === "uploading" ? "Enviando…"
          : s.state === "uploaded" ? "Enviado; aguardando admissão" : "Pronto para envio";
        const remove = el("button", {type: "button", className: "button button-quiet button-small", dataset: {stagedRemove: s.id},
          disabled: locked.has(s.id)}, ["Remover", el("span", {className: "visually-hidden", text: ` ${s.filename}`})]);
        if (locked.has(s.id)) remove.title = "Faz parte de um envio em andamento ou pendente; retome ou descarte o envio antes.";
        return el("li", {className: `staged-file${s.state === "rejected" ? " is-rejected" : ""}`, dataset: {stagedId: s.id}}, [
          el("div", {className: "staged-file__main"}, [el("span", {className: "staged-file__name", text: s.filename}),
            el("span", {className: "staged-file__meta", text: [s.relativePath, ui.bytes(s.size)].filter(Boolean).join(" · ")})]),
          el("span", {className: "staged-file__state", text: state}), remove]);
      }));
      if (focusedId) (list.querySelector(`[data-staged-remove="${CSS.escape(focusedId)}"]`) || $("staging-title")).focus?.();
    }
    const n = sendable().length, allowed = allowance();
    const queue = $("queue-button");
    if (sending) queue.textContent = "Enviando…";
    else if (uncertain) queue.textContent = "Retomar envio anterior";
    else if (!n) queue.textContent = "Enviar PDFs";
    else if (n > allowed && allowed > 0) queue.textContent = `Enviar ${allowed} de ${n} PDF(s)`;
    else queue.textContent = `Enviar ${n} PDF(s)`;
    queue.disabled = !capabilities || (!uncertain && !sending && (n === 0 || allowed === 0));
    const limits = capabilities?.limits || {};
    let hint = "";
    if (sending) hint = progressText;
    else if (uncertain) hint = "O envio anterior não teve confirmação do servidor. Retome para repetir exatamente o mesmo pedido, sem duplicar jobs.";
    else if (n && allowed === 0) hint = `Limite de ${limits.pending_jobs_per_owner} jobs pendentes atingido. Aguarde a conclusão de jobs em andamento.`;
    else if (n > allowed) hint = `Cada envio admite até ${allowed} arquivo(s) agora (até ${limits.batch_max_items} por envio e ${limits.pending_jobs_per_owner} jobs pendentes). Os demais permanecem na lista.`;
    $("queue-hint").textContent = hint;
    $("discard-pending-button").hidden = !uncertain;
    $("clear-staging-button").disabled = !!pendingBatch;
    $("reset-workspace-button").disabled = !!pendingBatch;
  }

  function renderSummary() {
    const parts = [];
    if (metrics) {
      const by = metrics.by_status || {};
      const running = (by.queued || 0) + (by.processing || 0) + (by.cancel_requested || 0);
      parts.push(`${ui.count(metrics.total_jobs, "documento")} no servidor`);
      if (running) parts.push(`${running} em andamento`);
      if (by.completed) parts.push(ui.count(by.completed, "concluído"));
      if (by.failed) parts.push(`${by.failed} com falha`);
      if (by.canceled) parts.push(ui.count(by.canceled, "cancelado"));
    }
    $("status-summary").textContent = parts.length ? parts.join(" · ") : (lastSync
      ? `${ui.count(cards.size, "documento")} no histórico carregado · métricas indisponíveis`
      : "Carregando o histórico…");
    $("sync-status").textContent = syncError
      ? `Falha ao atualizar: ${syncError}${lastSync ? ` Exibindo dados de ${ui.time(lastSync)}.` : ""}`
      : lastSync ? `Atualizado às ${ui.time(lastSync)}` : "";
    $("sync-status").dataset.tone = syncError ? "error" : "";
    $("show-hidden-button").textContent = `Mostrar ocultos (${hidden.size})`;
    $("show-hidden-button").disabled = hidden.size === 0;
    $("remove-completed-button").disabled = !values().some((j) => TERMINAL.has(j.status));
    const advanced = [$("created-from").value, $("created-to").value, $("quality-filter").value, $("batch-filter").value].filter(Boolean).length;
    $("more-filters-label").textContent = advanced ? `Mais filtros (${advanced})` : "Mais filtros";
  }

  function render() {
    renderList();
    renderSelection();
    renderStaging();
    renderSummary();
    if (detailId) renderDetail();
  }

  // ---------------------------------------------------------------- detail panel
  function detailRows(j) {
    const rowsOut = [["Criado em", ui.date(j.created_at)], ["Atualizado em", ui.date(j.updated_at)]];
    if (j.relativePath) rowsOut.push(["Caminho na pasta", j.relativePath]);
    if (j.size != null) rowsOut.push(["Tamanho do original", ui.bytes(j.size)]);
    if (j.batch_id) rowsOut.push(["Lote", j.batch_id]);
    rowsOut.push(["Arquivos", retentionText(j)]);
    rowsOut.push(["Qualidade", j.quality ? `${qualityText(j.quality)} (heurística; não mede precisão)` : "—"]);
    rowsOut.push(["Opções solicitadas", formatOptions(j.options_requested)]);
    if (j.options_effective) rowsOut.push(["Opções efetivas", formatOptions(j.options_effective)]);
    if (j.parent_job_id) rowsOut.push(["Reprocessado de", j.parent_job_id, "parent"]);
    const error = errorText(j);
    if (error) rowsOut.push(["Erro", error]);
    rowsOut.push(["Identificador", j.job_id, "mono"]);
    return rowsOut;
  }
  function renderDetail() {
    const j = cards.get(detailId);
    if (!j) { closeDetail(); return; }
    $("detail-title").textContent = j.filename;
    const status = $("detail-status");
    const statusKey = `${j.status}|${j.progress_percent}`;
    if (status.dataset.key !== statusKey) {
      status.dataset.key = statusKey;
      const percent = Math.min(100, Math.max(0, Number(j.progress_percent || 0)));
      status.replaceChildren(...[el("span", {className: `badge badge-${j.status}`, text: STATUS[j.status] || j.status}),
        ACTIVE.has(j.status) ? el("div", {className: "meter", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100",
          "aria-valuenow": String(percent), "aria-label": "Progresso"}, el("span", {className: "meter__fill", style: `width:${percent}%`})) : null].filter(Boolean));
    }
    const listData = detailRows(j), listKey = JSON.stringify(listData);
    const dl = $("detail-list");
    if (dl.dataset.key !== listKey) {
      const hadFocus = dl.contains(document.activeElement);
      dl.dataset.key = listKey;
      dl.replaceChildren(...listData.map(([term, value, kind]) => {
        let content = value;
        if (kind === "parent" && cards.has(value)) content = el("button", {type: "button", className: "link-button mono", dataset: {action: "detail", id: value}, text: value});
        return el("div", {}, [el("dt", {text: term}), el("dd", {className: kind === "mono" || kind === "parent" ? "mono" : ""}, content)]);
      }));
      if (hadFocus) (dl.querySelector("button") || $("detail-title")).focus({preventScroll: true});
    }
    const signature = [j.job_id, j.status, available(j), gone(j), !!j.options_requested].join("|");
    const actionsBox = $("detail-actions");
    if (actionsBox.dataset.signature !== signature) {
      actionsBox.dataset.signature = signature;
      const id = j.job_id, terminalKept = TERMINAL.has(j.status) && !gone(j), items = [];
      if (available(j)) items.push(actionButton("download", id, "Baixar PDF", {className: "button button-primary"}));
      if (ACTIVE.has(j.status)) items.push(actionButton("cancel", id, "Cancelar job"));
      if (terminalKept) {
        items.push(actionButton("retry", id, j.options_requested ? "Reprocessar com as mesmas opções" : "Reprocessar com as opções atuais"));
        if (j.options_requested) items.push(actionButton("retry-current", id, "Reprocessar com as opções atuais"));
      }
      items.push(actionButton("hide", id, "Ocultar desta lista", {className: "button button-quiet", hint: "Apenas nesta aba; não altera o servidor."}));
      actionsBox.replaceChildren(...items);
      $("detail-danger").hidden = !terminalKept;
      $("detail-danger-actions").replaceChildren(...(terminalKept ? [actionButton("delete", id, "Excluir arquivos…", {className: "button button-danger"})] : []));
      const results = available(j);
      $("results-switch").hidden = !results;
      $("text-button").dataset.id = id; $("report-button").dataset.id = id;
      $("results-unavailable").hidden = results;
      $("results-unavailable").textContent = gone(j) ? "Os arquivos deste job não estão mais disponíveis." : "Texto e relatório ficam disponíveis quando o job é concluído e enquanto os arquivos estão retidos.";
      if (!results && resultView) { resultView = null; drawResult(); }
    }
  }
  const mobileDetail = window.matchMedia("(max-width: 719px)");
  function syncDetailMode() {
    const modal = !!detailId && mobileDetail.matches;
    const drawer = $("detail-drawer");
    drawer.setAttribute("role", modal ? "dialog" : "complementary");
    if (modal) drawer.setAttribute("aria-modal", "true");
    else drawer.removeAttribute("aria-modal");
    for (const child of $("conteudo").children) if (child !== drawer) child.inert = modal;
    document.querySelector(".topbar").inert = modal;
    document.querySelector(".skip-link").inert = modal;
    if (modal && !drawer.contains(document.activeElement)) $("detail-title").focus();
  }
  mobileDetail.addEventListener("change", syncDetailMode);
  function openDetail(id, trigger) {
    $("detail-notice").hidden = true;
    $("detail-notice").textContent = "";
    if (detailId !== id) { resultView = null; drawResult(); }
    detailId = id; detailTrigger = trigger || null;
    for (const node of ["detail-status", "detail-list", "detail-actions"]) { delete $(node).dataset.key; delete $(node).dataset.signature; }
    renderDetail();
    $("detail-drawer").hidden = false;
    document.body.classList.add("has-detail");
    syncDetailMode();
    $("detail-title").focus();
  }
  function closeDetail() {
    const id = detailId;
    detailId = null; resultView = null; drawResult();
    $("detail-drawer").hidden = true;
    document.body.classList.remove("has-detail");
    syncDetailMode();
    const back = detailTrigger?.isConnected ? detailTrigger : rows.get(id)?.querySelector(".row-title");
    detailTrigger = null;
    (back?.isConnected ? back : $("history-title")).focus({preventScroll: false});
  }
  $("detail-close-button").onclick = closeDetail;
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && detailId && !document.querySelector("dialog[open]")) { event.preventDefault(); closeDetail(); }
    if (event.key === "Tab" && detailId && mobileDetail.matches && !document.querySelector("dialog[open]")) {
      const controls = [...$("detail-drawer").querySelectorAll('button, a[href], input, select, textarea, summary, [tabindex="0"]')]
        .filter((node) => !node.disabled && node.getClientRects().length > 0);
      const first = controls[0], last = controls.at(-1);
      if (!first) { event.preventDefault(); $("detail-title").focus(); }
      else if (event.shiftKey && (document.activeElement === first || document.activeElement === $("detail-title"))) {
        event.preventDefault(); last.focus();
      } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });

  // ---------------------------------------------------------------- results: text and report
  function drawResult() {
    const box = $("result-content");
    for (const b of [$("text-button"), $("report-button")]) b.setAttribute("aria-pressed", String(resultView?.action === b.dataset.action));
    box.replaceChildren();
    if (!resultView) return;
    const r = resultView.result || {};
    if (resultView.action === "text") {
      const pageCount = Number(r.page_count) || 1, page = Number(r.page) || 1, text = String(r.text ?? "");
      const start = Number(r.offset) || 0;
      const meta = [`Página ${page} de ${pageCount}`];
      if (r.page_character_count != null) meta.push(text.length ? `caracteres ${start + 1}–${start + text.length} de ${r.page_character_count}` : `${r.page_character_count} caractere(s)`);
      if (r.source) meta.push(`origem: ${SOURCES[r.source] || r.source}`);
      const select = el("select", {id: "text-page"}, Array.from({length: Math.min(pageCount, 300)}, (_, n) => el("option", {value: String(n + 1), text: String(n + 1), selected: n + 1 === page})));
      select.onchange = act(() => loadText(resultView.id, Number(select.value), 0));
      const controls = el("div", {className: "text-controls"}, [
        el("label", {className: "field field-inline"}, ["Página", select]),
        el("button", {type: "button", className: "button button-small", disabled: page <= 1, onclick: act(() => loadText(resultView.id, page - 1, 0))}, "Página anterior"),
        r.next_page != null ? el("button", {type: "button", id: "text-next", className: "button button-small", onclick: act(() => loadText(resultView.id, r.next_page, r.next_offset || 0))}, "Próximo trecho") : null,
        el("button", {type: "button", className: "button button-small button-quiet", onclick: () => copyText(text)}, "Copiar trecho"),
      ]);
      box.append(el("p", {className: "result-meta", text: meta.join(" · ")}), controls,
        el("pre", {className: "text-output", tabIndex: 0, "aria-label": `Texto da página ${page}`, text: text || "(sem texto neste trecho)"}));
      if (r.truncated) box.append(el("p", {className: "hint", text: "Esta página continua no próximo trecho."}));
      return;
    }
    box.append(reportSummary(r));
    box.append(el("details", {className: "disclosure disclosure-plain"}, [
      el("summary", {}, el("span", {className: "disclosure__label", text: "Relatório técnico completo (JSON)"})),
      el("pre", {id: "report-json", className: "text-output", tabIndex: 0, text: JSON.stringify(r, null, 2)})]));
  }
  function reportSummary(r) {
    const q = r.quality || {}, v = r.output_validation || {};
    const pairs = [];
    if (r.generated_at) pairs.push(["Gerado em", ui.date(r.generated_at)]);
    if (r.duration_ms != null) pairs.push(["Duração", `${(r.duration_ms / 1000).toLocaleString("pt-BR", {maximumFractionDigits: 1})} s`]);
    if (r.page_count != null) pairs.push(["Páginas", String(r.page_count)]);
    if (Array.isArray(r.pages) && r.pages.length) {
      const counts = {};
      r.pages.forEach((p) => { counts[p.source] = (counts[p.source] || 0) + 1; });
      pairs.push(["Origem do texto", Object.entries(counts).map(([s, n]) => `${SOURCES[s] || s}: ${n} página(s)`).join("; ")]);
    }
    if (q.label) pairs.push(["Qualidade", `${qualityText(q.label)} · ${Math.round((q.valid_ratio || 0) * 100)}% de termos plausíveis · ${q.character_count ?? 0} caracteres (heurística, não mede precisão)`]);
    if (r.fallback_attempted != null) pairs.push(["OCR alternativo", r.used_fallback ? "usado" : r.fallback_attempted ? "tentado, não usado" : "não tentado"]);
    if (v.requested_format) pairs.push(["Saída", `${optionText(v.requested_format)} · ${v.pdf_readable ? "PDF legível" : "PDF não legível"} · ${v.page_count_matches ? "páginas conferem" : "páginas divergem"} · PDF/A não validado`]);
    if (r.input?.size_bytes != null) pairs.push(["Original", `${ui.bytes(r.input.size_bytes)} · SHA-256 ${r.input.sha256}`, "mono"]);
    if (r.output?.size_bytes != null) pairs.push(["Resultado", `${ui.bytes(r.output.size_bytes)} · SHA-256 ${r.output.sha256}`, "mono"]);
    if (r.engines && Object.keys(r.engines).length) pairs.push(["Motores", Object.entries(r.engines).map(([n, ver]) => `${n} ${ver || "(versão não registrada)"}`).join("; ")]);
    if (r.options_effective) pairs.push(["Opções efetivas", formatOptions(r.options_effective)]);
    const wrap = el("div", {className: "report-summary"}, el("dl", {className: "detail-list detail-list-compact"},
      pairs.map(([t, d, kind]) => el("div", {}, [el("dt", {text: t}), el("dd", {className: kind === "mono" ? "mono" : "", text: d})]))));
    if (Array.isArray(r.warnings) && r.warnings.length) {
      wrap.append(el("p", {className: "detail-section__title", text: "Avisos"}),
        el("ul", {className: "plain-list warnings"}, r.warnings.map((w) => el("li", {text: WARNINGS[w] || String(w)}))));
    }
    return wrap;
  }
  async function loadText(id, page = 1, offset = 0) {
    const query = page > 1 || offset > 0 ? `?page=${page}&offset=${offset}` : "";
    const result = await json(`/api/v1/jobs/${encodeURIComponent(id)}/text${query}`);
    if (detailId !== id) return;
    resultView = {action: "text", id, result}; drawResult();
    document.querySelector("#result-content .text-output")?.focus({preventScroll: true});
  }
  async function copyText(text) {
    try { await navigator.clipboard.writeText(text); notify("Trecho copiado para a área de transferência.", "success"); }
    catch (_) { notify("Não foi possível copiar automaticamente; selecione o texto e copie.", "error"); }
  }

  // ---------------------------------------------------------------- sync
  function merge(job) {
    cards.set(job.job_id, {...cards.get(job.job_id), ...job});
  }
  async function pages(path, params = {}) {
    const items = []; let cursor;
    do {
      const query = new URLSearchParams({limit: "100", ...params}); if (cursor) query.set("cursor", cursor);
      const page = await json(`${path}?${query}`);
      items.push(...page.items); cursor = page.next_cursor;
    } while (cursor);
    return items;
  }
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      // Collect every page before replacing history: a failed page cannot erase rows.
      const jobs = new Map((await pages("/api/v1/jobs")).map((j) => [j.job_id, j]));
      for (const id of [...cards.keys()]) if (!jobs.has(id)) { cards.delete(id); selected.delete(id); }
      for (const job of jobs.values()) merge(job);
      try { metrics = await json("/api/v1/metrics"); } catch (_) { metrics = null; }
      if (batchesLoaded) await loadBatches().catch(() => {});
      lastSync = new Date(); lastFull = Date.now(); syncError = null;
      render();
    } finally { refreshing = false; }
  }
  async function refreshActive() {
    if (refreshing) return;
    refreshing = true;
    try {
      const before = [...cards.values()].filter((j) => ACTIVE.has(j.status)).map((j) => j.job_id);
      const seen = new Map();
      for (const status of ACTIVE) for (const j of await pages("/api/v1/jobs", {status})) seen.set(j.job_id, j);
      for (const id of before) {
        if (seen.has(id)) continue;
        try { seen.set(id, await json(`/api/v1/jobs/${encodeURIComponent(id)}`)); }
        catch (e) { if (e.status === 404) { cards.delete(id); selected.delete(id); } else throw e; }
      }
      for (const j of seen.values()) merge(j);
      try { metrics = await json("/api/v1/metrics"); } catch (_) { metrics = null; }
      lastSync = new Date(); syncError = null;
      render();
    } finally { refreshing = false; }
  }
  async function tick() {
    if (busy || refreshing || document.hidden || !token) return;
    try {
      if (Date.now() - lastFull >= FULL_SYNC_MS) await refresh();
      else if (activeCount()) await refreshActive();
    } catch (e) { syncError = e.message; renderSummary(); }
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });

  async function loadBatches() {
    const items = await pages("/api/v1/batches");
    batches.clear();
    items.forEach((b) => batches.set(b.batch_id, b));
    batchesLoaded = true;
    const select = $("batch-filter"), current = select.value;
    select.replaceChildren(el("option", {value: "", text: "Todos os lotes"}), ...items.slice().reverse().map((b) =>
      el("option", {value: b.batch_id, text: `${ui.date(b.created_at)} · ${b.items.length} arquivo(s)${b.terminal ? "" : " · em andamento"}`})));
    if (batches.has(current)) select.value = current;
    renderBatchSummary();
  }
  function renderBatchSummary() {
    const b = batches.get($("batch-filter").value);
    if (!b) { $("batch-summary").textContent = ""; return; }
    const counts = Object.entries(b.counts || {}).map(([s, n]) => `${n} ${(STATUS[s] || s).toLocaleLowerCase("pt-BR")}`).join(", ");
    $("batch-summary").textContent = `Lote de ${ui.date(b.created_at)}: ${b.items.length} arquivo(s) — ${counts}. Progresso ${b.progress_percent}%.`;
  }

  // ---------------------------------------------------------------- capabilities and options
  function options() {
    const extract = $("processing-mode").value === "extract_text";
    return {processing_mode: extract ? "extract_text" : "preserve_existing_text",
      languages: extract ? [] : $("ocr-language").value.split("+"),
      output_format: extract ? "pdf_searchable" : $("output-format").value,
      compression_profile: extract ? "none" : $("compression-profile").value,
      fallback_policy: extract ? "disabled" : $("fallback-policy").value};
  }
  function renderOptionsSummary() {
    if (!capabilities) return;
    const ocr = capabilities.processing_modes.includes("preserve_existing_text");
    $("options-summary").textContent = formatOptions(options()) + (ocr ? "" : " · OCR indisponível neste servidor");
  }
  async function loadCapabilities() {
    capabilities = await json("/api/v1/capabilities");
    const fill = (id, items) => $(id).replaceChildren(...items.map((value) => el("option", {value, text: optionText(value)})));
    const languages = [...capabilities.languages];
    if (languages.includes("eng") && languages.includes("por")) languages.push("eng+por");
    fill("processing-mode", capabilities.processing_modes); fill("ocr-language", languages);
    fill("output-format", capabilities.output_formats); fill("compression-profile", capabilities.compression_profiles);
    fill("fallback-policy", capabilities.fallback_policies);
    $("processing-mode").value = capabilities.processing_modes.includes("preserve_existing_text") ? "preserve_existing_text" : "extract_text";
    for (const [id, value] of [["ocr-language", "por"], ["output-format", "pdf_searchable"], ["compression-profile", "none"], ["fallback-policy", "disabled"]]) {
      if ([...$(id).options].some((o) => o.value === value)) $(id).value = value;
    }
    modeChanged();
    const ocr = capabilities.processing_modes.includes("preserve_existing_text");
    $("capability-note").textContent = ocr
      ? "Extrair texto usa apenas a camada de texto do PDF. OCR preserva o texto existente e reconhece imagens. A qualidade informada é heurística."
      : "O motor de OCR não está disponível neste servidor: só é possível extrair o texto que já existe no PDF. Páginas digitalizadas não são reconhecidas.";
    $("engine-list").replaceChildren(...Object.entries(capabilities.engines || {}).map(([name, e]) =>
      el("li", {text: `${name}: ${e.available ? "disponível" : "indisponível"}${e.version ? ` (versão ${e.version})` : ""}${(e.reasons || []).length ? ` — ${e.reasons.map((r) => REASONS[r] || r).join("; ")}` : ""}`})));
    $("capability-warnings").replaceChildren(...(capabilities.warnings || []).map((w) => el("li", {text: WARNINGS[w] || w})));
    const l = capabilities.limits || {};
    $("limits-note").textContent = [l.max_upload_bytes && `Até ${ui.bytes(l.max_upload_bytes)}`, l.max_pages && `${l.max_pages} páginas por arquivo`,
      l.batch_max_items && `${l.batch_max_items} arquivos por envio`].filter(Boolean).join(", ")
      + (l.job_retention_seconds ? `. Resultados ficam disponíveis por ${ui.duration(l.job_retention_seconds)} após o término.` : ".");
    $("capabilities-retry").hidden = true;
    renderOptionsSummary();
  }
  function modeChanged() {
    for (const id of ["ocr-language", "output-format", "compression-profile", "fallback-policy"]) $(id).disabled = $("processing-mode").value === "extract_text";
    renderOptionsSummary();
  }
  $("processing-mode").onchange = modeChanged;
  for (const id of ["ocr-language", "output-format", "compression-profile", "fallback-policy"]) $(id).onchange = renderOptionsSummary;
  $("capabilities-retry").onclick = act(async () => { await loadCapabilities(); notify("Capacidades consultadas.", "success"); });

  // ---------------------------------------------------------------- intake
  function addFiles(files) {
    const ids = []; let ignored = 0;
    const max = capabilities?.limits?.max_upload_bytes;
    for (const file of files) {
      if (!file.name.toLowerCase().endsWith(".pdf")) { ignored++; continue; }
      const id = crypto.randomUUID();
      const tooLarge = max && file.size > max;
      staged.set(id, {id, file, filename: file.name, relativePath: file.webkitRelativePath || "", size: file.size,
        state: tooLarge ? "rejected" : "ready", error: tooLarge ? `acima do limite de ${ui.bytes(max)}` : null});
      ids.push(id);
    }
    if (ignored) notify(`${ids.length} PDF(s) adicionados; ${ignored} arquivo(s) que não são PDF foram ignorados.`, "info");
    render();
    return ids;
  }
  async function prepare(s) {
    if (s.uploaded && s.upload) return s.upload.upload_id;
    if (!s.upload) s.upload = await post("/api/v1/upload-sessions", {filename: s.filename, size_bytes: s.file.size});
    try {
      await api(s.upload.content_url, {method: "PUT", headers: {"Content-Type": "application/pdf"}, body: s.file});
    } catch (e) {
      if ([400, 404, 409, 410, 413, 415, 422].includes(e.status)) delete s.upload;
      throw e;
    }
    s.uploaded = true;
    return s.upload.upload_id;
  }
  async function enqueue(explicit) {
    if (!capabilities) throw fail("Aguarde a consulta de capacidades antes de enviar.");
    if (!pendingBatch) {
      const candidates = (explicit || sendable()).filter((id) => staged.get(id)?.state !== "rejected");
      const ids = candidates.slice(0, allowance());
      if (!ids.length) throw fail(candidates.length ? "Limite de jobs pendentes atingido; aguarde a conclusão de jobs em andamento." : "Nenhum PDF pronto para envio.");
      pendingBatch = {key: crypto.randomUUID(), ids, options: options()};
    }
    const rejected = [];
    let receipt;
    try {
      if (!pendingBatch.body) {
        const items = [];
        for (const [n, id] of pendingBatch.ids.entries()) {
          const s = staged.get(id);
          s.state = "uploading";
          progressText = `Enviando ${n + 1} de ${pendingBatch.ids.length}: ${s.filename}`;
          renderStagingOnly();
          try {
            items.push({client_item_id: id, upload_id: await prepare(s), options: pendingBatch.options});
            s.state = "uploaded";
          } catch (e) {
            // File-specific rejections stay visible; global or ambiguous failures stop this send.
            if ([400, 409, 413, 415, 422].includes(e.status)) { s.state = "rejected"; s.error = e.message; rejected.push(s); }
            else { s.state = "ready"; throw e; }
          }
          renderStagingOnly();
        }
        if (!items.length) throw fail(`Nenhum arquivo foi enviado. Rejeitado(s): ${rejected.map((s) => `${s.filename} — ${s.error}`).join("; ")}`);
        pendingBatch.ids = items.map((i) => i.client_item_id);
        pendingBatch.body = {items};
      }
      receipt = await post("/api/v1/batches", pendingBatch.body, pendingBatch.key);
    } catch (e) {
      // Before the admission request, or after a definitive rejection, nothing was admitted:
      // abandon the key. Ambiguous failures keep the exact request for a safe replay.
      if (!pendingBatch?.body || definitive(e) || e.status === undefined) {
        if (pendingBatch?.body && [404, 409, 410].includes(e.status)) {
          for (const id of pendingBatch.ids) { const s = staged.get(id); if (s) { delete s.upload; s.uploaded = false; } }
        }
        for (const id of pendingBatch?.ids || []) { const s = staged.get(id); if (s && s.state !== "rejected") s.state = "ready"; }
        pendingBatch = null;
      }
      throw e;
    }
    for (const item of receipt.items) {
      const s = staged.get(item.client_item_id);
      staged.delete(item.client_item_id);
      merge({job_id: item.job_id, filename: item.filename, status: item.status, progress_percent: item.progress_percent || 0,
        created_at: receipt.created_at, updated_at: receipt.created_at, relativePath: s?.relativePath || "", size: s?.size, batch_id: receipt.batch_id});
    }
    pendingBatch = null;
    const rest = sendable().length;
    notify(`Envio admitido: ${receipt.items.length} arquivo(s) na fila (lote ${receipt.batch_id.slice(0, 8)}).`
      + (rejected.length ? ` ${rejected.length} rejeitado(s): ${rejected.map((s) => s.filename).join(", ")}.` : "")
      + (rest ? ` ${rest} arquivo(s) continuam na lista para o próximo envio.` : ""), rejected.length ? "error" : "success");
    await refresh();
  }
  function renderStagingOnly() { renderStaging(); }

  $("dropzone").addEventListener("dragover", (e) => { e.preventDefault(); $("dropzone").classList.add("is-over"); });
  $("dropzone").addEventListener("dragleave", () => $("dropzone").classList.remove("is-over"));
  $("dropzone").addEventListener("drop", (e) => {
    e.preventDefault(); $("dropzone").classList.remove("is-over");
    const files = [...(e.dataTransfer?.files || [])];
    if (files.length) addFiles(files);
  });
  for (const id of ["directory-input", "multi-file-input"]) $(id).addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
  $("file-input").addEventListener("change", (e) => {
    const files = [...e.target.files]; e.target.value = "";
    if (!files.length) return;
    act(async () => {
      const ids = addFiles(files);
      if (pendingBatch) {
        throw fail("Há um envio anterior sem confirmação do servidor. O arquivo foi adicionado à lista; retome ou descarte o envio pendente antes de enviá-lo.");
      }
      await enqueue(ids);
    })();
  });
  $("upload-form").addEventListener("submit", (e) => e.preventDefault());
  $("queue-button").onclick = act(() => enqueue());
  $("staging-list").addEventListener("click", (e) => {
    const b = e.target.closest("[data-staged-remove]");
    if (!b || b.disabled) return;
    staged.delete(b.dataset.stagedRemove);
    render();
    ($("staging-list").querySelector("[data-staged-remove]:not([disabled])") || $("multi-file-input").closest("label")).focus?.();
  });
  $("clear-staging-button").onclick = () => {
    if (pendingBatch) return;
    staged.clear(); render(); notify("Lista de arquivos a enviar esvaziada. Nada foi enviado ao servidor.", "info");
  };
  $("discard-pending-button").onclick = act(async () => {
    const ok = await ui.confirmAction({title: "Descartar envio pendente?",
      body: "O servidor pode ter aceitado o envio anterior sem que a resposta chegasse. Se você descartar e enviar de novo, jobs duplicados podem ser criados. O histórico será atualizado para conferência.",
      confirmLabel: "Descartar pedido", cancelLabel: "Manter para retomar"});
    if (!ok) return;
    // Uploads of a possibly admitted request are not reused; a new send uploads again.
    for (const id of pendingBatch?.ids || []) {
      const s = staged.get(id);
      if (s && s.state !== "rejected") { s.state = "ready"; delete s.upload; s.uploaded = false; }
    }
    pendingBatch = null;
    await refresh();
    notify("Envio pendente descartado. Confira o histórico antes de enviar novamente.", "info");
  });

  // ---------------------------------------------------------------- actions on jobs
  async function download(path, filename) {
    const blob = await (await api(path)).blob();
    const url = URL.createObjectURL(blob), anchor = document.createElement("a");
    anchor.href = url; anchor.download = filename; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }
  async function retry(j, mode) {
    if (!j.retryRequest) j.retryRequest = {key: crypto.randomUUID(), body: mode === "same" ? {} : {options: options()}};
    else if ((mode === "same") !== !j.retryRequest.body.options) notify("Repetindo o reprocessamento pendente deste job, com as opções do pedido original.", "info");
    try {
      const receipt = await post(`/api/v1/jobs/${encodeURIComponent(j.job_id)}/retry`, j.retryRequest.body, j.retryRequest.key);
      delete j.retryRequest;
      notify(`Reprocessamento criado: novo job ${receipt.job_id.slice(0, 8)}. O original foi preservado.`, "success");
    } catch (e) {
      if (definitive(e)) delete j.retryRequest;
      throw e;
    }
  }
  async function dispatch(action, id, trigger) {
    const j = cards.get(id);
    if (!j) return;
    document.querySelectorAll("details.menu[open]").forEach((m) => { m.open = false; });
    if (action === "detail") { openDetail(id, trigger); return; }
    if (action === "hide") {
      hidden.add(id); selected.delete(id);
      if (detailId === id) closeDetail();
      render(); notify(`“${j.filename}” foi ocultado desta aba. Nada mudou no servidor.`, "info"); return;
    }
    if (action === "download") { await download(`/api/v1/jobs/${encodeURIComponent(id)}/artifact/content`, `${j.filename.replace(/\.pdf$/i, "")}-searchable.pdf`); return; }
    if (action === "text") { await loadText(id, 1, 0); return; }
    if (action === "report") {
      const result = await json(`/api/v1/jobs/${encodeURIComponent(id)}/report`);
      if (detailId === id) { resultView = {action: "report", id, result}; drawResult(); }
      return;
    }
    if (action === "cancel") {
      const ok = await ui.confirmAction({title: "Cancelar este job?",
        body: `“${j.filename}”: na fila, o cancelamento é imediato; em processamento, ele é solicitado e concluído quando o motor parar. Os arquivos não são excluídos e é possível reprocessar depois.`,
        confirmLabel: "Cancelar job", cancelLabel: "Voltar"});
      if (!ok) return;
      const result = await post(`/api/v1/jobs/${encodeURIComponent(id)}/cancel`, {});
      notify(result.outcome === "already_terminal" ? "O job já havia terminado." : result.outcome === "canceled" ? "Job cancelado." : "Cancelamento solicitado; o estado será atualizado quando o processamento parar.", "success");
    }
    if (action === "delete") {
      const ok = await ui.confirmAction({title: "Excluir os arquivos deste job?", danger: true,
        body: `“${j.filename}”: o PDF original e o resultado serão removidos. Não será mais possível baixar, consultar texto ou relatório nem reprocessar. O registro do job permanece no histórico. Esta ação é irreversível.`,
        confirmLabel: "Excluir arquivos", cancelLabel: "Manter arquivos"});
      if (!ok) return;
      const receipt = await json(`/api/v1/jobs/${encodeURIComponent(id)}/artifacts`, {method: "DELETE"});
      notify(receipt.retention.purge_pending ? "Acesso aos arquivos encerrado; a remoção física está pendente." : "Arquivos excluídos.", "success");
    }
    if (action === "retry" || action === "retry-current") await retry(j, action === "retry" && j.options_requested ? "same" : "current");
    await refresh();
  }
  document.addEventListener("click", (event) => {
    const b = event.target.closest("[data-action]");
    if (!b || b.disabled) return;
    // Viewing and hiding never touch the server, so they stay available during uploads.
    if (["detail", "hide"].includes(b.dataset.action)) dispatch(b.dataset.action, b.dataset.id, b).catch((e) => notify(e.message, "error"));
    else act(() => dispatch(b.dataset.action, b.dataset.id, b))();
  });
  $("cards-grid").addEventListener("change", (e) => {
    const id = e.target.dataset.select;
    if (!id) return;
    if (e.target.checked) selected.add(id); else selected.delete(id);
    render();
  });
  $("select-visible-button").onclick = () => { visible().slice(0, limit).forEach((j) => selected.add(j.job_id)); render(); };
  $("clear-selection-button").onclick = () => { selected.clear(); render(); $("history-title").focus(); };
  $("clear-queue-button").onclick = act(async () => {
    const ids = selectedJobs().filter((j) => ACTIVE.has(j.status)).map((j) => j.job_id);
    const max = capabilities?.limits?.cancel_max_items || 25;
    if (!ids.length) return;
    if (ids.length > max) throw fail(`Selecione até ${max} jobs por cancelamento.`);
    const ok = await ui.confirmAction({title: `Cancelar ${ids.length} job(s)?`,
      body: "Jobs na fila são cancelados imediatamente; os que estão em processamento recebem um pedido de cancelamento. Nenhum arquivo é excluído.",
      confirmLabel: "Cancelar jobs", cancelLabel: "Voltar"});
    if (!ok) return;
    const result = await post("/api/v1/jobs/cancel-batch", {job_ids: ids});
    const counts = {};
    result.items.forEach((i) => { counts[i.outcome] = (counts[i.outcome] || 0) + 1; });
    notify(`Cancelamento: ${Object.entries(counts).map(([o, n]) => `${n} ${OUTCOMES[o] || o}`).join(", ")}.`, "success");
    await refresh();
  });
  $("download-all-button").onclick = act(async () => {
    if (!exportPending) {
      const ids = selectedJobs().filter(available).map((j) => j.job_id);
      const max = capabilities?.limits?.export_max_items || 25;
      if (!ids.length) throw fail("Selecione resultados concluídos para gerar o ZIP.");
      if (ids.length > max) throw fail(`O ZIP aceita até ${max} resultados.`);
      exportPending = {key: crypto.randomUUID(), body: {job_ids: ids}};
    }
    if (!exportPending.receipt) {
      try { exportPending.receipt = await post("/api/v1/exports", exportPending.body, exportPending.key); }
      catch (e) { if (definitive(e)) exportPending = null; throw e; }
    }
    const id = exportPending.receipt.export_id, deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      let result;
      try { result = await json(`/api/v1/exports/${id}`); }
      catch (e) { if ([404, 410].includes(e.status)) exportPending = null; throw e; }
      if (result.status === "ready") {
        await download(result.download_url, "resultados-ocr.zip");
        exportPending = null;
        const excluded = (result.items || []).filter((i) => !i.included);
        notify(`ZIP baixado com ${(result.items || []).length - excluded.length} resultado(s)`
          + (excluded.length ? `; não incluído(s): ${excluded.map((i) => `${i.filename} (${EXCLUDED[i.reason] || i.reason})`).join(", ")}` : "")
          + (result.expires_at ? `. Disponível para novo download até ${ui.date(result.expires_at)}.` : "."), excluded.length ? "error" : "success");
        return;
      }
      if (result.status === "failed") { exportPending = null; throw fail(`A preparação do ZIP falhou: ${ui.errorMessage(result.error_code, null, null)}`); }
      $("selection-hint").textContent = `Preparando ZIP ${id.slice(0, 8)}…`;
      await new Promise((resolve) => setTimeout(resolve, Math.max(1, result.poll_after_seconds || 1) * 1000));
    }
    throw fail("O ZIP ainda está em preparação. Clique em “Retomar ZIP pendente” para consultar o mesmo pedido.");
  });
  $("discard-export-button").onclick = () => { exportPending = null; render(); notify("Pedido de ZIP pendente esquecido nesta aba. Escolha a seleção e gere outro.", "info"); };

  // ---------------------------------------------------------------- view-only controls
  const closeMenus = () => document.querySelectorAll("details.menu[open]").forEach((m) => { m.open = false; });
  $("remove-completed-button").onclick = () => {
    values().filter((j) => TERMINAL.has(j.status)).forEach((j) => { hidden.add(j.job_id); selected.delete(j.job_id); });
    closeMenus(); render(); notify("Documentos encerrados ocultados nesta aba. Nada mudou no servidor.", "info");
  };
  $("show-hidden-button").onclick = () => { hidden.clear(); closeMenus(); render(); notify("Documentos ocultos exibidos novamente.", "info"); };
  $("reset-workspace-button").onclick = act(async () => {
    if (pendingBatch) return;
    closeMenus();
    const ok = await ui.confirmAction({title: "Limpar a visualização?",
      body: "Oculta todo o histórico desta aba e remove da lista os arquivos ainda não enviados. Jobs no servidor continuam executando; nada é cancelado nem excluído.",
      confirmLabel: "Limpar visualização", cancelLabel: "Voltar"});
    if (!ok) return;
    for (const id of cards.keys()) hidden.add(id);
    staged.clear(); selected.clear();
    if (detailId) closeDetail();
    notify("Visualização limpa. Use Visualização › Mostrar ocultos para ver o histórico de novo.", "info");
  });
  $("refresh-workspace-button").onclick = act(async () => {
    try { await refresh(); notify("Histórico atualizado do servidor.", "success"); }
    catch (e) { syncError = e.message; throw e; }
  });
  $("show-more-button").onclick = () => {
    const firstNew = visible()[limit];
    limit += PAGE; render();
    if (firstNew) rows.get(firstNew.job_id)?.querySelector(".row-title")?.focus();
  };
  $("filter-group").onclick = (e) => {
    const b = e.target.closest("[data-filter]");
    if (!b) return;
    filter = b.dataset.filter; limit = PAGE;
    document.querySelectorAll("#filter-group [data-filter]").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    render();
  };
  const savePreferences = () => {
    try { localStorage.setItem(`ocr-display:${subject}`, JSON.stringify({search: $("search-input").value, sort: $("sort-select").value})); } catch (_) { /* optional */ }
  };
  $("search-input").oninput = () => { limit = PAGE; savePreferences(); render(); };
  $("sort-select").onchange = () => { savePreferences(); render(); };
  for (const id of ["created-from", "created-to", "quality-filter"]) $(id).onchange = () => { limit = PAGE; render(); };
  $("batch-filter").onchange = () => { limit = PAGE; renderBatchSummary(); render(); };
  $("more-filters").addEventListener("toggle", () => {
    if ($("more-filters").open && !batchesLoaded && token) loadBatches().catch((e) => notify(e.message, "error"));
  });
  $("clear-filters-button").onclick = () => {
    for (const id of ["created-from", "created-to", "quality-filter", "batch-filter", "search-input"]) $(id).value = "";
    filter = "all"; limit = PAGE;
    document.querySelectorAll("#filter-group [data-filter]").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.filter === "all")));
    renderBatchSummary(); savePreferences(); render();
  };

  // ---------------------------------------------------------------- startup
  async function start() {
    try {
      const session = await ui.browserToken();
      token = session.access_token; subject = session.subject;
    } catch (e) { notify(e.message, "error"); return; }
    try {
      localStorage.removeItem("ocr-recognizer-workspace-v2");
      const pref = JSON.parse(localStorage.getItem(`ocr-display:${subject}`) || "{}");
      $("search-input").value = pref.search || "";
      $("sort-select").value = ["recent", "oldest", "name", "status", "size_desc", "size_asc"].includes(pref.sort) ? pref.sort : "recent";
    } catch (_) { /* no restored job content */ }
    // History and capabilities are independent: one failure must not block the other.
    const [history, caps] = await Promise.allSettled([refresh(), loadCapabilities()]);
    const problems = [];
    if (history.status === "rejected") { syncError = history.reason.message; problems.push(`Histórico: ${history.reason.message}`); }
    if (caps.status === "rejected") {
      $("capabilities-retry").hidden = false;
      $("options-summary").textContent = "Capacidades indisponíveis";
      $("capability-note").textContent = "Não foi possível consultar as capacidades; o envio fica desabilitado até a consulta funcionar.";
      problems.push(`Capacidades: ${caps.reason.message}`);
    }
    render();
    if (problems.length) notify(problems.join(" "), "error");
    else notify("Histórico sincronizado.", "info");
    setInterval(tick, POLL_MS);
  }
  render();
  start();
})();
