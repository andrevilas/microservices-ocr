"""Human-admin provisioning. Secret issuance is deliberately absent from MCP."""
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.application import ApplicationError, Principal
from app.security import require_identity_admin
from app.services.identity_store import IdentityNotFound, IdentityStore, get_identity_store
from app.http_contract import error_responses, example, operation


class ServiceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True,
                             json_schema_extra={"examples": [{"name": "Local importer"}]})
    name: str = Field(min_length=1, max_length=80)


class CredentialInput(ServiceInput):
    model_config = ConfigDict(json_schema_extra={"examples": [{"name": "Local reader", "scopes": ["jobs:read", "artifacts:read"], "expires_in_seconds": 2592000}]})
    scopes: list[Literal["jobs:read", "artifacts:read", "jobs:submit", "jobs:cancel"]] = Field(min_length=1, max_length=4)
    expires_in_seconds: int = Field(default=2592000, ge=60, le=31536000, strict=True)


class ServiceResponse(BaseModel):
    id: str
    name: str
    created_at: float
    created_by: str
    disabled_at: float | None


class CredentialResponse(BaseModel):
    id: str
    principal_id: str
    name: str
    scopes: list[str]
    created_at: float
    created_by: str
    expires_at: float
    revoked_at: float | None
    last_used_at: float | None


class IssuedCredential(BaseModel):
    credential: CredentialResponse
    token: str = Field(repr=False, description="Returned once. Store securely; never pass to an MCP tool.")


def no_store(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


router = APIRouter(prefix="/api/v1/service-principals", tags=["Service identities"],
                   dependencies=[Depends(no_store)],
                   responses=error_responses(401, 403, 404, 405, 422, 500))
SERVICE_EXAMPLE = {"id": "service:00000000000000000000000000000001", "name": "Local importer",
                   "created_at": 1792065600.0, "created_by": "user:1", "disabled_at": None}
CREDENTIAL_EXAMPLE = {"id": "00000000000000000000000000000002", "principal_id": SERVICE_EXAMPLE["id"],
                      "name": "Local reader", "scopes": ["artifacts:read", "jobs:read"],
                      "created_at": 1792065600.0, "created_by": "user:1", "expires_at": 1794657600.0,
                      "revoked_at": None, "last_used_at": None}


def call(operation, *args):
    try:
        return operation(*args)
    except IdentityNotFound:
        raise ApplicationError(404, "identity_not_found", "Identity or credential not found.") from None
    except ValueError as error:
        raise ApplicationError(422, "invalid_identity_request", str(error)) from None


@router.post("", response_model=ServiceResponse, status_code=201, operation_id="ocr_create_service_principal",
             openapi_extra=operation(admin=True), responses=example(SERVICE_EXAMPLE, 201))
def create_service(body: ServiceInput, admin: Principal = Depends(require_identity_admin), store: IdentityStore = Depends(get_identity_store)):
    """Create an independent service owner. Requires a human administrator Bearer JWT."""
    return call(store.create_service, body.name, admin.subject)


@router.get("", response_model=list[ServiceResponse], operation_id="ocr_list_service_principals",
            openapi_extra=operation(admin=True), responses=example([SERVICE_EXAMPLE]))
def list_services(after: str = Query("", max_length=40), limit: int = Query(100, ge=1, le=100),
                  admin: Principal = Depends(require_identity_admin), store: IdentityStore = Depends(get_identity_store)):
    """List metadata in ID order, at most 100; use the last ID as after. Never returns secrets."""
    return store.list_services(after, limit)


@router.post("/{principal_id}/disable", response_model=ServiceResponse, operation_id="ocr_disable_service_principal",
             openapi_extra=operation(admin=True), responses=example({**SERVICE_EXAMPLE, "disabled_at": 1792065700.0}))
def disable_service(principal_id: str, admin: Principal = Depends(require_identity_admin), store: IdentityStore = Depends(get_identity_store)):
    """Permanently disable all service credentials. Idempotent; does not delete documents."""
    return call(store.disable_service, principal_id, admin.subject)


@router.post("/{principal_id}/credentials", response_model=IssuedCredential, status_code=201, operation_id="ocr_issue_service_credential",
             openapi_extra=operation(admin=True), responses=example({"credential": CREDENTIAL_EXAMPLE, "token": "<returned-once>"}, 201))
def issue_credential(principal_id: str, body: CredentialInput, admin: Principal = Depends(require_identity_admin), store: IdentityStore = Depends(get_identity_store)):
    """Issue a credential with explicit scopes and expiry. Store the returned token securely; it cannot be retrieved again."""
    metadata, token = call(store.issue_credential, principal_id, body.name, body.scopes, body.expires_in_seconds, admin.subject)
    return IssuedCredential(credential=CredentialResponse(**metadata), token=token)


@router.get("/{principal_id}/credentials", response_model=list[CredentialResponse], operation_id="ocr_list_service_credentials",
            openapi_extra=operation(admin=True), responses=example([CREDENTIAL_EXAMPLE]))
def list_credentials(principal_id: str, after: str = Query("", max_length=32), limit: int = Query(100, ge=1, le=100),
                     admin: Principal = Depends(require_identity_admin), store: IdentityStore = Depends(get_identity_store)):
    """List credential metadata in ID order; use last ID as after. Secrets and hashes are excluded."""
    return call(store.list_credentials, principal_id, after, limit)


@router.delete("/{principal_id}/credentials/{credential_id}", response_model=CredentialResponse, operation_id="ocr_revoke_service_credential",
               openapi_extra=operation(admin=True), responses=example({**CREDENTIAL_EXAMPLE, "revoked_at": 1792065700.0}))
def revoke_credential(principal_id: str, credential_id: str, admin: Principal = Depends(require_identity_admin), store: IdentityStore = Depends(get_identity_store)):
    """Revoke a credential idempotently. The next HTTP or MCP request is denied; in-flight work is not interrupted."""
    return call(store.revoke_credential, principal_id, credential_id, admin.subject)
