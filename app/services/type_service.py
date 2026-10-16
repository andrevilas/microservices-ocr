"""Owner-scoped persistent draft CRUD and immutable schema publication for document types (service layer only).

Precondition: ContextService(jobs) was already initialized by the caller, so document_contexts exists.
Published versions are append-only: document_type_versions only ever receives INSERT and SELECT.
"""
import hashlib
import json
import re
import sqlite3
from contextlib import suppress
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.application import ApplicationError, Principal
from app.document_models import SCHEMA_VERSION, ContextCreate, TypeDraft, compose_schema
from app.document_values import validate_values
from app.services.batch_service import request_identity
from app.services.storage_service import JobStore

MAX_TYPES_PER_OWNER = 100
MAX_VERSIONS_PER_OWNER = 100
MAX_VERSIONS_PER_TYPE = 20
MAX_REVISION = 2**31 - 1
_ID = re.compile(r"[0-9a-f]{32}")
_DTO = ConfigDict(extra="forbid", frozen=True)


class TypeReceipt(BaseModel):
    """Creation receipt only; it is not the current state of the type."""
    model_config = _DTO
    type_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    context_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    revision: int = Field(default=1, ge=1, le=1)
    created_at: datetime


class TypeSummary(BaseModel):
    model_config = _DTO
    type_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    context_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    name: str
    revision: int = Field(ge=1, le=MAX_REVISION)
    created_at: datetime
    updated_at: datetime


class TypeRecord(BaseModel):
    model_config = _DTO
    type_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    context_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    revision: int = Field(ge=1, le=MAX_REVISION)
    created_at: datetime
    updated_at: datetime
    body: TypeDraft


class TypePage(BaseModel):
    model_config = _DTO
    items: list[TypeSummary]
    next_cursor: str | None = None


class TypeVersionSummary(BaseModel):
    """Stable consumer reference: type_id + version + schema_sha256 (the hash covers only the canonical envelope)."""
    model_config = _DTO
    type_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    context_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    version: int = Field(ge=1)
    draft_revision: int = Field(ge=1, le=MAX_REVISION)
    context_revision: int = Field(ge=1, le=MAX_REVISION)
    type_name: str
    context_name: str
    schema_version: str
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class TypeVersionRecord(TypeVersionSummary):
    schema: dict  # decoded from the stored canonical bytes, never recomputed


class TypeVersionPage(BaseModel):
    model_config = _DTO
    items: list[TypeVersionSummary]
    next_cursor: int | None = None


def _type_not_found() -> ApplicationError:
    return ApplicationError(404, "type_not_found", "Type not found.")


def _context_not_found() -> ApplicationError:
    return ApplicationError(404, "context_not_found", "Context not found.")


def _unavailable() -> ApplicationError:
    return ApplicationError(503, "storage_unavailable", "Type storage is unavailable.")


def _query_error() -> ApplicationError:
    return ApplicationError(422, "invalid_type_query", "Invalid type query parameters.")


def _snapshot(body: TypeDraft) -> TypeDraft:
    """Revalidate from a plain dump: frozen models may hold lists mutated after construction."""
    try:
        return TypeDraft.model_validate(body.model_dump(mode="json", warnings="error"))
    except Exception:
        raise ApplicationError(422, "invalid_type_body", "Type body is invalid.") from None


