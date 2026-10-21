"""Assisted extraction HTTP API (E3): queue, read and apply extraction runs.

Runs are proposals produced by the server's local model runtime; they never
change metadata. Applying a run creates a draft revision (origin=extracted)
under the usual CAS and Idempotency-Key rules; approval stays a separate,
explicit user action. 503 extraction_disabled while the feature is off.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Request
from starlette.responses import Response

from app.application import Principal
from app.document_http_examples import response_example
from app.extraction_http_examples import extraction_list_example, extraction_receipt_example, extraction_view_example
from app.extraction_models import ApplyExtractionInput, CreateExtractionInput, ExtractionList, ExtractionReceipt, ExtractionView
from app.http_contract import error_responses, example, operation
from app.metadata_models import SaveMetadataReceipt
from app.routes.documents import Reader, Writer, metadata_write_principal
from app.routes.uploads import json_input
from app.services.document_runtime import DocumentServices, get_document_services
from app.services.extraction_service import APPLY_SCOPES, CREATE_SCOPES, READ_SCOPES

router = APIRouter(
    prefix="/api/v1/jobs/{job_id}/extractions", tags=["Extractions"],
    responses=error_responses(401, 403, 404, 405, 408, 409, 410, 413, 422, 429, 500, 503),
)
ID = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


def extraction_principal(principal: Annotated[Principal, Depends(metadata_write_principal)]) -> Principal:
    principal.require("contexts:read")
    return principal


def _services(request: Request) -> DocumentServices:
    # Each route's principal dependency (and its scopes) resolves before this one.
    return get_document_services(request)


Creator = Annotated[Principal, Depends(extraction_principal)]
Services = Annotated[DocumentServices, Depends(_services)]


def _body(model, examples, scopes):
    schema = {**model.model_json_schema(), "examples": examples}
    return {**operation(*scopes), "requestBody": {"required": True, "content": {"application/json": {"schema": schema}}}}


@router.post(
    "", response_model=ExtractionReceipt, status_code=202, operation_id="ocr_create_metadata_extraction",
    openapi_extra=_body(CreateExtractionInput, [{"type_id": "5" * 32}, {"context_id": "6" * 32},
                                                {"type_id": "5" * 32, "page_start": 1, "page_end": 2,
                                                 "retry": True}], CREATE_SCOPES),
    responses={**error_responses(415),
               200: {"model": ExtractionReceipt, "description": "Existing run for the same inputs replayed.",
                     **example(extraction_receipt_example(True))[200]},
               202: {"description": "Run queued.", **example(extraction_receipt_example(), 202)[202]}},
)
async def create_extraction(job_id: ID, request: Request, response: Response, principal: Creator,
                            services: Services):
    """Queue a field extraction (type_id) or a classification (context_id) over the canonical text.

    The text goes only to the server's configured local model runtime. Every
    proposed value must parse against the published schema and be found
    literally, with recomputed offsets, in the cited page; anything else is
    counted as a rejection. Identical inputs replay the existing run unless
    retry=true. Poll the run until state is completed or failed.
    """
    body = await json_input(request, CreateExtractionInput, max_bytes=4096)
    receipt = await services.execution.run(
        principal, lambda control: services.extractions.create(principal, job_id, body, checkpoint=control.checkpoint))
    response.status_code = 200 if receipt.replayed else 202
    response.headers["Cache-Control"] = "no-store"
    return receipt


@router.get("", response_model=ExtractionList, operation_id="ocr_list_metadata_extractions",
            responses=example(extraction_list_example()), openapi_extra=operation(*READ_SCOPES))
async def list_extractions(job_id: ID, principal: Reader, services: Services):
    """List every extraction run of a caller-owned job, oldest first."""
    return await services.execution.run(principal, lambda control: services.extractions.list(principal, job_id))


@router.get("/{run_id}", response_model=ExtractionView, operation_id="ocr_get_metadata_extraction",
            responses=example(extraction_view_example()), openapi_extra=operation(*READ_SCOPES))
async def get_extraction(job_id: ID, run_id: ID, principal: Reader, services: Services):
    """Return one run: state, outcome, model and prompt version, usage, proposals and rejection counts."""
    return await services.execution.run(principal, lambda control: services.extractions.get(principal, job_id, run_id))


@router.post(
    "/{run_id}/apply", response_model=SaveMetadataReceipt, status_code=201,
    operation_id="ocr_apply_metadata_extraction",
    openapi_extra=_body(ApplyExtractionInput, [{"expected_head": None, "expected_sha256": None},
                                               {"expected_head": "3" * 32, "expected_sha256": "c" * 64}],
                        APPLY_SCOPES),
    responses={**error_responses(415), **{status: {
        **example(response_example(SaveMetadataReceipt), status)[status], "model": SaveMetadataReceipt,
        "description": "Historical receipt replay." if status == 200 else "Draft revision created from the run.",
        "headers": {"Idempotency-Replayed": {"description": "Whether the original persisted receipt was replayed.",
                                             "schema": {"type": "string", "enum": ["true", "false"]}}},
    } for status in (200, 201)}},
)
async def apply_extraction(
    job_id: ID, run_id: ID, request: Request, response: Response, principal: Writer, services: Services,
    idempotency_key: Annotated[str, Header(min_length=8, max_length=128, pattern=r"^[\x00-\x7f]+$")],
):
    """Create a draft revision from a completed field extraction.

    Existing values are never overwritten: only fields without a value receive
    the proposals, as origin=extracted bound to the run. Conflicts are stored
    without a value and block approval until a person resolves them. The draft
    still needs ocr_approve_document_metadata with explicitly reviewed fields.
    """
    body = await json_input(request, ApplyExtractionInput, max_bytes=4096)
    receipt, replayed = await services.execution.run(
        principal, lambda control: services.extractions.apply(
            principal, job_id, run_id, body, key=idempotency_key, checkpoint=control.checkpoint))
    response.status_code = 200 if replayed else 201
    response.headers["Idempotency-Replayed"] = "true" if replayed else "false"
    return receipt
