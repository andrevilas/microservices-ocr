"""Shared test fixtures for authentication and test client setup."""

import pytest

from app.config import settings
from app.services.user_store import get_user_store


@pytest.fixture(autouse=True, scope="session")
def _ensure_admin_user():
    """Ensure the admin user exists before any tests run.

    The lifespan function creates the admin, but TestClient at module level
    may not trigger the lifespan in all Starlette versions. This fixture
    guarantees the admin is available for all test sessions.
    """
    store = get_user_store()
    store.ensure_admin(
        name=settings.admin_name,
        email=settings.admin_email,
        password=settings.admin_password,
    )
