"""HTTP transport for document contexts and types. Services own ACL, quota, CAS, idempotency and publication.

Write bodies are read with json_input (2 MiB streamed cap) only after the scopes are checked, so no handler
declares a Pydantic body parameter: FastAPI would parse it before any limit applies."""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, Path, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from app.application import Principal
from app.catalog_models import (
    ContextInput, ContextPage, ContextReceipt, ContextRecord, ContextSummary, ContextUpdateInput,
    TypeInput, TypePage, TypePublishInput, TypeReceipt, TypeRecord, TypeSummary, TypeUpdateInput,
    TypeVersionPage, TypeVersionRecord, TypeVersionSummary, TypeVersionViewSummary,
)
from app.document_models import ContextCreate, TypeDraft, compose_schema
from app.http_contract import error_responses, operation
from app.routes.uploads import json_contract, json_input
from app.security import require_principal
from app.services.catalog_runtime import CatalogServices, get_catalog_services

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_SQLITE_INT = 2**63 - 1

router = APIRouter(prefix="/api/v1", tags=["Catalog"],
                   responses=error_responses(401, 403, 404, 405, 408, 409, 413, 415, 422, 429, 500, 503))

# -- synthetic, model-built examples (never hand-written dumps, so they cannot drift from the schemas)
_NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
_CONTEXT_ID, _TYPE_ID = "1" * 32, "2" * 32
_CONTEXT = ContextCreate.model_validate(ContextInput.model_config["json_schema_extra"]["examples"][0])
_TYPE = TypeDraft.model_validate(TypeInput.model_config["json_schema_extra"]["examples"][0])
_COMPOSED = compose_schema(_CONTEXT.common_fields, _TYPE.specific_fields)
_VERSION = TypeVersionSummary(type_id=_TYPE_ID, context_id=_CONTEXT_ID, version=1, draft_revision=1, context_revision=1,
                              type_name=_TYPE.name, context_name=_CONTEXT.name, schema_version="1",
                              schema_sha256=_COMPOSED.sha256, created_at=_NOW)
_VERSION_VIEW = TypeVersionViewSummary(**_VERSION.model_dump(),
                                       lifecycle={"state": "published", "revision": 1, "changed_at": _NOW})
_EXAMPLES = {
    ContextReceipt: ContextReceipt(context_id=_CONTEXT_ID, created_at=_NOW),
    ContextSummary: ContextSummary(context_id=_CONTEXT_ID, name=_CONTEXT.name, revision=1, created_at=_NOW, updated_at=_NOW),
    ContextRecord: ContextRecord(context_id=_CONTEXT_ID, revision=2, created_at=_NOW, updated_at=_NOW, body=_CONTEXT),
    TypeReceipt: TypeReceipt(type_id=_TYPE_ID, context_id=_CONTEXT_ID, created_at=_NOW),
    TypeSummary: TypeSummary(type_id=_TYPE_ID, context_id=_CONTEXT_ID, name=_TYPE.name, revision=1, created_at=_NOW, updated_at=_NOW),
    TypeRecord: TypeRecord(type_id=_TYPE_ID, context_id=_CONTEXT_ID, revision=2, created_at=_NOW, updated_at=_NOW, body=_TYPE),
    TypeVersionSummary: _VERSION,
    TypeVersionRecord: TypeVersionRecord(**_VERSION.model_dump(), lifecycle=_VERSION_VIEW.lifecycle,
                                         schema=json.loads(_COMPOSED.canonical)),
}
_EXAMPLES[ContextPage] = ContextPage(items=[_EXAMPLES[ContextSummary]])
_EXAMPLES[TypePage] = TypePage(items=[_EXAMPLES[TypeSummary]])
_EXAMPLES[TypeVersionPage] = TypeVersionPage(items=[_VERSION_VIEW])
_REPLAYED = {"Idempotency-Replayed": {"description": "true when the original persisted receipt for this identity/key is replayed.",
                                      "schema": {"type": "string", "enum": ["true", "false"]}}}


def _reply(model, *statuses, idempotent=False):
    value = _EXAMPLES[model].model_dump(mode="json", by_alias=True)
    return {status: {"model": model, "description": "Original receipt replayed." if status == 200 and idempotent
                     else "Created." if status == 201 else "Successful response.",
                     "content": {"application/json": {"example": value}},
                     **({"headers": _REPLAYED} if idempotent else {})}
            for status in statuses}


