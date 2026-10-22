"""OCR-EV-06: ocr-graph/1 export from approved snapshots and its ocr-rag/2 delivery."""
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from app.application import ApplicationError
from app.document_models import ContextCreate, FieldSpec, TypeDraft
from app.rag_mcp_tools import rag_request
from app.rag_models import DeliveryInput
from app.results import JobReport, OutputValidation, QualityReport, digest_file, persist_pages, persist_report
from app.services.context_service import ContextService
from app.graph_contract import GraphDocument, Identity, node_id, parse_graph, verify_graph
from app.services.graph_export import approved_material
from app.services.rag_delivery import Destination, prepare_delivery
from app.services.rag_delivery_contract import parse_manifest, parse_receipt, match_receipt, manifest_digest
from app.services.storage_service import JobStore
from test_document_metadata import runtime, who
from test_document_metadata_approval import APPROVAL_SCOPES
from test_document_package import pdf_bytes

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")
READER = {"jobs:read", "artifacts:read", "metadata:read"}
PAGES = ["Processo 0012/2026 emitido pela Prefeitura Municipal de Alfa em 2026-03-04",
         "Partes: Prefeitura Municipal de Alfa e Fornecedor Beta. Valor 1500.00. CPF 000.000.000-00"]


def fields():
    return [
        FieldSpec(name="numero_processo", type="identifier", description="Process", indexable=True,
                  sensitivity="public"),
        FieldSpec(name="emissor", type="string", description="Issuer", indexable=True),
        FieldSpec(name="partes", type="string", cardinality="many", description="Parties", indexable=True),
        FieldSpec(name="valor", type="decimal", description="Amount", indexable=True),
        FieldSpec(name="data_documento", type="date", description="Date", indexable=True, sensitivity="public"),
        FieldSpec(name="cpf", type="string", description="Restricted", indexable=True, sensitivity="restricted"),
        FieldSpec(name="nota", type="string", description="Not indexable"),
    ]


def quote(page, text, item_index=None):
    start = PAGES[page - 1].index(text)
    ref = dict(page=page, text_sha256=hashlib.sha256(PAGES[page - 1].encode()).hexdigest(),
               start=start, end=start + len(text), quote=text)
    if item_index is not None:
        ref["item_index"] = item_index
    return ref


def document(e, key, approve=True):
    job = e.jobs.create(key + ".pdf", pdf_bytes(PAGES), owner_user_id=1)
    output = job.working_dir / "final-searchable-pdfa.pdf"
    output.write_bytes(pdf_bytes(PAGES, xmp=True))
    out = digest_file(output)
    pages = persist_pages(job.working_dir, PAGES, "pdf_text")
    persist_report(job.working_dir, JobReport(
        job_id=job.job_id, generated_at=datetime.now(timezone.utc), duration_ms=1,
        input=digest_file(job.input_pdf_path), output=out, page_count=2, pages=pages,
        quality=QualityReport(label="HIGH", character_count=sum(map(len, PAGES)), valid_ratio=1.0,
                              min_text=1, valid_ratio_threshold=0.5),
        stages=[], engines={}, fallback_attempted=False, used_fallback=False, warnings=[],
        output_validation=OutputValidation(
            requested_format="pdfa", pdf_readable=True, page_count_matches=True, pdfa_conformance="passed",
            pdfa_profile="2b", pdfa_validator="veraPDF", pdfa_validated_sha256=out.sha256)))
    e.jobs.update(job.job_id, status="completed", progress_percent=100, output_pdf_path=output)
    ev = lambda *a: [dict(quote(*a), document_id=job.job_id)]
    values = [
        dict(field_name="numero_processo", raw="0012/2026", evidence=ev(1, "0012/2026")),
        dict(field_name="emissor", raw="Prefeitura Municipal de Alfa", evidence=ev(1, "Prefeitura Municipal de Alfa")),
        dict(field_name="partes", raw=["Prefeitura Municipal de Alfa", "Fornecedor Beta"],
             evidence=[dict(quote(2, "Prefeitura Municipal de Alfa", 0), document_id=job.job_id),
                       dict(quote(2, "Fornecedor Beta", 1), document_id=job.job_id)]),
        dict(field_name="valor", raw="1500.00", evidence=ev(2, "1500.00")),
        dict(field_name="data_documento", raw="2026-03-04", evidence=ev(1, "2026-03-04")),
        dict(field_name="cpf", raw="000.000.000-00", evidence=ev(2, "000.000.000-00")),
        dict(field_name="nota", raw="interna"),
    ]
    draft, _ = e.service.save(e.owner, job.job_id, dict(
        expected_head=None, expected_sha256=None, segments=[dict(
            client_key="main", segment_id=None, page_start=1, page_end=2, type_id=e.version.type_id,
            type_version=e.version.version, fields=values)]), key=key + "-draft-0001")
    if not approve:
        return SimpleNamespace(job=job, draft=draft, approved=None)
    snapshot = e.service.get(e.owner, job.job_id, draft.revision_id, sha256=draft.sha256)
    body = dict(revision_id=draft.revision_id, expected_head=draft.revision_id, expected_sha256=draft.sha256,
                reviewed_fields=[dict(segment_id=s.segment_id, field_name=f.field_name)
                                 for s in snapshot.segments for f in s.fields if f.raw is not None])
    approved, _ = e.service.approve(who(scopes=APPROVAL_SCOPES), job.job_id, body, key=key + "-approve-0001")
    return SimpleNamespace(job=job, draft=draft, approved=approved)


