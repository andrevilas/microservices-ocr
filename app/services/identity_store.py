"""Individual service identities and one-way credentials for the local API/MCP pilot."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path
from threading import Lock
from uuid import uuid4

from app.config import settings
from app.scopes import SERVICE_SCOPES  # Re-exported: other modules import it from here.

TOKEN_PATTERN = re.compile(r"ocr_svc_([0-9a-f]{32})\.[A-Za-z0-9_-]{43}\Z")


class IdentityNotFound(Exception):
    pass


class IdentityStore:
    def __init__(self, path: Path | None = None):
        self.path = path or settings.ocr_tmp_dir / "identities.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS service_principals (
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                created_at REAL NOT NULL, created_by TEXT NOT NULL,
                disabled_at REAL
            );
            CREATE TABLE IF NOT EXISTS service_credentials (
                id TEXT PRIMARY KEY, principal_id TEXT NOT NULL REFERENCES service_principals(id),
                name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                scopes TEXT NOT NULL, created_at REAL NOT NULL, created_by TEXT NOT NULL,
                expires_at REAL NOT NULL, revoked_at REAL, last_used_at REAL
            );
            CREATE TABLE IF NOT EXISTS identity_audit (
                id INTEGER PRIMARY KEY, occurred_at REAL NOT NULL,
                actor TEXT NOT NULL, action TEXT NOT NULL, principal_id TEXT NOT NULL,
                credential_id TEXT
            );
        """)

    def close(self):
        with self._lock:
            self._conn.close()

    def _audit(self, actor: str, action: str, principal_id: str, credential_id: str | None = None):
        self._conn.execute("INSERT INTO identity_audit (occurred_at,actor,action,principal_id,credential_id) VALUES (?,?,?,?,?)",
                           (time.time(), actor, action, principal_id, credential_id))

    def _principal(self, principal_id: str):
        row = self._conn.execute("SELECT * FROM service_principals WHERE id=?", (principal_id,)).fetchone()
        if row is None:
            raise IdentityNotFound()
        return row

    @staticmethod
    def _credential(row):
        return {key: json.loads(row[key]) if key == "scopes" else row[key]
                for key in ("id", "principal_id", "name", "scopes", "created_at", "created_by", "expires_at", "revoked_at", "last_used_at")}

    @staticmethod
    def _valid_scopes(scopes) -> bool:
        """Non-empty list of distinct known service scopes; never raises."""
        return (isinstance(scopes, list) and 0 < len(scopes) <= len(SERVICE_SCOPES)
                and all(type(item) is str and item in SERVICE_SCOPES for item in scopes)
                and len(set(scopes)) == len(scopes))

    def create_service(self, name: str, actor: str) -> dict:
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError("Service name must contain 1 to 80 characters.")
        principal_id = "service:" + uuid4().hex
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO service_principals (id,name,created_at,created_by) VALUES (?,?,?,?)",
                               (principal_id, name.strip(), time.time(), actor))
            self._audit(actor, "service.created", principal_id)
            return dict(self._principal(principal_id))

    def list_services(self, after: str = "", limit: int = 100) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._conn.execute("SELECT * FROM service_principals WHERE id>? ORDER BY id LIMIT ?", (after, limit))]

    def issue_credential(self, principal_id: str, name: str, scopes: list[str], expires_in_seconds: int, actor: str) -> tuple[dict, str]:
        if not name.strip() or len(name) > 80:
            raise ValueError("Credential name must contain 1 to 80 characters.")
        if not self._valid_scopes(scopes):
            raise ValueError("Invalid or duplicate service scopes.")
        if isinstance(expires_in_seconds, bool) or not isinstance(expires_in_seconds, int) or not 60 <= expires_in_seconds <= 31536000:
            raise ValueError("Credential lifetime must be 60 to 31536000 seconds.")
        credential_id = uuid4().hex
        token = f"ocr_svc_{credential_id}.{secrets.token_urlsafe(32)}"
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            if self._principal(principal_id)["disabled_at"] is not None:
                raise ValueError("Service is disabled.")
            self._conn.execute("""INSERT INTO service_credentials
                (id,principal_id,name,token_hash,scopes,created_at,created_by,expires_at)
                VALUES (?,?,?,?,?,?,?,?)""", (credential_id, principal_id, name.strip(), digest, json.dumps(sorted(scopes)), now, actor, now + expires_in_seconds))
            self._audit(actor, "credential.issued", principal_id, credential_id)
            row = self._conn.execute("SELECT * FROM service_credentials WHERE id=?", (credential_id,)).fetchone()
            return self._credential(row), token

    def list_credentials(self, principal_id: str, after: str = "", limit: int = 100) -> list[dict]:
        with self._lock:
            self._principal(principal_id)
            rows = self._conn.execute("SELECT * FROM service_credentials WHERE principal_id=? AND id>? ORDER BY id LIMIT ?", (principal_id, after, limit))
            return [self._credential(row) for row in rows]

    def revoke_credential(self, principal_id: str, credential_id: str, actor: str) -> dict:
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("SELECT * FROM service_credentials WHERE principal_id=? AND id=?", (principal_id, credential_id)).fetchone()
            if row is None:
                raise IdentityNotFound()
            if row["revoked_at"] is None:
                self._conn.execute("UPDATE service_credentials SET revoked_at=? WHERE id=?", (time.time(), credential_id))
                self._audit(actor, "credential.revoked", principal_id, credential_id)
            return self._credential(self._conn.execute("SELECT * FROM service_credentials WHERE id=?", (credential_id,)).fetchone())

    def disable_service(self, principal_id: str, actor: str) -> dict:
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            if self._principal(principal_id)["disabled_at"] is None:
                self._conn.execute("UPDATE service_principals SET disabled_at=? WHERE id=?", (time.time(), principal_id))
                self._audit(actor, "service.disabled", principal_id)
            return dict(self._principal(principal_id))

    def authenticate(self, token: str) -> dict | None:
        match = TOKEN_PATTERN.fullmatch(token)
        if match is None:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._lock, self._conn:
            # Validate and update last-used atomically with revoke/disable transactions.
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute("""SELECT c.*, p.disabled_at FROM service_credentials c
                JOIN service_principals p ON p.id=c.principal_id WHERE c.id=?""", (match[1],)).fetchone()
            now = time.time()
            if row is None or not secrets.compare_digest(row["token_hash"], digest):
                return None
            if row["revoked_at"] is not None or row["disabled_at"] is not None or row["expires_at"] <= now:
                return None
            # Persisted scopes are re-validated so an invalid row can never authenticate.
            try:
                scopes = json.loads(row["scopes"])
            except (TypeError, ValueError, RecursionError):
                return None
            if not self._valid_scopes(scopes):
                return None
            self._conn.execute("UPDATE service_credentials SET last_used_at=? WHERE id=?", (now, row["id"]))
            return {"subject": row["principal_id"], "credential_id": row["id"], "scopes": scopes}


_identity_store: IdentityStore | None = None
_init_lock = Lock()


def get_identity_store() -> IdentityStore:
    global _identity_store
    with _init_lock:
        if _identity_store is None:
            _identity_store = IdentityStore()
        return _identity_store
