"""Manual document metadata saves through an internal Python interface.

DocumentMetadataService is not exposed through HTTP or MCP. Binding preparation
also remains available to trusted callers holding their own admission and lease.

Pass plan.check_locked as MetadataStorage.write_reserved's trusted callback.
The caller must own the shared JobStore lock and write transaction. Lock.locked
can detect an absent lock, but cannot establish which thread owns it.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
from typing import Callable
from uuid import uuid4

from app.application import ApplicationError, Principal
from app.document_models import FieldSpec
from app.metadata_approval import (
    _authorize as _authorize_approval, _input as _approval_input, prepare_approval,
)
from app.metadata_approval_contract import (
    ApproveMetadataReceipt, approval_receipt_for, approval_request_identity,
    validate_approval_receipt,
)
from app.metadata_evidence import assess_snapshot
from app.metadata_models import (
    MetadataSnapshot, PersistentID, ProvenanceRecord, SaveMetadataReceipt,
    SHA256, SchemaSnapshot, SegmentInput, SegmentRecord,
    snapshot_bytes, validate_manual_input,
)
from app.metadata_validation import MetadataValidationError, normalize_manual_values
from app.services.batch_service import request_identity
from app.services.canonical_text_service import CanonicalTextService
from app.services.metadata_canonical import MetadataCanonicalSource
from app.services.metadata_files import MetadataFiles, _absolute
from app.services.metadata_runtime import MetadataServices
from app.services.metadata_storage import (
    MetadataStorage, _checkpoint, _raise, _sanitized, _validate_checkpoint,
)
from app.services.metadata_store import (
    MetadataRevision, MetadataStore, _guard, _hex, _integer, _receipt, _require,
)
from app.services.storage_service import JobStore
from app.services.type_service import TypeService


def _invalid():
    return ApplicationError(422, "invalid_metadata_input", "Invalid manual metadata input.")


def _conflict():
    return ApplicationError(409, "metadata_head_conflict", "The metadata head does not match.")


def _unavailable():
    return ApplicationError(503, "metadata_unavailable", "Stored metadata is unavailable.")


class ResolvedMetadataSegment(SegmentInput):
    """Detached manual entries and binding references, before normalization."""

    segment_id: PersistentID
    context_id: PersistentID
    schema_sha256: SHA256


@dataclass(frozen=True, slots=True)
class MetadataBindingPlan:
    """Immutable decisions; each DTO/map accessor returns a fresh detached view."""

    _ids: tuple[tuple[str, str], ...] = field(repr=False)
    _segments: tuple[bytes, ...] = field(repr=False)
    _schemas: tuple[bytes, ...] = field(repr=False)
    _check: Callable = field(repr=False, compare=False)

    @property
    def segment_ids(self):
        return dict(self._ids)

    @property
    def segments(self):
        return tuple(ResolvedMetadataSegment.model_validate_json(value, strict=True)
                     for value in self._segments)

    @property
    def schemas(self):
        return tuple(SchemaSnapshot.model_validate_json(value, strict=True)
                     for value in self._schemas)

    def check_locked(self, conn):
        """Recheck new associations immediately before the caller's index insert.

        This performs no connection acquisition, locking, or transaction control.
        Existing associations intentionally have no catalog eligibility check.
        """
        return self._check(conn)


class MetadataBindingPreparer:
    @_guard
    def __init__(self, jobs: JobStore, types: TypeService):
        if not isinstance(jobs, JobStore) or not isinstance(types, TypeService):
            raise _unavailable()
        if types.jobs is not jobs:
            raise _unavailable()
        self.jobs = jobs
        self.types = types

    @_guard
    def prepare(self, principal, job_id, page_count, body, head):
        """Resolve bindings against a trusted exact HEAD, outside the SQL lock.

        A retained ID is accepted only from this HEAD. Its embedded schema and
        context remain authoritative for an unchanged type/version/page interval.
        New or changed associations use an owner-scoped published snapshot, and
        must pass the captured eligibility checks again at index commit time.
        """
        jobs, types = self.jobs, self.types
        if types.jobs is not jobs:
            raise _unavailable()
        if not isinstance(principal, Principal):
            raise _invalid()
        if not _hex(job_id, 32) or not _integer(page_count, 1, 300):
            raise _invalid()
        try:
            request = validate_manual_input(body)
        except ValueError:
            raise _invalid() from None

        # Keep authorization used by the callback independent of caller objects.
        actor = Principal(subject=principal.subject, user_id=principal.user_id,
                          scopes=frozenset(principal.scopes), is_admin=principal.is_admin)
        previous = {}
        embedded = {}
        if head is None:
            if request.expected_head is not None or request.expected_sha256 is not None:
                raise _conflict()
        else:
            if not isinstance(head, MetadataSnapshot):
                raise _unavailable()
            try:
                encoded_head = snapshot_bytes(head)
                trusted = MetadataSnapshot.model_validate_json(encoded_head, strict=True)
            except Exception:
                raise _unavailable() from None
            if (trusted.job_id != job_id or trusted.document_id != job_id
                    or trusted.page_count != page_count):
                raise _unavailable()
            if (request.expected_head != trusted.revision_id
                    or request.expected_sha256 != hashlib.sha256(encoded_head).hexdigest()):
                raise _conflict()
            previous = {segment.segment_id: segment for segment in trusted.segments}
            embedded = {schema.schema_sha256: schema for schema in trusted.schemas}

        ids, resolved, schemas, checks = [], [], {}, []
        allocated = set(previous)
        for segment in request.segments:
            if segment.page_end > page_count:
                raise _invalid()
            old = None
            if segment.segment_id is None:
                segment_id = uuid4().hex
                while segment_id in allocated:
                    segment_id = uuid4().hex
            else:
                segment_id = segment.segment_id
                old = previous.get(segment_id)
                if old is None:
                    raise _invalid()
            allocated.add(segment_id)

            unchanged = old is not None and (
                old.type_id, old.type_version, old.page_start, old.page_end
            ) == (
                segment.type_id, segment.type_version, segment.page_start, segment.page_end
            )
            if unchanged:
                # Neither context nor schema hash is client-controlled. Retain
                # both from HEAD, even when the catalog version is deprecated.
                schema = embedded[old.schema_sha256]
                context_id = old.context_id
            else:
                # Public owner/scopes lookup is deliberately outside the lock.
                version = types.get_version(actor, segment.type_id, segment.type_version)
                if version.type_id != segment.type_id or version.version != segment.type_version:
                    raise _unavailable()
                schema = SchemaSnapshot.model_validate(
                    {"schema_sha256": version.schema_sha256, "schema": version.schema_document},
                    strict=True,
                )
                if version.lifecycle.state != "published":
                    raise ApplicationError(409, "type_version_deprecated",
                                           "The type version is deprecated.")
                context_id = version.context_id
                checks.append((segment.type_id, segment.type_version,
                               schema.schema_sha256, context_id))

            existing = schemas.get(schema.schema_sha256)
            if existing is not None and existing != schema:
                raise _unavailable()
            schemas[schema.schema_sha256] = schema
            value = ResolvedMetadataSegment.model_validate({
                **segment.model_dump(mode="python", warnings="error"),
                "segment_id": segment_id, "context_id": context_id,
                "schema_sha256": schema.schema_sha256,
            }, strict=True)
            ids.append((segment.client_key, segment_id))
            resolved.append(value.model_dump_json().encode("utf-8"))

        # Only immutable scalar decisions enter this closure. It never consults
        # request, HEAD, returned DTOs/maps, or the preparer's mutable attributes.
        decisions = tuple(checks)

        @_guard
        def check_locked(conn):
            if (types.jobs is not jobs or conn is None
                    or conn is not jobs._conn
                    or not jobs._lock.locked()
                    or conn.in_transaction is not True):
                raise _unavailable()
            jobs._check_available()
            for type_id, version, schema_sha256, context_id in decisions:
                current = types._binding_version(conn, actor, type_id, version, schema_sha256)
                if (current.type_id != type_id or current.version != version
                        or current.schema_sha256 != schema_sha256
                        or current.context_id != context_id):
                    raise _unavailable()

        return MetadataBindingPlan(
            tuple(ids), tuple(resolved),
            tuple(schema.model_dump_json(by_alias=True).encode("utf-8")
                  for schema in schemas.values()),
            check_locked,
        )


class DocumentMetadataService:
    """Save manual drafts using an existing, initialized shared runtime only."""

    @_guard
    def __init__(self, bundle: MetadataServices, types: TypeService):
        _require(isinstance(bundle, MetadataServices)
                 and isinstance(types, TypeService))
        _require(isinstance(bundle.index, MetadataStore)
                 and isinstance(bundle.files, MetadataFiles)
                 and isinstance(bundle.storage, MetadataStorage)
                 and isinstance(bundle.jobs, JobStore))
        self.bundle = bundle
        self.types = types
        self.canonical = CanonicalTextService(bundle.jobs)
        self._shared = (bundle, bundle.index, bundle.files, bundle.storage,
                        bundle.jobs, types, self.canonical)
        self._check_shared()

    def _check_shared(self):
        bundle, index, files, storage, jobs, types, canonical = self._shared
        _require(self.bundle is bundle and self.types is types
                 and self.canonical is canonical
                 and bundle.index is index and bundle.files is files
                 and bundle.storage is storage and bundle.jobs is jobs
                 and index.jobs is jobs and types.jobs is jobs
                 and storage.store is index and storage.files is files
                 and canonical.store is jobs)
        _require(files._root == _absolute(jobs.root_dir))

    @contextmanager
    def open_snapshot(self, principal, job_id, revision_id, *, sha256, checkpoint=None):
        """Yield a detached verified revision while retaining one artifact lease.

        Checkpoints are trusted, nonblocking internal callbacks. They must perform
        no I/O or JobStore lock acquisition. Admission is not repeated after I/O:
        an admitted read may finish despite a retention change. Caller exceptions
        are outside the setup error boundary and propagate unchanged.
        """
        with ExitStack() as stack:
            try:
                _validate_checkpoint(checkpoint)
                self._check_shared()
                bundle = self.bundle
                bundle.ensure_healthy()
                index, files, jobs = bundle.index, bundle.files, bundle.jobs
                index._identity(principal, write=False)
                if not (_hex(job_id, 32) and _hex(revision_id, 32) and _hex(sha256, 64)):
                    raise _invalid()

                with index._transaction():
                    owner, _ = index._admit(principal, job_id, write=False)

                job = stack.enter_context(jobs.artifact_lease(job_id, owner))
                with index._transaction() as conn:
                    current_owner, _ = index._admit(principal, job_id, write=False)
                    _require(current_owner == owner and job.job_id == job_id
                             and job.effective_owner == owner)
                    revision = index._revision(conn, revision_id)
                    if (revision is None or revision.owner != owner
                            or revision.job_id != job_id):
                        raise ApplicationError(404, "metadata_revision_not_found",
                                               "Metadata revision not found.")
                    if revision.sha256 != sha256:
                        raise ApplicationError(409, "metadata_revision_conflict",
                                               "The metadata revision does not match.")
                    revision = replace(revision)

                _checkpoint(checkpoint)
                snapshot = files.read(
                    job, revision_id=revision.revision_id,
                    sha256=revision.sha256, size_bytes=revision.size_bytes)
                encoded = snapshot_bytes(snapshot)
                detached = MetadataSnapshot.model_validate_json(encoded, strict=True)
                _require(
                    hashlib.sha256(encoded).hexdigest() == revision.sha256
                    and len(encoded) == revision.size_bytes
                    and detached.revision_id == revision.revision_id == revision_id
                    and detached.job_id == detached.document_id == job_id
                    and detached.parent_revision_id == revision.parent_revision_id
                    and detached.state == revision.state
                    and detached.created_at == revision.created_at
                    and detached.author.actor_subject == revision.owner)
                _checkpoint(checkpoint)
            except Exception as error:
                _raise(_sanitized(error))
            _checkpoint_before_yield = checkpoint
            # Keep setup failures sanitized without catching the caller's block.
            try:
                _checkpoint(_checkpoint_before_yield)
            except Exception as error:
                _raise(_sanitized(error))
            yield detached

    @_guard
    def get(self, principal, job_id, revision_id, *, sha256, checkpoint=None) -> MetadataSnapshot:
        """Return one detached revision after releasing its artifact lease."""
        with self.open_snapshot(
                principal, job_id, revision_id, sha256=sha256,
                checkpoint=checkpoint) as snapshot:
            return snapshot

    @_guard
    def list_revisions(self, principal, job_id) -> tuple[MetadataRevision, ...]:
        """Return detached, bounded SQL summaries without reading artifacts."""
        self._check_shared()
        self.bundle.ensure_healthy()
        return tuple(replace(revision) for revision in
                     self.bundle.index.list_revisions(principal, job_id))

    @_guard
    def approve(self, principal, job_id, body, *, key, checkpoint=None) -> tuple[ApproveMetadataReceipt, bool]:
        """Approve the verified current draft under one external artifact lease."""
        _validate_checkpoint(checkpoint)
        self._check_shared()
        bundle = self.bundle
        bundle.ensure_healthy()
        index, files, storage, jobs = (
            bundle.index, bundle.files, bundle.storage, bundle.jobs)
        request = _approval_input(body)
        key_hash, request_hash = approval_request_identity(key, request)
        admission = index.preflight_approval(
            principal, job_id, key_hash=key_hash, request_hash=request_hash)
        if admission.receipt is not None:
            return validate_approval_receipt(admission.receipt), True
        index._cas(admission.head, request.expected_head, request.expected_sha256)

        _checkpoint(checkpoint)
        with self.canonical.open(principal, job_id) as prepared:
            manifest = prepared.manifest
            with index._transaction() as conn:
                owner, auth_source = index._admit(principal, job_id, write=False)
                current = index._head(conn, owner, job_id)
                index._cas(current, request.expected_head, request.expected_sha256)
                _require(current is not None)
                job = jobs.jobs.get(job_id)
                _require(job is not None and job.job_id == job_id
                         and job.effective_owner == owner)
                owner_id = job.owner_user_id
                _authorize_approval(principal, owner_id)
                _require(owner == principal.subject
                         and auth_source == principal.auth_source)
                revision = index._revision(conn, current.revision_id)
                _require(revision is not None
                         and revision.owner == owner and revision.job_id == job_id
                         and revision.revision_id == current.revision_id
                         and revision.sha256 == current.sha256
                         and revision.state in ("draft", "approved"))
                revision = replace(revision)
                job_identity = (job.job_id, job.effective_owner, job.owner_user_id,
                                job.parent_job_id, job.working_dir)

            # The copied index decision is verified against physical bytes outside
            # the SQL lock. No public get(), second lease, or catalog lookup occurs.
            head = files.read(
                job, revision_id=revision.revision_id,
                sha256=revision.sha256, size_bytes=revision.size_bytes)
            encoded_head = snapshot_bytes(head)
            head = MetadataSnapshot.model_validate_json(encoded_head, strict=True)
            _require(hashlib.sha256(encoded_head).hexdigest() == revision.sha256
                     and len(encoded_head) == revision.size_bytes
                     and head.revision_id == revision.revision_id
                     and head.parent_revision_id == revision.parent_revision_id
                     and head.created_at == revision.created_at
                     and head.state == revision.state
                     and head.author.actor_subject == owner)
            _require(
                head.job_id == manifest["job_id"] == job_id
                and head.document_id == manifest["document_id"] == job_id
                and head.artifact_version == manifest["artifact_version"]
                and head.text_version == manifest["text_version"]
                and head.canonical_sha256 == manifest["sha256"]
                and head.input_pdf_sha256 == manifest["input_pdf_sha256"]
                and head.output_pdf_sha256 == manifest["output_pdf_sha256"]
                and head.page_count == manifest["page_count"]
                and head.parent_job_id == manifest.get("parent_job_id")
                and head.parent_job_id == job_identity[3])
            manifest_job_identity = (manifest["job_id"], manifest.get("parent_job_id"))
            _checkpoint(checkpoint)

            source = MetadataCanonicalSource(prepared)
            source_errors = []

            def page_source(number):
                try:
                    _checkpoint(checkpoint)
                    return source(number)
                except ApplicationError as error:
                    source_errors.append(error)
                    raise

            try:
                candidate = prepare_approval(
                    principal, owner_id, head, request, page_source)
            except ApplicationError:
                if source_errors:
                    raise source_errors[0] from None
                raise
            canonical_bytes = snapshot_bytes(candidate)
            receipt = approval_receipt_for(candidate)

            def check_approval_locked(conn):
                # This callback has authority only over the captured draft and
                # admitted job. It acquires no lock and performs no artifact I/O.
                self._check_shared()
                index._connection_locked(conn)
                _authorize_approval(principal, owner_id)
                current_owner, current_auth = index._admit(
                    principal, job_id, write=False)
                _require((current_owner, current_auth) == (owner, auth_source))
                cached = jobs.jobs.get(job_id)
                _require(cached is job
                         and (cached.job_id, cached.effective_owner, cached.owner_user_id,
                              cached.parent_job_id, cached.working_dir) == job_identity
                         and type(cached.owner_user_id) is int
                         and cached.owner_user_id == owner_id
                         and (cached.job_id, cached.parent_job_id) == manifest_job_identity)
                current_head = index._head(conn, owner, job_id)
                index._cas(current_head, revision.revision_id, revision.sha256)
                _require(current_head is not None
                         and current_head.revision_id == revision.revision_id
                         and current_head.sha256 == revision.sha256)
                current_revision = index._revision(conn, revision.revision_id)
                _require(current_revision == revision and revision.state == "draft")

            _checkpoint(checkpoint)
            reserved = index.reserve_approval(
                principal, job_id, key_hash=key_hash, request_hash=request_hash,
                expected_head=request.expected_head,
                expected_sha256=request.expected_sha256,
                revision_id=candidate.revision_id, sha256=receipt.sha256,
                size_bytes=len(canonical_bytes), created_at=candidate.created_at)
            if reserved.receipt is not None:
                return validate_approval_receipt(reserved.receipt), True
            result = storage.write_approval_reserved(
                principal, job, reserved.pending, receipt, canonical_bytes,
                check_bindings_locked=check_approval_locked,
                **({"checkpoint": checkpoint} if checkpoint is not None else {}))
            return validate_approval_receipt(result), False

    @staticmethod
    def _validation_error(error):
        return (_unavailable() if error.code == "canonical_source_invalid"
                else _invalid())

    @_guard
    def save(self, principal, job_id, body, *, key, checkpoint=None) -> tuple[SaveMetadataReceipt, bool]:
        """Return a detached receipt and whether this request was replayed."""
        _validate_checkpoint(checkpoint)
        self._check_shared()
        bundle = self.bundle
        bundle.ensure_healthy()
        index, files, storage, jobs = (
            bundle.index, bundle.files, bundle.storage, bundle.jobs)
        try:
            request = validate_manual_input(body)
        except ValueError:
            raise _invalid() from None
        key_hash, request_hash = request_identity(key, {
            "operation": "document-metadata-save/1",
            "job_id": job_id,
            "input": request.model_dump(mode="json", by_alias=True, warnings="error"),
        })
        admission = index.preflight(
            principal, job_id, key_hash=key_hash, request_hash=request_hash)
        if admission.receipt is not None:
            return _receipt(admission.receipt), True
        index._cas(admission.head, request.expected_head, request.expected_sha256)

        # This is the sole lease; it remains open through publication and cleanup.
        _checkpoint(checkpoint)
        with self.canonical.open(principal, job_id) as prepared:
            manifest = prepared.manifest
            with index._transaction() as conn:
                owner, auth_source = index._admit(principal, job_id, write=True)
                current = index._head(conn, owner, job_id)
                index._cas(current, request.expected_head, request.expected_sha256)
                job = jobs.jobs.get(job_id)
                _require(job is not None and job.job_id == job_id
                         and job.effective_owner == owner)
                revision = None
                if current is not None:
                    revision = index._revision(conn, current.revision_id)
                    _require(revision is not None
                             and revision.owner == owner and revision.job_id == job_id
                             and revision.revision_id == current.revision_id
                             and revision.sha256 == current.sha256
                             and revision.state in ("draft", "approved"))

            # Filesystem reads and serialization always run outside the SQL lock.
            head = None
            if revision is not None:
                head = files.read(
                    job, revision_id=revision.revision_id,
                    sha256=revision.sha256, size_bytes=revision.size_bytes)
                try:
                    encoded_head = snapshot_bytes(head)
                    head = MetadataSnapshot.model_validate_json(encoded_head, strict=True)
                except Exception:
                    raise _unavailable() from None
                _require(hashlib.sha256(encoded_head).hexdigest() == revision.sha256
                         and len(encoded_head) == revision.size_bytes
                         and head.revision_id == revision.revision_id
                         and head.parent_revision_id == revision.parent_revision_id
                         and head.created_at == revision.created_at
                         and head.state == revision.state)
                _require(
                    head.job_id == manifest["job_id"] == job_id
                    and head.document_id == manifest["document_id"] == job_id
                    and head.artifact_version == manifest["artifact_version"]
                    and head.text_version == manifest["text_version"]
                    and head.canonical_sha256 == manifest["sha256"]
                    and head.input_pdf_sha256 == manifest["input_pdf_sha256"]
                    and head.output_pdf_sha256 == manifest["output_pdf_sha256"]
                    and head.page_count == manifest["page_count"]
                    and head.parent_job_id == manifest.get("parent_job_id"))

            if revision is not None:
                _checkpoint(checkpoint)
            plan = MetadataBindingPreparer(jobs, self.types).prepare(
                principal, job_id, manifest["page_count"], request, head)
            source = MetadataCanonicalSource(prepared)
            source_errors = []

            def page_source(number):
                # assess_snapshot sanitizes arbitrary callback failures. Preserve
                # framework storage/integrity errors for the outer guard instead.
                try:
                    _checkpoint(checkpoint)
                    return source(number)
                except ApplicationError as error:
                    source_errors.append(error)
                    raise

            created_at = datetime.now(timezone.utc)
            revision_id = uuid4().hex
            try:
                # _admit supplies the principal's actual admitted auth source,
                # including the existing restrictions on service principals.
                provenance = ProvenanceRecord(
                    actor_subject=principal.subject, auth_source=auth_source,
                    recorded_at=created_at, execution_id=None)
                schemas = list(plan.schemas)
                fields_by_hash = {
                    schema.schema_sha256: [
                        FieldSpec.model_validate(value, strict=True)
                        for value in schema.schema_document["fields"]
                    ] for schema in schemas
                }
                segments = []
                for segment in plan.segments:
                    records = normalize_manual_values(
                        fields_by_hash[segment.schema_sha256], segment.fields, provenance)
                    segments.append(SegmentRecord(
                        segment_id=segment.segment_id,
                        page_start=segment.page_start, page_end=segment.page_end,
                        context_id=segment.context_id, type_id=segment.type_id,
                        type_version=segment.type_version,
                        schema_sha256=segment.schema_sha256, fields=records))
                snapshot = MetadataSnapshot(
                    revision_id=revision_id,
                    parent_revision_id=request.expected_head,
                    job_id=manifest["job_id"], document_id=manifest["document_id"],
                    artifact_version=manifest["artifact_version"],
                    text_version=manifest["text_version"],
                    canonical_sha256=manifest["sha256"],
                    input_pdf_sha256=manifest["input_pdf_sha256"],
                    output_pdf_sha256=manifest["output_pdf_sha256"],
                    page_count=manifest["page_count"],
                    parent_job_id=manifest.get("parent_job_id"),
                    created_at=created_at, author=provenance,
                    schemas=schemas, segments=segments, state="draft", approval=None)
                # Readiness blockers are allowed for drafts; invalid quotes are not.
                assess_snapshot(snapshot, page_source)
                canonical_bytes = snapshot_bytes(snapshot)
                sha256 = hashlib.sha256(canonical_bytes).hexdigest()
                receipt = SaveMetadataReceipt(
                    revision_id=revision_id, sha256=sha256, segment_ids=plan.segment_ids)
            except MetadataValidationError as error:
                if source_errors:
                    raise source_errors[0] from None
                raise self._validation_error(error) from None
            except ValueError:
                raise _invalid() from None

            check_bindings_locked = plan.check_locked
            _checkpoint(checkpoint)
            reserved = index.reserve(
                principal, job_id, key_hash=key_hash, request_hash=request_hash,
                expected_head=request.expected_head,
                expected_sha256=request.expected_sha256,
                revision_id=revision_id, sha256=sha256,
                size_bytes=len(canonical_bytes), created_at=created_at)
            if reserved.receipt is not None:
                return _receipt(reserved.receipt), True
            return storage.write_reserved(
                principal, job, reserved.pending, receipt, canonical_bytes,
                check_bindings_locked=check_bindings_locked,
                **({"checkpoint": checkpoint} if checkpoint is not None else {})), False
