"""ocr-graph/1 export of the approved metadata snapshot of a caller-owned job (OCR-EV-06)."""
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query

from app.http_contract import error_responses, example, operation
from app.routes.documents import Reader, Services
from app.graph_contract import GraphDocument, parse_graph
from app.services.graph_export import approved_material

router = APIRouter(
    prefix="/api/v1/jobs/{job_id}/graph", tags=["Graph"],
    responses=error_responses(401, 403, 404, 405, 408, 409, 410, 422, 429, 500, 503),
)
ID = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
_SCOPES = ("jobs:read", "artifacts:read", "metadata:read")
_EXAMPLE = {
    "schema_version": "ocr-graph/1", "document_id": "1" * 32, "metadata_revision_id": "3" * 32,
    "metadata_sha256": "a" * 64, "output_pdf_sha256": "b" * 64, "sensitivity_ceiling": "internal",
    "nodes": [{"node_id": "n:" + "c" * 24, "kind": "document", "entity_type": None, "label": "Documento " + "1" * 32,
               "sensitivity": "public", "identity": {"scope": "document", "basis": "document", "segment_id": None,
                                                     "field_name": None, "item_index": None},
               "hints": [], "type_id": None, "type_version": None, "page_start": None, "page_end": None}],
    "edges": [], "claims": [], "omitted": {"not_indexable": 2, "above_ceiling": 1, "not_reviewed": 0},
}


@router.get("", response_model=GraphDocument, operation_id="ocr_get_document_graph",
            responses=example(_EXAMPLE), openapi_extra=operation(*_SCOPES))
async def get_graph(job_id: ID, principal: Reader, services: Services,
                    metadata_revision_id: Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")] = None,
                    sensitivity_ceiling: Literal["public", "internal", "restricted"] = "internal"):
    """Export entities, relations and claims of the latest (or the given) APPROVED snapshot.

    Only reviewed values of fields declared indexable and at or below the
    ceiling are included; omitted values are counted, never shown. Entities
    stay scoped to document, segment, field and item: equal names are never
    merged, and identifier values travel only as hints. Every relation and
    claim carries the page evidence (offsets in Unicode code points and the
    page text SHA-256) reviewed for that value.
    """
    material = await services.execution.run(
        principal, lambda control: approved_material(
            services.document, principal, job_id, sensitivity_ceiling, metadata_revision_id))
    return parse_graph(material.graph)
