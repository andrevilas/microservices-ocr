"""Owner-scoped persistent CRUD for document contexts (service layer only; no routes)."""
import json
import re
import sqlite3
from contextlib import suppress
from datetime import datetime, timezone
from uuid import uuid4

from app.application import ApplicationError, Principal
from app.catalog_models import MAX_REVISION, ContextPage, ContextReceipt, ContextRecord, ContextSummary
from app.document_models import ContextCreate
from app.services.batch_service import request_identity
from app.services.storage_service import JobStore

MAX_CONTEXTS_PER_OWNER = 100
_ID = re.compile(r"[0-9a-f]{32}")


def _not_found() -> ApplicationError:
    return ApplicationError(404, "context_not_found", "Context not found.")


def _unavailable() -> ApplicationError:
    return ApplicationError(503, "storage_unavailable", "Context storage is unavailable.")


def _query_error() -> ApplicationError:
    return ApplicationError(422, "invalid_context_query", "Invalid context query parameters.")


def _snapshot(body: ContextCreate) -> ContextCreate:
    """Validate original Python types before dumping, then detach mutable values."""
    try:
        if not isinstance(body, ContextCreate):
            raise ValueError
        validated = ContextCreate.model_validate(body, strict=True)
        return ContextCreate.model_validate(
            validated.model_dump(mode="python", warnings="error"), strict=True)
    except Exception:
        pass
    raise ApplicationError(422, "invalid_context_body", "Context body is invalid.") from None


