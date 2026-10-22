"""Derive ocr-graph/1 from one approved metadata snapshot (OCR-EV-06).

Only reviewed values of fields declared `indexable` and at or below the
destination's sensitivity ceiling are exported. Values never become shared
entities here: identity stays scoped to document, segment, field and item.
"""
import hashlib
from dataclasses import dataclass

from app.application import ApplicationError
from app.metadata_models import MetadataSnapshot, snapshot_bytes
from app.graph_contract import (
    ENTITY_FIELDS, SENSITIVITY_RANK, Claim, Edge, Evidence, GraphDocument, Hint, Identity, Node, Omitted,
    claim_id, edge_id, graph_bytes, node_id,
)


@dataclass(frozen=True)
class GraphMaterial:
    revision_id: str
    metadata: bytes
    graph: bytes


def _text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _evidence(field, index, many):
    return tuple(Evidence(page=ref.page, start=ref.start, end=ref.end, quote=ref.quote,
                          text_sha256=ref.text_sha256)
                 for ref in field.evidence if (ref.item_index == index if many else True))


def build_graph(snapshot: MetadataSnapshot, metadata_sha256: str, ceiling: str) -> GraphDocument:
    if snapshot.state != "approved":
        raise ValueError("graph_requires_approved_metadata")
    document = snapshot.document_id
    specs = {schema.schema_sha256: {spec["name"]: spec for spec in schema.schema_document["fields"]}
             for schema in snapshot.schemas}

    def make_node(kind, label, sensitivity, identity, **extra):
        identity = Identity(scope="document", **identity)
        return Node(node_id=node_id(document, identity), kind=kind, label=label,
                    sensitivity=sensitivity, identity=identity, **extra)

    root = make_node("document", "Documento " + document, "public", {"basis": "document"})
    nodes, edges, claims = [root], [], []
    omitted = {"not_indexable": 0, "above_ceiling": 0, "not_reviewed": 0}
    for segment in snapshot.segments:
        hub = make_node("segment", f"Segmento p. {segment.page_start}-{segment.page_end}", "public",
                        {"basis": "segment", "segment_id": segment.segment_id},
                        type_id=segment.type_id, type_version=segment.type_version,
                        page_start=segment.page_start, page_end=segment.page_end)
        nodes.append(hub)
        edges.append(Edge(edge_id=edge_id(document, root.node_id, hub.node_id, "has_segment"),
                          source=root.node_id, target=hub.node_id, predicate="has_segment", sensitivity="public"))
        fields = specs.get(segment.schema_sha256)
        if fields is None:
            raise ValueError("graph_schema_missing")
        for field in segment.fields:
            if field.status == "not_found" or field.normalized is None:
                continue
            spec = fields.get(field.field_name)
            if spec is None:
                raise ValueError("graph_field_unknown")
            if field.status != "reviewed":
                omitted["not_reviewed"] += 1
                continue
            if not spec.get("indexable", False):
                omitted["not_indexable"] += 1
                continue
            sensitivity = spec.get("sensitivity", "internal")
            if SENSITIVITY_RANK[sensitivity] > SENSITIVITY_RANK[ceiling]:
                omitted["above_ceiling"] += 1
                continue
            many = isinstance(field.normalized, list)
            for index, value in enumerate(field.normalized if many else [field.normalized]):
                evidence = _evidence(field, index, many)
                kind = spec["type"]
                entity_type = ("identifier" if kind == "identifier"
                               else ENTITY_FIELDS.get(field.field_name) if kind == "string" else None)
                if entity_type is None:
                    claims.append(Claim(
                        claim_id=claim_id(document, hub.node_id, field.field_name, index), subject=hub.node_id,
                        field_name=field.field_name, item_index=index, value_type=kind, value=_text(value),
                        sensitivity=sensitivity, evidence=evidence))
                    continue
                hints = ((Hint(scheme="identifier", field_name=field.field_name, value=_text(value)),)
                         if entity_type == "identifier" else ())
                entity = make_node("entity", _text(value), sensitivity,
                                   {"basis": "field_value", "segment_id": segment.segment_id,
                                    "field_name": field.field_name, "item_index": index},
                                   entity_type=entity_type, hints=hints)
                nodes.append(entity)
                predicate = "field:" + field.field_name
                edges.append(Edge(edge_id=edge_id(document, hub.node_id, entity.node_id, predicate),
                                  source=hub.node_id, target=entity.node_id, predicate=predicate,
                                  sensitivity=sensitivity, evidence=evidence))
    return GraphDocument(schema_version="ocr-graph/1", document_id=document,
                         metadata_revision_id=snapshot.revision_id, metadata_sha256=metadata_sha256,
                         output_pdf_sha256=snapshot.output_pdf_sha256, sensitivity_ceiling=ceiling,
                         nodes=tuple(nodes), edges=tuple(edges), claims=tuple(claims), omitted=Omitted(**omitted))


def approved_material(document, principal, job_id, ceiling, revision_id=None) -> GraphMaterial:
    """Latest (or the requested) approved snapshot bytes and its graph."""
    approved = [r for r in document.list_revisions(principal, job_id) if r.state == "approved"]
    if revision_id is not None:
        approved = [r for r in approved if r.revision_id == revision_id]
    if not approved:
        raise ApplicationError(409, "metadata_not_approved", "The document has no matching approved metadata snapshot.")
    revision = max(approved, key=lambda r: r.created_at)
    with document.open_snapshot(principal, job_id, revision.revision_id, sha256=revision.sha256) as snapshot:
        metadata = snapshot_bytes(snapshot)
        digest = hashlib.sha256(metadata).hexdigest()
        if digest != revision.sha256:
            raise ApplicationError(409, "metadata_revision_conflict", "Metadata snapshot bytes changed.")
        try:
            graph = build_graph(snapshot, digest, ceiling)
        except ValueError:
            raise ApplicationError(422, "graph_unrepresentable", "The approved snapshot cannot be exported as a graph.") from None
    return GraphMaterial(revision_id=revision.revision_id, metadata=metadata, graph=graph_bytes(graph))
