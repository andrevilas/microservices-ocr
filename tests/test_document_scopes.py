"""Local JWT scopes and provenance: legacy preservation, versioned grants, fail-closed claims."""
import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import security
from app.application import ApplicationError, Principal
from app.config import settings
from app.routes.auth import auth_router
from app.scopes import DOCUMENT_SCOPES, LEGACY_SCOPES, LOCAL_ALLOWED_SCOPES, local_role_scopes
from app.services.auth_service import create_token, decode_token

SECRET = "synthetic-test-secret-not-real"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign(payload: dict, secret: str = SECRET) -> str:
    head = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps(payload).encode())
    sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{b64(sig)}"


def claims(user_id=7, role="user", **extra) -> dict:
    now = int(time.time())
    return {"sub": user_id, "email": "fake@example.test", "role": role, "exp": now + 600, "iat": now, **extra}


class FakeUsers:
    def __init__(self, role="user", user_id=7):
        self.role, self.user_id = role, user_id

    def get_user_by_id(self, user_id):
        return {"id": user_id, "role": self.role} if user_id == self.user_id else None


class FakeIdentities:
    def authenticate(self, token):
        if token == "ocr_svc_synthetic":
            return {"subject": "service:abc", "scopes": ["jobs:read", "jobs:submit"], "credential_id": "cred-1"}
        return None


@pytest.fixture
def env(monkeypatch):
    state = {"users": FakeUsers()}
    monkeypatch.setattr(settings, "jwt_secret", SECRET)
    monkeypatch.setattr(settings, "browser_public_origin", "")
    monkeypatch.setattr(security, "get_user_store", lambda: state["users"])
    monkeypatch.setattr(security, "get_identity_store", lambda: FakeIdentities())
    return state


def denied(token):
    with pytest.raises(ApplicationError) as error:
        security.authenticate_bearer(token)
    assert error.value.status == 401
    assert "scope" not in str(error.value.args).lower() and "ocr_authz" not in str(error.value.args)


def test_policy_sets():
    assert len(LEGACY_SCOPES) == 5 and len(DOCUMENT_SCOPES) == 6 and len(LOCAL_ALLOWED_SCOPES) == 11
    assert local_role_scopes("user") == local_role_scopes("admin") == LOCAL_ALLOWED_SCOPES
    for role in ("viewer", "", None, "ADMIN"):
        assert local_role_scopes(role) == LEGACY_SCOPES


@pytest.mark.parametrize("role", ["user", "admin"])
def test_create_token_claims(role):
    token = create_token(7, "fake@example.test", role, SECRET, exp_minutes=5)
    payload = decode_token(token, SECRET)
    assert payload is not None and decode_token(token, "other-secret") is None
    assert type(payload["ocr_authz_version"]) is int and payload["ocr_authz_version"] == 1
    assert payload["scopes"] == sorted(LOCAL_ALLOWED_SCOPES) and len(payload["scopes"]) == 11
    assert (payload["sub"], payload["email"], payload["role"]) == (7, "fake@example.test", role)
    assert 0 < payload["exp"] - payload["iat"] <= 300


def test_legacy_token_keeps_five_scopes(env):
    payload = claims()
    assert "scopes" not in payload and "ocr_authz_version" not in payload
    principal = security.authenticate_bearer(sign(payload))
    assert principal.scopes == LEGACY_SCOPES
    assert principal.auth_source == "local_jwt" and principal.subject == "user:7" and principal.user_id == 7


@pytest.mark.parametrize("role", ["user", "admin"])
def test_new_token_gets_eleven_scopes(env, role):
    env["users"] = FakeUsers(role)
    principal = security.authenticate_bearer(create_token(7, "fake@example.test", role, SECRET))
    assert principal.scopes == LOCAL_ALLOWED_SCOPES
    assert principal.auth_source == "local_jwt" and principal.subject == "user:7" and principal.user_id == 7
    assert principal.is_admin is (role == "admin")


