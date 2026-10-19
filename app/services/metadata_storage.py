"""Internal coordination of an already reserved manual metadata write.

The trusted caller holds one artifact lease throughout this call. This is not a
save API: validation, IDs and the binding callback belong to the caller. No
recovery, catalog lookup, approval or retention integration is performed here.
"""
from __future__ import annotations

from app.application import ApplicationError
from app.config import settings
from app.metadata_models import SaveMetadataReceipt
from app.services.metadata_files import MetadataFiles, _absolute
from app.services.metadata_store import (
    MetadataPending, MetadataStore, _error, _guard, _hex, _input, _integer,
    _receipt, _require,
)


def _raise(error):
    try:
        raise error from None
    finally:
        error.__context__ = None
        error.__cause__ = None


def _sanitized(error):
    if isinstance(error, ApplicationError):
        return ApplicationError(error.status, error.code, error.message)
    return _error("metadata_unavailable")


class MetadataStorage:
    """Use the same store instance for writers and any future reconciliation."""

    @_guard
    def __init__(self, store: MetadataStore, files: MetadataFiles):
        _require(isinstance(store, MetadataStore) and isinstance(files, MetadataFiles))
        _require(files._root == _absolute(store.jobs.root_dir))
        self.store = store
        self.files = files

    def _admission_locked(self, conn, principal, job, reservation):
        store = self.store
        store._connection_locked(conn)
        _require(reservation.operation == "save-v1")
        owner, auth_source = store._admit(principal, reservation.job_id, write=True)
        actual = store._pending(conn, reservation.pending_id)
        if actual is None:
            _raise(_error("metadata_pending_not_found"))
        _require(actual == reservation and actual.state == "active")
        _require(actual.owner == owner and actual.actor == owner
                 and actual.auth_source == auth_source)
        cached = store.jobs.jobs.get(actual.job_id)
        _require(cached is not None and job.job_id == actual.job_id
                 and job.effective_owner == owner and cached.effective_owner == owner
                 and job.working_dir == cached.working_dir
                 and job.parent_job_id == cached.parent_job_id)
        _require(self.files._root == _absolute(store.jobs.root_dir))
        head = store._head(conn, owner, actual.job_id)
        store._cas(head, actual.expected_head, actual.expected_sha256)
        _require((actual.charged_slots, actual.charged_bytes,
                  actual.approval_slots, actual.approval_bytes)
                 == store._charges(head, actual.size_bytes, operation=actual.operation))

    @staticmethod
    def _verify_header(snapshot, reservation, receipt):
        _require(snapshot.state == "draft" and snapshot.approval is None
                 and snapshot.revision_id == reservation.revision_id
                 and snapshot.job_id == reservation.job_id
                 and snapshot.document_id == reservation.job_id
                 and snapshot.parent_revision_id == reservation.expected_head
                 and snapshot.created_at == reservation.created_at
                 and snapshot.author.actor_subject == reservation.actor
                 and snapshot.author.auth_source == reservation.auth_source
                 and snapshot.author.recorded_at == reservation.created_at
                 and snapshot.author.execution_id is None)
        _require(set(receipt.segment_ids.values())
                 == {segment.segment_id for segment in snapshot.segments})

    def _reconcile(self, reservation, receipt, attempt, *, quarantine_sql_failure=False):
        # A successful NEW transaction, including its commit, is essential.
        # Lifecycle admission must not obscure a committed index here.
        try:
            with self.store._transaction() as conn:
                outcome = self.store._index_outcome_locked(conn, reservation, receipt)
                if outcome == "uncommitted":
                    self.store._mark_cleanup_locked(conn, reservation.pending_id)
        except Exception:
            if quarantine_sql_failure:
                with self.store.jobs._lock:
                    self.store.jobs._quarantine_locked()
            _raise(_error("metadata_unavailable"))
        if outcome == "confirmed":
            return
        if outcome != "uncommitted" or attempt is None:
            if quarantine_sql_failure and outcome != "uncommitted":
                with self.store.jobs._lock:
                    self.store.jobs._quarantine_locked()
            _raise(_error("metadata_unavailable"))
        try:
            attempt.cleanup()
            attempt.verify_absent()
        except Exception:
            _raise(_error("metadata_unavailable"))
        try:
            with self.store._transaction() as conn:
                _require(self.store._index_outcome_locked(conn, reservation, receipt)
                         == "uncommitted")
                self.store._release_pending_after_cleanup_locked(conn, reservation.pending_id)
        except Exception:
            if quarantine_sql_failure:
                with self.store.jobs._lock:
                    self.store.jobs._quarantine_locked()
            _raise(_error("metadata_unavailable"))

    @_guard
    def _reconcile_unclaimed(self, principal, job, reservation, receipt):
        """Claim an exact, owned handoff for cleanup without lifecycle admission."""
        store = self.store
        if (type(reservation) is not MetadataPending
                or reservation.operation != "save-v1"
                or not _hex(reservation.pending_id, 32)
                or not _hex(reservation.job_id, 32)):
            return
        owner, auth_source = store._identity(principal, write=True)
        detached = _receipt(receipt)
        _require(self.files._root == _absolute(store.jobs.root_dir))
        claimed = False
        attempt = None
        try:
            try:
                with store._transaction() as conn:
                    store._connection_locked(conn)
                    # A duplicate owns neither cleanup nor removal of this flag.
                    if reservation.pending_id in store._active_writes:
                        return
                    actual = store._pending(conn, reservation.pending_id)
                    if actual is None:
                        return
                    supplied, stored = vars(reservation), vars(actual)
                    if (supplied.keys() != stored.keys()
                            or any(type(supplied[name]) is not type(value)
                                   for name, value in stored.items())
                            or actual != reservation or actual.state != "active"
                            or (actual.owner, actual.actor, actual.auth_source)
                            != (owner, owner, auth_source)
                            or detached.revision_id != actual.revision_id
                            or detached.sha256 != actual.sha256):
                        return
                    cached = store.jobs.jobs.get(actual.job_id)
                    if not (cached is not None and job.job_id == actual.job_id
                            and job.effective_owner == owner
                            and cached.effective_owner == owner
                            and job.working_dir == cached.working_dir
                            and job.parent_job_id == cached.parent_job_id):
                        return
                    _require(self.files._root == _absolute(store.jobs.root_dir))
                    outcome = store._index_outcome_locked(conn, actual, detached)
                    if outcome == "confirmed":
                        return
                    _require(outcome == "uncommitted")
                    store._active_writes.add(actual.pending_id)
                    claimed = True
            except Exception:
                # An uncertain diagnostic transaction cannot authorize file I/O.
                with store.jobs._lock:
                    store.jobs._quarantine_locked()
                _raise(_error("metadata_unavailable"))
            try:
                # Preparation only derives the exact pending-owned names. Retain
                # its result before any cleanup or physical absence verification.
                attempt = self.files.prepare(
                    job, pending_id=actual.pending_id,
                    revision_id=actual.revision_id, sha256=actual.sha256,
                    size_bytes=actual.size_bytes,
                )
            except Exception:
                # Reconciliation can still durably mark cleanup, retaining all
                # charges when physical absence cannot be established.
                pass
            self._reconcile(actual, detached, attempt, quarantine_sql_failure=True)
        finally:
            if claimed:
                with store.jobs._lock:
                    store._active_writes.discard(reservation.pending_id)

    @_guard
    def write_reserved(self, principal, job, reservation: MetadataPending,
                       receipt: SaveMetadataReceipt, canonical_bytes: bytes, *,
                       check_bindings_locked) -> SaveMetadataReceipt:
        """Caller already owns one artifact lease and supplies a trusted callback.

        The callback runs on the final transaction's connection immediately before
        the index helper. Its presence alone makes no claim of C4 validation.
        """
        claimed = False
        attempt = None
        detached = None
        try:
            try:
                _input(type(reservation) is MetadataPending
                       and _hex(reservation.pending_id, 32)
                       and _hex(reservation.job_id, 32))
                with self.store._transaction() as conn:
                    self._admission_locked(conn, principal, job, reservation)
                    detached = _receipt(receipt)
                    _input(detached.revision_id == reservation.revision_id
                           and detached.sha256 == reservation.sha256
                           and type(canonical_bytes) is bytes
                           and callable(check_bindings_locked))
                    if reservation.pending_id in self.store._active_writes:
                        _raise(_error("metadata_write_pending"))
                    self.store._active_writes.add(reservation.pending_id)
                    claimed = True
                    # Retain the attempt before any publication is possible.
                    attempt = self.files.prepare(
                        job, pending_id=reservation.pending_id,
                        revision_id=reservation.revision_id, sha256=reservation.sha256,
                        size_bytes=reservation.size_bytes,
                    )
                    reserved = self.store._reserved_disk_bytes_locked(
                        conn, reservation.owner, reservation.job_id)
                    minimum = settings.upload_min_free_mb
                    _require(_integer(minimum, 0, 2**63 - 1))
                    if attempt.available_bytes() < reserved + minimum * 1024 * 1024:
                        _raise(ApplicationError(503, "storage_unavailable",
                                                "Insufficient free disk space."))
                # No SQL lock during publication, hashing, verified read or fsync.
                attempt.publish(canonical_bytes)
                snapshot = self.files.read(
                    job, revision_id=reservation.revision_id,
                    sha256=reservation.sha256, size_bytes=reservation.size_bytes,
                )
                self._verify_header(snapshot, reservation, detached)
                with self.store._transaction() as conn:
                    self._admission_locked(conn, principal, job, reservation)
                    check_bindings_locked(conn)
                    result = self.store._commit_index_locked(
                        conn, principal, reservation.pending_id, detached)
                return result
            except Exception as exc:
                failure = _sanitized(exc)
                if claimed:
                    self._reconcile(reservation, detached, attempt)
                else:
                    self._reconcile_unclaimed(principal, job, reservation, receipt)
                _raise(failure)
        finally:
            # Covers initial transaction exit as well as diagnosis and release.
            # A refused duplicate never owns, and therefore never clears, this flag.
            if claimed:
                with self.store.jobs._lock:
                    self.store._active_writes.discard(reservation.pending_id)
