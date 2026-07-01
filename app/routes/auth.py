"""Authentication and user management routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.services import auth_service
from app.services.user_store import get_user_store

logger = logging.getLogger(__name__)

auth_router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ---------------------------------------------------------------------------
# Helper: extract current user from request
# ---------------------------------------------------------------------------


def get_current_user(request: Request) -> dict | None:
    """Extract and verify the current user from cookie or Authorization header.

    Returns the user dict (from DB) or None if unauthenticated.
    """
    token: str | None = None

    # 1. Prefer explicit API credentials when present.
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]

    # 2. Fall back to the browser session cookie.
    if not token:
        token = request.cookies.get("access_token")

    if not token:
        return None

    payload = auth_service.decode_token(token, settings.jwt_secret)
    if payload is None:
        return None

    user_id = payload.get("sub")
    if user_id is None:
        return None

    store = get_user_store()
    return store.get_user_by_id(int(user_id))


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


def _accepts_html(request: Request) -> bool:
    """Check if the request prefers HTML responses."""
    accept = request.headers.get("Accept", "")
    return "text/html" in accept


async def require_auth(request: Request) -> dict:
    """Dependency that requires an authenticated user.

    Redirects to /login for browser requests, returns 401 for API calls.
    """
    user = get_current_user(request)
    if user is None:
        if _accepts_html(request):
            raise HTTPException(
                status_code=status.HTTP_302_FOUND,
                headers={"Location": "/login"},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
        )
    return user


async def require_admin(request: Request) -> dict:
    """Dependency that requires an authenticated admin user."""
    user = await require_auth(request)
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return user


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------


@auth_router.get("/login")
async def login_page(request: Request):
    """Render the login page."""
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"app_name": settings.app_name},
    )


@auth_router.post("/auth/login")
async def login(request: Request):
    """Authenticate user with email and password, set JWT cookie."""
    body = await request.json()
    email = body.get("email", "").strip()
    password = body.get("password", "")

    if not email or not password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email and password are required.",
        )

    store = get_user_store()
    user = store.get_user_by_email(email)

    if user is None or not store.verify_password(
        user["password_hash"], user["salt"], password
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )

    token = auth_service.create_token(
        user_id=user["id"],
        email=user["email"],
        role=user["role"],
        secret=settings.jwt_secret,
        exp_minutes=settings.jwt_exp_minutes,
    )

    response = JSONResponse(
        content={"access_token": token, "token_type": "bearer"}
    )
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=settings.jwt_exp_minutes * 60,
    )
    return response


@auth_router.post("/auth/logout")
async def logout():
    """Clear the access_token cookie."""
    response = JSONResponse(content={"detail": "Logged out"})
    response.delete_cookie(key="access_token", path="/")
    return response


@auth_router.get("/auth/me")
async def me(user: dict = Depends(require_auth)):
    """Return the current authenticated user info."""
    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "role": user["role"],
    }


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------


@auth_router.get("/admin/users")
async def users_page(request: Request, user: dict = Depends(require_admin)):
    """Render the user management page (admin only)."""
    return templates.TemplateResponse(
        request=request,
        name="users.html",
        context={"app_name": settings.app_name},
    )


@auth_router.get("/api/users")
async def list_users(user: dict = Depends(require_admin)):
    """Return JSON list of all users (admin JWT only)."""
    store = get_user_store()
    return store.list_users()


@auth_router.post("/api/users", status_code=status.HTTP_201_CREATED)
async def create_user(request: Request, user: dict = Depends(require_admin)):
    """Create a new user with role='user' (admin JWT only)."""
    body = await request.json()
    name = body.get("name", "").strip()
    email = body.get("email", "").strip()
    password = body.get("password", "")

    if not name or not email or not password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Fields name, email and password are required.",
        )

    store = get_user_store()
    try:
        new_user = store.create_user(name=name, email=email, password=password, role="user")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists.",
        )

    return new_user
