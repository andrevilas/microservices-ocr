"""Pure scope policy for local JWTs; no storage, runtime or security imports."""

LEGACY_SCOPES = frozenset({"jobs:read", "artifacts:read", "jobs:submit", "jobs:cancel", "artifacts:delete"})
DOCUMENT_SCOPES = frozenset({"contexts:read", "contexts:write", "contexts:publish",
                             "metadata:read", "metadata:write", "metadata:approve"})
LOCAL_ALLOWED_SCOPES = LEGACY_SCOPES | DOCUMENT_SCOPES
# Service credentials never receive contexts:publish or metadata:approve (human-only decisions).
SERVICE_SCOPES = LEGACY_SCOPES | frozenset({"contexts:read", "contexts:write", "metadata:read", "metadata:write"})
# Scopes an OAuth binding may grant; explicit bindings, never role- or claim-derived.
OAUTH_SCOPES = LEGACY_SCOPES | DOCUMENT_SCOPES
LOCAL_AUTHZ_VERSION = 1


def local_role_scopes(role: object) -> frozenset[str]:
    """Maximum local scopes for a role; unknown roles keep only the legacy set."""
    if role in ("user", "admin"):
        return LOCAL_ALLOWED_SCOPES
    return LEGACY_SCOPES


def signed_local_scopes(payload: dict) -> frozenset[str] | None:
    """Parse the signed authorization claims of a local JWT.

    Returns None for a legacy token (both claims absent). Raises ValueError for
    any partial or malformed claim; callers must fail closed.
    """
    has_version, has_scopes = "ocr_authz_version" in payload, "scopes" in payload
    if not has_version and not has_scopes:
        return None
    version, scopes = payload.get("ocr_authz_version"), payload.get("scopes")
    if not (has_version and has_scopes) or type(version) is not int or version != LOCAL_AUTHZ_VERSION:
        raise ValueError("invalid authorization claims")
    if not isinstance(scopes, list) or len(scopes) > len(LOCAL_ALLOWED_SCOPES):
        raise ValueError("invalid authorization claims")
    if any(type(item) is not str or item not in LOCAL_ALLOWED_SCOPES for item in scopes):
        raise ValueError("invalid authorization claims")
    if len(set(scopes)) != len(scopes):
        raise ValueError("invalid authorization claims")
    return frozenset(scopes)


def effective_local_scopes(payload: dict, current_role: object) -> frozenset[str]:
    """Legacy tokens get the legacy set; versioned ones the signed set limited by the current role."""
    signed = signed_local_scopes(payload)
    if signed is None:
        return LEGACY_SCOPES
    return signed & local_role_scopes(current_role)
