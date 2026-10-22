"""Bearer authentication for new transports; legacy API keys and cookies are excluded."""
import math
import time

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.application import ApplicationError, Principal
from app.config import settings
from app.observability import admit_principal
from app.scopes import effective_local_scopes
from app.services.auth_service import decode_token
from app.services.user_store import get_user_store
from app.services.identity_store import get_identity_store

bearer = HTTPBearer(auto_error=False, scheme_name="PrincipalBearer",
                    description="User JWT or individual opaque service credential. Not the legacy global API key.")


def authenticate_bearer(token: str | None) -> Principal:
    """Authenticate, then charge the principal's request rate (HTTP v1, MCP and legacy Bearer)."""
    principal = _authenticate(token)
    admit_principal(principal)
    return principal


def _authenticate(token: str | None) -> Principal:
    denied = ApplicationError(401, "authentication_required", "A valid Bearer token is required.")
    if not token:
        raise denied
    if token.startswith("ocr_svc_"):
        identity = get_identity_store().authenticate(token)
        if identity is None:
            raise denied
        return Principal(subject=identity["subject"], user_id=None, scopes=frozenset(identity["scopes"]),
                         kind="service", credential_id=identity["credential_id"], auth_source="service")
    payload = decode_token(token, settings.jwt_secret)
    if not isinstance(payload, dict):
        raise denied
    subject, expiry = payload.get("sub"), payload.get("exp")
    if isinstance(subject, bool) or not isinstance(subject, (int, str)):
        raise denied
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry) or expiry <= time.time():
        raise denied
    try:
        user_id = int(subject)
    except (ValueError, TypeError):
        raise denied from None
    user = get_user_store().get_user_by_id(user_id) if 0 < user_id <= 9223372036854775807 else None
    if user is None:
        raise denied
    try:
        scopes = effective_local_scopes(payload, user.get("role"))
    except ValueError:
        raise denied from None
    return Principal(subject=f"user:{user_id}", user_id=user_id, scopes=scopes,
                     is_admin=user.get("role") == "admin", auth_source="local_jwt")


def require_principal(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    return authenticate_bearer(credentials.credentials if credentials else None)


def require_identity_admin(principal: Principal = Depends(require_principal)) -> Principal:
    if principal.kind != "user" or not principal.is_admin:
        raise ApplicationError(403, "admin_required", "A human administrator identity is required.")
    return principal