def _contract(model, *scopes):
    # json_contract defaults to jobs:submit, which is wrong here: always overwrite with the explicit scopes.
    return {**json_contract(model), **operation(*scopes)}


def _key():
    return Header(..., min_length=8, max_length=128, description="ASCII idempotency key, 8-128 characters.")


def _limit():
    return Query(25, ge=1, le=100, description="Page size, 1-100.")


# -- contexts
@router.get("/contexts", response_model=ContextPage, operation_id="ocr_list_contexts",
            openapi_extra=operation("contexts:read"), responses=_reply(ContextPage, 200))
def list_contexts(limit: int = _limit(), after: str | None = Query(None, description="Opaque cursor from next_cursor."),
                  principal: Principal = Depends(require_principal), services: CatalogServices = Depends(get_catalog_services)):
    """List caller-owned contexts as summaries (no fields), ordered by ID. Continue with next_cursor."""
    return services.contexts.list(principal, limit=limit, after=after)


@router.post("/contexts", response_model=ContextReceipt, status_code=201, operation_id="ocr_create_context",
             openapi_extra=_contract(ContextInput, "contexts:read", "contexts:write"),
             responses=_reply(ContextReceipt, 201, 200, idempotent=True))
async def create_context(request: Request, response: Response, idempotency_key: str = _key(),
                         principal: Principal = Depends(require_principal), services: CatalogServices = Depends(get_catalog_services)):
    """Create a context once per identity/key. 201 on creation; 200 with the original receipt on replay. A different body under the same key conflicts. The receipt is not the current state."""
    principal.require("contexts:read")
    principal.require("contexts:write")
    body = await json_input(request, ContextInput, max_bytes=MAX_BODY_BYTES)
    receipt, replayed = await run_in_threadpool(services.contexts.create, principal=principal, body=body, key=idempotency_key)
    response.status_code = 200 if replayed else 201
    response.headers["Idempotency-Replayed"] = "true" if replayed else "false"
    return receipt


@router.get("/contexts/{context_id}", response_model=ContextRecord, operation_id="ocr_get_context",
            openapi_extra=operation("contexts:read"), responses=_reply(ContextRecord, 200))
def get_context(context_id: str, principal: Principal = Depends(require_principal),
                services: CatalogServices = Depends(get_catalog_services)):
    """Get the current context with its revision. Foreign and missing IDs both return 404."""
    return services.contexts.get(principal, context_id)


@router.put("/contexts/{context_id}", response_model=ContextRecord, operation_id="ocr_update_context",
            openapi_extra=_contract(ContextUpdateInput, "contexts:read", "contexts:write"),
            responses=_reply(ContextRecord, 200))
async def update_context(context_id: str, request: Request, principal: Principal = Depends(require_principal),
                         services: CatalogServices = Depends(get_catalog_services)):
    """Replace the whole context if expected_revision is current (compare-and-swap). Published versions are never changed. Not retried automatically; on 409 read it again."""
    principal.require("contexts:read")
    principal.require("contexts:write")
    body = await json_input(request, ContextUpdateInput, max_bytes=MAX_BODY_BYTES)
    return await run_in_threadpool(services.contexts.update, principal, context_id, body.body,
                                   expected_revision=body.expected_revision)


# -- document types
@router.get("/contexts/{context_id}/types", response_model=TypePage, operation_id="ocr_list_document_types",
            openapi_extra=operation("contexts:read"), responses=_reply(TypePage, 200))
def list_document_types(context_id: str, limit: int = _limit(),
                        after: str | None = Query(None, description="Opaque cursor from next_cursor."),
                        principal: Principal = Depends(require_principal),
                        services: CatalogServices = Depends(get_catalog_services)):
    """List document types of a caller-owned context as summaries (no fields), ordered by ID."""
    return services.types.list(principal, context_id, limit=limit, after=after)


@router.post("/contexts/{context_id}/types", response_model=TypeReceipt, status_code=201,
             operation_id="ocr_create_document_type",
             openapi_extra=_contract(TypeInput, "contexts:read", "contexts:write"),
             responses=_reply(TypeReceipt, 201, 200, idempotent=True))
