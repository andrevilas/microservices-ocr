"""Document package HTTP API (E4a): create, list, read and download versioned packages.

Packages derive from an approved metadata snapshot; they never modify it.
Creation is naturally idempotent per job, approved revision and original
inclusion, so it takes no Idempotency-Key. Downloads re-hash the stored ZIP
and return 409 if it changed; 410 after expiry or artifact removal.
"""
from typing import Annotated

from fastapi import APIRouter, Path, Request
from starlette.responses import Response

from app.document_http_streaming import DocumentDownloadResponse
from app.http_contract import error_responses, operation
from app.package_models import CreatePackageInput, PackageList, PackageReceipt, PackageView
from app.routes.documents import Reader, Services
from app.routes.uploads import json_input

router = APIRouter(
    prefix="/api/v1/jobs/{job_id}/packages", tags=["Packages"],
    responses=error_responses(401, 403, 404, 405, 408, 409, 410, 413, 422, 429, 500, 503),
)
ID = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
_SCOPES = ("jobs:read", "artifacts:read", "metadata:read")


def _body_contract():
    schema = CreatePackageInput.model_json_schema()
    return {**operation(*_SCOPES), "requestBody": {
        "required": True, "content": {"application/json": {"schema": schema}}}}


@router.post(
    "", response_model=PackageReceipt, status_code=201,
    operation_id="ocr_create_document_package", openapi_extra=_body_contract(),
    responses={**error_responses(415), 200: {"model": PackageReceipt, "description": "Existing package replayed."}},
)
async def create_package(job_id: ID, request: Request, response: Response, principal: Reader, services: Services):
    """Build a PDF/A package from the latest (or the given) approved metadata snapshot.

    The domain fields whose published spec has destination=xmp are written to
    XMP under a declared extension schema; restricted fields never enter the
    PDF. veraPDF validates the exact final bytes after the last write and any
    other verdict than passed rejects the package (422). Returns 200 with the
    existing package when one is ready for the same revision and options.
    """
    body = await json_input(request, CreatePackageInput, max_bytes=4096)
    receipt = await services.execution.run(
        principal, lambda control: services.packages.create(
            principal, job_id, body, checkpoint=control.checkpoint))
    response.status_code = 200 if receipt.replayed else 201
    response.headers["Cache-Control"] = "no-store"
    return receipt


@router.get("", response_model=PackageList, operation_id="ocr_list_document_packages",
            openapi_extra=operation(*_SCOPES))
async def list_packages(job_id: ID, principal: Reader, services: Services):
    """List every package version of a caller-owned job, oldest first, with its state and manifest."""
    return await services.execution.run(principal, lambda control: services.packages.list(principal, job_id))


@router.get("/{package_id}", response_model=PackageView, operation_id="ocr_get_document_package",
            openapi_extra=operation(*_SCOPES))
async def get_package(job_id: ID, package_id: ID, principal: Reader, services: Services):
    """Return one package: state (ready, invalid, expired), expiry, hashes and the full manifest."""
    return await services.execution.run(
        principal, lambda control: services.packages.get(principal, job_id, package_id))


@router.get(
    "/{package_id}/content", response_class=Response, operation_id="ocr_download_document_package",
    openapi_extra=operation(*_SCOPES),
    responses={200: {
        "description": "Exact package ZIP download.",
        "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
        "headers": {
            "Content-Length": {"schema": {"type": "integer", "minimum": 0}},
            "X-Content-SHA256": {"schema": {"type": "string", "pattern": "^[0-9a-f]{64}$"}},
            "Content-Disposition": {"schema": {"type": "string", "enum": ['attachment; filename="package.zip"']}},
            "Cache-Control": {"schema": {"type": "string", "enum": ["no-store"]}},
        },
    }},
)
async def download_package(job_id: ID, package_id: ID, principal: Reader, services: Services):
    """Download package.zip after re-hashing it against the recorded SHA-256.

    A changed or missing file permanently marks the package invalid (409).
    Range requests are not supported.
    """
    return DocumentDownloadResponse(lambda: services.streams.open_package(principal, job_id, package_id))