def _strict_int(value, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


class ContextService:
    def __init__(self, jobs: JobStore):
        self.jobs = jobs
        try:
            with jobs._lock, jobs._get_conn() as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS document_contexts (
                    context_id TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision>=1), created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, create_key_hash TEXT NOT NULL, create_request_hash TEXT NOT NULL,
                    UNIQUE(owner, create_key_hash))""")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_document_contexts_owner ON document_contexts(owner, context_id)")
        except sqlite3.Error:
            raise _unavailable() from None

    # -- shared connection helpers (callers never nest these: the store lock is not reentrant)
    def _read(self, work):
        try:
            with self.jobs._lock:
                return work(self.jobs._get_conn())
        except sqlite3.Error:
            raise _unavailable() from None

    def _write(self, work):
        with self.jobs._lock:
            conn = self.jobs._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = work(conn)
                conn.commit()
                return result
            except BaseException as error:
                with suppress(sqlite3.Error):
                    conn.rollback()
                if isinstance(error, sqlite3.Error):
                    raise _unavailable() from None
                raise

    @staticmethod
    def _persisted(parse):
        """Run a pure parser over stored data; bad persisted data is a storage fault, never echoed."""
        try:
            return parse()
        except (ValueError, TypeError):  # pydantic.ValidationError is a ValueError
            raise _unavailable() from None

    @staticmethod
    def _record(row) -> ContextRecord:
        """row = (context_id, revision, created_at, updated_at, payload)"""
        return ContextService._persisted(lambda: ContextRecord(
            context_id=row[0], revision=row[1], created_at=datetime.fromisoformat(row[2]),
            updated_at=datetime.fromisoformat(row[3]), body=ContextCreate.model_validate_json(row[4])))

    @staticmethod
    def _summary(row) -> ContextSummary:
        """row = (context_id, payload, revision, created_at, updated_at)"""
        return ContextService._persisted(lambda: ContextSummary(
            context_id=row[0], name=ContextCreate.model_validate_json(row[1]).name, revision=row[2],
            created_at=datetime.fromisoformat(row[3]), updated_at=datetime.fromisoformat(row[4])))

    @staticmethod
    def _receipt(context_id, created_at) -> ContextReceipt:
        return ContextService._persisted(lambda: ContextReceipt(
            context_id=context_id, revision=1, created_at=datetime.fromisoformat(created_at)))

    def create(self, principal: Principal, body: ContextCreate, key: str) -> tuple[ContextReceipt, bool]:
        principal.require("contexts:read")
        principal.require("contexts:write")
        snapshot = _snapshot(body)
        key_hash, request_hash = request_identity(key, {"operation": "context.create.v1", "body": snapshot.model_dump(mode="json")})
        payload = snapshot.model_dump_json()
        owner, context_id = principal.subject, uuid4().hex
        now = datetime.now(timezone.utc).isoformat()

        def work(conn):
            row = conn.execute("SELECT context_id, created_at, create_request_hash FROM document_contexts "
                               "WHERE owner=? AND create_key_hash=?", (owner, key_hash)).fetchone()
            if row:
                if row[2] != request_hash:
                    raise ApplicationError(409, "idempotency_conflict", "This key was used for a different context.")
                return self._receipt(row[0], row[1]), True
            if conn.execute("SELECT COUNT(*) FROM document_contexts WHERE owner=?", (owner,)).fetchone()[0] >= MAX_CONTEXTS_PER_OWNER:
                raise ApplicationError(429, "context_quota_exceeded", "The context limit for this owner was reached.")
            conn.execute("INSERT INTO document_contexts VALUES (?,?,?,?,?,?,?,?)",
                         (context_id, owner, payload, 1, now, now, key_hash, request_hash))
            return self._receipt(context_id, now), False

        return self._write(work)

    def get(self, principal: Principal, context_id: str) -> ContextRecord:
        principal.require("contexts:read")
        if not isinstance(context_id, str) or not _ID.fullmatch(context_id):
            raise _not_found()
        row = self._read(lambda conn: conn.execute(
            "SELECT context_id, revision, created_at, updated_at, payload FROM document_contexts "
            "WHERE context_id=? AND owner=?", (context_id, principal.subject)).fetchone())
        if row is None:
            raise _not_found()
        return self._record(row)

    def list(self, principal: Principal, *, limit: int = 25, after: str | None = None) -> ContextPage:
        principal.require("contexts:read")
        if not _strict_int(limit, 1, 100) or not (after is None or (isinstance(after, str) and _ID.fullmatch(after))):
            raise _query_error()
        rows = self._read(lambda conn: conn.execute(
            "SELECT context_id, payload, revision, created_at, updated_at FROM document_contexts "
            "WHERE owner=? AND context_id>? ORDER BY context_id ASC LIMIT ?",
            (principal.subject, after or "", limit + 1)).fetchall())
        items = [self._summary(r) for r in rows[:limit]]
        return ContextPage(items=items, next_cursor=items[-1].context_id if len(rows) > limit else None)

    def update(self, principal: Principal, context_id: str, body: ContextCreate, *, expected_revision: int) -> ContextRecord:
        principal.require("contexts:read")
        principal.require("contexts:write")
        snapshot = _snapshot(body)
        if not _strict_int(expected_revision, 1, MAX_REVISION):
            raise _query_error()
        if not isinstance(context_id, str) or not _ID.fullmatch(context_id):
            raise _not_found()
        owner, payload = principal.subject, snapshot.model_dump_json()
        now = datetime.now(timezone.utc).isoformat()

        def work(conn):
            row = conn.execute("SELECT revision, created_at FROM document_contexts WHERE context_id=? AND owner=?",
                               (context_id, owner)).fetchone()
            if row is None:
                raise _not_found()
            if type(row[0]) is not int:
                raise _unavailable()
            # Everything the response needs is validated before any write, so a bad row is never half-updated.
            created_at = self._persisted(lambda: datetime.fromisoformat(row[1]))
            if row[0] != expected_revision:
                raise ApplicationError(409, "context_revision_conflict", "The context was changed; read it again.")
            if row[0] >= MAX_REVISION:
                raise ApplicationError(409, "context_revision_limit", "The context reached its revision limit.")
            changed = conn.execute("UPDATE document_contexts SET payload=?, revision=revision+1, updated_at=? "
                                   "WHERE context_id=? AND owner=? AND revision=?",
                                   (payload, now, context_id, owner, expected_revision)).rowcount
            if changed != 1:
                raise ApplicationError(409, "context_revision_conflict", "The context was changed; read it again.")
            # Built before commit: a failure here rolls the UPDATE back.
            return ContextRecord(context_id=context_id, revision=expected_revision + 1, created_at=created_at,
                                 updated_at=datetime.fromisoformat(now), body=snapshot)

        return self._write(work)
