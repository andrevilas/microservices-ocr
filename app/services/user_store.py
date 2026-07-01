"""SQLite-backed user store for authentication and user management."""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
from threading import Lock

from app.config import settings

logger = logging.getLogger(__name__)

_CREATE_USERS_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    created_at TEXT NOT NULL
)
"""


def _hash_password(password: str, salt: str) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256 with the given hex salt."""
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        bytes.fromhex(salt),
        100_000,
    ).hex()


def _generate_salt() -> str:
    """Generate a random 32-byte salt as a hex string."""
    return os.urandom(32).hex()


class UserStore:
    """Thread-safe SQLite user store in the same data volume as JobStore."""

    def __init__(self) -> None:
        self._db_path = settings.user_db_path or (settings.ocr_tmp_dir / "users.sqlite3")
        self._lock = Lock()
        self._conn: sqlite3.Connection | None = None
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(
                str(self._db_path), check_same_thread=False, timeout=10
            )
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.execute(_CREATE_USERS_TABLE_SQL)
        conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_user(
        self,
        name: str,
        email: str,
        password: str,
        role: str = "user",
    ) -> dict:
        """Create a new user and return the user dict (without password_hash/salt)."""
        salt = _generate_salt()
        password_hash = _hash_password(password, salt)

        from datetime import datetime, timezone

        created_at = datetime.now(timezone.utc).isoformat()

        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    "INSERT INTO users (name, email, password_hash, salt, role, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (name, email, password_hash, salt, role, created_at),
                )
                conn.commit()
                user_id = conn.execute(
                    "SELECT last_insert_rowid()"
                ).fetchone()[0]
            except sqlite3.IntegrityError:
                raise ValueError(f"Email already exists: {email}")

        return {
            "id": user_id,
            "name": name,
            "email": email,
            "role": role,
            "created_at": created_at,
        }

    def get_user_by_email(self, email: str) -> dict | None:
        """Return the full user dict (including password_hash and salt) or None."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT id, name, email, password_hash, salt, role, created_at "
                "FROM users WHERE email = ?",
                (email,),
            ).fetchone()

        if row is None:
            return None

        return {
            "id": row["id"],
            "name": row["name"],
            "email": row["email"],
            "password_hash": row["password_hash"],
            "salt": row["salt"],
            "role": row["role"],
            "created_at": row["created_at"],
        }

    def get_user_by_id(self, user_id: int) -> dict | None:
        """Return the user dict (without password_hash/salt) or None."""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT id, name, email, role, created_at "
                "FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()

        if row is None:
            return None

        return {
            "id": row["id"],
            "name": row["name"],
            "email": row["email"],
            "role": row["role"],
            "created_at": row["created_at"],
        }

    def list_users(self) -> list[dict]:
        """Return a list of all users (without password_hash/salt)."""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute(
                "SELECT id, name, email, role, created_at FROM users ORDER BY id"
            ).fetchall()

        return [
            {
                "id": row["id"],
                "name": row["name"],
                "email": row["email"],
                "role": row["role"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    @staticmethod
    def verify_password(stored_hash: str, salt: str, password: str) -> bool:
        """Verify a password against a stored hash and salt."""
        candidate = _hash_password(password, salt)
        return candidate == stored_hash

    def ensure_admin(self, name: str, email: str, password: str) -> None:
        """Create the admin user if no admin exists yet. Only one admin allowed."""
        with self._lock:
            conn = self._get_conn()
            existing = conn.execute(
                "SELECT id FROM users WHERE role = 'admin'"
            ).fetchone()

        if existing is not None:
            logger.debug("Admin user already exists (id=%s), skipping creation.", existing["id"])
            return

        try:
            self.create_user(name=name, email=email, password=password, role="admin")
            logger.info("Admin user created: %s <%s>", name, email)
        except ValueError:
            logger.warning("Admin user email %s already taken, skipping.", email)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_user_store: UserStore | None = None


def get_user_store() -> UserStore:
    """Return the singleton UserStore instance (lazily created)."""
    global _user_store
    if _user_store is None:
        _user_store = UserStore()
    return _user_store
