"""Single runtime-owned periodic collector; no scheduler starts on import."""
import logging
import re
from threading import Event, Thread
from app.config import settings
from app.services.artifact_retention import safe_rmtree

logger = logging.getLogger(__name__)


class RetentionWorker:
    def __init__(self, uploads, exports):
        self.uploads, self.exports, self.jobs = uploads, exports, uploads.jobs
        self._stop = Event()
        self._thread = None

    def reconcile_startup(self):
        # Only stale legacy staging directories; this runs under RuntimeLease,
        # before accepting requests. Never sweep these while a request can use one.
        for path in self.jobs.root_dir.iterdir():
            if re.fullmatch(r"legacy-batch-[a-z0-9_]{8}", path.name):
                try:
                    safe_rmtree(self.jobs.root_dir, path, path.name)
                except OSError:
                    logger.warning("Legacy staging cleanup deferred")
        self.collect()

    def collect(self):
        # Isolate failures so one blocked directory/category cannot disable others.
        for action in (self.jobs.cleanup_expired, self._uploads, self._exports):
            try:
                action()
            except Exception:
                logger.warning("Retention collection deferred (%s)", action.__name__)

    def _uploads(self):
        with self.jobs._lock, self.jobs._get_conn():
            self.uploads._expire()

    def _exports(self):
        with self.jobs._lock:
            self.exports._expire()

    def start(self):
        if self._thread is not None:
            return
        self.reconcile_startup()
        self._stop.clear()
        self._thread = Thread(target=self._run, name="ocr-retention", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(settings.retention_interval_seconds):
            self.collect()

    def stop(self):
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(settings.worker_shutdown_timeout_seconds)
        if self._thread.is_alive():
            raise RuntimeError("Retention worker did not stop before the deadline.")
        self._thread = None
