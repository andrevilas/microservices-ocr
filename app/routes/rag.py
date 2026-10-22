"""Authenticated RAG delivery operations; package bytes only over HTTP."""
import json
from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool
from app.application import Principal
from app.http_contract import error_responses, operation, example
from app.rag_models import DeliveryId, DeliveryInput, DeliveryStatus, DestinationList, RecoveryLink
from app.routes.uploads import json_contract, json_input
from app.security import require_principal
from app.services.rag_application import require_rag
from app.services.rag_delivery_contract import DeliveryReceipt, parse_receipt

router = APIRouter(prefix="/api/v1/rag", tags=["RAG delivery"],
                   responses=error_responses(401, 403, 404, 409, 413, 415, 422, 429, 500, 503))
STATUS = DeliveryStatus(id="a" * 64, job_id="1" * 32, state="queued", deadline=1793000000,
                        attempts=0, next_attempt=1792900000, error=None).model_dump()
RECEIPT = parse_receipt(json.dumps(dict(contract_version="ocr-rag/1", delivery_id="a" * 64,
    destination_id="normas", manifest_sha256="b" * 64, destination_document_id="2" * 32,
    destination_revision="c" * 64, storage_state="stored", indexing_state="indexed",
    artifacts=[{"role": role, "media_type": media, "sha256": "d" * 64, "size_bytes": 100}
               for role, media in (("pdf", "application/pdf"), ("pages", "application/x-ndjson"))])).encode()).model_dump(mode="json")
LINK = RecoveryLink(delivery_id="a" * 64, size_bytes=1000, sha256="d" * 64,
    content_url="/api/v1/rag/deliveries/" + "a" * 64 + "/recovery/content").model_dump()


@router.get("/destinations", response_model=DestinationList, responses=example({"items": []}), openapi_extra=operation("jobs:read"))
async def destinations(request: Request, principal: Principal = Depends(require_principal)):
    """List caller-owned configured destinations without exposing URLs or credentials."""
    return await run_in_threadpool(require_rag(request).list_rag_destinations, principal)


@router.post("/deliveries", response_model=DeliveryStatus,
             responses=example(STATUS),
             openapi_extra={**json_contract(DeliveryInput), **operation("jobs:read", "artifacts:read")})
async def submit(request: Request, principal: Principal = Depends(require_principal)):
    """Queue delivery of a completed result; the same job and destination replay the existing delivery."""
    principal.require("jobs:read")
    principal.require("artifacts:read")
    body = await json_input(request, DeliveryInput, max_bytes=4096)
    return await run_in_threadpool(require_rag(request).submit_rag_delivery, principal, body)


@router.get("/deliveries/{delivery_id}", response_model=DeliveryStatus, responses=example(STATUS), openapi_extra=operation("jobs:read"))
async def status(request: Request, delivery_id: DeliveryId, principal: Principal = Depends(require_principal)):
    """Read owned delivery progress; queued admission does not prove indexing."""
    return await run_in_threadpool(require_rag(request).get_rag_delivery, principal, delivery_id)


@router.get("/deliveries/{delivery_id}/receipt", response_model=DeliveryReceipt, responses=example(RECEIPT), openapi_extra=operation("jobs:read"))
async def receipt(request: Request, delivery_id: DeliveryId, principal: Principal = Depends(require_principal)):
    """Read the last verified receipt. Its indexing state is authoritative for this delivery."""
    value = await run_in_threadpool(require_rag(request).get_rag_receipt, principal, delivery_id)
    return parse_receipt(json.dumps(value).encode())


@router.post("/deliveries/{delivery_id}/cancel", response_model=DeliveryStatus,
             responses=example({**STATUS, "state": "canceled"}),
             openapi_extra=operation("jobs:read", "artifacts:delete"))
async def cancel(request: Request, delivery_id: DeliveryId, principal: Principal = Depends(require_principal)):
    """Abandon delivery and release its hold; expired source files may then be deleted."""
    return await run_in_threadpool(require_rag(request).cancel_rag_delivery, principal, delivery_id)


@router.get("/deliveries/{delivery_id}/recovery", response_model=RecoveryLink,
            responses=example(LINK),
            openapi_extra=operation("jobs:read", "artifacts:read"))
async def recovery(request: Request, delivery_id: DeliveryId, principal: Principal = Depends(require_principal)):
    """Read the authenticated download path, size and hash of a retained recovery package."""
    return await run_in_threadpool(require_rag(request).get_rag_recovery, principal, delivery_id)


@router.get("/deliveries/{delivery_id}/recovery/content", response_class=Response,
            responses={200: {"description": "Immutable recovery package bytes; downloading never releases hold.",
                             "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}, "example": ""}}}},
            openapi_extra=operation("jobs:read", "artifacts:read"))
async def content(request: Request, delivery_id: DeliveryId, principal: Principal = Depends(require_principal)):
    """Download the retained package without changing its hold or proving external receipt."""
    raw = await run_in_threadpool(require_rag(request).outbox.recovery_package, principal, delivery_id)
    return Response(raw, media_type="application/octet-stream", headers={
        "Cache-Control": "no-store", "Content-Disposition": 'attachment; filename="rag-recovery.json"'})
