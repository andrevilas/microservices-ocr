"""Pure ocr-graph/1 contract: entities, relations and claims with page evidence.

Shared byte for byte with the independent RAG receiver. A graph is derived from
one approved metadata snapshot of one document. Identity is conservative: every
entity is scoped to its document, segment, field and list item, so two values
with the same text are never merged here. Identifier values travel as hints
for an auditable resolution step in the consumer; a hint is not a merge.

`verify_graph` binds a graph to the delivered artifacts: document identity,
PDF and metadata hashes, derived node identifiers and every evidence quote
against the exact page text. Passing it does not make a value true, only
traceable to the approved snapshot and to the page it was reviewed against.
"""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

_CONFIG = ConfigDict(
    frozen=True,
    extra="forbid",
    strict=True,
    revalidate_instances="always",
    validate_default=True,
    hide_input_in_errors=True,
)
MAX_GRAPH_BYTES = 2 * 1024 * 1024
MAX_NODES = 2000
MAX_EDGES = 4000
MAX_CLAIMS = 2000
MAX_EVIDENCE = 32
SENSITIVITY_RANK = {"public": 0, "internal": 1, "restricted": 2}
# Field names whose reviewed string values denote things rather than attributes.
# Everything else that is exported becomes a claim on its segment.
ENTITY_FIELDS = {
    "emissor": "agent",
    "partes": "agent",
    "entidade_responsavel": "organization",
    "serventia": "organization",
    "municipio": "place",
}

Hex32 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{32}$")]
Sha256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
NodeId = Annotated[str, StringConstraints(strict=True, pattern=r"^n:[0-9a-f]{24}$")]
EdgeId = Annotated[str, StringConstraints(strict=True, pattern=r"^r:[0-9a-f]{24}$")]
ClaimId = Annotated[str, StringConstraints(strict=True, pattern=r"^c:[0-9a-f]{24}$")]
PersistentId = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64,
                                                 pattern=r"^[A-Za-z0-9_-]+$")]
FieldName = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Label = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2000)]
Sensitivity = Literal["public", "internal", "restricted"]
Count = Annotated[int, Field(strict=True, ge=0, le=100000)]
Page = Annotated[int, Field(strict=True, ge=1, le=500)]
Offset = Annotated[int, Field(strict=True, ge=0, le=500000)]
ItemIndex = Annotated[int, Field(strict=True, ge=0, le=999)]


class Evidence(BaseModel):
    model_config = _CONFIG

    page: Page
    start: Offset
    end: Offset
    quote: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2000)]
    text_sha256: Sha256

    @model_validator(mode="after")
    def _range(self):
        if self.start >= self.end or len(self.quote) != self.end - self.start:
            raise ValueError("evidence_range")
        return self


EvidenceList = Annotated[tuple[Evidence, ...], Field(max_length=MAX_EVIDENCE)]


class Identity(BaseModel):
    """What a node stands for. Scope `document` never crosses documents."""
    model_config = _CONFIG

    scope: Literal["document"]
    basis: Literal["document", "segment", "field_value"]
    segment_id: PersistentId | None = None
    field_name: FieldName | None = None
    item_index: ItemIndex | None = None

    @model_validator(mode="after")
    def _shape(self):
        has = (self.segment_id is not None, self.field_name is not None, self.item_index is not None)
        expected = {"document": (False, False, False), "segment": (True, False, False),
                    "field_value": (True, True, True)}[self.basis]
        if has != expected:
            raise ValueError("identity_shape")
        return self


class Hint(BaseModel):
    """Identifier value usable by a consumer for explicit, auditable linking."""
    model_config = _CONFIG

    scheme: Literal["identifier"]
    field_name: FieldName
    value: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)]


class Node(BaseModel):
    model_config = _CONFIG

    node_id: NodeId
    kind: Literal["document", "segment", "entity"]
    entity_type: Literal["agent", "organization", "place", "identifier"] | None = None
    label: Label
    sensitivity: Sensitivity
    identity: Identity
    hints: Annotated[tuple[Hint, ...], Field(max_length=4)] = ()
    type_id: PersistentId | None = None
    type_version: Annotated[int, Field(strict=True, ge=1)] | None = None
    page_start: Page | None = None
    page_end: Page | None = None

    @model_validator(mode="after")
    def _shape(self):
        basis = {"document": "document", "segment": "segment", "entity": "field_value"}[self.kind]
        if self.identity.basis != basis or (self.entity_type is None) != (self.kind != "entity"):
            raise ValueError("node_shape")
        segment_fields = (self.type_id, self.type_version, self.page_start, self.page_end)
        if self.kind == "segment":
            if None in segment_fields or self.page_start > self.page_end:
                raise ValueError("segment_shape")
        elif any(value is not None for value in segment_fields):
            raise ValueError("node_shape")
        if self.hints and self.entity_type != "identifier":
            raise ValueError("hint_shape")
        return self


class Edge(BaseModel):
    model_config = _CONFIG

    edge_id: EdgeId
    source: NodeId
    target: NodeId
    predicate: Annotated[str, StringConstraints(strict=True, pattern=r"^(has_segment|field:[a-z][a-z0-9_]{0,63})$")]
    sensitivity: Sensitivity
    evidence: EvidenceList = ()


class Claim(BaseModel):
    """A reviewed literal value of a segment field (date, amount, text...)."""
    model_config = _CONFIG

    claim_id: ClaimId
    subject: NodeId
    field_name: FieldName
    item_index: ItemIndex
    value_type: Literal["string", "identifier", "integer", "decimal", "boolean", "date", "enum"]
    value: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=4000)]
    sensitivity: Sensitivity
    evidence: EvidenceList = ()


