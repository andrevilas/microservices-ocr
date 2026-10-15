"""Explicit job control. Cancel request lifetime is independent of transport lifetime."""
from fastapi import APIRouter, Depends, Header, Request, Response
from starlette.concurrency import run_in_threadpool
from app.application import Principal
from app.control_models import CancelInput, CancelPage, CancelResult, RetryInput, RetryReceipt
from app.http_contract import error_responses, example, operation
from app.routes.uploads import json_contract, json_input
from app.security import require_principal
from app.services.control_service import ControlService, get_control_service, retry_and_enqueue
from app.services.job_queue import JobQueueProcessor, get_job_queue_processor

router = APIRouter(prefix="/api/v1/jobs", tags=["Job control"],
                   responses=error_responses(401, 403, 404, 405, 408, 409, 410, 413, 415, 422, 429, 500, 503))
CANCEL = {"job_id": "0" * 32, "status": "cancel_requested", "outcome": "requested", "poll_after_seconds": 1}
_contract = json_contract(CancelInput)
_contract.update(operation("jobs:cancel"))


@router.post("/cancel-batch", response_model=CancelPage, operation_id="ocr_cancel_jobs",
             openapi_extra=_contract, responses=example({"items": [CANCEL]}))
async def cancel_jobs(request: Request, principal: Principal = Depends(require_principal),
                      service: ControlService = Depends(get_control_service)):
    """Cancel up to 25 explicit IDs, returning a result per item. Foreign/missing IDs both yield not_found. No implicit global cancellation."""
    principal.require("jobs:cancel")
    body = await json_input(request, CancelInput)
    return await run_in_threadpool(service.cancel_many, principal, body)


@router.post("/{job_id}/cancel", response_model=CancelResult, operation_id="ocr_cancel_job",
             openapi_extra=operation("jobs:cancel"), responses=example(CANCEL))
def cancel_job(job_id: str, principal: Principal = Depends(require_principal),
               service: ControlService = Depends(get_control_service)):
    """Idempotently cancel a queued job or persist cancel_requested for running work. Poll until canceled; completion may win the race and returns already_terminal."""
    return service.cancel(principal, job_id)


@router.post("/{job_id}/retry", response_model=RetryReceipt, status_code=202, operation_id="ocr_retry_job",
             openapi_extra=json_contract(RetryInput),
             responses=example({"job_id": "1" * 32, "parent_job_id": "0" * 32, "status": "queued", "poll_after_seconds": 2}, 202))
async def retry_job(job_id: str, request: Request, response: Response,
                    idempotency_key: str = Header(..., min_length=8, max_length=128),
                    principal: Principal = Depends(require_principal),
                    service: ControlService = Depends(get_control_service),
                    queue: JobQueueProcessor = Depends(get_job_queue_processor)):
    """Create a derived job from retained input of a terminal original. Omitted options inherit its requested options; legacy jobs need explicit options. New capabilities/quotas apply. Same key/payload replays the queued receipt."""
    principal.require("jobs:submit")
    body = await json_input(request, RetryInput)
    receipt, replayed = await run_in_threadpool(retry_and_enqueue, service, queue, principal, job_id, body, idempotency_key)
    response.headers["Idempotency-Replayed"] = str(replayed).lower()
    return receipt
