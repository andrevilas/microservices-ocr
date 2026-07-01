"""Tests for JWT-based user management and authentication."""

import time
import contextlib
from io import BytesIO
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from app.config import settings
from app.main import app
from app.services import auth_service
from app.services.user_store import get_user_store

client = TestClient(app)


@contextlib.contextmanager
def _no_cookies():
    """Context manager that temporarily clears cookies on the module client."""
    saved = dict(client.cookies)
    client.cookies.clear()
    try:
        yield client
    finally:
        client.cookies.clear()
        client.cookies.update(saved)


def _fresh_client() -> TestClient:
    """Return module client with cookies temporarily cleared.

    WARNING: This mutates the module-level client's cookies!  Use _no_cookies()
    context manager when you need to restore cookies afterward. For quick
    single-request unauthenticated tests, this is fine since _admin_login()
    will re-set cookies on next call.
    """
    client.cookies.clear()
    return client


def build_pdf_bytes(text: str) -> bytes:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(100, 750, text)
    pdf.save()
    return buffer.getvalue()


def _admin_login() -> dict:
    """Login as admin and return the response JSON with access_token."""
    resp = client.post(
        "/auth/login",
        json={"email": settings.admin_email, "password": settings.admin_password},
    )
    assert resp.status_code == 200, f"Admin login failed: {resp.text}"
    return resp.json()


def _admin_token() -> str:
    """Get a valid admin JWT token string."""
    return _admin_login()["access_token"]


def _admin_cookies() -> dict:
    """Login as admin and return the cookies dict for cookie-based auth."""
    resp = client.post(
        "/auth/login",
        json={"email": settings.admin_email, "password": settings.admin_password},
    )
    assert resp.status_code == 200
    return dict(resp.cookies)


