"""OCR-EV-06 end to end: OCR job -> metadata over MCP -> approved graph -> ocr-rag/2 -> receiver graph.

Synthetic documents only. The OCR server must have RAG_CONFIG_FILE with destinations
`proc` (process A) and `outro` (process B) owned by the admin user; the receiver
tokens for reading come from private files. Writes `results.json` (no secrets) and
`export.json` (the receiver's scoped graph export, input of the GraphRAG adapter).
"""
import argparse
import asyncio
import hashlib
import io
import json
import sys
import time
from pathlib import Path

import httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parent))
from matrix import Recorder, admin_token, put_bytes  # noqa: E402

DOCUMENTS = {
    # key: (destination, pages)
    "alfa": ("proc", ["Oficio PROC-2026-77 emitido pela Prefeitura Municipal de Alfa.",
                      "Partes: Prefeitura Municipal de Alfa e Fornecedor Beta Ltda. Valor declarado 1500.00."]),
    "beta": ("proc", ["Despacho PROC-2026-77 emitido pela Prefeitura Municipal de Alfa.",
                      "Partes: Prefeitura Municipal de Alfa e Construtora Gama. Valor declarado 820.50."]),
    "outro": ("outro", ["Oficio PROC-2026-99 emitido pela Prefeitura Municipal de Alfa.",
                        "Partes: Prefeitura Municipal de Alfa e Fornecedor Delta. Valor declarado 99.00."]),
}
FIELDS = [
    {"name": "numero_processo", "type": "identifier", "description": "Numero do processo.", "indexable": True,
     "sensitivity": "public"},
    {"name": "emissor", "type": "string", "description": "Emissor declarado.", "indexable": True},
    {"name": "partes", "type": "string", "description": "Partes.", "indexable": True, "cardinality": "many"},
    {"name": "valor", "type": "decimal", "description": "Valor declarado.", "indexable": True},
    {"name": "assunto", "type": "string", "description": "Restrito.", "indexable": True, "sensitivity": "restricted"},
    {"name": "nota", "type": "string", "description": "Nao indexavel."},
]


def pdf(pages):
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer)
    for text in pages:
        c.setFont("Helvetica", 14)
        c.drawString(40, 760, text)
        c.showPage()
    c.save()
    return buffer.getvalue()


def evidence(job_id, texts, page, quote, item_index=None):
    text = texts[page - 1]
    start = text.index(quote)
    ref = {"document_id": job_id, "page": page, "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
           "start": start, "end": start + len(quote), "quote": quote}
    if item_index is not None:
        ref["item_index"] = item_index
    return ref


def values(job_id, texts):
    """Literal values located in the OCR text itself, so offsets come from the server's text."""
    first, second = texts
    number = next(w.strip(".") for w in first.split() if w.startswith("PROC-"))
    parties = second.split("Partes: ", 1)[1].split(". Valor")[0].split(" e ")
    amount = second.rsplit("Valor declarado ", 1)[1].rstrip(".")
    ev = lambda *a: [evidence(job_id, texts, *a)]
    return [
        {"field_name": "numero_processo", "raw": number, "evidence": ev(1, number)},
        {"field_name": "emissor", "raw": "Prefeitura Municipal de Alfa", "evidence": ev(1, "Prefeitura Municipal de Alfa")},
        {"field_name": "partes", "raw": parties,
         "evidence": [evidence(job_id, texts, 2, p, i) for i, p in enumerate(parties)]},
        {"field_name": "valor", "raw": amount, "evidence": ev(2, amount)},
        {"field_name": "assunto", "raw": "sigiloso"},
        {"field_name": "nota", "raw": "interna"},
    ]