@pytest.fixture
def env(tmp_path):
    e = SimpleNamespace(jobs=JobStore(tmp_path.absolute() / "jobs"), owner=who(), reader=who(scopes=READER))
    try:
        runtime(e)
        context, _ = ContextService(e.jobs).create(e.owner, ContextCreate(name="Processos"), "context-graph-0001")
        draft, _ = e.types.create(e.owner, context.context_id, TypeDraft(
            name="Processo", specific_fields=fields()), "type-graph-0001")
        e.version, _ = e.types.publish(e.owner, draft.type_id, expected_revision=1, expected_context_revision=1,
                                       key="publish-graph-0001")
        yield e
    finally:
        assert e.jobs._lock.acquire(timeout=5)
        try:
            e.jobs._conn.close()
        finally:
            e.jobs._lock.release()


def graph_of(e, doc, ceiling="internal"):
    material = approved_material(e.service, e.reader, doc.job.job_id, ceiling)
    return material, parse_graph(material.graph)


def test_graph_entities_claims_evidence_and_omissions(env):
    doc = document(env, "alfa")
    material, graph = graph_of(env, doc)
    assert graph.document_id == doc.job.job_id and graph.metadata_revision_id == doc.approved.revision_id
    assert hashlib.sha256(material.metadata).hexdigest() == graph.metadata_sha256 == doc.approved.sha256
    assert graph.output_pdf_sha256 == digest_file(doc.job.output_pdf_path).sha256
    kinds = [node.kind for node in graph.nodes]
    assert kinds.count("document") == 1 and kinds.count("segment") == 1
    entities = {(n.identity.field_name, n.identity.item_index): n for n in graph.nodes if n.kind == "entity"}
    assert {k: (n.entity_type, n.label) for k, n in entities.items()} == {
        ("numero_processo", 0): ("identifier", "0012/2026"),
        ("emissor", 0): ("agent", "Prefeitura Municipal de Alfa"),
        ("partes", 0): ("agent", "Prefeitura Municipal de Alfa"),
        ("partes", 1): ("agent", "Fornecedor Beta")}
    # The same name in two fields stays two document-scoped entities.
    assert entities[("emissor", 0)].node_id != entities[("partes", 0)].node_id
    assert [h.model_dump() for h in entities[("numero_processo", 0)].hints] == [
        {"scheme": "identifier", "field_name": "numero_processo", "value": "0012/2026"}]
    assert all(not n.hints for k, n in entities.items() if k[0] != "numero_processo")
    claims = {c.field_name: (c.value_type, c.value) for c in graph.claims}
    assert claims == {"valor": ("decimal", "1500"), "data_documento": ("date", "2026-03-04")}
    assert graph.omitted.model_dump() == {"not_indexable": 1, "above_ceiling": 1, "not_reviewed": 0}
    by_target = {edge.target: edge for edge in graph.edges}
    beta = by_target[entities[("partes", 1)].node_id]
    assert beta.predicate == "field:partes" and [(x.page, x.quote) for x in beta.evidence] == [(2, "Fornecedor Beta")]
    verify_graph(graph, job_id=doc.job.job_id, pdf_sha256=graph.output_pdf_sha256,
                 metadata_sha256=graph.metadata_sha256, pages=PAGES)
    assert material.graph == approved_material(env.service, env.reader, doc.job.job_id, "internal").graph


