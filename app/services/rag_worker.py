"""Explicitly started, bounded delivery worker; never starts during import."""
import math
import logging
import shutil
import time
from threading import Event, Lock, Thread

from app.services.rag_outbox import binding
from app.config import settings

logger = logging.getLogger(__name__)


class RagDeliveryWorker:
    """Internal operator service. Monitoring is not an end-user authorization API."""

    def __init__(self, outbox, destinations, *, interval_seconds, batch_size, warning_seconds,
                 transport=None):
        if (type(interval_seconds) not in (int, float) or not math.isfinite(interval_seconds)
                or interval_seconds <= 0 or interval_seconds > 86400
                or type(batch_size) is not int or not 1 <= batch_size <= 100
                or type(warning_seconds) is not int or not 0 <= warning_seconds <= 2**31 - 1):
            raise ValueError("invalid worker policy")
        self.outbox = outbox
        self.interval_seconds, self.batch_size = interval_seconds, batch_size
        self.warning_seconds, self.transport = warning_seconds, transport
        self._config_lock, self._collect_lock, self._lifecycle_lock = Lock(), Lock(), Lock()
        self._destinations = {}
        self.replace_destinations(destinations)
        self._stop = Event()
        self._thread = None
        self._failure = None
        self._notice_state = None

    def replace_destinations(self, destinations):
        """Trusted operator configuration; no URL or token from tool arguments."""
        mapped = {}
        for destination in destinations:
            destination.validate()
            key = binding(destination)
            if key in mapped:
                raise ValueError("duplicate destination binding")
            mapped[key] = destination
        with self._config_lock:
            self._destinations = mapped

    def snapshot(self):
        """Aggregate local warnings without document text, URLs, identities or tokens."""
        now = time.time()
        with self.outbox.transaction() as db:
            self.outbox.expire(db, now)
            states = dict(db.execute("SELECT state,count(*) FROM rag_outbox GROUP BY state").fetchall())
            due, near, payload_bytes = db.execute("""
                SELECT
                  coalesce(sum(CASE WHEN state IN ('queued','retry','sending') AND next_attempt<=?
                    AND coalesce(claim_until,0)<=? THEN 1 ELSE 0 END),0),
                  coalesce(sum(CASE WHEN state IN ('queued','retry','sending') AND deadline<=?
                    THEN 1 ELSE 0 END),0),
                  coalesce(sum(length(payload)),0)
                FROM rag_outbox""", (now, now, now + self.warning_seconds)).fetchone()
        return {"states": states, "due": due, "near_deadline": near,
                "requires_resolution": states.get("resolution", 0), "queued_payload_bytes": payload_bytes,
                "worker_failure": self._failure}

    def collect(self):
        """At most batch_size attempts; no overlapping collector in this worker."""
        if not self._collect_lock.acquire(blocking=False):
            return {"busy": True, "processed": 0}
        try:
            now = time.time()
            with self.outbox.transaction() as db:
                self.outbox.expire(db, now)
                due = db.execute("""
                    SELECT id,destination FROM rag_outbox
                    WHERE state IN ('queued','retry','sending')
                      AND next_attempt<=? AND coalesce(claim_until,0)<=?
                    ORDER BY next_attempt,created,id LIMIT ?""",
                    (now, now, self.batch_size)).fetchall()
            processed = 0
            outcomes = {}
            for delivery_id, key in due:
                if self._stop.is_set():
                    break
                with self._config_lock:
                    destination = self._destinations.get(key)
                if destination is None:
                    # Missing/changed config never falls back to another credential or URL.
                    with self.outbox.transaction() as db:
                        row = self.outbox.row(db, delivery_id)
                        outcome = "busy" if row["state"] in ("queued", "retry", "sending") else row["state"]
                        if (row["state"] in ("queued", "retry", "sending")
                                and (row["claim_until"] or 0) <= time.time()):
                            db.execute("UPDATE rag_outbox SET state='resolution',error='destination_missing',"
                                       "claim=NULL,claim_until=NULL WHERE id=?", (delivery_id,))
                            self.outbox.audit(db, delivery_id, "resolution", time.time())
                            outcome = "resolution"
                else:
                    outcome = self.outbox.run_one(delivery_id, destination, transport=self.transport)
                processed += 1
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
            return {"busy": False, "processed": processed, "outcomes": outcomes}
        finally:
            self._collect_lock.release()

    def observe(self):
        """Local aggregate alerts only; never messages to external recipients."""
        snapshot = self.snapshot()
        pressure = shutil.disk_usage(self.outbox.store.root_dir).free < settings.upload_min_free_mb * 1024 * 1024
        flags = (snapshot["requires_resolution"] > 0, snapshot["near_deadline"] > 0, pressure)
        if flags != self._notice_state:
            if any(flags):
                logger.warning("RAG operational attention: resolution=%s near_deadline=%s storage_pressure=%s", *flags)
            elif self._notice_state is not None:
                logger.info("RAG operational attention cleared")
            self._notice_state = flags
        return {**snapshot, "storage_pressure": pressure}

    def _run(self):
        while not self._stop.is_set():
            try:
                self.collect()
                self.observe()
            except Exception:
                # Fail closed on unexpected storage/control errors. Never log payloads.
                self._failure = "worker_failed"
                logger.error("RAG delivery worker stopped; operator recovery required")
                self._stop.set()
                break
            self._stop.wait(self.interval_seconds)

    def start(self):
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self.outbox.store._check_available()
            self._stop.clear()
            self._failure = None
            self._thread = Thread(target=self._run, name="ocr-rag-delivery", daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("invalid stop timeout")
        with self._lifecycle_lock:
            self._stop.set()
            thread = self._thread
            if thread is None:
                return True
            thread.join(timeout)
            if thread.is_alive():
                return False  # Never claim shutdown while an HTTP attempt is still running.
            self._thread = None
            return True
