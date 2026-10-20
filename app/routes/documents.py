"""Basic metadata reads over the shared document execution runtime."""
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request

from app.application import Principal
from app.document_canonical_models import (
    CanonicalManifest, CanonicalPageChunk, CanonicalPageText,
)
from app.document_http_examples import response_example
from app.document_content_models import MetadataSchemaChunk
from app.document_transport_models import MetadataFieldPage, MetadataRevisionPage, MetadataView
from app.http_contract import error_responses, example, operation
from app.security import require_principal
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
