"""Bearer authentication for new transports; legacy API keys and cookies are excluded."""
import math
import time

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.application import ApplicationError, Principal
from app.config import settings
from app.services.auth_service import decode_token
from app.services.user_store import get_user_store

bearer = HTTPBearer(auto_error=False, scheme_name="UserBearer")


def authenticate_bearer(token: str | None) -> Principal:
    denied = ApplicationError(401, "authentication_required", "A valid Bearer token is required.")
    if not token:
        raise denied
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
    if user_id <= 0 or get_user_store().get_user_by_id(user_id) is None:
        raise denied
    return Principal(subject=f"user:{user_id}", user_id=user_id,
                     scopes=frozenset({"jobs:read", "artifacts:read"}))


def require_principal(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> Principal:
    return authenticate_bearer(credentials.credentials if credentials else None)
