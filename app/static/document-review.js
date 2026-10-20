/* Read-only document review. The Bearer lives only in this closure; nothing is stored or logged. */
(() => {
  "use strict";
  const ui = window.OcrUi, {el} = ui;
  const $ = (id) => document.getElementById(id);
  const jobId = $("rv-root").dataset.jobId;
  const ID = /^[0-9a-f]{32}$/, HASH = /^[0-9a-f]{64}$/, CODE = /^[a-z][a-z0-9_]{0,63}$/, CURSOR = /^[A-Za-z0-9_-]{1,2048}$/;
  const TIMEOUT_MS = 15000, MAX_PAGES = 300, MAX_REVISIONS = 32, MAX_OFFSET = 500000, CHUNK = 1000;
  const STATES = {draft: "Rascunho", approved: "Aprovada"};
  const MESSAGES = {
    shape: "A resposta do servidor não corresponde ao documento solicitado. Nada foi exibido.",
    http: "O servidor não concluiu a operação. Tente novamente.",
    403: "Esta identidade não tem permissão para ver este documento.",
    404: "Documento não encontrado para esta conta.",
    410: "Os arquivos deste documento expiraram ou foram excluídos.",
    429: "O servidor está ocupado. Tente novamente em instantes.",
    scope: "Esta identidade não tem permissão para ler o texto deste documento (jobs:read e artifacts:read).",
    noMetadataScope: "Esta identidade não tem permissão metadata:read; os metadados não foram consultados. O texto canônico continua disponível.",
    noMetadata: "Nenhum metadado salvo para este documento. O texto canônico continua disponível para leitura.",
    unexpected: "Não foi possível concluir a operação.",
  };
  let token = null, epoch = 0, manifest = null, chunk = null, head = null, shown = null, revisions = [], cursor = null;
  const inflight = new Map();

  const obj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const int = (v, lo, hi) => Number.isSafeInteger(v) && v >= lo && v <= hi;
  const str = (v, max) => typeof v === "string" && v.length <= max;
  const isId = (v) => typeof v === "string" && ID.test(v);
  const isHash = (v) => typeof v === "string" && HASH.test(v);
  const points = (text) => Array.from(text).length;
  const fault = (kind, status, code) => Object.assign(new Error(MESSAGES[status] || MESSAGES[kind] || ui.NETWORK), {kind, status, code});
  if (!isId(jobId)) { $("rv-status").textContent = MESSAGES[404]; return; }
  const base = `/api/v1/jobs/${jobId}`;

  // ------------------------------------------------------------ state and messages
  function say(message, tone = "info", {retry = false, login = false} = {}) {
    $("rv-status").textContent = message;
    $("rv-status").dataset.tone = tone;
    $("rv-retry").hidden = !retry;
    $("rv-login").hidden = !login;
  }
  function wipe() {
    manifest = chunk = head = shown = cursor = null; revisions = [];
    for (const name of ["rv-manifest", "rv-chunk-meta", "rv-text", "rv-meta-note", "rv-meta", "rv-segments", "rv-revs", "rv-page"]) $(name).replaceChildren();
    for (const name of ["rv-page", "rv-next", "rv-head"]) $(name).disabled = true;
    $("rv-more").hidden = true;
  }
  function halt() {
    epoch++;
    for (const controller of inflight.values()) controller.abort();
    inflight.clear();
    token = null;
    wipe();
  }
  function expire() {
    halt();
    say("Sua sessão expirou. Redirecionando para o login…", "error", {login: true});
    location.assign("/login");
  }
  function report(error) {
    if (error.kind === "stale") return;
    // Mismatches and lost access remove everything sensitive; transient failures keep what is shown.
    const closed = error.kind === "shape" || [403, 404, 410].includes(error.status);
    if (closed) wipe();
    say(error.kind ? error.message : MESSAGES.unexpected, "error", {retry: !closed});
  }
  const run = async (operation) => { try { await operation(); } catch (error) { report(error); } };

  // ------------------------------------------------------------ requests
  async function get(path, params = {}, key = path) {
    if (!token) throw fault("stale");
    inflight.get(key)?.abort();
    const controller = new AbortController(), mine = epoch;
    inflight.set(key, controller);
    const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
    const stale = () => mine !== epoch || inflight.get(key) !== controller;
    try {
      const query = new URLSearchParams(params).toString();
      let response;
      try {
        response = await fetch(ui.apiPath(query ? `${path}?${query}` : path),
          {credentials: "omit", cache: "no-store", signal: controller.signal, headers: {Authorization: `Bearer ${token}`}});
      } catch (_) { throw fault(stale() ? "stale" : "network"); }
      if (stale()) throw fault("stale");
      if (response.status === 401) { expire(); throw fault("stale"); }
      let body = null;
      try { body = await response.json(); } catch (_) { /* fixed message below */ }
      if (stale()) throw fault("stale");
      if (!response.ok) {
        const code = obj(body) && obj(body.error) && typeof body.error.code === "string" && CODE.test(body.error.code) ? body.error.code : null;
        throw fault("http", response.status, code);
      }
      if (body === null) throw fault("shape");
      return body;
    } finally {
      clearTimeout(timer);
      if (inflight.get(key) === controller) inflight.delete(key);
    }
  }

  // ------------------------------------------------------------ canonical text
  async function loadManifest() {
    const m = await get(`${base}/canonical/manifest`);
    const ok = obj(m) && m.job_id === jobId && m.document_id === jobId && m.artifact_version === 1 && m.text_version === 1
      && int(m.page_count, 1, MAX_PAGES) && int(m.total_characters, 0, 1e8) && int(m.size_bytes, 1, 33554432)
      && isHash(m.sha256) && isHash(m.input_pdf_sha256) && isHash(m.output_pdf_sha256);
    if (!ok) throw fault("shape");
    manifest = m;
    $("rv-manifest").textContent = `${ui.count(m.page_count, "página")} · ${m.total_characters} caracteres (pontos de código) · SHA-256 do texto canônico ${m.sha256.slice(0, 12)}…`;
    $("rv-manifest").title = `SHA-256: ${m.sha256}`;
    $("rv-page").replaceChildren(...Array.from({length: m.page_count}, (_, i) => el("option", {value: String(i + 1), text: `Página ${i + 1}`})));
    $("rv-page").disabled = false;
  }
  function nextOk(c) {
    if (c.truncated && c.end_offset <= c.offset) return false;
    const next = c.truncated ? [c.page, c.end_offset] : c.page < c.page_count ? [c.page + 1, 0] : [null, null];
    return c.next_page === next[0] && c.next_offset === next[1];
  }
  async function loadChunk(page, offset = 0) {
    if (!manifest) return;
    chunk = null; // a page change always restarts the cursor
    $("rv-next").disabled = true;
    $("rv-text").replaceChildren();
    $("rv-chunk-meta").textContent = "Carregando texto…";
    const c = await get(`${base}/canonical/pages`, {page, offset, max_chars: CHUNK}, "chunk");
    const ok = obj(c) && c.job_id === jobId && c.document_id === jobId && c.artifact_version === 1 && c.text_version === 1
      && c.page === page && c.offset === offset && c.page_count === manifest.page_count && c.canonical_sha256 === manifest.sha256
      && c.normalization === "identity-utf8-v1" && c.offset_unit === "unicode_code_points" && isHash(c.text_sha256)
      && str(c.text, 2 * CHUNK) && points(c.text) <= CHUNK && int(c.end_offset, offset, MAX_OFFSET)
      && int(c.page_character_count, c.end_offset, MAX_OFFSET) && points(c.text) === c.end_offset - offset
      && c.truncated === (c.end_offset < c.page_character_count) && nextOk(c);
    if (!ok) throw fault("shape");
    chunk = c;
    $("rv-page").value = String(c.page);
    $("rv-text").textContent = c.text || "(sem texto neste trecho)";
    $("rv-chunk-meta").textContent = `Página ${c.page} de ${c.page_count} · pontos de código ${c.offset}–${c.end_offset} de ${c.page_character_count} · hash da página ${c.text_sha256.slice(0, 12)}…`;
    $("rv-chunk-meta").title = `SHA-256 da página completa: ${c.text_sha256}`;
    $("rv-next").disabled = c.next_page === null;
    $("rv-next").textContent = c.next_page === null ? "Fim do texto" : c.next_page === c.page ? "Próximo trecho" : "Próxima página";
  }

  // ------------------------------------------------------------ metadata
  const segmentOk = (s) => obj(s) && isId(s.segment_id) && isId(s.type_id) && isId(s.context_id) && int(s.page_start, 1, MAX_PAGES)
    && int(s.page_end, s.page_start, MAX_PAGES) && int(s.type_version, 1, 1e6) && int(s.field_count, 0, 100) && isHash(s.schema_sha256);
  const viewOk = (v, want) => obj(v) && v.job_id === jobId && v.document_id === jobId && isId(v.revision_id) && isHash(v.sha256)
    && (!want || (v.revision_id === want.revision_id && v.sha256 === want.sha256))
    && (v.parent_revision_id === null || isId(v.parent_revision_id)) && isHash(v.canonical_sha256) && Object.hasOwn(STATES, v.state)
    && int(v.page_count, 1, MAX_PAGES) && str(v.created_at, 64) && Array.isArray(v.segments) && v.segments.length >= 1
    && v.segments.length <= 16 && v.segments.every(segmentOk);
  const revisionOk = (r) => obj(r) && isId(r.revision_id) && isHash(r.sha256) && Object.hasOwn(STATES, r.state) && str(r.created_at, 64)
    && int(r.size_bytes, 1, 1 << 30) && (r.parent_revision_id === null || isId(r.parent_revision_id));

  function showView(v) {
    shown = {revision_id: v.revision_id, sha256: v.sha256};
    const historical = !head || head.revision_id !== v.revision_id;
    $("rv-meta-note").textContent = historical
      ? "Revisão histórica fixada por identificador e hash. Não é a versão atual (HEAD); use “Recarregar versão atual” para voltar."
      : "Versão atual (HEAD) dos metadados.";
    const rows = [["Revisão", v.revision_id, "mono"], ["Estado", STATES[v.state]], ["SHA-256 da revisão", v.sha256, "mono"],
      ["Criada em", ui.date(v.created_at)], ["Páginas do documento", String(v.page_count)]];
    $("rv-meta").replaceChildren(...rows.map(([term, value, kind]) =>
      el("div", {}, [el("dt", {text: term}), el("dd", {className: kind || "", text: value})])));
    $("rv-segments").replaceChildren(...v.segments.map((s) => el("li", {}, [
      el("span", {text: `Páginas ${s.page_start}–${s.page_end} · tipo ${s.type_id.slice(0, 8)}… versão ${s.type_version} · ${s.field_count} campo(s)`, title: s.type_id}),
      el("span", {className: "badge", text: STATES[v.state]})])));
    renderRevisions();
  }
  async function loadHead() {
    $("rv-head").disabled = false;
    try {
      const v = await get(`${base}/metadata`, {}, "meta");
      if (!viewOk(v)) throw fault("shape");
      head = {revision_id: v.revision_id, sha256: v.sha256};
      showView(v);
      return true;
    } catch (error) {
      if (error.status !== 404 || error.code !== "metadata_not_found") throw error;
      head = shown = null; revisions = []; cursor = null;
      for (const name of ["rv-meta", "rv-segments", "rv-revs"]) $(name).replaceChildren();
      $("rv-more").hidden = true;
      $("rv-meta-note").textContent = MESSAGES.noMetadata;
      return false;
    }
  }
  async function selectRevision(summary) {
    const v = await get(`${base}/metadata/revisions/${summary.revision_id}`, {sha256: summary.sha256}, "meta");
    if (!viewOk(v, summary)) throw fault("shape");
    showView(v);
  }
  function renderRevisions() {
    $("rv-revs").replaceChildren(...revisions.map((r) => {
      const isHead = head?.revision_id === r.revision_id, isShown = shown?.revision_id === r.revision_id;
      return el("li", {dataset: {revision: r.revision_id}}, [
        el("span", {}, [el("span", {className: "mono", text: `${r.revision_id.slice(0, 8)}…`, title: r.revision_id}),
          ` · ${STATES[r.state]} · ${ui.date(r.created_at)}`, isHead ? " · Versão atual (HEAD)" : " · Revisão histórica"]),
        el("button", {type: "button", className: "button button-small", "aria-pressed": isShown ? "true" : "false",
          dataset: {revision: r.revision_id}, text: isShown ? "Em exibição" : "Ver esta revisão"})]);
    }));
    $("rv-more").hidden = cursor === null;
  }
  async function loadRevisions(more) {
    const page = await get(`${base}/metadata/revisions`, {limit: 25, ...(more && cursor ? {after: cursor} : {})}, "revisions");
    const ok = obj(page) && page.job_id === jobId && Array.isArray(page.revisions) && page.revisions.length <= MAX_REVISIONS
      && page.revisions.every(revisionOk) && (page.next_cursor === null || (typeof page.next_cursor === "string" && CURSOR.test(page.next_cursor)));
    const merged = ok ? (more ? revisions.concat(page.revisions) : page.revisions) : [];
    if (!ok || merged.length > MAX_REVISIONS || new Set(merged.map((r) => r.revision_id)).size !== merged.length) throw fault("shape");
    revisions = merged; cursor = page.next_cursor;
    renderRevisions();
  }

  // ------------------------------------------------------------ lifecycle
  async function start() {
    halt();
    const mine = epoch;
    say("Recuperando a sessão…");
    try {
      const session = await ui.browserToken();
      if (mine !== epoch) return;
      token = session.access_token;
    } catch (error) {
      if (mine === epoch) say(error.message, "error", {retry: true, login: true});
      return;
    }
    try {
      const me = await get("/api/v1/me", {}, "me");
      if (!obj(me) || !Array.isArray(me.scopes) || me.scopes.length > 64 || !me.scopes.every((s) => str(s, 64))) throw fault("shape");
      const scopes = new Set(me.scopes);
      const canon = scopes.has("jobs:read") && scopes.has("artifacts:read");
      if (!canon) { wipe(); say(MESSAGES.scope, "error"); return; }
      const metadata = scopes.has("metadata:read");
      say("Carregando o documento…");
      if (!metadata) $("rv-meta-note").textContent = MESSAGES.noMetadataScope;
      await loadManifest();
      const jobs = [loadChunk(1, 0)];
      if (metadata) jobs.push(loadHead().then((found) => found && loadRevisions(false)));
      const failed = (await Promise.allSettled(jobs)).find((r) => r.status === "rejected");
      if (failed) throw failed.reason;
      if (mine === epoch) say("Documento carregado. Visualização somente leitura.", "success");
    } catch (error) { report(error); }
  }

  $("rv-page").onchange = () => run(() => loadChunk(Number($("rv-page").value), 0));
  $("rv-next").onclick = () => chunk && chunk.next_page !== null && run(() => loadChunk(chunk.next_page, chunk.next_offset));
  $("rv-head").onclick = () => run(async () => { if (await loadHead()) await loadRevisions(false); });
  $("rv-more").onclick = () => run(() => loadRevisions(true));
  $("rv-retry").onclick = start;
  $("rv-revs").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-revision]");
    const summary = button && revisions.find((r) => r.revision_id === button.dataset.revision);
    if (summary) run(() => selectRevision(summary));
  });
  addEventListener("pagehide", halt);
  addEventListener("pageshow", (event) => { if (event.persisted) start(); });
  start();
})();