def test_ceiling_controls_restricted_and_public_values(env):
    doc = document(env, "teto")
    _, restricted = graph_of(env, doc, "restricted")
    assert "000.000.000-00" in {c.value for c in restricted.claims}
    assert restricted.omitted.above_ceiling == 0
    _, public = graph_of(env, doc, "public")
    assert {n.identity.field_name for n in public.nodes if n.kind == "entity"} == {"numero_processo"}
    assert {c.field_name for c in public.claims} == {"data_documento"}
    assert public.omitted.above_ceiling == 4


def test_documents_never_share_entities(env):
    first, second = document(env, "doc-um"), document(env, "doc-dois")
    ids = [{n.node_id for n in graph_of(env, d)[1].nodes} for d in (first, second)]
    assert not ids[0] & ids[1]
    identity = Identity(scope="document", basis="document")
    assert node_id(first.job.job_id, identity) != node_id(second.job.job_id, identity)


def test_draft_or_unknown_revision_is_refused(env):
    doc = document(env, "rascunho", approve=False)
    for revision in (None, doc.draft.revision_id):
        with pytest.raises(ApplicationError) as caught:
            approved_material(env.service, env.reader, doc.job.job_id, "internal", revision)
        assert caught.value.code == "metadata_not_approved"


def test_tampering_is_detected_by_the_receiver_check(env):
    doc = document(env, "adulterado")
    _, graph = graph_of(env, doc)
    data = graph.model_dump(mode="json")
    edge = next(e for e in data["edges"] if e["evidence"])
    edge["evidence"][0]["quote"] = "X" + edge["evidence"][0]["quote"][1:]
    tampered = GraphDocument.model_validate_json(json.dumps(data))
    with pytest.raises(ValueError, match="graph_evidence_mismatch"):
        verify_graph(tampered, job_id=doc.job.job_id, pdf_sha256=graph.output_pdf_sha256,
                     metadata_sha256=graph.metadata_sha256, pages=PAGES)
    with pytest.raises(ValueError, match="graph_source_mismatch"):
        verify_graph(graph, job_id=doc.job.job_id, pdf_sha256="0" * 64,
                     metadata_sha256=graph.metadata_sha256, pages=PAGES)
    forged = graph.model_dump(mode="json")
    forged["nodes"][1]["node_id"] = "n:" + "0" * 24
    with pytest.raises(ValueError):
        GraphDocument.model_validate_json(json.dumps(forged))


def test_delivery_v2_carries_metadata_and_graph(env):
    doc = document(env, "entrega")
    material = approved_material(env.service, env.reader, doc.job.job_id, "internal")
    dest = Destination("http://127.0.0.1:8092", "processos", env.owner.subject, "process_record",
                       process_reference="proc-1", token="t" * 40)
    plain = parse_manifest(json.dumps(json.loads(
        prepare_delivery(env.jobs, env.owner, doc.job.job_id, dest).payload)["manifest"]).encode())
    assert plain.contract_version == "ocr-rag/1" and plain.metadata_state == "absent"
    prepared = prepare_delivery(env.jobs, env.owner, doc.job.job_id, dest, material)
    body = json.loads(prepared.payload)
    manifest = parse_manifest(json.dumps(body["manifest"]).encode())
    assert manifest.contract_version == "ocr-rag/2" and manifest.metadata_state == "approved"
    assert manifest.delivery_id != plain.delivery_id
    raw = {role: base64.b64decode(value) for role, value in body["artifacts"].items()}
    assert sorted(raw) == ["graph", "metadata", "pages", "pdf"]
    assert raw["metadata"] == material.metadata and raw["graph"] == material.graph
    sha = {a.role: a.sha256 for a in manifest.artifacts}
    pages = [json.loads(line)["text"] for line in raw["pages"].splitlines()]
    verify_graph(parse_graph(raw["graph"]), job_id=manifest.job_id, pdf_sha256=sha["pdf"],
                 metadata_sha256=sha["metadata"], pages=pages)
    receipt = dict(contract_version="ocr-rag/1", delivery_id=manifest.delivery_id,
                   destination_id=manifest.destination_id, manifest_sha256=manifest_digest(manifest),
                   destination_document_id="d" * 32, destination_revision="e" * 64,
                   artifacts=[a.model_dump() for a in manifest.artifacts], storage_state="stored",
                   indexing_state="indexed")
    # A receiver that answers with the old contract version does not match.
    with pytest.raises(ValueError):
        parse_receipt(json.dumps(receipt).encode())
    receipt["contract_version"] = "ocr-rag/2"
    assert match_receipt(manifest, parse_receipt(json.dumps(receipt).encode()))