async def create_document_type(context_id: str, request: Request, response: Response, idempotency_key: str = _key(),
                               principal: Principal = Depends(require_principal),
                               services: CatalogServices = Depends(get_catalog_services)):
    """Create a draft type in a context once per identity/key. 201 on creation; 200 with the original receipt on replay."""
    principal.require("contexts:read")
    principal.require("contexts:write")
    body = await json_input(request, TypeInput, max_bytes=MAX_BODY_BYTES)
    receipt, replayed = await run_in_threadpool(services.types.create, principal=principal, context_id=context_id,
                                                body=body, key=idempotency_key)
    response.status_code = 200 if replayed else 201
    response.headers["Idempotency-Replayed"] = "true" if replayed else "false"
    return receipt


@router.get("/document-types/{type_id}", response_model=TypeRecord, operation_id="ocr_get_document_type",
            openapi_extra=operation("contexts:read"), responses=_reply(TypeRecord, 200))
def get_document_type(type_id: str, principal: Principal = Depends(require_principal),
                      services: CatalogServices = Depends(get_catalog_services)):
    """Get the current draft type with its revision. Foreign and missing IDs both return 404."""
    return services.types.get(principal, type_id)


@router.put("/document-types/{type_id}", response_model=TypeRecord, operation_id="ocr_update_document_type",
            openapi_extra=_contract(TypeUpdateInput, "contexts:read", "contexts:write"),
            responses=_reply(TypeRecord, 200))
async def update_document_type(type_id: str, request: Request, principal: Principal = Depends(require_principal),
                               services: CatalogServices = Depends(get_catalog_services)):
    """Replace the whole draft type if expected_revision is current (compare-and-swap). Published versions are never changed. Not retried automatically."""
    principal.require("contexts:read")
    principal.require("contexts:write")
    body = await json_input(request, TypeUpdateInput, max_bytes=MAX_BODY_BYTES)
    return await run_in_threadpool(services.types.update, principal, type_id, body.body,
                                   expected_revision=body.expected_revision)


# -- immutable versions
@router.post("/document-types/{type_id}/versions", response_model=TypeVersionSummary, status_code=201,
             operation_id="ocr_publish_document_type",
             openapi_extra=_contract(TypePublishInput, "contexts:read", "contexts:publish"),
             responses=_reply(TypeVersionSummary, 201, 200, idempotent=True))
async def publish_document_type(type_id: str, request: Request, response: Response, idempotency_key: str = _key(),
                                principal: Principal = Depends(require_principal),
                                services: CatalogServices = Depends(get_catalog_services)):
    """Publish an immutable version from the current type and context revisions. 201 on creation; 200 replays the original summary even after later edits. Reference it by type_id + version + schema_sha256."""
    principal.require("contexts:read")
    principal.require("contexts:publish")
    body = await json_input(request, TypePublishInput, max_bytes=MAX_BODY_BYTES)
    summary, replayed = await run_in_threadpool(
        services.types.publish, principal, type_id, expected_revision=body.expected_revision,
        expected_context_revision=body.expected_context_revision, key=idempotency_key)
    response.status_code = 200 if replayed else 201
    response.headers["Idempotency-Replayed"] = "true" if replayed else "false"
    return summary


@router.get("/document-types/{type_id}/versions", response_model=TypeVersionPage,
            operation_id="ocr_list_document_type_versions", openapi_extra=operation("contexts:read"),
            responses=_reply(TypeVersionPage, 200))
def list_document_type_versions(type_id: str, limit: int = _limit(),
                                after: int | None = Query(None, ge=1, le=MAX_SQLITE_INT, description="Last version of the previous page."),
                                principal: Principal = Depends(require_principal),
                                services: CatalogServices = Depends(get_catalog_services)):
    """List published version summaries (no schema) in ascending order."""
    return services.types.list_versions(principal, type_id, limit=limit, after=after)


@router.get("/document-types/{type_id}/versions/{version}", response_model=TypeVersionRecord,
            response_model_by_alias=True, operation_id="ocr_get_document_type_version",
            openapi_extra=operation("contexts:read"), responses=_reply(TypeVersionRecord, 200))
def get_document_type_version(type_id: str, version: int = Path(..., ge=1, le=MAX_SQLITE_INT),
                              principal: Principal = Depends(require_principal),
                              services: CatalogServices = Depends(get_catalog_services)):
    """Get one immutable version with its stored canonical schema under the wire name "schema". The hash covers exactly those canonical bytes."""
    return services.types.get_version(principal, type_id, version)