async def tool(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    if result.is_error:
        raise RuntimeError(name + ": " + " ".join(getattr(c, "text", "") for c in result.content)[:300])
    return result.structured_content


async def run(args, rec: Recorder):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    http = httpx2.Client(base_url=args.api, trust_env=False, timeout=60)
    token = admin_token(http, Path(args.admin_env))
    auth = {"Authorization": "Bearer " + token}
    http.headers.update(auth)
    rag = httpx2.Client(base_url=args.rag, trust_env=False, timeout=60)
    read = {key: Path(path).read_text().strip() for key, path in
            (("proc", args.rag_token_proc), ("outro", args.rag_token_outro))}
    run_id = str(int(time.time()))
    destinations = http.get("/api/v1/rag/destinations").json()["items"]
    rec.check("ocr", "destinations", {d["key"] for d in destinations} >= {"proc", "outro"},
              ceilings={d["key"]: d["graph_sensitivity_ceiling"] for d in destinations})
    context = http.post("/api/v1/contexts", json={"name": "EV06 " + run_id},
                        headers={"Idempotency-Key": "ev06-ctx-" + run_id}).json()
    drafted = http.post(f"/api/v1/contexts/{context['context_id']}/types",
                        json={"name": "Oficio sintetico", "specific_fields": FIELDS},
                        headers={"Idempotency-Key": "ev06-type-" + run_id}).json()
    version = http.post(f"/api/v1/document-types/{drafted['type_id']}/versions",
                        json={"expected_revision": 1, "expected_context_revision": 1},
                        headers={"Idempotency-Key": "ev06-pub-" + run_id}).json()
    rec.check("ocr", "type_published", "version" in version, version=version.get("version"))
    jobs = {}
    for key, (_, pages) in DOCUMENTS.items():
        data = pdf(pages)
        receipt = http.post("/api/v1/upload-sessions", json={"filename": f"ev06-{key}.pdf", "size_bytes": len(data),
                                                              "sha256": hashlib.sha256(data).hexdigest()}).json()
        assert put_bytes(args.api, token, receipt, data) == 200
        job = http.post("/api/v1/jobs", json={"upload_id": receipt["upload_id"], "options": {}},
                        headers={"Idempotency-Key": f"ev06-job-{key}-{run_id}"}).json()
        jobs[key] = job["job_id"]
    for key, job_id in jobs.items():
        for _ in range(300):
            state = http.get(f"/api/v1/jobs/{job_id}").json()["status"]
            if state in ("completed", "failed", "canceled"):
                break
            time.sleep(1)
        rec.check("ocr", f"job_completed:{key}", state == "completed", status=state)

    graphs = {}
    async with httpx2.AsyncClient(headers=auth, trust_env=False, timeout=60) as transport_http:
        async with Client(streamable_http_client(args.api + "/mcp", http_client=transport_http)) as mcp:
            for key, job_id in jobs.items():
                texts = []
                for page in (1, 2):
                    chunk = await tool(mcp, "ocr_get_document_canonical_chunk", job_id=job_id, page=page)
                    texts.append(chunk["text"])
                rec.check("ocr", f"ocr_text:{key}", all("Prefeitura Municipal de Alfa" in t for t in texts))
                saved = await tool(mcp, "ocr_save_document_metadata", job_id=job_id, idempotency_key=f"ev06-save-{key}-{run_id}",
                                   body={"expected_head": None, "expected_sha256": None, "segments": [{
                                       "client_key": "main", "segment_id": None, "page_start": 1, "page_end": 2,
                                       "type_id": version["type_id"], "type_version": version["version"],
                                       "fields": values(job_id, texts)}]})
                segment = saved["segment_ids"]["main"]
                approved = await tool(mcp, "ocr_approve_document_metadata", job_id=job_id,
                                      idempotency_key=f"ev06-approve-{key}-{run_id}",
                                      body={"revision_id": saved["revision_id"], "expected_head": saved["revision_id"],
                                            "expected_sha256": saved["sha256"],
                                            "reviewed_fields": [{"segment_id": segment, "field_name": f["name"]}
                                                                for f in FIELDS]})
                rec.check("ocr", f"approved_via_mcp:{key}", approved["state"] == "approved")
                via_mcp = await tool(mcp, "ocr_get_document_graph", job_id=job_id)
                via_http = http.get(f"/api/v1/jobs/{job_id}/graph").json()
                rec.check("ocr", f"graph_mcp_equals_http:{key}", via_mcp == via_http)
                omitted = via_http["omitted"]
                rec.check("ocr", f"graph_omissions:{key}",
                          omitted == {"not_indexable": 1, "above_ceiling": 1, "not_reviewed": 0}, omitted=omitted)
                graphs[key] = via_http

    deliveries = {}
    for key, job_id in jobs.items():
        status = http.post("/api/v1/rag/deliveries", json={"job_id": job_id, "destination": DOCUMENTS[key][0],
                                                           "include_graph": True}).json()
        deliveries[key] = status["id"]
    for key, delivery in deliveries.items():
        for _ in range(120):
            state = http.get(f"/api/v1/rag/deliveries/{delivery}").json()["state"]
            if state in ("indexed", "resolution", "canceled"):
                break
            time.sleep(1)
        receipt = http.get(f"/api/v1/rag/deliveries/{delivery}/receipt").json()
        rec.check("rag", f"delivered_indexed:{key}", state == "indexed" and receipt["contract_version"] == "ocr-rag/2",
                  state=state, roles=sorted(a["role"] for a in receipt.get("artifacts", [])))

    scope = {"proc": {"destination": "processos", "kind": "process_record", "process": "proc-ev06"},
             "outro": {"destination": "processos", "kind": "process_record", "process": "proc-outro"}}
    search = lambda who, body: rag.post("/v1/graph/search", headers={"Authorization": "Bearer " + read[who]},
                                        json={**scope[who], **body})
    hits = search("proc", {"query": "Prefeitura Municipal de Alfa", "limit": 20})
    rec.check("rag", "graph_search_ok", hits.status_code == 200, status=hits.status_code)
    body = hits.json()
    found = body.get("hits", [])
    documents = {h["node"]["node_id"]: h for h in found}
    alfa_ids = {n["node_id"] for n in graphs["alfa"]["nodes"]}
    beta_ids = {n["node_id"] for n in graphs["beta"]["nodes"]}
    outro_ids = {n["node_id"] for n in graphs["outro"]["nodes"]}
    rec.check("rag", "same_name_distinct_nodes", len({i for i in documents if i in alfa_ids}) >= 1
              and len({i for i in documents if i in beta_ids}) >= 1)
    dumped = json.dumps(body)
    rec.check("rag", "no_cross_process_leak", not any(i in dumped for i in outro_ids)
              and "Fornecedor Delta" not in dumped, hits=len(found))
    numbers = search("proc", {"query": "PROC-2026-77", "limit": 10}).json().get("hits", [])
    links = [link for h in numbers for link in h.get("candidate_links", [])]
    rec.check("rag", "identifier_hint_links_documents",
              any(link.get("node_id") in beta_ids | alfa_ids for link in links)
              and not any(link.get("node_id") in outro_ids for link in links), links=len(links))
    evidence_ok = all(c.get("quote") and c.get("pdf_sha256") for h in found for e in h.get("edges", [])
                      for c in e.get("citations", e.get("evidence", [])))
    rec.check("rag", "citations_carry_quote_and_pdf_hash", evidence_ok)
    other = search("outro", {"query": "Prefeitura Municipal de Alfa", "limit": 20}).json().get("hits", [])
    rec.check("rag", "other_process_sees_only_its_own", bool(other) and all(
        h["node"]["node_id"] in outro_ids for h in other))
    rec.check("rag", "unauthenticated_refused", rag.post("/v1/graph/search", json={**scope["proc"], "query": "Alfa"}
                                                         ).status_code == 401)
    export = rag.post("/v1/graph/export", headers={"Authorization": "Bearer " + read["proc"]}, json=scope["proc"])
    rec.check("rag", "export_ok", export.status_code == 200, status=export.status_code)
    exported = export.json()
    ids = {n["node_id"] for d in exported.get("documents", []) for n in d["graph"]["nodes"]}
    rec.check("rag", "export_scoped", ids == alfa_ids | beta_ids, documents=len(exported.get("documents", [])))
    (out / "export.json").write_text(json.dumps(exported, ensure_ascii=False), encoding="utf-8")
    (out / "graphs.json").write_text(json.dumps(graphs, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"jobs": jobs, "deliveries": deliveries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--admin-env", required=True)
    parser.add_argument("--rag", required=True)
    parser.add_argument("--rag-token-proc", required=True)
    parser.add_argument("--rag-token-outro", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    rec = Recorder()
    try:
        ids = asyncio.run(run(args, rec))
    except Exception as error:  # record and fail; never print request bodies or tokens
        rec.check("e2e", "completed_without_exception", False, error=type(error).__name__ + ": " + str(error)[:300])
        ids = {}
    Path(args.out, "results.json").write_text(json.dumps({"ids": ids, "checks": rec.rows}, indent=1), encoding="utf-8")
    failed = [r for r in rec.rows if not r["ok"]]
    print(f"{len(rec.rows) - len(failed)}/{len(rec.rows)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
