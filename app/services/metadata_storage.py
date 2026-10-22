"""Internal coordination of already reserved metadata writes.

The trusted caller holds one artifact lease throughout each call. Validation,
IDs and binding callbacks belong to the caller. Approval callers must already
have validated policy, the verified draft HEAD and canonical evidence. Physical
publication and SQL coordination neither replace prepare_approval nor establish
human presence. No startup recovery or retention integration is performed here.
"""
from __future__ import annotations

from app.application import ApplicationError, Principal
from app.config import settings
from app.metadata_approval import _authorize as _authorize_approval
from app.metadata_approval_contract import ApproveMetadataReceipt
from app.metadata_models import SaveMetadataReceipt
from app.services.metadata_files import MetadataFiles, _absolute
from app.services.metadata_store import (
    MetadataPending, MetadataStore, _approval_receipt, _error, _guard, _hex, _input, _integer,
    _receipt, _require,
)


def _validate_checkpoint(checkpoint):
    if checkpoint is not None and not callable(checkpoint):
        raise ApplicationError(422, "invalid_metadata_input", "Invalid manual metadata input.")


def _checkpoint(checkpoint):
    """Invoke trusted cooperative control, including under the SQL lock.

    Callbacks must be nonblocking and perform no I/O or JobStore lock acquisition.
    They confer no admission authority and are never taken from client input.
    """
    if checkpoint is not None:
        checkpoint()


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

    def _admission_locked(self, conn, principal, job, reservation, *, operation="save-v1"):
        store = self.store
        store._connection_locked(conn)
        _require(operation in ("save-v1", "approve-v1")
                 and reservation.operation == operation)
        approving = operation == "approve-v1"
        if approving:
            _authorize_approval(
                principal, principal.user_id if type(principal) is Principal else None)
        owner, auth_source = store._admit(
            principal, reservation.job_id, write=not approving)
        actual = store._pending(conn, reservation.pending_id)
        if actual is None:
            _raise(_error("metadata_pending_not_found"))
        supplied, stored = vars(reservation), vars(actual)
        _require(supplied.keys() == stored.keys()
                 and all(type(supplied[name]) is type(value)
                         for name, value in stored.items())
                 and actual == reservation and actual.state == "active")
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
        # The store also enforces the draft's one-slot/MiB approval reserve.
        _require((actual.charged_slots, actual.charged_bytes,
                  actual.approval_slots, actual.approval_bytes)
                 == store._charges(head, actual.size_bytes, operation=actual.operation))

    @staticmethod
    def _detach_receipt(receipt, reservation):
        approving = reservation.operation == "approve-v1"
        detached = _approval_receipt(receipt) if approving else _receipt(receipt)
        _input(detached.revision_id == reservation.revision_id
               and detached.sha256 == reservation.sha256)
        if approving:
            _input((detached.reviewed_revision_id, detached.reviewed_sha256)
                   == (reservation.expected_head, reservation.expected_sha256))
        return detached

    @staticmethod
    def _verify_header(snapshot, reservation, receipt):
        _require(snapshot.revision_id == reservation.revision_id
                 and snapshot.job_id == reservation.job_id
                 and snapshot.document_id == reservation.job_id
                 and snapshot.parent_revision_id == reservation.expected_head
                 and snapshot.created_at == reservation.created_at
                 and snapshot.author.actor_subject == reservation.actor
                 and snapshot.author.auth_source == reservation.auth_source
                 and snapshot.author.recorded_at == reservation.created_at
                 and snapshot.author.execution_id is None
                 and receipt.revision_id == reservation.revision_id
                 and receipt.sha256 == reservation.sha256)
        if reservation.operation == "approve-v1":
            approval = snapshot.approval
            _require(snapshot.state == "approved" and approval is not None)
            _require(approval.reviewed_revision_id == reservation.expected_head
                     and approval.reviewed_sha256 == reservation.expected_sha256
                     and approval.actor_subject == reservation.actor
                     and approval.auth_source == reservation.auth_source
                     and approval.approved_at == reservation.created_at
                     and (receipt.reviewed_revision_id, receipt.reviewed_sha256)
                     == (reservation.expected_head, reservation.expected_sha256))
        else:
            _require(reservation.operation == "save-v1"
                     and snapshot.state == "draft" and snapshot.approval is None)
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
    def _reconcile_unclaimed(self, principal, job, reservation, receipt, *,
                             operation="save-v1"):
        """Claim an exact, owned handoff for cleanup without lifecycle admission."""
        store = self.store
        if (type(reservation) is not MetadataPending
                or operation not in ("save-v1", "approve-v1")
                or reservation.operation != operation
                or not _hex(reservation.pending_id, 32)
                or not _hex(reservation.job_id, 32)):
            return
        approving = operation == "approve-v1"
        if approving:
            _authorize_approval(
                principal, principal.user_id if type(principal) is Principal else None)
        owner, auth_source = store._identity(principal, write=not approving)
        detached = self._detach_receipt(receipt, reservation)
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
                       check_bindings_locked, checkpoint=None) -> SaveMetadataReceipt:
        """Caller owns one lease; callbacks provide binding and cooperative checks."""
        return self._write_reserved(
            principal, job, reservation, receipt, canonical_bytes,
            operation="save-v1", check_bindings_locked=check_bindings_locked,
            checkpoint=checkpoint)

    @_guard
    def write_approval_reserved(self, principal, job, reservation: MetadataPending,
                                receipt: ApproveMetadataReceipt, canonical_bytes: bytes, *,
                                check_bindings_locked, checkpoint=None) -> ApproveMetadataReceipt:
        """Publish a trusted approval candidate under the caller's existing lease.

        The caller has validated approval policy, current verified draft HEAD and
        canonical evidence. The callback rechecks bindings and lifecycle authority
        on the final connection immediately before indexing. This layer does not
        replace prepare_approval or establish human presence.
        """
        return self._write_reserved(
            principal, job, reservation, receipt, canonical_bytes,
            operation="approve-v1", check_bindings_locked=check_bindings_locked,
            checkpoint=checkpoint)

    def _write_reserved(self, principal, job, reservation, receipt, canonical_bytes, *,
                        operation, check_bindings_locked, checkpoint=None):
        claimed = False
        attempt = None
        detached = None
        try:
            try:
                _validate_checkpoint(checkpoint)
                _checkpoint(checkpoint)
                _input(type(reservation) is MetadataPending
                       and _hex(reservation.pending_id, 32)
                       and _hex(reservation.job_id, 32))
                with self.store._transaction() as conn:
                    self._admission_locked(
                        conn, principal, job, reservation, operation=operation)
                    detached = self._detach_receipt(receipt, reservation)
                    _input(type(canonical_bytes) is bytes
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
                _checkpoint(checkpoint)
                attempt.publish(canonical_bytes)
                snapshot = self.files.read(
                    job, revision_id=reservation.revision_id,
                    sha256=reservation.sha256, size_bytes=reservation.size_bytes,
                )
                self._verify_header(snapshot, reservation, detached)
                _checkpoint(checkpoint)
                with self.store._transaction() as conn:
                    self._admission_locked(
                        conn, principal, job, reservation, operation=operation)
                    check_bindings_locked(conn)
                    _checkpoint(checkpoint)
                    commit = (self.store._commit_approval_index_locked
                              if operation == "approve-v1"
                              else self.store._commit_index_locked)
                    result = commit(conn, principal, reservation.pending_id, detached)
                return result
            except Exception as exc:
                failure = _sanitized(exc)
                if claimed:
                    self._reconcile(reservation, detached, attempt)
                else:
                    self._reconcile_unclaimed(
                        principal, job, reservation, receipt, operation=operation)
                _raise(failure)
        finally:
            # Covers initial transaction exit as well as diagnosis and release.
            # A refused duplicate never owns, and therefore never clears, this flag.
            if claimed:
                with self.store.jobs._lock:
                    self.store._active_writes.discard(reservation.pending_id)
