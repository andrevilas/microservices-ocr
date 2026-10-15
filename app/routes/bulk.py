"""Persistent batches and ZIP export metadata; bytes remain authenticated HTTP."""
from fastapi import APIRouter, Depends, Header, Query, Request, Response
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from app.application import Principal
from app.bulk_models import BatchInput, BatchPage, BatchReceipt, BatchView, ExportInput, ExportReceipt
from app.http_contract import error_responses, example, operation
from app.routes.uploads import json_contract, json_input
from app.security import require_principal
from app.services.batch_service import BatchService, dispatch_batch, get_batch_service
from app.services.export_service import ExportService, get_export_service
from app.services.job_queue import JobQueueProcessor, get_job_queue_processor
from app.services.upload_service import CHUNK

router = APIRouter(prefix="/api/v1", tags=["Batches and exports"],
                   responses=error_responses(401, 403, 404, 405, 409, 410, 413, 422, 429, 500, 503))

BATCH_EXAMPLE = {"batch_id": "1" * 32, "created_at": "2026-10-15T12:00:00Z", "admission_policy": "all_or_nothing",
                 "poll_after_seconds": 2, "items": [{"client_item_id": "item-1", "upload_id": "0" * 32, "job_id": "2" * 32,
                                                      "filename": "document.pdf", "status": "queued", "progress_percent": 0, "error_code": None}]}
BATCH_VIEW = {**BATCH_EXAMPLE, "counts": {"queued": 1}, "progress_percent": 0, "terminal": False}
EXPORT_EXAMPLE = {"export_id": "3" * 32, "created_at": "2026-10-15T12:00:00Z", "expires_at": "2026-10-15T13:00:00Z",
                  "status": "ready", "media_type": "application/zip", "size_bytes": 300, "sha256": "0" * 64,
                  "download_url": "/api/v1/exports/" + "3" * 32 + "/content",
                  "items": [{"job_id": "2" * 32, "filename": "document.pdf", "status": "queued", "included": False,
                             "reason": "job_not_completed", "archive_name": None, "size_bytes": None, "sha256": None}]}


@router.post("/batches", response_model=BatchReceipt, status_code=202, operation_id="ocr_create_batch",
             openapi_extra=json_contract(BatchInput), responses=example(BATCH_EXAMPLE, 202))
async def create_batch(request: Request, response: Response, idempotency_key: str = Header(..., min_length=8, max_length=128),
                       principal: Principal = Depends(require_principal), service: BatchService = Depends(get_batch_service),
                       queue: JobQueueProcessor = Depends(get_job_queue_processor)):
    """Atomically admit up to 25 owned ready uploads. No jobs on validation failure. Retry the same key and payload after dispatch failure; poll the batch for live status."""
    principal.require("jobs:submit")
    body = await json_input(request, BatchInput)
    receipt, replayed = await run_in_threadpool(dispatch_batch, service, queue, principal, body, idempotency_key)
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return receipt


@router.get("/batches", response_model=BatchPage, operation_id="ocr_list_batches", openapi_extra=operation("jobs:read"), responses=example({"items": [BATCH_VIEW], "next_cursor": None}))
def list_batches(limit: int = Query(50, ge=1, le=100), cursor: str | None = Query(None, max_length=512),
                 principal: Principal = Depends(require_principal), service: BatchService = Depends(get_batch_service)):
    """List caller-owned batches by creation time and ID, with a cursor bound to the caller."""
    return service.list(principal, limit, cursor)


@router.get("/batches/{batch_id}", response_model=BatchView, operation_id="ocr_get_batch", openapi_extra=operation("jobs:read"), responses=example(BATCH_VIEW))
def get_batch(batch_id: str, principal: Principal = Depends(require_principal), service: BatchService = Depends(get_batch_service)):
    """Read the persistent item manifest, live states/errors, counts and aggregate progress."""
    return service.get(principal, batch_id)


_export_contract = json_contract(ExportInput)
_export_contract.update(operation("jobs:read", "artifacts:read"))


@router.post("/exports", response_model=ExportReceipt, status_code=202, operation_id="ocr_create_export",
             openapi_extra=_export_contract, responses=example(ExportReceipt(export_id="3" * 32, created_at="2026-10-15T12:00:00Z").model_dump(mode="json"), 202))
async def create_export(request: Request, response: Response, idempotency_key: str = Header(..., min_length=8, max_length=128),
                        principal: Principal = Depends(require_principal), service: ExportService = Depends(get_export_service)):
    """Admit an asynchronous ZIP export of explicit owned job IDs. Poll its status. The worker snapshots included/excluded items, even when none are ready. Other owners/missing IDs reject admission. Same key replays the original queued receipt."""
    principal.require("jobs:read")
    principal.require("artifacts:read")
    body = await json_input(request, ExportInput)
    receipt, replayed = await run_in_threadpool(service.create, principal, body, idempotency_key)
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return receipt


@router.get("/exports/{export_id}", response_model=ExportReceipt, operation_id="ocr_get_export", openapi_extra=operation("jobs:read", "artifacts:read"), responses=example(EXPORT_EXAMPLE))
def get_export(export_id: str, principal: Principal = Depends(require_principal), service: ExportService = Depends(get_export_service)):
    """Return the caller-owned ZIP snapshot manifest, hash and authenticated download URL; expired exports return 410."""
    return service.get(principal, export_id)


class ExportResponse(StreamingResponse):
    def __init__(self, service, receipt, source):
        self.service, self.receipt, self.source = service, receipt, source
        def chunks():
            while chunk := source.read(CHUNK):
                yield chunk
        super().__init__(chunks(), media_type="application/zip", headers={
            "Content-Length": str(receipt.size_bytes),
            "Content-Disposition": f'attachment; filename="{receipt.export_id}.zip"',
            "ETag": '"' + receipt.sha256 + '"'})

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Includes disconnects before the generator starts; release only after reads finish.
            self.service.close_download(self.receipt.export_id, self.source)


@router.get("/exports/{export_id}/content", operation_id="ocr_download_export", response_class=StreamingResponse,
            openapi_extra=operation("jobs:read", "artifacts:read"),
            responses={200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}}}})
def download_export(export_id: str, principal: Principal = Depends(require_principal), service: ExportService = Depends(get_export_service)):
    """Stream an authenticated ZIP from disk. Full download only; new requests after expiry fail. An active download retains its lease until disconnect or completion."""
    receipt, source = service.open_download(principal, export_id)
    return ExportResponse(service, receipt, source)