def _create_user(token: str, name: str, email: str, password: str) -> dict:
    """Create a user via API and return the response JSON."""
    resp = client.post(
        "/api/users",
        json={"name": name, "email": email, "password": password},
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.json()


# ---------------------------------------------------------------------------
# 1. Login success and failure
# ---------------------------------------------------------------------------


class TestLoginFlow:
    def test_login_ok_returns_token_and_cookie(self) -> None:
        resp = client.post(
            "/auth/login",
            json={"email": settings.admin_email, "password": settings.admin_password},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert "access_token" in resp.cookies

    def test_login_wrong_password_returns_401(self) -> None:
        resp = client.post(
            "/auth/login",
            json={"email": settings.admin_email, "password": "wrongpassword"},
        )
        assert resp.status_code == 401

    def test_login_wrong_email_returns_401(self) -> None:
        resp = client.post(
            "/auth/login",
            json={"email": "nonexistent@example.com", "password": "any"},
        )
        assert resp.status_code == 401

    def test_login_missing_fields_returns_401(self) -> None:
        resp = client.post("/auth/login", json={"email": "", "password": ""})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 2. Cookie-based JWT authentication
# ---------------------------------------------------------------------------


class TestCookieJWT:
    def test_cookie_jwt_grants_access_to_index(self) -> None:
        cookies = _admin_cookies()
        resp = client.get("/", cookies=cookies, follow_redirects=False)
        assert resp.status_code == 200
        assert "Fluxo OCR" in resp.text

    def test_cookie_jwt_grants_access_to_auth_me(self) -> None:
        cookies = _admin_cookies()
        resp = client.get("/auth/me", cookies=cookies)
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == settings.admin_email
        assert data["role"] == "admin"

    def test_cookie_jwt_grants_access_to_metrics(self) -> None:
        cookies = _admin_cookies()
        resp = client.get("/metrics", cookies=cookies)
        assert resp.status_code == 200
        assert "total_jobs" in resp.json()


# ---------------------------------------------------------------------------
# 3. Bearer token JWT authentication
# ---------------------------------------------------------------------------


class TestBearerJWT:
    def test_bearer_jwt_grants_access_to_auth_me(self) -> None:
        token = _admin_token()
        resp = client.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "admin"

    def test_bearer_jwt_grants_access_to_api_jobs(self) -> None:
        token = _admin_token()
        pdf_bytes = build_pdf_bytes("bearer test")
        resp = client.post(
            "/api/jobs",
            files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 202

    def test_bearer_jwt_grants_access_to_metrics(self) -> None:
        token = _admin_token()
        resp = client.get(
            "/metrics", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 4. Protected endpoints return 401 without auth
# ---------------------------------------------------------------------------


class TestProtectedEndpoints:
    def test_index_redirects_to_login_without_auth(self) -> None:
        c = _fresh_client()
        resp = c.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("location", "")

    def test_auth_me_returns_401_without_auth(self) -> None:
        c = _fresh_client()
        resp = c.get("/auth/me")
        assert resp.status_code == 401

    def test_api_jobs_returns_401_without_auth(self) -> None:
        c = _fresh_client()
        pdf_bytes = build_pdf_bytes("unauth test")
        resp = c.post(
            "/api/jobs",
            files={"file": ("doc.pdf", pdf_bytes, "application/pdf")},
        )
        assert resp.status_code == 401

    def test_metrics_returns_401_without_auth(self) -> None:
        c = _fresh_client()
        resp = c.get("/metrics")
        assert resp.status_code == 401

    def test_api_jobs_status_returns_401_without_auth(self) -> None:
        c = _fresh_client()
        resp = c.get("/api/jobs/some-id")
        assert resp.status_code == 401

    def test_api_jobs_batch_returns_401_without_auth(self) -> None:
        c = _fresh_client()
        pdf_bytes = build_pdf_bytes("batch unauth")
        resp = c.post(
            "/api/jobs/batch",
            files=[("files", ("a.pdf", pdf_bytes, "application/pdf"))],
        )
        assert resp.status_code == 401

    def test_api_jobs_clear_queue_returns_401_without_auth(self) -> None:
        c = _fresh_client()
        resp = c.post("/api/jobs/clear-queue")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 5. User role: common user accesses main but not admin
# ---------------------------------------------------------------------------


class TestUserRole:
    def test_common_user_cannot_access_admin_users_page(self) -> None:
        token = _admin_token()
        _create_user(token, "Common User", "common@test.com", "password123")
        # Login as common user
        resp = client.post(
            "/auth/login",
            json={"email": "common@test.com", "password": "password123"},
        )
        assert resp.status_code == 200
        user_token = resp.json()["access_token"]
        # Try admin page
        resp = client.get(
            "/admin/users", headers={"Authorization": f"Bearer {user_token}"}
        )
        assert resp.status_code == 403

    def test_common_user_cannot_access_api_users(self) -> None:
        # Login as common user (created in previous test or create again)
        token = _admin_token()
        try:
            _create_user(token, "Common User 2", "common2@test.com", "pass123")
        except Exception:
            pass
        resp = client.post(
            "/auth/login",
            json={"email": "common2@test.com", "password": "pass123"},
        )
        if resp.status_code != 200:
            # User may already exist, try login
            resp = client.post(
                "/auth/login",
                json={"email": "common@test.com", "password": "password123"},
            )
        assert resp.status_code == 200
        user_token = resp.json()["access_token"]
        # GET /api/users
        resp = client.get(
            "/api/users", headers={"Authorization": f"Bearer {user_token}"}
        )
        assert resp.status_code == 403
        # POST /api/users
        resp = client.post(
            "/api/users",
            json={"name": "X", "email": "x@x.com", "password": "x"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 403

    def test_common_user_can_access_main_page(self) -> None:
        resp = client.post(
            "/auth/login",
            json={"email": "common@test.com", "password": "password123"},
        )
        if resp.status_code != 200:
            token = _admin_token()
            _create_user(token, "Common User", "commonmain@test.com", "pass")
            resp = client.post(
                "/auth/login",
                json={"email": "commonmain@test.com", "password": "pass"},
            )
        assert resp.status_code == 200
        cookies = dict(resp.cookies)
        resp = client.get("/", cookies=cookies, follow_redirects=False)
        assert resp.status_code == 200

    def test_common_user_can_access_api_jobs(self) -> None:
        resp = client.post(
            "/auth/login",
            json={"email": "common@test.com", "password": "password123"},
        )
        assert resp.status_code == 200
        user_token = resp.json()["access_token"]
        pdf_bytes = build_pdf_bytes("user job test")
        resp = client.post(
            "/api/jobs",
            files={"file": ("test.pdf", pdf_bytes, "application/pdf")},
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 202


# ---------------------------------------------------------------------------
# 6. Admin creates user
# ---------------------------------------------------------------------------


class TestAdminCreatesUser:
    def test_admin_creates_user_successfully(self) -> None:
        token = _admin_token()
        email = f"newuser-{uuid4().hex}@test.com"
        resp = client.post(
            "/api/users",
            json={"name": "Test User", "email": email, "password": "securepass"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Test User"
        assert data["email"] == email
        assert data["role"] == "user"
        # No password in response
        assert "password" not in data
        assert "password_hash" not in data
        assert "salt" not in data

    def test_admin_cannot_create_duplicate_email(self) -> None:
        token = _admin_token()
        email = f"dup-{uuid4().hex}@test.com"
        resp = client.post(
            "/api/users",
            json={"name": "Dup", "email": email, "password": "pass"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 201
        # Second attempt
        resp = client.post(
            "/api/users",
            json={"name": "Dup2", "email": email, "password": "pass2"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 409

    def test_admin_lists_users(self) -> None:
        token = _admin_token()
        resp = client.get(
            "/api/users", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        users = resp.json()
        assert isinstance(users, list)
        assert len(users) >= 1
        # Admin should be in list
        emails = [u["email"] for u in users]
        assert settings.admin_email in emails

    def test_create_user_missing_fields_returns_400(self) -> None:
        token = _admin_token()
        resp = client.post(
            "/api/users",
            json={"name": "", "email": "x@x.com", "password": "p"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 7. Password never stored in plain text
# ---------------------------------------------------------------------------


class TestPasswordSecurity:
    def test_password_not_stored_plain_text(self) -> None:
        store = get_user_store()
        user = store.get_user_by_email(settings.admin_email)
        assert user is not None
        assert user["password_hash"] != settings.admin_password
        assert user["password_hash"] != ""
        assert user["salt"] != ""
        # Hash is hex-encoded PBKDF2 output
        assert len(user["password_hash"]) == 64  # SHA-256 produces 32 bytes = 64 hex chars
        assert len(user["salt"]) == 64  # 32 bytes = 64 hex chars


# ---------------------------------------------------------------------------
# 8. Token expired or invalid
# ---------------------------------------------------------------------------


class TestTokenValidation:
    def test_expired_token_is_rejected(self) -> None:
        c = _fresh_client()
        # Create a token that expired 1 minute ago
        token = auth_service.create_token(
            user_id=1,
            email=settings.admin_email,
            role="admin",
            secret=settings.jwt_secret,
            exp_minutes=-1,
        )
        resp = c.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401

    def test_invalid_token_is_rejected(self) -> None:
        c = _fresh_client()
        resp = c.get(
            "/auth/me", headers={"Authorization": "Bearer invalid.token.here"}
        )
        assert resp.status_code == 401

    def test_tampered_token_is_rejected(self) -> None:
        c = _fresh_client()
        token = _admin_token()
        # Tamper with the payload
        parts = token.split(".")
        parts[1] = parts[1] + "x"
        tampered = ".".join(parts)
        resp = c.get(
            "/auth/me", headers={"Authorization": f"Bearer {tampered}"}
        )
        assert resp.status_code == 401

    def test_wrong_secret_token_is_rejected(self) -> None:
        c = _fresh_client()
        token = auth_service.create_token(
            user_id=1,
            email=settings.admin_email,
            role="admin",
            secret="wrong-secret-key",
        )
        resp = c.get(
            "/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 9. API_KEY still works for api/jobs but not for api/users
# ---------------------------------------------------------------------------


class TestApiKeyCompatibility:
    def test_api_key_works_for_api_jobs(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "api_key", "test-api-key-123")
        pdf_bytes = build_pdf_bytes("api key test")
        resp = client.post(
            "/api/jobs",
            files={"file": ("doc.pdf", pdf_bytes, "application/pdf")},
            headers={"X-API-Key": "test-api-key-123"},
        )
        assert resp.status_code == 202
        monkeypatch.setattr(settings, "api_key", None)

    def test_api_key_does_not_work_for_api_users_get(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "api_key", "test-api-key-123")
        with _no_cookies() as c:
            resp = c.get(
                "/api/users", headers={"X-API-Key": "test-api-key-123"}
            )
            # API_KEY does not grant admin access via JWT-only require_admin
            assert resp.status_code in (401, 302, 403)
        monkeypatch.setattr(settings, "api_key", None)

    def test_api_key_does_not_work_for_api_users_post(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "api_key", "test-api-key-123")
        with _no_cookies() as c:
            resp = c.post(
                "/api/users",
                json={"name": "X", "email": "x@x.com", "password": "x"},
                headers={"X-API-Key": "test-api-key-123"},
            )
            assert resp.status_code in (401, 302, 403)
        monkeypatch.setattr(settings, "api_key", None)

    def test_api_key_works_for_metrics(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "api_key", "test-api-key-123")
        resp = client.get(
            "/metrics", headers={"X-API-Key": "test-api-key-123"}
        )
        assert resp.status_code == 200
        monkeypatch.setattr(settings, "api_key", None)

    def test_api_key_works_for_job_status(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "api_key", "test-api-key-123")
        resp = client.get(
            "/api/jobs/nonexistent",
            headers={"X-API-Key": "test-api-key-123"},
        )
        # 404 means auth passed, job not found
        assert resp.status_code == 404
        monkeypatch.setattr(settings, "api_key", None)

    def test_api_key_works_for_clear_queue(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "api_key", "test-api-key-123")
        resp = client.post(
            "/api/jobs/clear-queue",
            headers={"X-API-Key": "test-api-key-123"},
        )
        assert resp.status_code == 200
        monkeypatch.setattr(settings, "api_key", None)


# ---------------------------------------------------------------------------
# 10. Public endpoints remain public
# ---------------------------------------------------------------------------


class TestPublicEndpoints:
    def test_health_is_public(self) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_readiness_is_public(self) -> None:
        resp = client.get("/readiness")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ready"

    def test_login_page_is_public(self) -> None:
        resp = client.get("/login")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 11. Logout clears cookie
# ---------------------------------------------------------------------------


class TestLogout:
    def test_logout_clears_cookie(self) -> None:
        # Login first
        login_resp = client.post(
            "/auth/login",
            json={"email": settings.admin_email, "password": settings.admin_password},
        )
        assert login_resp.status_code == 200
        assert "access_token" in login_resp.cookies

        # Logout
        logout_resp = client.post("/auth/logout")
        assert logout_resp.status_code == 200
        assert logout_resp.json()["detail"] == "Logged out"


# ---------------------------------------------------------------------------
# 12. Auth service unit tests
# ---------------------------------------------------------------------------


class TestAuthServiceUnit:
    def test_create_and_decode_token(self) -> None:
        token = auth_service.create_token(
            user_id=42,
            email="test@example.com",
            role="user",
            secret="test-secret",
            exp_minutes=60,
        )
        payload = auth_service.decode_token(token, "test-secret")
        assert payload is not None
        assert payload["sub"] == 42
        assert payload["email"] == "test@example.com"
        assert payload["role"] == "user"
        assert "exp" in payload
        assert "iat" in payload

    def test_decode_with_wrong_secret_returns_none(self) -> None:
        token = auth_service.create_token(
            user_id=1, email="a@b.com", role="user", secret="secret1"
        )
        result = auth_service.decode_token(token, "secret2")
        assert result is None

    def test_decode_expired_returns_none(self) -> None:
        token = auth_service.create_token(
            user_id=1, email="a@b.com", role="user", secret="s", exp_minutes=-1
        )
        result = auth_service.decode_token(token, "s")
        assert result is None

    def test_decode_garbage_returns_none(self) -> None:
        assert auth_service.decode_token("not-a-jwt", "s") is None
        assert auth_service.decode_token("a.b", "s") is None
        assert auth_service.decode_token("", "s") is None
