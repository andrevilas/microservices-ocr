"""Human-admin provisioning. Secret issuance is deliberately absent from MCP."""
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from app.application import ApplicationError, ErrorResponse, Principal
from app.security import require_identity_admin
from app.services.identity_store import IdentityNotFound, IdentityStore, get_identity_store


class ServiceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)


class CredentialInput(ServiceInput):
    scopes: list[str] = Field(min_length=1, max_length=4)
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
                   responses={code: {"model": ErrorResponse} for code in (401, 403, 404, 422)})


def call(operation, *args):
    try:
        return operation(*args)
    except IdentityNotFound:
        raise ApplicationError(404, "identity_not_found", "Identity or credential not found.") from None
    except ValueError as error:
        raise ApplicationError(422, "invalid_identity_request", str(error)) from None


@router.post("", response_model=ServiceResponse, status_code=201, operation_id="ocr_create_service_principal")
def create_service(body: ServiceInput, admin: Principal = Depends(require_identity_admin), store: IdentityStore = Depends(get_identity_store)):
    return call(store.create_service, body.name, admin.subject)


@router.get("", response_model=list[ServiceResponse], operation_id="ocr_list_service_principals")
def list_services(after: str = Query("", max_length=40), limit: int = Query(100, ge=1, le=100),
                  admin: Principal = Depends(require_identity_admin), store: IdentityStore = Depends(get_identity_store)):
    return store.list_services(after, limit)


@router.post("/{principal_id}/disable", response_model=ServiceResponse, operation_id="ocr_disable_service_principal")
def disable_service(principal_id: str, admin: Principal = Depends(require_identity_admin), store: IdentityStore = Depends(get_identity_store)):
    return call(store.disable_service, principal_id, admin.subject)


@router.post("/{principal_id}/credentials", response_model=IssuedCredential, status_code=201, operation_id="ocr_issue_service_credential")
def issue_credential(principal_id: str, body: CredentialInput, admin: Principal = Depends(require_identity_admin), store: IdentityStore = Depends(get_identity_store)):
    metadata, token = call(store.issue_credential, principal_id, body.name, body.scopes, body.expires_in_seconds, admin.subject)
    return IssuedCredential(credential=CredentialResponse(**metadata), token=token)


@router.get("/{principal_id}/credentials", response_model=list[CredentialResponse], operation_id="ocr_list_service_credentials")
def list_credentials(principal_id: str, after: str = Query("", max_length=32), limit: int = Query(100, ge=1, le=100),
                     admin: Principal = Depends(require_identity_admin), store: IdentityStore = Depends(get_identity_store)):
    return call(store.list_credentials, principal_id, after, limit)


@router.delete("/{principal_id}/credentials/{credential_id}", response_model=CredentialResponse, operation_id="ocr_revoke_service_credential")
def revoke_credential(principal_id: str, credential_id: str, admin: Principal = Depends(require_identity_admin), store: IdentityStore = Depends(get_identity_store)):
    return call(store.revoke_credential, principal_id, credential_id, admin.subject)
