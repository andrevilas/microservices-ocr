"""Single runtime-owned periodic collector; no scheduler starts on import."""
import logging
import re
from threading import Event, Thread
from app.application import ApplicationError
from app.config import settings
from app.services.artifact_retention import safe_rmtree
from app.services.metadata_runtime import (
    MetadataServices,
    _assert_metadata_health_locked,
    _observe_health_on_control,
    assert_metadata_health,
)

logger = logging.getLogger(__name__)


class RetentionWorker:
    def __init__(self, uploads, exports, metadata=None):
        self.uploads, self.exports, self.jobs = uploads, exports, uploads.jobs
        if metadata is not None:
            if not isinstance(metadata, MetadataServices):
                raise TypeError("metadata must be MetadataServices.")
            if metadata.jobs is not self.jobs or exports.jobs is not self.jobs:
                raise ValueError("Retention services must share the same JobStore.")
        self.metadata = metadata
        self._stop = Event()
        self._thread = None

    def reconcile_startup(self):
        # Only stale legacy staging directories; this runs under RuntimeLease,
        # before accepting requests. Namespace recovery belongs to the lifespan.
        assert_metadata_health(self.jobs)
        for path in self.jobs.root_dir.iterdir():
            if re.fullmatch(r"legacy-batch-[a-z0-9_]{8}", path.name):
                with self.jobs._lock:
                    _assert_metadata_health_locked(self.jobs)
                    try:
                        safe_rmtree(self.jobs.root_dir, path, path.name)
                    except (KeyboardInterrupt, SystemExit):
                        _observe_health_on_control(self.jobs, locked=True)
                        raise
                    except OSError:
                        _assert_metadata_health_locked(self.jobs)
                        logger.warning("Legacy staging cleanup deferred")
                    else:
                        _assert_metadata_health_locked(self.jobs)
        self.collect()

    def collect(self):
        # A category failure is isolated only while SQL remains provably safe.
        assert_metadata_health(self.jobs)
        actions = (self.jobs.cleanup_expired, self._uploads, self._exports)
        if self.metadata is not None:
            actions = (self.metadata.recovery.collect_cleanup, *actions)
        for action in actions:
            assert_metadata_health(self.jobs)
            try:
                action()
            except (KeyboardInterrupt, SystemExit):
                _observe_health_on_control(self.jobs)
                raise
            except Exception:
                assert_metadata_health(self.jobs)
                logger.warning("Retention collection deferred")
            else:
                assert_metadata_health(self.jobs)

    def _uploads(self):
        with self.jobs._lock:
            # Check under the same lock, before entering a SQL context that could
            # otherwise commit or roll back somebody else's transaction.
            _assert_metadata_health_locked(self.jobs)
            try:
                with self.jobs._get_conn():
                    self.uploads._expire()
            except Exception:
                _assert_metadata_health_locked(self.jobs)
                raise
            _assert_metadata_health_locked(self.jobs)

    def _exports(self):
        with self.jobs._lock:
            _assert_metadata_health_locked(self.jobs)
            try:
                self.exports._expire()
            except (KeyboardInterrupt, SystemExit):
                _observe_health_on_control(self.jobs, locked=True)
                raise
            except Exception:
                _assert_metadata_health_locked(self.jobs)
                raise
            _assert_metadata_health_locked(self.jobs)

    def start(self):
        assert_metadata_health(self.jobs)
        if self._thread is not None:
            return
        self.reconcile_startup()
        self._stop.clear()
        self._thread = Thread(target=self._run, name="ocr-retention", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(settings.retention_interval_seconds):
            try:
                self.collect()
            except ApplicationError:
                logger.warning("Retention worker stopped: metadata unavailable")
                return

    def stop(self):
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(settings.worker_shutdown_timeout_seconds)
        if self._thread.is_alive():
            raise RuntimeError("Retention worker did not stop before the deadline.")
        self._thread = None
