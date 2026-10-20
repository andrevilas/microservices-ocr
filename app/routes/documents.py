"""Basic metadata reads over the shared document execution runtime."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Path, Query, Request
from starlette.responses import Response

from app.application import Principal
from app.document_canonical_models import (
    CanonicalManifest, CanonicalPageChunk, CanonicalPageText,
)
from app.document_http_examples import canonical_jsonl_example, request_example, response_example
from app.document_http_streaming import DocumentDownloadResponse
from app.metadata_models import MetadataSnapshot, SaveMetadataInput, SaveMetadataReceipt
from app.document_content_models import (
    MetadataEvidencePage, MetadataSchemaChunk, MetadataStringChunk, MetadataValuePage,
)
from app.document_transport_models import MetadataFieldPage, MetadataRevisionPage, MetadataView
from app.http_contract import error_responses, example, operation
from app.security import require_principal
from app.routes.uploads import json_contract, json_input
from app.services.document_runtime import DocumentServices, get_document_services


router = APIRouter(
    prefix="/api/v1/jobs/{job_id}", tags=["Documents"],
    responses=error_responses(401, 403, 404, 405, 408, 409, 413, 422, 429, 500, 503),
)
ID = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
Digest = Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")]


def metadata_principal(
    principal: Annotated[Principal, Depends(require_principal)],
) -> Principal:
    for scope in ("jobs:read", "artifacts:read", "metadata:read"):
        principal.require(scope)
    return principal


def metadata_services(
    request: Request,
    principal: Annotated[Principal, Depends(metadata_principal)],
) -> DocumentServices:
    # The scope dependency finishes before the synchronous provider is resolved.
    return get_document_services(request)


Reader = Annotated[Principal, Depends(metadata_principal)]
Services = Annotated[DocumentServices, Depends(metadata_services)]


def metadata_write_principal(
    principal: Annotated[Principal, Depends(metadata_principal)],
) -> Principal:
    principal.require("metadata:write")
    return principal


def metadata_write_services(
    request: Request,
    principal: Annotated[Principal, Depends(metadata_write_principal)],
) -> DocumentServices:
    return get_document_services(request)


Writer = Annotated[Principal, Depends(metadata_write_principal)]
WriteServices = Annotated[DocumentServices, Depends(metadata_write_services)]
SAVE_CONTRACT = json_contract(SaveMetadataInput)
SAVE_CONTRACT.update(operation("jobs:read", "artifacts:read", "metadata:read", "metadata:write"))
SAVE_CONTRACT["requestBody"]["content"]["application/json"]["schema"]["examples"] = [
    request_example(SaveMetadataInput)]


@router.put(
    "/metadata", response_model=SaveMetadataReceipt, status_code=201,
    operation_id="ocr_save_document_metadata",
    openapi_extra=SAVE_CONTRACT,
    responses={
        **error_responses(410, 415),
        **{status: {
            **example(response_example(SaveMetadataReceipt), status)[status],
            "model": SaveMetadataReceipt,
            "description": "Historical receipt replay." if status == 200 else "Draft revision created.",
            "headers": {"Idempotency-Replayed": {
                "description": "Whether the original persisted receipt was replayed.",
                "schema": {"type": "string", "enum": ["true", "false"]},
            }},
        } for status in (200, 201)},
    },
)
async def save_metadata(
    job_id: ID, request: Request, response: Response,
    principal: Writer, services: WriteServices,
    idempotency_key: Annotated[str, Header(
        min_length=8, max_length=128, pattern=r"^[\x00-\x7f]+$")],
):
    """Save a manual draft with exact HEAD preconditions.

    contexts:read is required only for new or changed catalog bindings.
    After an uncertain outcome, retry with the same Idempotency-Key and body.
    A replay can return a historical receipt after HEAD has advanced.
    """
    body = await json_input(request, SaveMetadataInput, max_bytes=512 * 1024)
    receipt, replayed = await services.execution.run(
        principal,
        lambda control: services.document.save(
            principal, job_id, body, key=idempotency_key,
            checkpoint=control.checkpoint),
    )
    response.status_code = 200 if replayed else 201
    response.headers["Idempotency-Replayed"] = "true" if replayed else "false"
    return receipt


def canonical_principal(
    principal: Annotated[Principal, Depends(require_principal)],
) -> Principal:
    for scope in ("jobs:read", "artifacts:read"):
        principal.require(scope)
    return principal


def canonical_services(
    request: Request,
    principal: Annotated[Principal, Depends(canonical_principal)],
) -> DocumentServices:
    # Resolve the shared provider only after both canonical scopes pass.
    return get_document_services(request)


CanonicalReader = Annotated[Principal, Depends(canonical_principal)]
CanonicalServices = Annotated[DocumentServices, Depends(canonical_services)]


@router.get(
    "/metadata", response_model=MetadataView,
    operation_id="ocr_get_document_metadata",
    responses=example(response_example(MetadataView)),
    openapi_extra=operation("jobs:read", "artifacts:read", "metadata:read"),
)
async def get_metadata(job_id: ID, principal: Reader, services: Services):
    return await services.execution.run(
        principal,
        lambda control: services.queries.get_current(
            principal, job_id, checkpoint=control.checkpoint),
    )


@router.get(
    "/metadata/revisions", response_model=MetadataRevisionPage,
    operation_id="ocr_list_document_metadata_revisions",
    responses=example(response_example(MetadataRevisionPage)),
    openapi_extra=operation("jobs:read", "artifacts:read", "metadata:read"),
)
async def list_metadata_revisions(
    job_id: ID, principal: Reader, services: Services,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    after: Annotated[str | None, Query(max_length=2048)] = None,
):
    return await services.execution.run(
        principal,
        lambda control: services.queries.list_revisions(
            principal, job_id, limit=limit, after=after,
            checkpoint=control.checkpoint),
    )


@router.get(
    "/metadata/revisions/{revision_id}", response_model=MetadataView,
    operation_id="ocr_get_document_metadata_revision",
    responses=example(response_example(MetadataView)),
    openapi_extra=operation("jobs:read", "artifacts:read", "metadata:read"),
)
async def get_metadata_revision(
    job_id: ID, revision_id: ID, sha256: Digest,
    principal: Reader, services: Services,
):
    return await services.execution.run(
        principal,
        lambda control: services.queries.get_revision(
            principal, job_id, revision_id, sha256=sha256,
            checkpoint=control.checkpoint),
    )


@router.get(
    "/metadata/revisions/{revision_id}/segments/{segment_id}/fields",
    response_model=MetadataFieldPage,
    operation_id="ocr_list_document_metadata_fields",
    responses=example(response_example(MetadataFieldPage)),
    openapi_extra=operation("jobs:read", "artifacts:read", "metadata:read"),
)
async def list_metadata_fields(
    job_id: ID, revision_id: ID, segment_id: ID, sha256: Digest,
    principal: Reader, services: Services,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    after: Annotated[str | None, Query(max_length=2048)] = None,
):
    return await services.execution.run(
        principal,
        lambda control: services.queries.list_fields(
            principal, job_id, revision_id, segment_id, sha256=sha256,
            limit=limit, after=after, checkpoint=control.checkpoint),
    )


@router.get(
    "/metadata/revisions/{revision_id}/segments/{segment_id}/fields/{field_name}/values",
    response_model=MetadataValuePage,
    operation_id="ocr_list_document_metadata_values",
    responses=example(response_example(MetadataValuePage)),
    openapi_extra=operation("jobs:read", "artifacts:read", "metadata:read"),
)
async def list_metadata_values(
    job_id: ID, revision_id: ID, segment_id: ID,
    field_name: Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]{0,63}$")],
    sha256: Digest, principal: Reader, services: Services,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    after: Annotated[str | None, Query(max_length=2048)] = None,
):
    return await services.execution.run(
        principal,
        lambda control: services.queries.list_values(
            principal, job_id, revision_id, segment_id, field_name, sha256=sha256,
            limit=limit, after=after, checkpoint=control.checkpoint),
    )


@router.get(
    "/metadata/revisions/{revision_id}/segments/{segment_id}/fields/{field_name}/evidence",
    response_model=MetadataEvidencePage,
    operation_id="ocr_list_document_metadata_evidence",
    responses=example(response_example(MetadataEvidencePage)),
    openapi_extra=operation("jobs:read", "artifacts:read", "metadata:read"),
)
async def list_metadata_evidence(
    job_id: ID, revision_id: ID, segment_id: ID,
    field_name: Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]{0,63}$")],
    sha256: Digest, principal: Reader, services: Services,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    after: Annotated[str | None, Query(max_length=2048)] = None,
):
    return await services.execution.run(
        principal,
        lambda control: services.queries.list_evidence(
            principal, job_id, revision_id, segment_id, field_name, sha256=sha256,
            limit=limit, after=after, checkpoint=control.checkpoint),
    )


@router.get(
    "/metadata/revisions/{revision_id}/segments/{segment_id}/fields/{field_name}/strings/{part}",
    response_model=MetadataStringChunk,
    operation_id="ocr_get_document_metadata_string",
    responses=example(response_example(MetadataStringChunk)),
    openapi_extra=operation("jobs:read", "artifacts:read", "metadata:read"),
)
async def get_metadata_string(
    job_id: ID, revision_id: ID, segment_id: ID,
    field_name: Annotated[str, Path(pattern=r"^[a-z][a-z0-9_]{0,63}$")],
    part: Literal["raw", "normalized"],
    sha256: Digest, principal: Reader, services: Services,
    item_index: Annotated[int | None, Query(ge=0, le=99)] = None,
    offset: Annotated[int, Query(ge=0, le=262144)] = 0,
    max_chars: Annotated[int, Query(ge=1, le=1000)] = 1000,
):
    return await services.execution.run(
        principal,
        lambda control: services.queries.get_string(
            principal, job_id, revision_id, segment_id, field_name, sha256=sha256,
            part=part, item_index=item_index, offset=offset, max_chars=max_chars,
            checkpoint=control.checkpoint),
    )


@router.get(
    "/metadata/revisions/{revision_id}/schemas/{schema_sha256}",
    response_model=MetadataSchemaChunk,
    operation_id="ocr_get_document_metadata_schema",
    responses=example(response_example(MetadataSchemaChunk)),
    openapi_extra=operation("jobs:read", "artifacts:read", "metadata:read"),
)
async def get_metadata_schema(
    job_id: ID, revision_id: ID,
    schema_sha256: Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")],
    sha256: Digest, principal: Reader, services: Services,
    offset: Annotated[int, Query(ge=0, le=262144)] = 0,
    max_chars: Annotated[int, Query(ge=1, le=1000)] = 1000,
):
    return await services.execution.run(
        principal,
        lambda control: services.queries.get_schema(
            principal, job_id, revision_id, schema_sha256, sha256=sha256,
            offset=offset, max_chars=max_chars, checkpoint=control.checkpoint),
    )


@router.get(
    "/metadata/revisions/{revision_id}/content",
    response_model=MetadataSnapshot, response_class=Response,
    operation_id="ocr_download_document_metadata",
    responses={200: {
        "model": MetadataSnapshot,
        "description": "Exact persisted metadata snapshot, up to 1 MiB.",
        "content": {"application/json": {"example": response_example(MetadataSnapshot)}},
        "headers": {
            "Content-Length": {"schema": {"type": "integer", "minimum": 0, "maximum": 1048576}},
            "X-Content-SHA256": {"schema": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
            "Content-Disposition": {"schema": {"type": "string", "enum": ['attachment; filename="metadata.json"']}},
            "Cache-Control": {"schema": {"type": "string", "enum": ["no-store"]}},
        },
    }},
    openapi_extra=operation("jobs:read", "artifacts:read", "metadata:read"),
)
async def download_metadata(
    job_id: ID, revision_id: ID, sha256: Digest,
    principal: Reader, services: Services,
):
    return DocumentDownloadResponse(lambda: services.streams.open_metadata(
        principal, job_id, revision_id, sha256=sha256))


@router.get(
    "/canonical/pages.jsonl", response_class=Response,
    operation_id="ocr_download_document_canonical",
    responses={200: {
        "description": "Exact canonical pages JSONL download.",
        "content": {"application/x-ndjson": {
            "schema": {"type": "string", "format": "binary"},
            "example": canonical_jsonl_example(),
        }},
        "headers": {
            "Content-Length": {"schema": {"type": "integer", "minimum": 0, "maximum": 33554432}},
            "X-Content-SHA256": {"schema": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
            "Content-Disposition": {"schema": {"type": "string", "enum": ['attachment; filename="pages.jsonl"']}},
            "Cache-Control": {"schema": {"type": "string", "enum": ["no-store"]}},
        },
    }},
    openapi_extra=operation("jobs:read", "artifacts:read"),
)
async def download_canonical(
    job_id: ID, principal: CanonicalReader, services: CanonicalServices,
):
    return DocumentDownloadResponse(lambda: services.streams.open_canonical(
        principal, job_id))


@router.get(
    "/canonical/manifest", response_model=CanonicalManifest,
    operation_id="ocr_get_document_canonical_manifest",
    responses=example(response_example(CanonicalManifest)),
    openapi_extra=operation("jobs:read", "artifacts:read"),
)
async def get_canonical_manifest(
    job_id: ID, principal: CanonicalReader, services: CanonicalServices,
):
    return await services.execution.run(
        principal,
        lambda control: services.queries.get_canonical_manifest(
            principal, job_id, checkpoint=control.checkpoint),
    )


@router.get(
    "/canonical/pages", response_model=CanonicalPageChunk,
    operation_id="ocr_get_document_canonical_chunk",
    responses=example(response_example(CanonicalPageChunk)),
    openapi_extra=operation("jobs:read", "artifacts:read"),
)
async def get_canonical_chunk(
    job_id: ID, principal: CanonicalReader, services: CanonicalServices,
    page: Annotated[int, Query(ge=1, le=300)] = 1,
    offset: Annotated[int, Query(ge=0, le=500000)] = 0,
    max_chars: Annotated[int, Query(ge=1, le=20000)] = 8000,
):
    return await services.execution.run(
        principal,
        lambda control: services.queries.get_canonical_chunk(
            principal, job_id, page=page, offset=offset, max_chars=max_chars,
            checkpoint=control.checkpoint),
    )


@router.get(
    "/canonical/pages/{page}", response_model=CanonicalPageText,
    operation_id="ocr_get_document_canonical_page",
    responses=example(response_example(CanonicalPageText)),
    openapi_extra=operation("jobs:read", "artifacts:read"),
)
async def get_canonical_page(
    job_id: ID, page: Annotated[int, Path(ge=1, le=300)],
    principal: CanonicalReader, services: CanonicalServices,
):
    return await services.execution.run(
        principal,
        lambda control: services.queries.get_canonical_page(
            principal, job_id, page, checkpoint=control.checkpoint),
    )
