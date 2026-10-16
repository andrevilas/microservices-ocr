"""Internal SQLite metadata index and conservative capacity reservations.

This module neither reads nor writes snapshot files. A committed index alone is
not proof of snapshot durability. C3b must verify published files and bindings
before calling the private commit helper inside _transaction(). Cleanup callers
must verify absence of ALL pending files before releasing a cleanup reservation.

Future approval may consume a draft head's reserved slot and MiB. Residual files
must remain charged until physical reconciliation; no approval is implemented.
"""
from __future__ import annotations

import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import wraps
from uuid import uuid4

from app.application import ApplicationError
from app.metadata_models import MAX_SNAPSHOT_BYTES, SaveMetadataReceipt

MAX_REVISIONS_PER_JOB = 32
MAX_JOB_BYTES = 32 * 1024 * 1024
MAX_OWNER_BYTES = 128 * 1024 * 1024
MAX_GLOBAL_BYTES = 512 * 1024 * 1024
_OPERATION = "save-v1"
_AUTH_SOURCES = frozenset(("unknown", "local_jwt", "service", "oauth", "legacy"))
_READ_SCOPES = ("jobs:read", "artifacts:read", "metadata:read")
_ERRORS = {
    "metadata_unavailable": (503, "Metadata is unavailable."),
    "metadata_invalid_input": (422, "Invalid metadata input."),
    "job_not_found": (404, "Job not found."),
    "job_not_completed": (409, "Job is not completed."),
    "idempotency_conflict": (409, "Idempotency key conflicts with this request."),
    "metadata_revision_conflict": (409, "Metadata revision conflicts with this request."),
    "metadata_write_pending": (409, "A metadata write is pending."),
    "metadata_pending_not_found": (409, "Metadata reservation was not found."),
    "metadata_quota_exceeded": (413, "Metadata capacity limit exceeded."),
}


def _error(code):
    status, message = _ERRORS[code]
    return ApplicationError(status, code, message)


def _fail(code):
    raise _error(code)


def _guard(method):
    """Discard unexpected exceptions before raising a fixed public error."""
    @wraps(method)
    def guarded(*args, **kwargs):
        failure = None
        try:
            return method(*args, **kwargs)
        except ApplicationError as exc:
            failure = ApplicationError(exc.status, exc.code, exc.message)
        except Exception:
            failure = _error("metadata_unavailable")
        try:
            raise failure from None
        finally:
            failure.__context__ = None
            failure.__cause__ = None
    return guarded


def _hex(value, length):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{%d}" % length, value, re.ASCII) is not None


def _owner(value):
    return type(value) is str and re.fullmatch(r"(?:user:[1-9][0-9]*|service:[0-9a-f]{32})", value, re.ASCII) is not None


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _require(condition):
    if not condition:
        _fail("metadata_unavailable")


def _input(condition):
    if not condition:
        _fail("metadata_invalid_input")


def _pair(head, sha256):
    return ((head is None and sha256 is None)
            or (_hex(head, 32) and _hex(sha256, 64)))


def _input_time(value):
    result = None
    try:
        if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
            result = value.astimezone(timezone.utc)
    except Exception:
        pass
    _input(result is not None)
    return result


def _stored_time(value):
    _require(type(value) is str and len(value) <= 40)
    result = datetime.fromisoformat(value)
    _require(result.tzinfo is not None and result.utcoffset() is not None)
    result = result.astimezone(timezone.utc)
    _require(result.isoformat() == value)
    return result


def _receipt(value, *, stored=False):
    result = None
    try:
        if isinstance(value, SaveMetadataReceipt) or type(value) is dict:
            # Validate original nested values before a serializer can coerce them.
            validated = SaveMetadataReceipt.model_validate(value, strict=True)
            detached = validated.model_dump(mode="python", warnings="error")
            result = SaveMetadataReceipt.model_validate(detached, strict=True)
    except Exception:
        pass
    if result is None:
        _fail("metadata_unavailable" if stored else "metadata_invalid_input")
    return result