@pytest.mark.parametrize("signed", [[], ["contexts:read"], ["jobs:read", "metadata:read"]])
def test_reduced_token_is_not_widened(env, signed):
    token = sign(claims(ocr_authz_version=1, scopes=signed))
    assert security.authenticate_bearer(token).scopes == frozenset(signed)


def test_signed_role_does_not_grant_admin(env):
    principal = security.authenticate_bearer(create_token(7, "fake@example.test", "admin", SECRET))
    assert principal.is_admin is False and principal.scopes == LOCAL_ALLOWED_SCOPES


def test_unknown_current_role_drops_document_scopes(env):
    env["users"] = FakeUsers("viewer")
    principal = security.authenticate_bearer(create_token(7, "fake@example.test", "user", SECRET))
    assert principal.scopes == LEGACY_SCOPES and principal.is_admin is False


def test_limited_token_stays_limited_for_permissive_role(env):
    env["users"] = FakeUsers("admin")
    token = sign(claims(role="admin", ocr_authz_version=1, scopes=["contexts:read"]))
    principal = security.authenticate_bearer(token)
    assert principal.scopes == frozenset({"contexts:read"}) and principal.is_admin is True


ALL = sorted(LOCAL_ALLOWED_SCOPES)
MALFORMED = [
    {"scopes": ["jobs:read"]},
    {"ocr_authz_version": 1},
    {"ocr_authz_version": True, "scopes": ALL},
    {"ocr_authz_version": None, "scopes": ALL},
    {"ocr_authz_version": "1", "scopes": ALL},
    {"ocr_authz_version": 1.0, "scopes": ALL},
    {"ocr_authz_version": 2, "scopes": ALL},
    {"ocr_authz_version": 1, "scopes": None},
    {"ocr_authz_version": 1, "scopes": "jobs:read"},
    {"ocr_authz_version": 1, "scopes": {"jobs:read": True}},
    {"ocr_authz_version": 1, "scopes": ["jobs:read", True]},
    {"ocr_authz_version": 1, "scopes": ["jobs:read", 1]},
    {"ocr_authz_version": 1, "scopes": ["jobs:read", None]},
    {"ocr_authz_version": 1, "scopes": ["jobs:read", "admin:all"]},
    {"ocr_authz_version": 1, "scopes": ["jobs:read", "jobs:read"]},
    {"ocr_authz_version": 1, "scopes": ALL + ["jobs:read"]},
]


@pytest.mark.parametrize("extra", MALFORMED)
def test_malformed_claims_fail_closed(env, extra):
    denied(sign(claims(**extra)))


def test_service_credential_unchanged(env):
    principal = security.authenticate_bearer("ocr_svc_synthetic")
    assert principal.kind == "service" and principal.auth_source == "service"
    assert principal.scopes == frozenset({"jobs:read", "jobs:submit"}) and principal.is_admin is False
    assert principal.credential_id == "cred-1"


def test_principal_compatibility():
    principal = Principal("user:1", 1, frozenset({"jobs:read"}), "user", None, False)
    assert principal.kind == "user" and principal.auth_source == "unknown"
    assert Principal(subject="x", user_id=None, scopes=frozenset()).auth_source == "unknown"


def test_browser_session_returns_same_legacy_token(env):
    app = FastAPI()
    app.include_router(auth_router)
    token = sign(claims())
    exp = decode_token(token, SECRET)["exp"]
    headers = {"Origin": "http://testserver", "X-OCR-Browser": "1"}
    client = TestClient(app, cookies={"access_token": token})
    response = client.post("/auth/browser-session", headers=headers)
    assert response.status_code == 200
    assert response.json()["access_token"] == token
    assert decode_token(response.json()["access_token"], SECRET)["exp"] == exp
    assert security.authenticate_bearer(response.json()["access_token"]).scopes == LEGACY_SCOPES