def _strict_int(value, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _valid_id(value) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


# Every read joins the owning context with the same owner, so a type is never visible through ID alone.
_TYPE_ROW = ("SELECT t.type_id, t.context_id, t.revision, t.created_at, t.updated_at, t.payload "
             "FROM document_types t JOIN document_contexts c ON c.context_id=t.context_id AND c.owner=t.owner ")


_VERSION_COLUMNS = ("type_id, context_id, version, draft_revision, context_revision, type_name, context_name, "
                    "schema_version, schema_sha256, created_at")


class TypeService:
    def __init__(self, jobs: JobStore):
        self.jobs = jobs
        try:
            with jobs._lock, jobs._get_conn() as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS document_types (
                    type_id TEXT PRIMARY KEY, context_id TEXT NOT NULL, owner TEXT NOT NULL, payload TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision>=1), created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, create_key_hash TEXT NOT NULL, create_request_hash TEXT NOT NULL,
                    UNIQUE(owner, create_key_hash))""")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_document_types_owner_context "
                             "ON document_types(owner, context_id, type_id)")
                conn.execute("""CREATE TABLE IF NOT EXISTS document_type_versions (
                    owner TEXT NOT NULL, type_id TEXT NOT NULL, context_id TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(version>=1), draft_revision INTEGER NOT NULL,
                    context_revision INTEGER NOT NULL, type_name TEXT NOT NULL, context_name TEXT NOT NULL,
                    schema_version TEXT NOT NULL, canonical BLOB NOT NULL, schema_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL, key_hash TEXT NOT NULL, request_hash TEXT NOT NULL,
                    PRIMARY KEY(type_id, version), UNIQUE(owner, key_hash))""")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_document_type_versions_owner_type "
                             "ON document_type_versions(owner, type_id, version)")
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
    def _record(row) -> TypeRecord:
        try:
            return TypeRecord(type_id=row[0], context_id=row[1], revision=row[2],
                              created_at=datetime.fromisoformat(row[3]), updated_at=datetime.fromisoformat(row[4]),
                              body=TypeDraft.model_validate_json(row[5]))
        except Exception:
            raise _unavailable() from None  # corrupt persisted row; never echo its content

    def create(self, principal: Principal, context_id: str, body: TypeDraft, key: str) -> tuple[TypeReceipt, bool]:
        principal.require("contexts:read")
        principal.require("contexts:write")
        snapshot = _snapshot(body)
        if not _valid_id(context_id):
            raise _context_not_found()  # before the key is hashed, so nothing is consumed
        key_hash, request_hash = request_identity(key, {"operation": "type.create.v1", "context_id": context_id,
                                                        "body": snapshot.model_dump(mode="json")})
        payload = snapshot.model_dump_json()
        owner, type_id = principal.subject, uuid4().hex
        now = datetime.now(timezone.utc).isoformat()

        def work(conn):
            if conn.execute("SELECT 1 FROM document_contexts WHERE context_id=? AND owner=?",
                            (context_id, owner)).fetchone() is None:
                raise _context_not_found()
            row = conn.execute("SELECT type_id, context_id, created_at, create_request_hash FROM document_types "
                               "WHERE owner=? AND create_key_hash=?", (owner, key_hash)).fetchone()
            if row:
                if row[3] != request_hash:
                    raise ApplicationError(409, "idempotency_conflict", "This key was used for a different type.")
                return row[0], row[1], row[2], True
            if conn.execute("SELECT COUNT(*) FROM document_types WHERE owner=?", (owner,)).fetchone()[0] >= MAX_TYPES_PER_OWNER:
                raise ApplicationError(429, "type_quota_exceeded", "The type limit for this owner was reached.")
            conn.execute("INSERT INTO document_types (type_id, context_id, owner, payload, revision, created_at, "
                         "updated_at, create_key_hash, create_request_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                         (type_id, context_id, owner, payload, 1, now, now, key_hash, request_hash))
            return type_id, context_id, now, False

        found_id, found_context, created_at, replayed = self._write(work)
        return TypeReceipt(type_id=found_id, context_id=found_context, revision=1,
                           created_at=datetime.fromisoformat(created_at)), replayed

    def get(self, principal: Principal, type_id: str) -> TypeRecord:
        principal.require("contexts:read")
        if not _valid_id(type_id):
            raise _type_not_found()
        row = self._read(lambda conn: conn.execute(
            _TYPE_ROW + "WHERE t.type_id=? AND t.owner=?", (type_id, principal.subject)).fetchone())
        if row is None:
            raise _type_not_found()
        return self._record(row)

    def list(self, principal: Principal, context_id: str, *, limit: int = 25, after: str | None = None) -> TypePage:
        principal.require("contexts:read")
        if not _strict_int(limit, 1, 100) or not (after is None or _valid_id(after)):
            raise _query_error()
        if not _valid_id(context_id):
            raise _context_not_found()
        owner = principal.subject

        def work(conn):
            if conn.execute("SELECT 1 FROM document_contexts WHERE context_id=? AND owner=?",
                            (context_id, owner)).fetchone() is None:
                raise _context_not_found()
            return conn.execute(
                "SELECT t.type_id, t.context_id, t.payload, t.revision, t.created_at, t.updated_at "
                "FROM document_types t JOIN document_contexts c ON c.context_id=t.context_id AND c.owner=t.owner "
                "WHERE t.owner=? AND t.context_id=? AND t.type_id>? ORDER BY t.type_id ASC LIMIT ?",
                (owner, context_id, after or "", limit + 1)).fetchall()

        rows = self._read(work)
        items = []
        for r in rows[:limit]:
            record = self._record((r[0], r[1], r[3], r[4], r[5], r[2]))
            items.append(TypeSummary(type_id=record.type_id, context_id=record.context_id, name=record.body.name,
                                     revision=record.revision, created_at=record.created_at,
                                     updated_at=record.updated_at))
        return TypePage(items=items, next_cursor=items[-1].type_id if len(rows) > limit else None)

    def update(self, principal: Principal, type_id: str, body: TypeDraft, *, expected_revision: int) -> TypeRecord:
        principal.require("contexts:read")
        principal.require("contexts:write")
        snapshot = _snapshot(body)
        if not _strict_int(expected_revision, 1, MAX_REVISION):
            raise _query_error()
        if not _valid_id(type_id):
            raise _type_not_found()
        owner, payload = principal.subject, snapshot.model_dump_json()
        now = datetime.now(timezone.utc).isoformat()

        def work(conn):
            row = conn.execute(_TYPE_ROW + "WHERE t.type_id=? AND t.owner=?", (type_id, owner)).fetchone()
            if row is None:
                raise _type_not_found()
            if row[2] != expected_revision:
                raise ApplicationError(409, "type_revision_conflict", "The type was changed; read it again.")
            if row[2] >= MAX_REVISION:
                raise ApplicationError(409, "type_revision_limit", "The type reached its revision limit.")
            changed = conn.execute("UPDATE document_types SET payload=?, revision=revision+1, updated_at=? "
                                   "WHERE type_id=? AND owner=? AND revision=?",
                                   (payload, now, type_id, owner, expected_revision)).rowcount
            if changed != 1:
                raise ApplicationError(409, "type_revision_conflict", "The type was changed; read it again.")
            return row[1], row[3]

        context_id, created_at = self._write(work)
        return TypeRecord(type_id=type_id, context_id=context_id, revision=expected_revision + 1,
                          created_at=datetime.fromisoformat(created_at), updated_at=datetime.fromisoformat(now),
                          body=snapshot)

    # -- immutable publication
    @staticmethod
    def _summary(row) -> TypeVersionSummary:
        try:
            return TypeVersionSummary(type_id=row[0], context_id=row[1], version=row[2], draft_revision=row[3],
                                      context_revision=row[4], type_name=row[5], context_name=row[6],
                                      schema_version=row[7], schema_sha256=row[8],
                                      created_at=datetime.fromisoformat(row[9]))
        except Exception:
            raise _unavailable() from None  # corrupt persisted row; never echo its content

    def publish(self, principal: Principal, type_id: str, *, expected_revision: int,
                expected_context_revision: int, key: str) -> tuple[TypeVersionSummary, bool]:
        principal.require("contexts:read")
        principal.require("contexts:publish")
        if not (_strict_int(expected_revision, 1, MAX_REVISION)
                and _strict_int(expected_context_revision, 1, MAX_REVISION)):
            raise _query_error()
        if not _valid_id(type_id):
            raise _type_not_found()
        key_hash, request_hash = request_identity(key, {
            "operation": "type.publish.v1", "type_id": type_id, "expected_revision": expected_revision,
            "expected_context_revision": expected_context_revision})
        owner = principal.subject
        now = datetime.now(timezone.utc).isoformat()

        def work(conn):
            # Replay first: it must survive later edits, a full quota and revision changes.
            row = conn.execute("SELECT " + _VERSION_COLUMNS + ", request_hash FROM document_type_versions "
                               "WHERE owner=? AND key_hash=?", (owner, key_hash)).fetchone()
            if row:
                if row[10] != request_hash:
                    raise ApplicationError(409, "idempotency_conflict", "This key was used for a different publication.")
                return self._summary(row), True
            found = conn.execute(
                "SELECT t.context_id, t.revision, t.payload, c.revision, c.payload FROM document_types t "
                "JOIN document_contexts c ON c.context_id=t.context_id AND c.owner=t.owner "
                "WHERE t.type_id=? AND t.owner=?", (type_id, owner)).fetchone()
            if found is None:
                raise _type_not_found()
            if found[1] != expected_revision:
                raise ApplicationError(409, "type_revision_conflict", "The type was changed; read it again.")
            if found[3] != expected_context_revision:
                raise ApplicationError(409, "context_revision_conflict", "The context was changed; read it again.")
            try:
                draft = TypeDraft.model_validate_json(found[2])
                context = ContextCreate.model_validate_json(found[4])
            except Exception:
                raise _unavailable() from None  # corrupt persisted content
            try:
                composed = compose_schema(context.common_fields, draft.specific_fields)
                validate_values([*context.common_fields, *draft.specific_fields], {})
            except ValueError:
                raise ApplicationError(422, "invalid_type_schema", "The type schema is invalid.") from None
            counts = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(type_id=?), 0), COALESCE(MAX(CASE WHEN type_id=? THEN version END), 0) "
                "FROM document_type_versions WHERE owner=?", (type_id, type_id, owner)).fetchone()
            if counts[0] >= MAX_VERSIONS_PER_OWNER or counts[1] >= MAX_VERSIONS_PER_TYPE:
                raise ApplicationError(429, "type_version_quota_exceeded", "The published version limit was reached.")
            version = counts[2] + 1
            conn.execute("INSERT INTO document_type_versions (owner, type_id, context_id, version, draft_revision, "
                         "context_revision, type_name, context_name, schema_version, canonical, schema_sha256, "
                         "created_at, key_hash, request_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (owner, type_id, found[0], version, found[1], found[3], draft.name, context.name,
                          SCHEMA_VERSION, composed.canonical, composed.sha256, now, key_hash, request_hash))
            return TypeVersionSummary(type_id=type_id, context_id=found[0], version=version,
                                      draft_revision=found[1], context_revision=found[3], type_name=draft.name,
                                      context_name=context.name, schema_version=SCHEMA_VERSION,
                                      schema_sha256=composed.sha256, created_at=datetime.fromisoformat(now)), False

        return self._write(work)

    def get_version(self, principal: Principal, type_id: str, version: int) -> TypeVersionRecord:
        principal.require("contexts:read")
        if not _strict_int(version, 1, 2**63 - 1):
            raise _query_error()
        if not _valid_id(type_id):
            raise _type_not_found()
        row = self._read(lambda conn: conn.execute(
            "SELECT " + _VERSION_COLUMNS + ", canonical FROM document_type_versions "
            "WHERE owner=? AND type_id=? AND version=?", (principal.subject, type_id, version)).fetchone())
        if row is None:
            raise ApplicationError(404, "type_version_not_found", "Type version not found.")
        summary = self._summary(row)
        canonical = row[10]
        try:
            if not isinstance(canonical, bytes) or hashlib.sha256(canonical).hexdigest() != summary.schema_sha256:
                raise ValueError
            schema = json.loads(canonical)
            if (not isinstance(schema, dict) or schema.get("schema_version") != summary.schema_version
                    or summary.schema_version != SCHEMA_VERSION
                    or not all(isinstance(schema.get(k), list) for k in ("common_fields", "specific_fields", "fields"))):
                raise ValueError
        except Exception:
            raise _unavailable() from None  # integrity failure; never echo stored content
        return TypeVersionRecord(**summary.model_dump(), schema=schema)

    def list_versions(self, principal: Principal, type_id: str, *, limit: int = 25,
                      after: int | None = None) -> TypeVersionPage:
        principal.require("contexts:read")
        if not _strict_int(limit, 1, 100) or not (after is None or _strict_int(after, 1, 2**63 - 1)):
            raise _query_error()
        if not _valid_id(type_id):
            raise _type_not_found()
        owner = principal.subject

        def work(conn):
            if conn.execute("SELECT 1 FROM document_types WHERE type_id=? AND owner=?", (type_id, owner)).fetchone() is None:
                raise _type_not_found()
            # Summary columns only: canonical BLOBs are never loaded for listings.
            return conn.execute("SELECT " + _VERSION_COLUMNS + " FROM document_type_versions "
                                "WHERE owner=? AND type_id=? AND version>? ORDER BY version ASC LIMIT ?",
                                (owner, type_id, after or 0, limit + 1)).fetchall()

        rows = self._read(work)
        items = [self._summary(r) for r in rows[:limit]]
        return TypeVersionPage(items=items, next_cursor=items[-1].version if len(rows) > limit else None)