def _receipt_json(receipt):
    return json.dumps(receipt.model_dump(mode="json", warnings="error"),
                      sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON member.")
        result[key] = value
    return result


@dataclass(frozen=True)
class MetadataHead:
    revision_id: str
    sha256: str
    state: str
    approval_slots: int
    approval_bytes: int


@dataclass(frozen=True)
class MetadataRevision:
    revision_id: str
    owner: str
    job_id: str
    parent_revision_id: str | None
    sha256: str
    size_bytes: int
    state: str
    created_at: datetime


@dataclass(frozen=True)
class MetadataPending:
    pending_id: str
    job_id: str
    owner: str
    operation: str
    key_hash: str
    request_hash: str
    revision_id: str
    sha256: str
    size_bytes: int
    expected_head: str | None
    expected_sha256: str | None
    created_at: datetime
    actor: str
    auth_source: str
    charged_slots: int
    charged_bytes: int
    approval_slots: int
    approval_bytes: int
    state: str


@dataclass(frozen=True)
class MetadataAdmission:
    head: MetadataHead | None
    receipt: SaveMetadataReceipt | None = None
    pending: MetadataPending | None = None


_DDL = (
    """CREATE TABLE IF NOT EXISTS metadata_heads (
        owner TEXT NOT NULL, job_id TEXT PRIMARY KEY,
        revision_id TEXT NOT NULL, sha256 TEXT NOT NULL,
        approval_slots INTEGER NOT NULL CHECK(typeof(approval_slots)='integer' AND approval_slots>=0),
        approval_bytes INTEGER NOT NULL CHECK(typeof(approval_bytes)='integer' AND approval_bytes>=0)
    )""",
    """CREATE TABLE IF NOT EXISTS metadata_revisions (
        revision_id TEXT PRIMARY KEY, owner TEXT NOT NULL, job_id TEXT NOT NULL,
        parent_revision_id TEXT, sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK(typeof(size_bytes)='integer' AND size_bytes>0),
        state TEXT NOT NULL CHECK(state IN ('draft','approved')), created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS metadata_operations (
        owner TEXT NOT NULL, job_id TEXT NOT NULL,
        operation TEXT NOT NULL CHECK(operation='save-v1'),
        key_hash TEXT NOT NULL, request_hash TEXT NOT NULL,
        revision_id TEXT NOT NULL, receipt_json TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(owner,job_id,operation,key_hash)
    )""",
    """CREATE TABLE IF NOT EXISTS metadata_pending (
        pending_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, owner TEXT NOT NULL,
        operation TEXT NOT NULL CHECK(operation='save-v1'),
        key_hash TEXT NOT NULL, request_hash TEXT NOT NULL,
        revision_id TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL,
        size_bytes INTEGER NOT NULL CHECK(typeof(size_bytes)='integer' AND size_bytes>0),
        expected_head TEXT, expected_sha256 TEXT, created_at TEXT NOT NULL,
        actor TEXT NOT NULL, auth_source TEXT NOT NULL
            CHECK(auth_source IN ('unknown','local_jwt','service','oauth','legacy')),
        charged_slots INTEGER NOT NULL CHECK(typeof(charged_slots)='integer' AND charged_slots>=0),
        charged_bytes INTEGER NOT NULL CHECK(typeof(charged_bytes)='integer' AND charged_bytes>=0),
        approval_slots INTEGER NOT NULL CHECK(typeof(approval_slots)='integer' AND approval_slots>=0),
        approval_bytes INTEGER NOT NULL CHECK(typeof(approval_bytes)='integer' AND approval_bytes>=0),
        state TEXT NOT NULL CHECK(state IN ('active','cleanup'))
    )""",
    """CREATE TABLE IF NOT EXISTS metadata_audit (
        owner TEXT NOT NULL, job_id TEXT NOT NULL, revision_id TEXT NOT NULL,
        parent_revision_id TEXT, sha256 TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action='save'), actor TEXT NOT NULL,
        auth_source TEXT NOT NULL
            CHECK(auth_source IN ('unknown','local_jwt','service','oauth','legacy')),
        created_at TEXT NOT NULL, UNIQUE(revision_id,action)
    )""",
    "CREATE INDEX IF NOT EXISTS metadata_revisions_owner_job ON metadata_revisions(owner,job_id)",
    "CREATE INDEX IF NOT EXISTS metadata_revisions_job ON metadata_revisions(job_id)",
    "CREATE INDEX IF NOT EXISTS metadata_heads_owner ON metadata_heads(owner)",
    "CREATE INDEX IF NOT EXISTS metadata_pending_owner ON metadata_pending(owner)",
    "CREATE INDEX IF NOT EXISTS metadata_operations_job ON metadata_operations(job_id)",
    "CREATE INDEX IF NOT EXISTS metadata_operations_revision ON metadata_operations(revision_id)",
    "CREATE INDEX IF NOT EXISTS metadata_audit_job ON metadata_audit(job_id)",
)
_REVISION_COLUMNS = "revision_id,owner,job_id,parent_revision_id,sha256,size_bytes,state,created_at"
_PENDING_COLUMNS = ("pending_id,job_id,owner,operation,key_hash,request_hash,revision_id,sha256,"
                    "size_bytes,expected_head,expected_sha256,created_at,actor,auth_source,"
                    "charged_slots,charged_bytes,approval_slots,approval_bytes,state")


class MetadataStore:
    @_guard
    def __init__(self, jobs):
        self.jobs = jobs
        with self._transaction() as conn:
            self._active_writes = set()
            for statement in _DDL:
                conn.execute(statement)

    @contextmanager
    def _transaction(self):
        """Own the shared nonreentrant lock and one immediate SQLite transaction."""
        failure = None
        with self.jobs._lock:
            conn = None
            begun = False
            try:
                conn = self.jobs._get_conn()
                _require(not conn.in_transaction)
                conn.execute("BEGIN IMMEDIATE")
                begun = True
                yield conn
                conn.commit()
            except BaseException as exc:
                if isinstance(exc, ApplicationError):
                    failure = ApplicationError(exc.status, exc.code, exc.message)
                else:
                    failure = _error("metadata_unavailable")
                if begun:
                    try:
                        conn.rollback()
                    except BaseException:
                        # Preserve the already sanitized failure, including its code.
                        pass
        if failure is not None:
            # contextlib throws the caller's exception into this generator.
            # Clear the implicit context AFTER raising attaches it, including
            # when __exit__ is running with that original exception active.
            try:
                raise failure from None
            finally:
                failure.__context__ = None
                failure.__cause__ = None

    def _connection_locked(self, conn):
        # Private callers MUST hold jobs._lock; Lock has no ownership inspection.
        _require(conn is self.jobs._get_conn() and conn.in_transaction)

    def _identity(self, principal, *, write):
        for scope in _READ_SCOPES:
            principal.require(scope)
        if write:
            principal.require("metadata:write")
        _require(_owner(principal.subject))
        _require(type(principal.auth_source) is str and principal.auth_source in _AUTH_SOURCES)
        return principal.subject, principal.auth_source

    def _admit(self, principal, job_id, *, write):
        identity = self._identity(principal, write=write)
        job = self.jobs.jobs.get(job_id)
        if job is None or job.effective_owner != identity[0]:
            _fail("job_not_found")
        # This helper does not acquire the shared nonreentrant lock.
        self.jobs.check_retained(job)
        if job.status != "completed":
            _fail("job_not_completed")
        return identity

    @staticmethod
    def _syntax(job_id, key_hash, request_hash):
        _input(_hex(job_id, 32) and _hex(key_hash, 64) and _hex(request_hash, 64))

    @staticmethod
    def _revision_row(row):
        _require(row is not None and len(row) == 8)
        revision_id, owner, job_id, parent, sha256, size, state, created = row
        _require(_hex(revision_id, 32) and _owner(owner) and _hex(job_id, 32))
        _require(parent is None or (_hex(parent, 32) and parent != revision_id))
        _require(_hex(sha256, 64) and _integer(size, 1, MAX_SNAPSHOT_BYTES))
        _require(type(state) is str and state in ("draft", "approved"))
        return MetadataRevision(revision_id, owner, job_id, parent, sha256,
                                size, state, _stored_time(created))

    @_guard
    def _revision(self, conn, revision_id):
        row = conn.execute("SELECT " + _REVISION_COLUMNS +
                           " FROM metadata_revisions WHERE revision_id=?", (revision_id,)).fetchone()
        return self._revision_row(row) if row is not None else None

    @_guard
    def _head(self, conn, owner, job_id):
        row = conn.execute("SELECT owner,job_id,revision_id,sha256,approval_slots,approval_bytes "
                           "FROM metadata_heads WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            for table in ("metadata_revisions", "metadata_operations", "metadata_audit"):
                _require(conn.execute("SELECT 1 FROM " + table + " WHERE job_id=? LIMIT 1",
                                      (job_id,)).fetchone() is None)
            return None
        stored_owner, stored_job, revision_id, sha256, slots, size = row
        _require(stored_owner == owner and stored_job == job_id)
        _require(_hex(revision_id, 32) and _hex(sha256, 64))
        revision = self._revision(conn, revision_id)
        _require(revision is not None and revision.owner == owner and revision.job_id == job_id
                 and revision.sha256 == sha256)
        expected = (1, MAX_SNAPSHOT_BYTES) if revision.state == "draft" else (0, 0)
        _require(type(slots) is int and type(size) is int and (slots, size) == expected)
        return MetadataHead(revision_id, sha256, revision.state, slots, size)

    @_guard
    def _replay(self, conn, owner, job_id, key_hash, request_hash):
        row = conn.execute(
            "SELECT owner,job_id,operation,key_hash,request_hash,revision_id,receipt_json,created_at "
            "FROM metadata_operations WHERE owner=? AND job_id=? AND operation=? AND key_hash=?",
            (owner, job_id, _OPERATION, key_hash)).fetchone()
        if row is None:
            return None
        stored_owner, stored_job, operation, key, request, revision_id, encoded, created = row
        _require((stored_owner, stored_job, operation, key) == (owner, job_id, _OPERATION, key_hash))
        _require(_hex(request, 64) and _hex(revision_id, 32))
        if request != request_hash:
            _fail("idempotency_conflict")
        _require(type(encoded) is str and len(encoded) <= 8192)
        receipt = _receipt(json.loads(encoded, object_pairs_hook=_unique_object), stored=True)
        revision = self._revision(conn, revision_id)
        _require(revision is not None and revision.owner == owner and revision.job_id == job_id
                 and revision.state == "draft" and receipt.revision_id == revision_id
                 and receipt.sha256 == revision.sha256 and _stored_time(created) == revision.created_at)
        return receipt

    @staticmethod
    def _cas(head, expected_head, expected_sha256):
        actual = (head.revision_id, head.sha256) if head is not None else (None, None)
        if actual != (expected_head, expected_sha256):
            _fail("metadata_revision_conflict")

    @staticmethod
    def _charges(head, size_bytes):
        approval_slots = 0 if head is not None and head.state == "draft" else 1
        approval_bytes = approval_slots * MAX_SNAPSHOT_BYTES
        return 1 + approval_slots, size_bytes + approval_bytes, approval_slots, approval_bytes

    @_guard
    def _pending(self, conn, pending_id):
        row = conn.execute("SELECT " + _PENDING_COLUMNS +
                           " FROM metadata_pending WHERE pending_id=?", (pending_id,)).fetchone()
        if row is None:
            return None
        values = list(row)
        _require(len(values) == 19)
        p = MetadataPending(*values[:11], _stored_time(values[11]), *values[12:])
        _require(_hex(p.pending_id, 32) and _hex(p.job_id, 32) and _owner(p.owner))
        _require(p.operation == _OPERATION and _hex(p.key_hash, 64) and _hex(p.request_hash, 64))
        _require(_hex(p.revision_id, 32) and _hex(p.sha256, 64))
        _require(_integer(p.size_bytes, 1, MAX_SNAPSHOT_BYTES) and _pair(p.expected_head, p.expected_sha256))
        _require(p.expected_head != p.revision_id and p.actor == p.owner and _owner(p.actor))
        _require(type(p.auth_source) is str and p.auth_source in _AUTH_SOURCES)
        _require(type(p.state) is str and p.state in ("active", "cleanup"))
        counters = (p.charged_slots, p.charged_bytes, p.approval_slots, p.approval_bytes)
        _require(all(type(value) is int for value in counters))
        _require((p.approval_slots, p.approval_bytes) in ((0, 0), (1, MAX_SNAPSHOT_BYTES)))
        _require(p.charged_slots == 1 + p.approval_slots
                 and p.charged_bytes == p.size_bytes + p.approval_bytes)
        return p

    @_guard
    def _quota(self, conn, owner, job_id, *, add_slots=0, add_bytes=0):
        # Validate only jobs referenced by metadata, including historical
        # revisions. Retention/tombstones do not release physical capacity.
        # NULLIF reproduces effective_owner's Python `or`; the legacy fallback
        # accepts positive SQLite INTEGERs only, never coerced TEXT or REALs.
        effective_owner = (
            "COALESCE(NULLIF(j.owner_principal_id,''), CASE WHEN "
            "typeof(j.owner_user_id)='integer' AND j.owner_user_id>0 "
            "THEN 'user:' || j.owner_user_id END)")
        valid_owner = (
            "typeof(m.owner)='text' AND ("
            "(substr(m.owner,1,5)='user:' AND substr(m.owner,6,1) GLOB '[1-9]' "
            "AND substr(m.owner,6) NOT GLOB '*[^0-9]*') OR "
            "(substr(m.owner,1,8)='service:' AND length(m.owner)=40 "
            "AND substr(m.owner,9) NOT GLOB '*[^0-9a-f]*'))")
        for table in ('metadata_revisions', 'metadata_heads', 'metadata_pending'):
            bad = conn.execute(
                "SELECT 1 FROM " + table + " m WHERE NOT (" + valid_owner + ") "
                "OR NOT EXISTS (SELECT 1 FROM jobs j WHERE j.job_id=m.job_id "
                "AND typeof(j.job_id)='text' AND length(j.job_id)=32 "
                "AND j.job_id NOT GLOB '*[^0-9a-f]*' "
                "AND m.owner=" + effective_owner + ") LIMIT 1").fetchone()
            _require(bad is None)
        _require(conn.execute(
            "SELECT 1 FROM metadata_revisions r WHERE "
            "NOT EXISTS (SELECT 1 FROM metadata_heads h WHERE "
            "h.job_id=r.job_id AND h.owner=r.owner) OR "
            "(r.parent_revision_id IS NOT NULL AND NOT EXISTS "
            "(SELECT 1 FROM metadata_revisions p WHERE "
            "p.revision_id=r.parent_revision_id AND p.revision_id<>r.revision_id "
            "AND p.owner=r.owner AND p.job_id=r.job_id)) LIMIT 1").fetchone() is None)
        # Each query returns small aggregates only. SUM overflow is an error,
        # never a floating-point or wrapped capacity estimate.
        revision_bad = ("typeof(size_bytes)<>'integer' OR size_bytes<1 OR size_bytes>1048576 "
                        "OR state NOT IN ('draft','approved')")
        head_bad = ("typeof(approval_slots)<>'integer' OR typeof(approval_bytes)<>'integer' OR "
                    "NOT EXISTS (SELECT 1 FROM metadata_revisions r WHERE "
                    "r.revision_id=metadata_heads.revision_id AND r.owner=metadata_heads.owner "
                    "AND r.job_id=metadata_heads.job_id AND r.sha256=metadata_heads.sha256 AND "
                    "((r.state='draft' AND approval_slots=1 AND approval_bytes=1048576) OR "
                    "(r.state='approved' AND approval_slots=0 AND approval_bytes=0)))")
        pending_bad = ("typeof(size_bytes)<>'integer' OR size_bytes<1 OR size_bytes>1048576 OR "
                       "typeof(charged_slots)<>'integer' OR typeof(charged_bytes)<>'integer' OR "
                       "typeof(approval_slots)<>'integer' OR typeof(approval_bytes)<>'integer' OR "
                       "NOT ((approval_slots=0 AND approval_bytes=0) OR "
                       "(approval_slots=1 AND approval_bytes=1048576)) OR "
                       "charged_slots<>1+approval_slots OR charged_bytes<>size_bytes+approval_bytes OR "
                       "state NOT IN ('active','cleanup') OR operation<>'save-v1' OR "
                       "NOT ((expected_head IS NULL AND expected_sha256 IS NULL "
                       "AND approval_slots=1 AND approval_bytes=1048576 "
                       "AND NOT EXISTS (SELECT 1 FROM metadata_heads h WHERE "
                       "h.job_id=metadata_pending.job_id)) OR "
                       "EXISTS (SELECT 1 FROM metadata_heads h "
                       "JOIN metadata_revisions r ON r.revision_id=h.revision_id "
                       "WHERE h.job_id=metadata_pending.job_id "
                       "AND h.owner=metadata_pending.owner AND r.owner=h.owner "
                       "AND r.job_id=h.job_id AND r.sha256=h.sha256 "
                       "AND h.revision_id=metadata_pending.expected_head "
                       "AND h.sha256=metadata_pending.expected_sha256 AND "
                       "((r.state='draft' AND metadata_pending.approval_slots=0 "
                       "AND metadata_pending.approval_bytes=0) OR "
                       "(r.state='approved' AND metadata_pending.approval_slots=1 "
                       "AND metadata_pending.approval_bytes=1048576))))")
        totals = [0, 0, 0, 0]
        for table, byte_column, slot_column, bad in (
            ("metadata_revisions", "size_bytes", "1", revision_bad),
            ("metadata_heads", "approval_bytes", "approval_slots", head_bad),
            ("metadata_pending", "charged_bytes", "charged_slots", pending_bad),
        ):
            sql = ("SELECT COALESCE(SUM(" + byte_column + "),0), "
                   "COALESCE(SUM(CASE WHEN owner=? THEN " + byte_column + " ELSE 0 END),0), "
                   "COALESCE(SUM(CASE WHEN job_id=? THEN " + byte_column + " ELSE 0 END),0), "
                   "COALESCE(SUM(CASE WHEN job_id=? THEN " + slot_column + " ELSE 0 END),0), "
                   "COALESCE(SUM(CASE WHEN " + bad + " THEN 1 ELSE 0 END),0) FROM " + table)
            row = conn.execute(sql, (owner, job_id, job_id)).fetchone()
            _require(len(row) == 5 and all(type(value) is int and value >= 0 for value in row)
                     and row[4] == 0)
            for index in range(4):
                totals[index] += row[index]
        global_bytes, owner_bytes, job_bytes, job_slots = totals
        if (global_bytes + add_bytes > MAX_GLOBAL_BYTES
                or owner_bytes + add_bytes > MAX_OWNER_BYTES
                or job_bytes + add_bytes > MAX_JOB_BYTES
                or job_slots + add_slots > MAX_REVISIONS_PER_JOB):
            _fail("metadata_quota_exceeded")

    @_guard
    def _reserved_disk_bytes_locked(self, conn, owner, job_id):
        """All head and pending disk reserves, including cleanup and expired jobs.

        Own pending capacity is already included. Other pending files may already
        occupy disk; counting their full reserve again deliberately trades write
        availability for conservative capacity admission. This is neither a
        unified upload/export quota nor protection against external consumption.
        """
        self._connection_locked(conn)
        self._quota(conn, owner, job_id)
        row = conn.execute(
            "SELECT COALESCE(SUM(bytes),0) FROM ("
            "SELECT approval_bytes AS bytes FROM metadata_heads UNION ALL "
            "SELECT charged_bytes AS bytes FROM metadata_pending)").fetchone()
        _require(row is not None and len(row) == 1
                 and type(row[0]) is int and row[0] >= 0)
        return row[0]

    @_guard
    def _index_outcome_locked(self, conn, reservation, receipt):
        """Diagnose only the captured attempt; no admission, lifecycle or repair.

        Caller owns the shared lock and a fresh transaction. Exceptions are also
        unknown outcomes to the coordinator. A later head is permitted, whereas
        contradictory references to this attempt never authorize cleanup.
        """
        self._connection_locked(conn)
        _require(type(reservation) is MetadataPending)
        r = reservation
        receipt = _receipt(receipt, stored=True)
        _require(receipt.revision_id == r.revision_id and receipt.sha256 == r.sha256)
        key = (r.owner, r.job_id, r.operation, r.key_hash)
        pending_rows = conn.execute(
            "SELECT pending_id FROM metadata_pending WHERE pending_id=? OR revision_id=? "
            "OR (owner=? AND job_id=? AND operation=? AND key_hash=?)",
            (r.pending_id, r.revision_id, *key)).fetchall()
        operation_rows = conn.execute(
            "SELECT owner,job_id,operation,key_hash,request_hash,revision_id,receipt_json,created_at "
            "FROM metadata_operations WHERE revision_id=? OR "
            "(owner=? AND job_id=? AND operation=? AND key_hash=?)",
            (r.revision_id, *key)).fetchall()
        audit_rows = conn.execute(
            "SELECT owner,job_id,revision_id,parent_revision_id,sha256,action,actor,auth_source,created_at "
            "FROM metadata_audit WHERE revision_id=?", (r.revision_id,)).fetchall()
        head_rows = conn.execute(
            "SELECT owner,job_id FROM metadata_heads WHERE revision_id=?",
            (r.revision_id,)).fetchall()
        revision = self._revision(conn, r.revision_id)
        if len(pending_rows) == 1:
            pending = self._pending(conn, pending_rows[0][0])
            if (pending is not None and pending.state in ("active", "cleanup")
                    and replace(pending, state=r.state) == r and r.state == "active"
                    and revision is None and not operation_rows and not audit_rows
                    and not head_rows):
                return "uncommitted"
            return "unknown"
        if pending_rows or revision is None or len(operation_rows) != 1 or len(audit_rows) != 1:
            return "unknown"
        expected_revision = MetadataRevision(
            r.revision_id, r.owner, r.job_id, r.expected_head, r.sha256,
            r.size_bytes, "draft", r.created_at)
        if revision != expected_revision:
            return "unknown"
        operation = tuple(operation_rows[0])
        if operation[:6] != (*key, r.request_hash, r.revision_id):
            return "unknown"
        encoded = operation[6]
        _require(type(encoded) is str and len(encoded) <= 8192)
        stored_receipt = _receipt(
            json.loads(encoded, object_pairs_hook=_unique_object), stored=True)
        if stored_receipt != receipt or _stored_time(operation[7]) != r.created_at:
            return "unknown"
        audit = tuple(audit_rows[0])
        if (audit[:8] != (r.owner, r.job_id, r.revision_id, r.expected_head,
                          r.sha256, "save", r.actor, r.auth_source)
                or _stored_time(audit[8]) != r.created_at):
            return "unknown"
        head = self._head(conn, r.owner, r.job_id)
        if head is None:
            return "unknown"
        expected_heads = [(r.owner, r.job_id)] if head.revision_id == r.revision_id else []
        if [tuple(row) for row in head_rows] != expected_heads:
            return "unknown"
        # A later reservation is legitimate only against the current head.
        later = conn.execute("SELECT pending_id FROM metadata_pending WHERE job_id=?",
                             (r.job_id,)).fetchall()
        if len(later) > 1:
            return "unknown"
        if later:
            pending = self._pending(conn, later[0][0])
            if (pending is None or pending.owner != r.owner
                    or (pending.expected_head, pending.expected_sha256)
                    != (head.revision_id, head.sha256)
                    or (pending.charged_slots, pending.charged_bytes,
                        pending.approval_slots, pending.approval_bytes)
                    != self._charges(head, pending.size_bytes)):
                return "unknown"
        return "confirmed"

    @_guard
    def preflight(self, principal, job_id, *, key_hash, request_hash):
        self._syntax(job_id, key_hash, request_hash)
        with self._transaction() as conn:
            owner, _ = self._admit(principal, job_id, write=True)
            receipt = self._replay(conn, owner, job_id, key_hash, request_hash)
            head = self._head(conn, owner, job_id)
            return MetadataAdmission(head, receipt)

    @_guard
    def reserve(self, principal, job_id, *, key_hash, request_hash, expected_head,
                expected_sha256, revision_id, sha256, size_bytes, created_at):
        self._syntax(job_id, key_hash, request_hash)
        _input(_pair(expected_head, expected_sha256))
        _input(_hex(revision_id, 32) and _hex(sha256, 64))
        _input(_integer(size_bytes, 1, MAX_SNAPSHOT_BYTES))
        created_at = _input_time(created_at)
        with self._transaction() as conn:
            owner, auth_source = self._admit(principal, job_id, write=True)
            receipt = self._replay(conn, owner, job_id, key_hash, request_hash)
            head = self._head(conn, owner, job_id)
            if receipt is not None:
                return MetadataAdmission(head, receipt)
            self._cas(head, expected_head, expected_sha256)
            existing = conn.execute(
                "SELECT pending_id FROM metadata_pending WHERE job_id=?", (job_id,)).fetchone()
            if existing is not None:
                pending = self._pending(conn, existing[0])
                _require(pending is not None and pending.owner == owner and pending.job_id == job_id)
                actual = (head.revision_id, head.sha256) if head is not None else (None, None)
                # Cleanup uncertainty never authorizes discarding a reservation.
                _require((pending.expected_head, pending.expected_sha256) == actual)
                _require((pending.charged_slots, pending.charged_bytes,
                          pending.approval_slots, pending.approval_bytes)
                         == self._charges(head, pending.size_bytes))
                _fail("metadata_write_pending")
            if (conn.execute("SELECT 1 FROM metadata_revisions WHERE revision_id=?", (revision_id,)).fetchone()
                    or conn.execute("SELECT 1 FROM metadata_pending WHERE revision_id=?", (revision_id,)).fetchone()):
                _fail("metadata_revision_conflict")
            charges = self._charges(head, size_bytes)
            self._quota(conn, owner, job_id, add_slots=charges[0], add_bytes=charges[1])
            pending_id = uuid4().hex
            conn.execute(
                "INSERT INTO metadata_pending (" + _PENDING_COLUMNS + ") "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (pending_id, job_id, owner, _OPERATION, key_hash, request_hash, revision_id,
                 sha256, size_bytes, expected_head, expected_sha256, created_at.isoformat(),
                 owner, auth_source, *charges, "active"))
            pending = self._pending(conn, pending_id)
            _require(pending is not None)
            return MetadataAdmission(head, pending=pending)

    @_guard
    def get_head(self, principal, job_id):
        _input(_hex(job_id, 32))
        with self._transaction() as conn:
            owner, _ = self._admit(principal, job_id, write=False)
            return self._head(conn, owner, job_id)

    @_guard
    def list_revisions(self, principal, job_id):
        _input(_hex(job_id, 32))
        with self._transaction() as conn:
            owner, _ = self._admit(principal, job_id, write=False)
            self._head(conn, owner, job_id)
            rows = conn.execute("SELECT " + _REVISION_COLUMNS +
                                " FROM metadata_revisions WHERE job_id=? "
                                "ORDER BY created_at,revision_id LIMIT ?",
                                (job_id, MAX_REVISIONS_PER_JOB + 1)).fetchall()
            _require(len(rows) <= MAX_REVISIONS_PER_JOB)
            revisions = tuple(self._revision_row(row) for row in rows)
            _require(all(revision.owner == owner and revision.job_id == job_id for revision in revisions))
            return revisions

    @_guard
    def _commit_index_locked(self, conn, principal, pending_id, receipt):
        """Caller owns lock + BEGIN IMMEDIATE and has verified files/bindings.

        Never acquires a lock or begins/ends a transaction. The returned receipt
        is not committed until the caller's _transaction() exits successfully.
        """
        self._connection_locked(conn)
        self._identity(principal, write=True)
        _input(_hex(pending_id, 32))
        receipt = _receipt(receipt)
        encoded = _receipt_json(receipt)
        pending = self._pending(conn, pending_id)
        if pending is None:
            _fail("metadata_pending_not_found")
        owner, auth_source = self._admit(principal, pending.job_id, write=True)
        if pending.owner != owner:
            _fail("job_not_found")
        if pending.state != "active" or pending.actor != owner or pending.auth_source != auth_source:
            _fail("metadata_write_pending")
        if receipt.revision_id != pending.revision_id or receipt.sha256 != pending.sha256:
            _fail("metadata_revision_conflict")
        replay = self._replay(conn, owner, pending.job_id, pending.key_hash, pending.request_hash)
        if replay is not None:
            _require(replay == receipt)
            revision = self._revision(conn, pending.revision_id)
            _require(revision is not None and revision.parent_revision_id == pending.expected_head
                     and revision.size_bytes == pending.size_bytes and revision.created_at == pending.created_at)
            return replay
        head = self._head(conn, owner, pending.job_id)
        self._cas(head, pending.expected_head, pending.expected_sha256)
        _require((pending.charged_slots, pending.charged_bytes,
                  pending.approval_slots, pending.approval_bytes) == self._charges(head, pending.size_bytes))
        _require(self._revision(conn, pending.revision_id) is None)
        # The durable pending row is already counted: do not charge it twice.
        self._quota(conn, owner, pending.job_id)
        timestamp = pending.created_at.isoformat()
        conn.execute(
            "INSERT INTO metadata_revisions (" + _REVISION_COLUMNS + ") VALUES (?,?,?,?,?,?,?,?)",
            (pending.revision_id, owner, pending.job_id, pending.expected_head,
             pending.sha256, pending.size_bytes, "draft", timestamp))
        conn.execute(
            "INSERT INTO metadata_heads (owner,job_id,revision_id,sha256,approval_slots,approval_bytes) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
            "owner=excluded.owner,revision_id=excluded.revision_id,sha256=excluded.sha256,"
            "approval_slots=excluded.approval_slots,approval_bytes=excluded.approval_bytes",
            (owner, pending.job_id, pending.revision_id, pending.sha256, 1, MAX_SNAPSHOT_BYTES))
        conn.execute(
            "INSERT INTO metadata_operations "
            "(owner,job_id,operation,key_hash,request_hash,revision_id,receipt_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (owner, pending.job_id, _OPERATION, pending.key_hash, pending.request_hash,
             pending.revision_id, encoded, timestamp))
        conn.execute(
            "INSERT INTO metadata_audit "
            "(owner,job_id,revision_id,parent_revision_id,sha256,action,actor,auth_source,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (owner, pending.job_id, pending.revision_id, pending.expected_head, pending.sha256,
             "save", pending.actor, pending.auth_source, timestamp))
        conn.execute("DELETE FROM metadata_pending WHERE pending_id=?", (pending_id,))
        return receipt

    @_guard
    def _mark_cleanup_locked(self, conn, pending_id):
        """Caller holds the shared lock and transaction; every charge survives."""
        self._connection_locked(conn)
        _input(_hex(pending_id, 32))
        pending = self._pending(conn, pending_id)
        if pending is None:
            _fail("metadata_pending_not_found")
        conn.execute("UPDATE metadata_pending SET state=? WHERE pending_id=?", ("cleanup", pending_id))

    @_guard
    def _release_pending_after_cleanup_locked(self, conn, pending_id):
        """C3b/C3c only: caller has verified absence of ALL pending files.

        Caller holds the shared lock and transaction. This helper makes no
        filesystem or permission claim and never releases a head's reserve.
        """
        self._connection_locked(conn)
        _input(_hex(pending_id, 32))
        pending = self._pending(conn, pending_id)
        if pending is None:
            return None
        _require(pending.state == "cleanup")
        _require(conn.execute("SELECT 1 FROM metadata_revisions WHERE revision_id=?",
                              (pending.revision_id,)).fetchone() is None)
        _require(conn.execute(
            "SELECT 1 FROM metadata_operations WHERE revision_id=? OR "
            "(owner=? AND job_id=? AND operation=? AND key_hash=?) LIMIT 1",
            (pending.revision_id, pending.owner, pending.job_id, _OPERATION,
             pending.key_hash)).fetchone() is None)
        conn.execute("DELETE FROM metadata_pending WHERE pending_id=?", (pending_id,))
