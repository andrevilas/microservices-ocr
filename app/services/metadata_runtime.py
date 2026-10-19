"""Lazy metadata services bound to the injected application's actual JobStore.

Connection health is not namespace readiness: startup recovery is still required.
Health checks acquire the nonreentrant jobs lock and must be called outside it.
MetadataStore._transaction remains the authority for transaction ownership.
Import and provider construction perform no I/O or application lookup.
"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from fastapi import Request

from app.services.metadata_files import MetadataFiles
from app.services.metadata_recovery import MetadataRecovery
from app.services.metadata_recovery_files import MetadataRecoveryFiles
from app.services.metadata_storage import MetadataStorage
from app.services.metadata_store import MetadataStore, _error, _guard, _require
from app.services.storage_service import JobStore


@_guard
def _assert_metadata_health_locked(jobs: JobStore) -> None:
    """Observe without repair. Caller MUST hold the nonreentrant jobs._lock.

    Never call this helper without that lock. Unsafe or uncertain observations
    quarantine the store; only a fresh JobStore can restore availability.
    """
    _require(isinstance(jobs, JobStore))
    try:
        jobs._check_available()
        conn = jobs._conn
        # Check absence first: _get_conn must never initialize a connection.
        _require(conn is not None)
        _require(conn is jobs._get_conn())
        _require(conn.in_transaction is False)
    except (KeyboardInterrupt, SystemExit):
        jobs._quarantine_locked()
        raise
    except Exception:
        jobs._quarantine_locked()
        raise _error("metadata_unavailable") from None


@_guard
def assert_metadata_health(jobs: JobStore) -> None:
    """Observe connection health without repair; call outside jobs._lock.

    This acquires the nonreentrant lock and does not establish namespace readiness.
    """
    _require(isinstance(jobs, JobStore))
    with jobs._lock:
        _assert_metadata_health_locked(jobs)


@dataclass(frozen=True)
class MetadataServices:
    index: MetadataStore
    files: MetadataFiles
    storage: MetadataStorage
    recovery: MetadataRecovery

    @property
    def jobs(self) -> JobStore:
        return self.index.jobs

    def ensure_healthy(self) -> None:
        """Check connection health outside jobs._lock; not namespace readiness."""
        assert_metadata_health(self.jobs)


class MetadataProvider:
    def __init__(self, job_application_provider):
        self._applications = job_application_provider
        # Independent of the nonreentrant lock acquired by service constructors.
        self._lock = Lock()
        self._store: JobStore | None = None
        self._services: MetadataServices | None = None
        # Bundles retain their JobStore, so identity keys cannot be reused during
        # this provider's lifetime. Returning to a store preserves active claims.
        self._services_by_store: dict[int, MetadataServices] = {}

    @_guard
    def __call__(self) -> MetadataServices:
        try:
            # Application instances may change while their JobStore stays shared.
            # Never invoke this callback while holding jobs._lock.
            jobs = self._applications().store
            _require(isinstance(jobs, JobStore))
            with self._lock:
                # Also gate cached lookups, and precede any schema initialization.
                assert_metadata_health(jobs)
                services = self._services_by_store.get(id(jobs))
                if services is None:
                    index = MetadataStore(jobs)
                    physical = MetadataRecoveryFiles(jobs.root_dir)
                    # The recovery adapter constructs MetadataFiles. Reuse that
                    # exact instance for storage and the exposed bundle.
                    files = physical._files
                    storage = MetadataStorage(index, files)
                    recovery = MetadataRecovery(index, physical)
                    services = MetadataServices(index, files, storage, recovery)
                    assert_metadata_health(jobs)
                    # Publish only fully constructed, healthy bundles.
                    self._services_by_store[id(jobs)] = services
                # A failed switch leaves the previous current bundle intact.
                self._services = services
                self._store = jobs
                return services
        except Exception:
            # The guard clears implicit exception context on this fixed error.
            # Control exceptions propagate unchanged.
            raise _error("metadata_unavailable") from None


def get_metadata_services(request: Request) -> MetadataServices:
    return request.app.state.metadata_provider()
