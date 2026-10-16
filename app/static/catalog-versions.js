/* Immutable version reader. Session and publication state remain with the caller. */
(() => {
  "use strict";
  function create({ui, get, getState, object, revisionOk, dateOk, fieldsOk, identityOk, clone, freeze, equal, onDetailChange}) {
    const versionUi = (id) => document.getElementById(`version-${id}`);
    const versionEditor = window.OcrCatalogFields.create(versionUi("fields"), {
      idPrefix: "immutable-version", readonly: true,
    });
    let destroyed = false;
    let versionBinding = null, versionItems = [], versionCursor = null;
    let versionListGeneration = 0, versionDetailGeneration = 0;
    let versionListing = false, versionReading = false, displayedVersion = null;
    let displayedRecord = null;
    function getDisplayed() {
      return versionBound() && displayedRecord ? freeze(clone(displayedRecord)) : null;
    }
    const versionKeys = ["type_id", "context_id", "version", "draft_revision", "context_revision", "schema_version", "schema_sha256", "type_name", "context_name", "created_at"];
    const lifecycleOk = (value) => object(value) &&
      ((value.state === "published" && value.revision === 1) || (value.state === "deprecated" && value.revision === 2)) &&
      dateOk(value.changed_at) && /(?:Z|\+00:00)$/.test(value.changed_at);
    function immutableVersionSummaryOk(binding, value) {
      return identityOk("type", binding.contextId, value) && value.type_id === binding.typeId &&
        [value.version, value.draft_revision, value.context_revision].every(revisionOk) &&
        typeof value.schema_sha256 === "string" && /^[0-9a-f]{64}$/.test(value.schema_sha256) &&
        [value.schema_version, value.type_name, value.context_name].every((item) => typeof item === "string") &&
        value.schema_version.length > 0 && dateOk(value.created_at);
    }
    function versionSummaryOk(binding, value) {
      return immutableVersionSummaryOk(binding, value) && lifecycleOk(value.lifecycle);
    }
    function versionPageOk(binding, value) {
      return object(value) && Array.isArray(value.items) && value.items.every((item) => versionSummaryOk(binding, item)) &&
        (value.next_cursor === null || revisionOk(value.next_cursor));
    }
    function versionRecordOk(binding, summary, value, fromReceipt = false) {
      return versionSummaryOk(binding, value) && versionKeys.every((key) => value[key] === summary[key]) &&
        (fromReceipt || (value.lifecycle.revision >= summary.lifecycle.revision &&
          (value.lifecycle.revision !== summary.lifecycle.revision || equal(value.lifecycle, summary.lifecycle)))) &&
        object(value.schema) && value.schema.schema_version === value.schema_version &&
        [value.schema.common_fields, value.schema.specific_fields, value.schema.fields].every(fieldsOk);
    }
    function versionBound(binding = versionBinding) {
      if (destroyed) return false;
      const state = getState();
      return !!binding && binding === versionBinding && state.active && state.kind === "type" &&
        state.selected === binding.typeId && state.parentId === binding.contextId;
    }
    function clearVersionDetail() {
      if (destroyed) return;
      ++versionDetailGeneration; versionReading = false; displayedVersion = null; displayedRecord = null;
      versionUi("detail").hidden = true;
      for (const id of ["reference", "meta", "lifecycle", "count", "detail-status"]) versionUi(id).textContent = "";
      versionEditor.setValue([], {inherited: []}); versionEditor.setReadonly(true);
      onDetailChange?.();
    }
    function resetVersions() {
      if (destroyed) return;
      ++versionListGeneration; versionListing = false;
      versionBinding = null; versionItems = []; versionCursor = null;
      clearVersionDetail(); versionUi("list").replaceChildren();
      versionUi("list-status").textContent = ""; versionUi("section").hidden = true;
    }
    function updateVersions() {
      if (destroyed) return;
      const state = getState();
      const bound = versionBound(), busy = !bound || state.writing || state.reading;
      versionUi("section").hidden = !bound;
      versionUi("refresh").disabled = busy || versionListing;
      versionUi("more").hidden = versionCursor === null;
      versionUi("more").disabled = busy || versionListing;
      versionUi("close").disabled = busy || (!versionReading && !displayedVersion);
      versionUi("list").setAttribute("aria-busy", String(versionListing));
      versionUi("detail-status").setAttribute("aria-busy", String(versionReading));
      versionUi("list").querySelectorAll("button").forEach((button) => { button.disabled = busy; });
    }
    function renderVersions() {
      if (destroyed) return;
      versionUi("list").replaceChildren(...versionItems.map((item) => {
        const button = ui.el("button", {type: "button", className: "link-button entity__name",
          text: `Versão ${item.version}`, "aria-current": displayedVersion === item.version ? "true" : null});
        button.addEventListener("click", () => openVersion(item));
        return ui.el("li", {className: "entity"}, [ui.el("div", {className: "entity__main"}, [button,
          ui.el("p", {className: "entity__meta", text: `${item.lifecycle.state === "published" ? "Publicada" : "Descontinuada"} · Criada em ${ui.date(item.created_at)}`})])]);
      }));
      updateVersions();
    }
    function bindVersions(record) {
      if (destroyed) return;
      if (!record) { resetVersions(); return; }
      const contextId = record.context_id, typeId = record.type_id;
      if (versionBinding?.typeId !== typeId || versionBinding?.contextId !== contextId) {
        resetVersions(); versionBinding = freeze({typeId, contextId});
      }
      updateVersions();
      void loadVersions();
    }
    async function loadVersions(append = false) {
      if (!versionBound()) return;
      const state = getState();
      if (state.writing || state.reading || versionListing) return;
      if (append && versionCursor === null) return;
      const binding = versionBinding, generation = ++versionListGeneration;
      const after = append ? versionCursor : null;
      const currentList = () => versionBound(binding) && generation === versionListGeneration;
      versionListing = true;
      versionUi("list-status").textContent = "Carregando versões…"; updateVersions();
      try {
        const query = new URLSearchParams({limit: "10"});
        if (after !== null) query.set("after", String(after));
        const page = await get(`/api/v1/document-types/${binding.typeId}/versions?${query}`, {
          valid: (value) => versionPageOk(binding, value),
        });
        if (!currentList()) return;
        const next = append ? versionItems.slice() : [], seen = new Set(next.map((item) => item.version));
        for (const item of page.items) if (!seen.has(item.version)) { seen.add(item.version); next.push(freeze(clone(item))); }
        versionItems = next; versionCursor = page.next_cursor; renderVersions();
        versionUi("list-status").textContent = next.length ? `${next.length} versão(ões) na lista. Situação consultada ao carregar a lista.` : "Este tipo ainda não tem versões imutáveis.";
      } catch (_) {
        if (currentList()) versionUi("list-status").textContent = "Não foi possível carregar as versões. A lista anterior foi preservada. Tente novamente.";
      } finally {
        if (!destroyed && binding === versionBinding && generation === versionListGeneration) { versionListing = false; updateVersions(); }
      }
    }
    function renderVersion(record) {
      if (destroyed) return;
      versionEditor.setValue(clone(record.schema.fields), {inherited: []}); versionEditor.setReadonly(true);
      versionUi("reference").textContent = `Tipo: ${record.type_id} · Versão: ${record.version} · SHA-256: ${record.schema_sha256}`;
      versionUi("meta").textContent = `Contexto: ${record.context_name} · Tipo: ${record.type_name} · draft_revision: ${record.draft_revision} · context_revision: ${record.context_revision} · schema_version: ${record.schema_version} · Criada em ${ui.date(record.created_at)}`;
      const lifecycle = record.lifecycle;
      versionUi("lifecycle").textContent = `${lifecycle.state === "published" ? "Publicada" : "Descontinuada — não disponível para novos vínculos; consulta permitida"} · Revisão ${lifecycle.revision} · Alterada em ${ui.date(lifecycle.changed_at)}. Situação consultada nesta abertura; selecione a versão novamente para atualizar.`;
      versionUi("count").textContent = `${record.schema.fields.length} campo(s) no esquema histórico: ${record.schema.common_fields.length} comuns e ${record.schema.specific_fields.length} específicos. Independente do rascunho atual.`;
      displayedVersion = record.version; displayedRecord = freeze(clone(record));
      versionUi("detail").hidden = false;
      renderVersions();
      onDetailChange?.();
    }
    async function openVersion(item) {
      if (!versionBound()) return;
      const state = getState();
      if (state.writing || state.reading) return;
      const binding = versionBinding, summary = freeze(clone(item));
      clearVersionDetail();
      const generation = versionDetailGeneration;
      const currentDetail = () => versionBound(binding) && generation === versionDetailGeneration;
      versionReading = true; renderVersions();
      versionUi("detail-status").textContent = `Carregando versão ${summary.version}…`; updateVersions();
      try {
        const record = await get(`/api/v1/document-types/${binding.typeId}/versions/${summary.version}`, {
          valid: (value) => versionRecordOk(binding, summary, value),
        });
        if (!currentDetail()) return;
        renderVersion(record);
        versionUi("detail-status").textContent = `Versão ${record.version} carregada para consulta.`;
      } catch (_) {
        if (currentDetail()) {
          clearVersionDetail(); renderVersions();
          versionUi("detail-status").textContent = "Não foi possível abrir a versão. Nenhum detalhe está selecionado. Selecione uma versão para tentar novamente.";
        }
      } finally {
        if (!destroyed && binding === versionBinding && generation === versionDetailGeneration) { versionReading = false; updateVersions(); }
      }
    }
    function refresh() { void loadVersions(); }
    function more() { void loadVersions(true); }
    function close() {
      if (!versionBound()) return;
      const state = getState();
      if (state.writing || state.reading) return;
      clearVersionDetail(); renderVersions();
    }
    versionUi("refresh").addEventListener("click", refresh);
    versionUi("more").addEventListener("click", more);
    versionUi("close").addEventListener("click", close);
    function invalidateReads() {
      if (destroyed) return;
      ++versionListGeneration; versionListing = false; clearVersionDetail();
    }
    function destroy() {
      if (destroyed) return;
      destroyed = true; displayedRecord = null;
      ++versionListGeneration; ++versionDetailGeneration;
      versionUi("refresh").removeEventListener("click", refresh);
      versionUi("more").removeEventListener("click", more);
      versionUi("close").removeEventListener("click", close);
      versionEditor.destroy();
    }
    return {
      getBinding: () => versionBinding,
      getDisplayed,
      lifecycleOk,
      bound: versionBound,
      bind: bindVersions,
      reset: resetVersions,
      update: updateVersions,
      load: loadVersions,
      clearDetail: clearVersionDetail,
      render: renderVersion,
      immutableSummaryOk: immutableVersionSummaryOk,
      recordOk: versionRecordOk,
      invalidateReads,
      destroy,
    };
  }
  window.OcrCatalogVersions = {create};
})();
