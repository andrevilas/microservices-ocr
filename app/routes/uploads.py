"""HTTP byte transport and idempotent admission. MCP consumes IDs, never PDF JSON."""
import asyncio

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from python_multipart.exceptions import MultipartParseError
from starlette.concurrency import run_in_threadpool

from app.application import ApplicationError, Principal
from app.config import settings
from app.options import ProcessingOptions
from app.http_contract import error_responses, example, operation
from app.security import require_principal
from app.services.job_queue import JobQueueProcessor, get_job_queue_processor
from app.services.upload_service import CHUNK, MIB, JobAdmission, UploadReceipt, UploadService, get_upload_service
from app.upload_stream import MultipartUpload


class ReservationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"filename": "example.pdf", "size_bytes": 12345}]})
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0, strict=True)
    sha256: str | None = Field(default=None, pattern="^[0-9a-f]{64}$")


class AdmissionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"upload_id": "00000000000000000000000000000001"}]})
    upload_id: str = Field(pattern="^[0-9a-f]{32}$")
    options: ProcessingOptions = Field(default_factory=ProcessingOptions)


RECEIPT = {"upload_id": "00000000000000000000000000000001", "filename": "example.pdf", "state": "reserved",
           "expected_size": 12345, "size_bytes": None, "sha256": None, "page_count": None,
           "expires_at": 1792072800.0, "content_url": "/api/v1/uploads/00000000000000000000000000000001/content"}
READY = {**RECEIPT, "state": "ready", "size_bytes": 12345, "sha256": "0" * 64, "page_count": 1}
router = APIRouter(prefix="/api/v1", tags=["Upload and admission"],
                   responses=error_responses(401, 403, 404, 405, 408, 409, 410, 413, 415, 422, 429, 500, 503))


def json_contract(model):
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})
    def inline(value):
        if isinstance(value, dict):
            if "$ref" in value:
                return inline(definitions[value["$ref"].rsplit("/", 1)[1]])
            return {key: inline(item) for key, item in value.items()}
        return [inline(item) for item in value] if isinstance(value, list) else value
    return {**operation("jobs:submit"), "requestBody": {"required": True, "content": {"application/json": {"schema": inline(schema)}}}}


async def json_input(request, model):
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise ApplicationError(415, "unsupported_media_type", "Send application/json.")
    payload = bytearray()
    try:
        async with asyncio.timeout(settings.upload_timeout_seconds):
            async for chunk in request.stream():
                if len(payload) + len(chunk) > 65536:
                    raise ApplicationError(413, "request_too_large", "JSON request exceeds 64 KiB.")
                payload.extend(chunk)
    except TimeoutError:
        raise ApplicationError(408, "upload_timeout", "Request deadline exceeded.") from None
    try:
        return model.model_validate_json(payload)
    except ValidationError as error:
        raise RequestValidationError(error.errors()) from None


async def upload_io(function, *args):
    # A cancelled client must not close/delete a file while a worker thread still uses it.
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


async def receive_upload(request, principal, service, upload_id, multipart=False):
    session = service.begin(principal, upload_id)
    try:
        parser = MultipartUpload(request.headers.get("content-type", ""), session) if multipart else None
        if not multipart and request.headers.get("content-type", "").split(";")[0] not in ("application/pdf", "application/octet-stream"):
            raise ApplicationError(415, "unsupported_media_type", "Send PDF bytes with application/pdf.")
        total = 0
        maximum = session.row["reserved_bytes"] + (65536 if multipart else 0)
        async with asyncio.timeout(settings.upload_timeout_seconds):
            async for chunk in request.stream():
                total += len(chunk)
                if total > maximum:
                    if session.row["sha256"]:
                        raise ApplicationError(409, "upload_content_conflict", "A ready upload cannot be replaced with different bytes.")
                    raise ApplicationError(413, "file_too_large", "Upload body exceeds the accepted size.")
                for start in range(0, len(chunk), CHUNK):
                    part = chunk[start:start + CHUNK]
                    await upload_io(parser.write if parser else session.write, part)
            if parser:
                parser.finish()
        return await upload_io(session.finish)
    except TimeoutError:
        raise ApplicationError(408, "upload_timeout", "Upload deadline exceeded; restart the content transfer.") from None
    except MultipartParseError:
        raise ApplicationError(422, "invalid_multipart", "Invalid multipart body.") from None
    except OSError:
        raise ApplicationError(503, "storage_unavailable", "Upload storage is unavailable.") from None
    finally:
        # Synchronous bounded cleanup must run even if the client task was cancelled.
        session.close()