def test_contract_couples_version_graph_and_approval():
    artifact = lambda role, media: {"role": role, "sha256": "a" * 64, "size_bytes": 10, "media_type": media}
    base = dict(delivery_id="x", destination_id="d", job_id="j", source_revision="r",
                source_kind="normative_source", process_reference=None)
    two = [artifact("pdf", "application/pdf"), artifact("pages", "application/x-ndjson")]
    meta, graph = artifact("metadata", "application/json"), artifact("graph", "application/json")
    cases = [("ocr-rag/1", "approved", two + [meta, graph]), ("ocr-rag/2", "absent", two),
             ("ocr-rag/2", "approved", two + [meta]), ("ocr-rag/2", "draft", two + [meta, graph]),
             ("ocr-rag/2", "absent", two + [graph])]
    for version, state, artifacts in cases:
        with pytest.raises(ValueError):
            parse_manifest(json.dumps(dict(base, contract_version=version, metadata_state=state,
                                           artifacts=artifacts)).encode())
    parse_manifest(json.dumps(dict(base, contract_version="ocr-rag/2", metadata_state="approved",
                                   artifacts=two + [meta, graph])).encode())


def test_mcp_delivery_request_omits_defaults():
    assert rag_request("submit_rag_delivery", [DeliveryInput(job_id="1" * 32, destination="normas")])[2] == {
        "job_id": "1" * 32, "destination": "normas"}
    assert rag_request("submit_rag_delivery", [DeliveryInput(job_id="1" * 32, destination="p", include_graph=True)])[
        2] == {"job_id": "1" * 32, "destination": "p", "include_graph": True}
    with pytest.raises(ValueError):
        DeliveryInput(job_id="1" * 32, destination="p", metadata_revision_id="2" * 32)


