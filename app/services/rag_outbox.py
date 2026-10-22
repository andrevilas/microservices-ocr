"""Durable staged delivery queue. No worker or API is activated by importing this."""
import json
import secrets
import shutil
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone

from app.application import ApplicationError
from app.config import settings
from app.services.rag_delivery import PreparedDelivery, prepare_delivery, send_delivery
from app.services.rag_delivery_contract import parse_manifest


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS rag_outbox(
        id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE, owner TEXT NOT NULL,
        destination TEXT NOT NULL, payload BLOB, state TEXT NOT NULL,
        created REAL NOT NULL, deadline REAL NOT NULL, attempts INTEGER NOT NULL,
        next_attempt REAL NOT NULL, claim TEXT, claim_until REAL,
        receipt TEXT, error TEXT, policy TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS rag_delivery_audit(
        delivery_id TEXT NOT NULL, event TEXT NOT NULL, at REAL NOT NULL,
        PRIMARY KEY(delivery_id,event))""")


@dataclass(frozen=True)
class DeliveryPolicy:
    # No production defaults: the operator must supply a reviewed policy.
    max_entries: int
    max_payload_bytes: int
    hold_seconds: int
    post_receipt_seconds: int
    max_attempts: int
    backoff_seconds: int
    claim_seconds: int

    def __post_init__(self):
        if any(type(v) is not int or not 1 <= v <= 2**31 - 1 for v in asdict(self).values()):
            raise ValueError("invalid delivery policy")
        if self.max_attempts > 100 or self.claim_seconds > self.hold_seconds:
            raise ValueError("invalid delivery policy")


def binding(destination):
    """Non-secret destination identity. Credential rotation preserves this binding."""
    return json.dumps({k: v for k, v in asdict(destination).items() if k != "token"},
                      sort_keys=True, separators=(",", ":"))


class RagOutbox:
    def __init__(self, store, policy: DeliveryPolicy):
        self.store, self.policy = store, policy

    @contextmanager
    def transaction(self, after_commit=None):
        with self.store._lock:
            conn = self.store._get_conn()
            try:
                conn.execute("BEGIN IMMEDIATE")
            except BaseException:
                if conn.in_transaction:
                    self.store._quarantine_locked()
                raise
            try:
                yield conn
            except BaseException:
                try:
                    conn.rollback()
                except BaseException:
                    self.store._quarantine_locked()
                    raise
                raise
            else:
                try:
                    conn.commit()
                    if after_commit is not None:
                        after_commit()
                except BaseException:
                    # An uncertain commit must never allow cleanup using stale cache.
                    self.store._quarantine_locked()
                    raise

    @staticmethod
    def row(conn, delivery_id):
        cursor = conn.execute("SELECT * FROM rag_outbox WHERE id=?", (delivery_id,))
        row = cursor.fetchone()
        if row is None:
            raise ApplicationError(404, "rag_delivery_not_found", "Delivery not found.")
        return dict(zip([d[0] for d in cursor.description], row))

    @staticmethod
    def audit(conn, delivery_id, event, now):
        conn.execute("INSERT OR IGNORE INTO rag_delivery_audit VALUES(?,?,?)",
                     (delivery_id, event, now))

    def expire(self, conn, now):
        # A deadline escalates to resolution; it never silently authorizes deletion.
        for (delivery_id,) in conn.execute(
                "SELECT id FROM rag_outbox WHERE deadline<=? AND state IN ('queued','sending','retry')",
                (now,)).fetchall():
            conn.execute("UPDATE rag_outbox SET state='resolution',claim=NULL,claim_until=NULL,"
                         "error='hold_deadline' WHERE id=?", (delivery_id,))
            self.audit(conn, delivery_id, "resolution", now)

    def enqueue(self, principal, job_id, destination):
        # The outer lease bridges packaging -> durable hold admission.
        with self.store.artifact_lease(job_id, principal.subject):
            prepared = prepare_delivery(self.store, principal, job_id, destination)
            manifest = parse_manifest(json.dumps(json.loads(prepared.payload)["manifest"]).encode())
            now = time.time()
            with self.transaction() as conn:
                self.expire(conn, now)
                old = conn.execute("SELECT id FROM rag_outbox WHERE job_id=?", (job_id,)).fetchone()
                if old:
                    row = self.row(conn, old[0])
                    if (row["id"] != manifest.delivery_id or row["destination"] != binding(destination)
                            or row["owner"] != principal.subject):
                        raise ApplicationError(409, "rag_delivery_conflict", "Job already has a different delivery.")
                    return row["id"]
                job = self.store.jobs[job_id]
                self.store.check_retained(job)
                if job.artifacts_removed_at is not None or job.status != "completed":
                    raise ApplicationError(409, "rag_source_unavailable", "Result became unavailable.")
                count, size = conn.execute(
                    "SELECT count(*),coalesce(sum(length(payload)),0) FROM rag_outbox").fetchone()
                # Unresolved deadlines/failures block further admissions.
                unresolved = conn.execute("SELECT 1 FROM rag_outbox WHERE state='resolution' LIMIT 1").fetchone()
                if (unresolved or count >= self.policy.max_entries
                        or size + len(prepared.payload) > self.policy.max_payload_bytes):
                    raise ApplicationError(429, "rag_queue_full", "Delivery capacity requires operator resolution.")
                # Backpressure before duplicating the packet into SQLite/WAL.
                # This is a conservative admission margin, not a filesystem quota.
                required_free = 3 * len(prepared.payload) + settings.upload_min_free_mb * 1024 * 1024
                if shutil.disk_usage(self.store.root_dir).free < required_free:
                    raise ApplicationError(429, "rag_storage_pressure", "Storage capacity requires operator resolution.")
                conn.execute("""INSERT INTO rag_outbox VALUES(?,?,?,?,?,'queued',?,?,0,?,NULL,NULL,NULL,NULL,?)""",
                             (manifest.delivery_id, job_id, principal.subject, binding(destination),
                              prepared.payload, now, now + self.policy.hold_seconds, now,
                              json.dumps(asdict(self.policy))))
                self.audit(conn, manifest.delivery_id, "queued", now)
            return manifest.delivery_id

    def status(self, principal, delivery_id):
        principal.require("jobs:read")
        with self.transaction() as conn:
            self.expire(conn, time.time())
            row = self.row(conn, delivery_id)
            if row["owner"] != principal.subject:
                raise ApplicationError(404, "rag_delivery_not_found", "Delivery not found.")
            return {k: row[k] for k in ("id", "job_id", "state", "deadline", "attempts", "next_attempt", "error")}

    def receipt(self, principal, delivery_id):
        """Read the last verified receiver receipt, including pending indexing."""
        principal.require("jobs:read")
        with self.transaction() as conn:
            row = self.row(conn, delivery_id)
            if row["owner"] != principal.subject:
                raise ApplicationError(404, "rag_delivery_not_found", "Delivery not found.")
            if row["receipt"] is None:
                raise ApplicationError(409, "rag_receipt_unavailable", "No verified receipt is available.")
            return json.loads(row["receipt"])

    def recovery_package(self, principal, delivery_id):
        """Export the immutable pending package without releasing its hold.

        This remains readable after the normal artifact deadline. It is a
        separate recovery capability, never a bypass of the artifact reader.
        Download completion is not proof of durable external storage.
        """
        principal.require("jobs:read")
        principal.require("artifacts:read")
        with self.transaction() as conn:
            row = self.row(conn, delivery_id)
            if row["owner"] != principal.subject:
                raise ApplicationError(404, "rag_delivery_not_found", "Delivery not found.")
            if row["payload"] is None:
                raise ApplicationError(409, "rag_package_unavailable", "No recovery package is retained.")
            return bytes(row["payload"])

    def run_one(self, delivery_id, destination, *, transport=None):
        """Trusted operator/worker entrypoint; credentials are supplied, never persisted."""
        destination.validate()
        now = time.time()
        claim = secrets.token_hex(16)
        with self.transaction() as conn:
            self.expire(conn, now)
            row = self.row(conn, delivery_id)
            if row["destination"] != binding(destination):
                raise ApplicationError(403, "rag_destination_forbidden", "Destination binding differs.")
            if row["state"] in ("indexed", "canceled", "resolution"):
                return row["state"]
            if row["next_attempt"] > now or (row["claim_until"] or 0) > now:
                return "busy"
            policy = DeliveryPolicy(**json.loads(row["policy"]))
            if row["attempts"] >= policy.max_attempts:
                conn.execute("UPDATE rag_outbox SET state='resolution',error='attempt_limit',claim=NULL,"
                             "claim_until=NULL WHERE id=?", (delivery_id,))
                self.audit(conn, delivery_id, "resolution", now)
                return "resolution"
            conn.execute("UPDATE rag_outbox SET state='sending',attempts=attempts+1,claim=?,claim_until=? WHERE id=?",
                         (claim, min(now + policy.claim_seconds, row["deadline"]), delivery_id))
            prepared = PreparedDelivery(bytes(row["payload"]), row["owner"])
        result, error = None, None
        try:
            result = send_delivery(prepared, destination, transport=transport)
        except ApplicationError as exc:
            error = exc.code
        # Unexpected crashes intentionally leave a durable claim until recovery.
        finished = time.time()
        cache_updates = []
        def update_cache():
            for job_id, deadline in cache_updates:
                self.store.jobs[job_id].artifacts_expires_at = deadline
        with self.transaction(after_commit=update_cache) as conn:
            self.expire(conn, finished)
            row = self.row(conn, delivery_id)
            if row["claim"] != claim or row["state"] != "sending":
                return "superseded"
            if (row["claim_until"] or 0) <= finished:
                # Late response cannot complete an expired claim.
                return "superseded"
            receipt = result.receipt.model_dump_json() if result else None
            if result and result.indexed:
                job = self.store.jobs[row["job_id"]]
                deadline = datetime.fromtimestamp(finished + policy.post_receipt_seconds, timezone.utc)
                if job.artifacts_expires_at:
                    deadline = max(deadline, job.artifacts_expires_at)
                updated = replace(job, artifacts_expires_at=deadline)
                self.store._persist(updated, commit=False)
                cache_updates.append((job.job_id, deadline))
                conn.execute("UPDATE rag_outbox SET state='indexed',receipt=?,payload=NULL,claim=NULL,"
                             "claim_until=NULL,error=NULL WHERE id=?", (receipt, delivery_id))
                self.audit(conn, delivery_id, "indexed", finished)
            else:
                permanent = error in ("rag_delivery_rejected", "rag_receipt_invalid", "rag_package_invalid")
                exhausted = row["attempts"] >= policy.max_attempts
                state = "resolution" if permanent or exhausted else "retry"
                delay = min(policy.backoff_seconds * 2 ** (row["attempts"] - 1),
                            max(0, row["deadline"] - finished))
                conn.execute("UPDATE rag_outbox SET state=?,receipt=?,error=?,next_attempt=?,"
                             "claim=NULL,claim_until=NULL WHERE id=?",
                             (state, receipt, error or "index_pending", finished + delay, delivery_id))
                if state == "resolution":
                    self.audit(conn, delivery_id, "resolution", finished)
        # Cache mutation only after confirmed commit; on ambiguity store is quarantined.
        if result and result.indexed:
            return "indexed"
        return state

    def cancel(self, principal, delivery_id):
        """Explicit owner action only; release hold, do not physically delete here."""
        principal.require("jobs:read")
        principal.require("artifacts:delete")
        now = time.time()
        with self.transaction() as conn:
            row = self.row(conn, delivery_id)
            if row["owner"] != principal.subject:
                raise ApplicationError(404, "rag_delivery_not_found", "Delivery not found.")
            if (row["claim_until"] or 0) > now:
                raise ApplicationError(409, "rag_delivery_busy", "Wait for the active attempt to finish.")
            if row["state"] in ("indexed", "canceled"):
                return row["state"]
            conn.execute("UPDATE rag_outbox SET state='canceled',payload=NULL,claim=NULL,claim_until=NULL WHERE id=?",
                         (delivery_id,))
            self.audit(conn, delivery_id, "canceled", now)
            return "canceled"