@router.post("/upload-sessions", response_model=UploadReceipt, status_code=201, operation_id="ocr_prepare_upload",
             openapi_extra=json_contract(ReservationInput), responses=example(RECEIPT, 201))
async def reserve(request: Request, principal: Principal = Depends(require_principal), service: UploadService = Depends(get_upload_service)):
    """Reserve input storage for a PDF. Send bytes to content_url using the same Bearer identity before expires_at."""
    principal.require("jobs:submit")
    body = await json_input(request, ReservationInput)
    return await run_in_threadpool(service.reserve, principal, body.filename, body.size_bytes, body.sha256)


@router.get("/uploads/{upload_id}", response_model=UploadReceipt, operation_id="ocr_get_upload",
            openapi_extra=operation("jobs:submit"), responses=example(READY))
def get_upload(upload_id: str, principal: Principal = Depends(require_principal), service: UploadService = Depends(get_upload_service)):
    """Get a caller-owned receipt. Inaccessible IDs return 404; expired reservations return 410."""
    return service.get(principal, upload_id)


@router.put("/uploads/{upload_id}/content", response_model=UploadReceipt, operation_id="ocr_upload_content",
            openapi_extra={**operation("jobs:submit"), "requestBody": {"required": True, "content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}}}},
            responses={**example(READY), **error_responses(408)})
async def upload_content(upload_id: str, request: Request, principal: Principal = Depends(require_principal), service: UploadService = Depends(get_upload_service)):
    """Stream PDF bytes to a reservation; validate size, checksum and structure. Same-byte retries preserve the upload; different bytes conflict."""
    return await receive_upload(request, principal, service, upload_id)


@router.post("/uploads", response_model=UploadReceipt, status_code=201, operation_id="ocr_upload_pdf",
             openapi_extra={**operation("jobs:submit"), "requestBody": {"required": True, "content": {"multipart/form-data": {
                 "schema": {"type": "object", "additionalProperties": False, "required": ["file"], "properties": {"file": {"type": "string", "format": "binary"}}}}}}},
             responses={**example(READY, 201), **error_responses(408)})
async def upload_pdf(request: Request, principal: Principal = Depends(require_principal), service: UploadService = Depends(get_upload_service)):
    """Convenience upload: exactly one PDF in multipart field file. Streams directly to reserved storage; no full-body spool. Retrying creates a new upload ID."""
    receipt = service.reserve(principal, "document.pdf", None)
    try:
        return await receive_upload(request, principal, service, receipt.upload_id, multipart=True)
    except BaseException:
        service.discard(principal, receipt.upload_id)
        raise


@router.post("/jobs", response_model=JobAdmission, status_code=202, operation_id="ocr_submit_job",
             openapi_extra=json_contract(AdmissionInput),
             responses={202: {**example({"job_id": "00000000000000000000000000000002", "upload_id": RECEIPT["upload_id"], "status": "queued", "poll_after_seconds": 2}, 202)[202],
                              "headers": {"Idempotency-Replayed": {"description": "Whether this is the original persisted receipt replayed for the same identity/key.", "schema": {"type": "string", "enum": ["true", "false"]}}}}})
async def submit_job(request: Request, response: Response, idempotency_key: str = Header(..., min_length=8, max_length=128),
                     principal: Principal = Depends(require_principal), service: UploadService = Depends(get_upload_service),
                     queue: JobQueueProcessor = Depends(get_job_queue_processor)):
    """Persist one job and validated immutable options per identity/key. Changed options conflict; replays retain the original receipt even if engine availability changes."""
    principal.require("jobs:submit")
    body = await json_input(request, AdmissionInput)
    try:
        receipt, replayed = await run_in_threadpool(service.submit, principal, body.upload_id, idempotency_key, body.options)
        job = service.jobs.get_snapshot(receipt.job_id)
        if job and job.status == "queued":
            try:
                await run_in_threadpool(queue.enqueue, job.job_id)
            except Exception:
                raise ApplicationError(503, "queue_unavailable", "Job is persisted; retry with the same key or recover the runtime queue.") from None
    except OSError:
        raise ApplicationError(503, "storage_unavailable", "Admission storage is unavailable; retry with the same key.") from None
    response.headers["Idempotency-Replayed"] = "true" if replayed else "false"
    return receipt