@pytest.fixture
def http(env):
    from fastapi import FastAPI
    from fastapi.exceptions import RequestValidationError
    from fastapi.testclient import TestClient
    from app.application import JobApplication
    from app.http_contract import error_response
    from app.routes.graph import router
    from app.security import require_principal
    from app.services.catalog_runtime import CatalogProvider
    from app.services.document_runtime import DocumentProvider
    app = FastAPI()
    app.include_router(router)
    catalog = CatalogProvider(lambda: JobApplication(env.jobs))()
    provider = DocumentProvider(lambda: env.bundle, lambda: catalog)
    app.state.document_provider = provider
    app.dependency_overrides[require_principal] = lambda: env.reader

    @app.exception_handler(ApplicationError)
    async def application_error(request, error):
        return error_response(request, error.status, error.code, error.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return error_response(request, 422, "validation_error", "Invalid request parameters.")

    with TestClient(app) as client:
        try:
            yield env, app, client, provider
        finally:
            client.portal.call(provider.shutdown)


def test_http_mcp_and_stdio_expose_the_same_graph(http):
    import asyncio
    from app.document_mcp_http import document_request
    from app.document_mcp_runtime import execute_document
    from app.remote import OPERATIONS
    from app.security import require_principal
    import re
    e, app, client, provider = http
    doc = document(e, "http")
    url = f"/api/v1/jobs/{doc.job.job_id}/graph"
    response = client.get(url)
    assert response.status_code == 200, response.text
    graph = GraphDocument.model_validate_json(response.content)
    assert graph == parse_graph(approved_material(e.service, e.reader, doc.job.job_id, "internal").graph)
    assert client.get(url, params={"sensitivity_ceiling": "restricted"}).json()["omitted"]["above_ceiling"] == 0
    assert client.get(url, params={"metadata_revision_id": doc.draft.revision_id}).status_code == 409
    assert client.get(url, params={"sensitivity_ceiling": "secret"}).status_code == 422
    app.dependency_overrides[require_principal] = lambda: who("user:2", scopes=READER, admin=True)
    assert client.get(url).status_code == 404
    app.dependency_overrides[require_principal] = lambda: who(scopes=READER - {"metadata:read"})
    assert client.get(url).status_code == 403
    via_mcp = asyncio.run(execute_document(provider, e.reader, "get_document_graph",
                                           (doc.job.job_id, None, "internal")))
    assert via_mcp == graph
    assert document_request("get_document_graph", (doc.job.job_id,)) == (
        "GET", f"/jobs/{doc.job.job_id}/graph", {"sensitivity_ceiling": "internal"}, None, None)
    from mcp.server.mcpserver.exceptions import ToolError
    with pytest.raises(ToolError):
        document_request("get_document_graph", (doc.job.job_id, None, "secret"))
    assert any(re.fullmatch(pattern, url) for pattern in OPERATIONS["GET"])


def test_rag_application_queues_graph_delivery_with_destination_ceiling(env):
    from app.services.rag_application import RagApplication
    from app.services.rag_outbox import DeliveryPolicy, RagOutbox
    doc = document(env, "fila")
    dest = Destination("http://127.0.0.1:8092", "processos", env.owner.subject, "process_record",
                       process_reference="proc-1", token="t" * 40)
    outbox = RagOutbox(env.jobs, DeliveryPolicy(4, 32 * 1024 * 1024, 3600, 300, 3, 10, 60))
    rag = RagApplication(outbox, {"proc": dest}, graph_ceilings={"proc": "restricted"})
    body = DeliveryInput(job_id=doc.job.job_id, destination="proc", include_graph=True)
    with pytest.raises(ApplicationError) as caught:
        rag.submit_rag_delivery(env.owner, body)
    assert caught.value.code == "rag_unavailable"
    rag.documents = lambda: SimpleNamespace(document=env.service)
    with pytest.raises(ApplicationError) as caught:
        rag.submit_rag_delivery(who(scopes={"jobs:read", "artifacts:read"}), body)
    assert caught.value.status == 403
    status = rag.submit_rag_delivery(env.owner, body)
    assert rag.submit_rag_delivery(env.owner, body).id == status.id
    payload = json.loads(outbox.recovery_package(env.owner, status.id))
    graph = parse_graph(base64.b64decode(payload["artifacts"]["graph"]))
    assert graph.sensitivity_ceiling == "restricted" and graph.omitted.above_ceiling == 0
    assert rag.list_rag_destinations(env.owner).items[0].graph_sensitivity_ceiling == "restricted"
    # The job now has a graph delivery; a plain delivery of it is a different package.
    with pytest.raises(ApplicationError) as caught:
        rag.submit_rag_delivery(env.owner, DeliveryInput(job_id=doc.job.job_id, destination="proc"))
    assert caught.value.code == "rag_delivery_conflict"


def test_contract_rejects_chosen_ids_and_incoherent_structure(env):
    doc = document(env, "estrutura")
    _, graph = graph_of(env, doc)
    data = graph.model_dump(mode="json")
    segment = next(n for n in data["nodes"] if n["kind"] == "segment")
    entity = next(n for n in data["nodes"] if n["kind"] == "entity")

    def rejected(mutate, reason):
        copy = json.loads(json.dumps(data))
        mutate(copy)
        with pytest.raises(ValueError, match=reason):
            GraphDocument.model_validate_json(json.dumps(copy))

    def edge_to(copy, node):
        return next(e for e in copy["edges"] if e["target"] == node["node_id"])

    rejected(lambda c: edge_to(c, entity).update(edge_id="r:" + "0" * 24), "relation_identity")
    rejected(lambda c: c["claims"][0].update(claim_id="c:" + "0" * 24), "relation_identity")
    # A field relation must point at the entity of that field and segment.
    def wrong_field(c):
        edge = edge_to(c, entity)
        edge["predicate"] = "field:outro_campo"
        from app.graph_contract import edge_id
        edge["edge_id"] = edge_id(c["document_id"], edge["source"], edge["target"], edge["predicate"])
    rejected(wrong_field, "relation_shape")
    def evidence_outside(c):
        c["nodes"] = [dict(n, page_end=1) if n["kind"] == "segment" else n for n in c["nodes"]]
    rejected(evidence_outside, "evidence_outside_segment")
    assert segment["page_start"] == 1 and segment["page_end"] == 2
