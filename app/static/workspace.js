/* The backend owns job history. Only display preferences are stored locally. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const cards = new Map(), hidden = new Set(), selected = new Set();
  const active = new Set(["queued", "processing", "cancel_requested"]);
  const terminal = new Set(["completed", "failed", "canceled"]);
  const labels = {ready: "Pronto para envio", queued: "Em fila", processing: "Processando", cancel_requested: "Cancelamento solicitado", completed: "Concluído", failed: "Falhou", canceled: "Cancelado"};
  let token = null, subject = null, capabilities = null, detailId = null, busy = false, refreshing = false;
  let pendingBatch = null, exportPending = null, resultView = null;
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
  const size = (n) => n == null ? "Tamanho no relatório" : `${(n / 1024).toFixed(0)} KB`;
  const date = (s) => s ? new Date(s).toLocaleString("pt-BR") : "—";
  const state = (message) => { $("job-state").textContent = message; $("job-state").classList.remove("hidden"); };
  const gone = (j) => ["expired", "deleted"].includes(j.retention?.state);
  const available = (j) => j.status === "completed" && !gone(j) && j.download_url;
  const preferenceKey = () => `ocr-display:${subject}`;
  const savePreferences = () => {
    try { localStorage.setItem(preferenceKey(), JSON.stringify({search: $("search-input").value, sort: $("sort-select").value})); } catch (_) { /* optional */ }
  };

  async function api(path, options = {}) {
    if (!path.startsWith("/api/v1/")) throw new Error("Caminho de operação inválido.");
    const response = await fetch(path, {...options, credentials: "omit", headers: {...options.headers, Authorization: `Bearer ${token}`}});
    if (response.status === 401) { token = null; location.assign("/login"); throw new Error("Sessão encerrada."); }
    if (!response.ok) {
      let error;
      try { error = (await response.json()).error; } catch (_) { /* sanitized fallback */ }
      const e = new Error(`${error?.message || "Operação não concluída"} (${error?.code || response.status}).`);
      e.status = response.status; e.code = error?.code; throw e;
    }
    return response;
  }
  const json = async (path, options) => (await api(path, options)).json();
  const post = (path, body, key) => json(path, {method: "POST", headers: {"Content-Type": "application/json", ...(key ? {"Idempotency-Key": key} : {})}, body: JSON.stringify(body)});
  const act = (operation) => async (...args) => {
    if (busy) return;
    busy = true; render();
    try { await operation(...args); } catch (e) { state(e.message); }
    finally { busy = false; render(); }
  };
  const values = () => [...cards.values()].filter(j => !hidden.has(j.job_id));
  function visible() {
    const filter = document.querySelector(".filter-chip.active")?.dataset.filter || "all";
    const search = $("search-input").value.toLocaleLowerCase();
    const result = values().filter(j => (filter === "all" || j.status === filter) && `${j.filename} ${j.relativePath || ""}`.toLocaleLowerCase().includes(search));
    const order = $("sort-select").value;
    return result.sort((a,b) => order === "name" ? a.filename.localeCompare(b.filename) : order === "recent" ? (b.created_at || "").localeCompare(a.created_at || "") : order.startsWith("size_") ? ((a.file?.size || 0) - (b.file?.size || 0)) * (order === "size_desc" ? -1 : 1) : (labels[a.status] || a.status).localeCompare(labels[b.status] || b.status));
  }
  const button = (action, id, text) => `<button type="button" class="secondary-button" data-action="${action}" data-id="${esc(id)}" ${busy ? "disabled" : ""}>${text}</button>`;
  function actions(j) {
    const id = j.job_id;
    let html = button("detail", id, "Detalhes");
    if (available(j)) html += button("download", id, "Baixar PDF");
    if (active.has(j.status)) html += button("cancel", id, "Cancelar");
    if (terminal.has(j.status) && !gone(j)) html += button("retry", id, "Reprocessar") + button("delete", id, "Excluir arquivos");
    return html;
  }
  function render() {
    const all = values(), list = visible();
    $("cards-grid").classList.toggle("hidden", !all.length);
    $("empty-state").classList.toggle("hidden", !!all.length);
    $("cards-grid").innerHTML = list.map(j => `<article class="file-card status-${esc(j.status)} ${selected.has(j.job_id) ? "selected" : ""}" data-card-id="${esc(j.job_id)}">
      <header class="card-topline"><label class="card-toggle"><input type="checkbox" data-select="${esc(j.job_id)}" ${selected.has(j.job_id) ? "checked" : ""} ${busy ? "disabled" : ""}> Selecionar</label><span class="card-badge">${esc(labels[j.status] || j.status)}</span></header>
      <h3>${esc(j.filename)}</h3><p class="card-path">${esc(j.relativePath || "")}</p>
      <div class="card-meta"><span>${size(j.file?.size)}</span><span>${j.status === "ready" ? "Arquivo local" : "Job " + esc(j.job_id.slice(0,8))}</span></div>
      <p>${gone(j) ? (j.retention.state === "deleted" ? "Arquivos excluídos" : "Arquivos expirados") : j.retention?.expires_at ? "Disponível até " + date(j.retention.expires_at) : ""}${j.retention?.purge_pending ? " · Remoção física pendente" : ""}</p>
      <div class="card-progress"><div class="card-progress__label">${esc(labels[j.status])} · ${Number(j.progress_percent || 0)}%</div><div class="card-progress__track"><span class="card-progress__fill" style="width:${Math.min(100, Math.max(0, Number(j.progress_percent || 0)))}%"></span></div></div>
      <footer class="card-footer">${actions(j)}</footer></article>`).join("") || "<p>Nenhum arquivo corresponde ao filtro.</p>";
    $("metric-total").textContent = all.length;
    $("metric-ready").textContent = all.filter(j => j.status === "ready").length;
    $("metric-completed").textContent = all.filter(j => j.status === "completed").length;
    $("metric-volume").textContent = size(all.reduce((n,j) => n + (j.file?.size || 0),0));
    $("results-summary").textContent = `Mostrando ${list.length} de ${all.length} arquivos. ${hidden.size} oculto(s) nesta aba.`;
    const running = all.filter(j => active.has(j.status)).length;
    $("progress-label").textContent = `${running} job(s) ativo(s)`;
    $("progress-detail").textContent = "Histórico restaurado do servidor. Progresso informado pelo processamento.";
    $("progress-fill").style.width = `${all.length ? all.reduce((n,j)=>n+(j.progress_percent||0),0)/all.length : 0}%`;
    $("queue-button").textContent = pendingBatch ? "Retomar envio anterior" : "Adicionar selecionados à fila";
    $("queue-button").disabled = busy || !capabilities || (!pendingBatch && !all.some(j => selected.has(j.job_id) && j.status === "ready"));
    $("clear-queue-button").disabled = busy || !all.some(j => selected.has(j.job_id) && active.has(j.status));
    $("download-all-button").disabled = busy || !all.some(available);
    $("remove-completed-button").disabled = busy || !all.some(j => terminal.has(j.status));
    $("reset-workspace-button").disabled = busy || !!pendingBatch;
    $("refresh-workspace-button").disabled = busy;
    if (detailId) renderDetail();
  }
  function renderDetail() {
    const j = cards.get(detailId);
    if (!j) return;
    $("detail-drawer").classList.remove("hidden");
    $("detail-title").textContent = j.filename;
    $("detail-body").innerHTML = `<dl class="detail-list"><div><dt>Estado</dt><dd>${esc(labels[j.status])}</dd></div><div><dt>Job</dt><dd>${esc(j.job_id)}</dd></div><div><dt>Criado</dt><dd>${date(j.created_at)}</dd></div><div><dt>Atualizado</dt><dd>${date(j.updated_at)}</dd></div><div><dt>Qualidade heurística</dt><dd>${esc(j.quality || "—")}</dd></div><div><dt>Retenção</dt><dd>${esc(j.retention?.state || "Arquivo local")}</dd></div><div><dt>Origem de retry</dt><dd>${esc(j.parent_job_id || "—")}</dd></div><div><dt>Erro</dt><dd>${esc(j.error_code || j.error || "—")}</dd></div></dl>
      <div class="drawer-actions">${actions(j)}${button("hide",j.job_id,"Ocultar card")}${available(j) ? button("text",j.job_id,"Consultar texto") + button("report",j.job_id,"Consultar relatório") : ""}</div>
      <div id="result-content" class="result-content" aria-live="polite"></div>`;
    if (resultView?.id === detailId && !gone(j)) showResult(resultView.action, resultView.id, resultView.result);
  }
  function merge(job) {
    const old = cards.get(job.job_id);
    cards.set(job.job_id, {...old, ...job});
  }
  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      // Collect every page before replacing history: a failed page cannot erase cards.
      const jobs = new Map(); let cursor;
      do {
        const query = new URLSearchParams({limit: "100"}); if (cursor) query.set("cursor",cursor);
        const page = await json("/api/v1/jobs?" + query);
        page.items.forEach(j => jobs.set(j.job_id,j)); cursor = page.next_cursor;
      } while (cursor);
      for (const [id,j] of cards) if (j.status !== "ready" && !jobs.has(id)) { cards.delete(id); selected.delete(id); }
      for (const job of jobs.values()) merge(job);
      render();
    } finally { refreshing = false; }
  }
  function options() {
    const extract = $("processing-mode").value === "extract_text";
    return {processing_mode: extract ? "extract_text" : "preserve_existing_text", languages: extract ? [] : $("ocr-language").value.split("+"),
      output_format: extract ? "pdf_searchable" : $("output-format").value,
      compression_profile: extract ? "none" : $("compression-profile").value,
      fallback_policy: extract ? "disabled" : $("fallback-policy").value};
  }
  function addFiles(files) {
    const ids=[];
    for (const file of files) {
      if (!file.name.toLowerCase().endsWith(".pdf")) continue;
      if (file.size > (capabilities?.limits?.max_upload_bytes || 80*1024*1024)) { state(`Arquivo acima do limite: ${file.name}`); continue; }
      const id=crypto.randomUUID();
      cards.set(id,{job_id:id, filename:file.name, relativePath:file.webkitRelativePath || "", file, status:"ready", progress_percent:0});
      selected.add(id); ids.push(id);
    }
    render(); return ids;
  }
  async function prepare(j) {
    if (!j.upload) j.upload = await post("/api/v1/upload-sessions",{filename:j.filename,size_bytes:j.file.size});
    try {await api(j.upload.content_url,{method:"PUT",headers:{"Content-Type":"application/pdf"},body:j.file});}
    catch(e) {if([400,404,409,410,413,422].includes(e.status)) delete j.upload;throw e;}
    return j.upload.upload_id;
  }
  async function enqueue(ids) {
    if (!capabilities) throw new Error("Aguarde a consulta de capacidades antes de enviar.");
    const jobs=ids.map(id=>cards.get(id)).filter(j=>j?.status === "ready");
    if (!jobs.length) return;
    const limit=capabilities.limits.batch_max_items;
    if (jobs.length>limit) throw new Error(`Selecione até ${limit} arquivos por lote.`);
    // The full request and key survive transport errors in this tab.
    if (!pendingBatch) pendingBatch={key:crypto.randomUUID(),ids,options:options()};
    let receipt;
    try {
      if (!pendingBatch.body) {
        const items=[];
        for (const j of jobs) items.push({client_item_id:j.job_id,upload_id:await prepare(j),options:pendingBatch.options});
        pendingBatch.body={items};
      }
      receipt=await post("/api/v1/batches",pendingBatch.body,pendingBatch.key);
    }
    catch(e) {
      // Definitive admission rejection permits a new request; ambiguous errors retain the exact request.
      if ([400,403,404,409,410,413,422].includes(e.status)) {
        pendingBatch=null;
      }
      throw e;
    }
    for (const item of receipt.items) {
      const old=cards.get(item.client_item_id); cards.delete(item.client_item_id); selected.delete(item.client_item_id);
      merge({...old,job_id:item.job_id,status:"queued",batch_id:receipt.batch_id});
    }
    pendingBatch=null; state(`Lote ${receipt.batch_id}: ${receipt.items.length} arquivo(s) admitido(s).`); await refresh();
  }
  async function download(path, filename) {
    const blob = await (await api(path)).blob();
    const url=URL.createObjectURL(blob), anchor=document.createElement("a");
    anchor.href=url; anchor.download=filename; anchor.click(); setTimeout(()=>URL.revokeObjectURL(url),60000);
  }
  async function dispatch(action,id) {
    const j=cards.get(id); if (!j) return;
    if (action === "detail") { if(detailId !== id) resultView=null; detailId=id; renderDetail(); return; }
    if (action === "hide") { hidden.add(id); selected.delete(id); detailId=null; $("detail-drawer").classList.add("hidden"); render(); return; }
    if (action === "download") return download(`/api/v1/jobs/${id}/artifact/content`,`${j.filename.replace(/\.pdf$/i,"")}-searchable.pdf`);
    if (action === "cancel") { await post(`/api/v1/jobs/${id}/cancel`,{}); state("Cancelamento solicitado; acompanhe o estado do job."); }
    if (action === "delete") {
      if (!confirm("Excluir os arquivos deste job? O histórico permanece. Esta ação é irreversível.")) return;
      const receipt=await json(`/api/v1/jobs/${id}/artifacts`,{method:"DELETE"});
      state(receipt.retention.purge_pending ? "Acesso encerrado; aguardando remoção física." : "Arquivos excluídos.");
    }
    if (action === "retry") {
      if (!j.retryRequest) j.retryRequest={key:crypto.randomUUID(),body:{options:options()}};
      const receipt=await post(`/api/v1/jobs/${id}/retry`,j.retryRequest.body,j.retryRequest.key);
      delete j.retryRequest; state(`Novo job: ${receipt.job_id}. Original preservado.`);
    }
    if (action === "text" || action === "report") {
      const result=await json(`/api/v1/jobs/${id}/${action}`);
      showResult(action,id,result); return;
    }
    await refresh();
  }
  function showResult(action,id,result) {
    resultView={action,id,result};
    const el=$("result-content"); if (!el) return;
    el.replaceChildren(); const pre=document.createElement("pre");
    pre.textContent=action === "text" ? `Página ${result.page}/${result.page_count}\n${result.text}` : JSON.stringify(result,null,2);
    el.appendChild(pre);
    if (action === "text" && result.next_page != null) {
      const next=document.createElement("button"); next.textContent="Próximo trecho";
      next.onclick=async()=>{next.disabled=true;try {showResult(action,id,await json(`/api/v1/jobs/${id}/text?page=${result.next_page}&offset=${result.next_offset}`));}catch(e){state(e.message);next.disabled=false;}};
      el.appendChild(next);
    }
  }
  document.addEventListener("click",event=>{
    const b=event.target.closest("[data-action]");
    if (b) act(()=>dispatch(b.dataset.action,b.dataset.id))();
  });
  $("cards-grid").addEventListener("change",e=>{if(e.target.dataset.select){const id=e.target.dataset.select;e.target.checked ? selected.add(id) : selected.delete(id);render();}});
  for(const id of ["directory-input","multi-file-input"]) $(id).addEventListener("change",e=>{addFiles(e.target.files);e.target.value="";});
  $("queue-button").onclick=act(()=>enqueue(pendingBatch?.ids || [...selected].filter(id=>cards.get(id)?.status === "ready")));
  $("upload-form").onsubmit=event=>{event.preventDefault();const files=[...$("file-input").files];act(async()=>{const ids=pendingBatch?.ids || addFiles(files);await enqueue(ids);$("file-input").value="";})();};
  $("clear-queue-button").onclick=act(async()=>{
    const ids=[...selected].filter(id=>active.has(cards.get(id)?.status));
    if(ids.length>25) throw new Error("Selecione até 25 jobs por cancelamento.");
    if(!confirm(`Solicitar cancelamento de ${ids.length} job(s) selecionado(s)?`)) return;
    const result=await post("/api/v1/jobs/cancel-batch",{job_ids:ids});
    state(result.items.map(i=>`${i.job_id.slice(0,8)}: ${i.outcome}`).join("; "));await refresh();
  });
  $("remove-completed-button").onclick=()=>{values().filter(j=>terminal.has(j.status)).forEach(j=>hidden.add(j.job_id));render();};
  $("reset-workspace-button").onclick=()=>{
    if(!confirm("Limpar apenas esta visualização e os arquivos locais ainda não enviados? Jobs no servidor continuam executando.")) return;
    for(const [id,j] of cards) {if(j.status === "ready") cards.delete(id);else hidden.add(id);}
    selected.clear(); detailId=null; $("detail-drawer").classList.add("hidden"); render();
  };
  $("refresh-workspace-button").onclick=act(async()=>{hidden.clear();await refresh();state("Histórico atualizado do servidor.");});
  $("download-all-button").onclick=act(async()=>{
    const ids=values().filter(available).map(j=>j.job_id);
    if(ids.length>25) throw new Error("O ZIP aceita até 25 resultados. Oculte os demais ou baixe os PDFs individualmente.");
    if(!exportPending) exportPending={key:crypto.randomUUID(),body:{job_ids:ids}};
    if(!exportPending.receipt) exportPending.receipt=await post("/api/v1/exports",exportPending.body,exportPending.key);
    const id=exportPending.receipt.export_id;
    for(let n=0;n<60;n++) {
      let result;
      try {result=await json(`/api/v1/exports/${id}`);}
      catch(e) {if([404,410].includes(e.status)) exportPending=null;throw e;}
      if(result.status === "ready") {await download(result.download_url,"resultados-ocr.zip");exportPending=null;return;}
      if(result.status === "failed") {exportPending=null;throw new Error(`Exportação falhou: ${result.error_code}`);}
      state(`Preparando ZIP ${id.slice(0,8)}…`);await new Promise(resolve=>setTimeout(resolve,1000));
    }
    throw new Error("ZIP ainda em preparação. Clique novamente para consultar o mesmo pedido.");
  });
  $("select-visible-button").onclick=()=>{visible().forEach(j=>selected.add(j.job_id));render();};
  $("clear-selection-button").onclick=()=>{selected.clear();render();};
  $("filter-group").onclick=e=>{const b=e.target.closest("[data-filter]");if(b){document.querySelectorAll(".filter-chip").forEach(x=>x.classList.toggle("active",x===b));render();}};
  $("search-input").oninput=()=>{savePreferences();render();};$("sort-select").onchange=()=>{savePreferences();render();};
  $("detail-close-button").onclick=()=>{detailId=null;$("detail-drawer").classList.add("hidden");};
  async function start() {
    try {
      const response=await fetch("/auth/browser-session",{method:"POST",credentials:"same-origin",headers:{"X-OCR-Browser":"1"}});
      if(response.status === 401) {location.assign("/login");return;}
      if(!response.ok) throw new Error("Não foi possível recuperar a sessão do navegador.");
      const session=await response.json(); token=session.access_token;subject=session.subject;
      try {localStorage.removeItem("ocr-recognizer-workspace-v2");const pref=JSON.parse(localStorage.getItem(preferenceKey())||"{}");$("search-input").value=pref.search||"";$("sort-select").value=pref.sort||"status";}catch(_){/* no restored job content */}
      await refresh();
      capabilities=await json("/api/v1/capabilities");
      const optionLabels={extract_text:"Extrair texto existente",preserve_existing_text:"OCR preservando texto",eng:"Inglês",por:"Português","por+eng":"Português e inglês",pdf_searchable:"PDF pesquisável",pdfa:"PDF/A (requer validação)",none:"Sem compactação",balanced:"Equilibrada",disabled:"Desativado",if_low_quality:"Tentar em baixa qualidade"};
      const fill=(id,items)=>{$(id).replaceChildren(...items.map(value=>{const option=document.createElement("option");option.value=value;option.textContent=optionLabels[value]||value;return option;}));};
      fill("processing-mode",capabilities.processing_modes);fill("ocr-language",[...capabilities.languages,...(capabilities.languages.length===2?["por+eng"]:[])]);
      fill("output-format",capabilities.output_formats);fill("compression-profile",capabilities.compression_profiles);fill("fallback-policy",capabilities.fallback_policies);
      $("processing-mode").value=capabilities.processing_modes.includes("preserve_existing_text")?"preserve_existing_text":"extract_text";
      for (const [id,value] of [["ocr-language","por"],["output-format","pdf_searchable"],["compression-profile","none"],["fallback-policy","disabled"]]) {
        if([...$(id).options].some(o=>o.value===value)) $(id).value=value;
      }
      const modeChanged=()=>{for(const id of ["ocr-language","output-format","compression-profile","fallback-policy"]) $(id).disabled=$("processing-mode").value === "extract_text";};
      $("processing-mode").onchange=modeChanged; modeChanged();
      $("capability-note").textContent="Extrair texto usa a camada existente do PDF. OCR preserva o texto e reconhece imagens quando o motor está disponível. Qualidade é heurística; PDF/A requer validação.";
      render(); state("Histórico sincronizado.");
      setInterval(async()=>{if(!busy && !detailId){try{await refresh();}catch(e){state(e.message);}}},5000);
    } catch(e) {state(e.message);}
  }
  render();start();
})();