class Omitted(BaseModel):
    """Counts of reviewed-snapshot values deliberately left out, never the values."""
    model_config = _CONFIG

    not_indexable: Count = 0
    above_ceiling: Count = 0
    not_reviewed: Count = 0


class GraphDocument(BaseModel):
    model_config = _CONFIG

    schema_version: Literal["ocr-graph/1"]
    document_id: Hex32
    metadata_revision_id: PersistentId
    metadata_sha256: Sha256
    output_pdf_sha256: Sha256
    sensitivity_ceiling: Sensitivity
    nodes: Annotated[tuple[Node, ...], Field(min_length=1, max_length=MAX_NODES)]
    edges: Annotated[tuple[Edge, ...], Field(max_length=MAX_EDGES)] = ()
    claims: Annotated[tuple[Claim, ...], Field(max_length=MAX_CLAIMS)] = ()
    omitted: Omitted

    @model_validator(mode="after")
    def _structure(self):
        ids = [node.node_id for node in self.nodes]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate_node")
        if [node.kind for node in self.nodes].count("document") != 1 or self.nodes[0].kind != "document":
            raise ValueError("document_node")
        for node in self.nodes:
            if node.node_id != node_id(self.document_id, node.identity):
                raise ValueError("node_identity")
            if SENSITIVITY_RANK[node.sensitivity] > SENSITIVITY_RANK[self.sensitivity_ceiling]:
                raise ValueError("above_ceiling")
        nodes = {node.node_id: node for node in self.nodes}
        segments = {node.identity.segment_id: node for node in self.nodes if node.kind == "segment"}
        if any(node.identity.segment_id not in segments for node in self.nodes if node.kind == "entity"):
            raise ValueError("dangling_reference")
        edge_ids = [edge.edge_id for edge in self.edges]
        claim_ids = [claim.claim_id for claim in self.claims]
        if len(set(edge_ids)) != len(edge_ids) or len(set(claim_ids)) != len(claim_ids):
            raise ValueError("duplicate_relation")
        for item in (*self.edges, *self.claims):
            ends = (item.source, item.target) if isinstance(item, Edge) else (item.subject,)
            if any(end not in nodes for end in ends):
                raise ValueError("dangling_reference")
            if SENSITIVITY_RANK[item.sensitivity] > SENSITIVITY_RANK[self.sensitivity_ceiling]:
                raise ValueError("above_ceiling")
            if isinstance(item, Edge):
                source, target = nodes[item.source], nodes[item.target]
                if item.edge_id != edge_id(self.document_id, item.source, item.target, item.predicate):
                    raise ValueError("relation_identity")
                if item.predicate == "has_segment":
                    valid = source.kind == "document" and target.kind == "segment" and not item.evidence
                else:
                    valid = (source.kind == "segment" and target.kind == "entity"
                             and target.identity.field_name == item.predicate[6:]
                             and target.identity.segment_id == source.identity.segment_id)
                segment = source
            else:
                segment = nodes[item.subject]
                if item.claim_id != claim_id(self.document_id, item.subject, item.field_name, item.item_index):
                    raise ValueError("relation_identity")
                valid = segment.kind == "segment"
            if not valid:
                raise ValueError("relation_shape")
            if any(not segment.page_start <= evidence.page <= segment.page_end for evidence in item.evidence):
                raise ValueError("evidence_outside_segment")
        return self


def _digest(prefix: str, material: dict) -> str:
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return prefix + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def node_id(document_id: str, identity: Identity) -> str:
    """Derived, never chosen: identical ids imply the same document-scoped identity."""
    return _digest("n:", {"document_id": document_id, **identity.model_dump(mode="json")})


def edge_id(document_id: str, source: str, target: str, predicate: str) -> str:
    return _digest("r:", {"document_id": document_id, "source": source, "target": target, "predicate": predicate})


def claim_id(document_id: str, subject: str, field_name: str, item_index: int) -> str:
    return _digest("c:", {"document_id": document_id, "subject": subject, "field_name": field_name,
                          "item_index": item_index})


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_):
    raise ValueError("invalid_json_constant")


def parse_graph(raw: bytes) -> GraphDocument:
    if type(raw) is not bytes or len(raw) > MAX_GRAPH_BYTES:
        raise ValueError("graph_size")
    try:
        text = raw.decode("utf-8", errors="strict")
        root = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("invalid_graph_json") from None
    if not isinstance(root, dict):
        raise ValueError("invalid_graph_json")
    return GraphDocument.model_validate_json(text)


def graph_bytes(graph: GraphDocument) -> bytes:
    graph = GraphDocument.model_validate(graph.model_dump(mode="python", warnings=False))
    return json.dumps(graph.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def verify_graph(graph: GraphDocument, *, job_id: str, pdf_sha256: str, metadata_sha256: str,
                 pages: list[str]) -> None:
    """Bind the graph to the delivered artifacts; raise ValueError on any mismatch."""
    graph = GraphDocument.model_validate(graph.model_dump(mode="python", warnings=False))
    if (graph.document_id != job_id or graph.output_pdf_sha256 != pdf_sha256
            or graph.metadata_sha256 != metadata_sha256):
        raise ValueError("graph_source_mismatch")
    hashes = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in pages]
    for node in graph.nodes:
        if node.page_end is not None and node.page_end > len(pages):
            raise ValueError("graph_page_range")
    for item in (*graph.edges, *graph.claims):
        for evidence in item.evidence:
            if (evidence.page > len(pages) or hashes[evidence.page - 1] != evidence.text_sha256
                    or pages[evidence.page - 1][evidence.start:evidence.end] != evidence.quote):
                raise ValueError("graph_evidence_mismatch")
