"""Internal C4b binding preparation, not a document metadata save API.

The future DocumentMetadataService must authenticate, hold an artifact lease,
preflight replay, and read the exact HEAD through its index and verified files
before calling prepare. This module does not implement those prerequisites,
normalization, replay, persistence, or public save/get/list operations.

Pass plan.check_locked as MetadataStorage.write_reserved's trusted callback.
The caller must own the shared JobStore lock and write transaction. Lock.locked
can detect an absent lock, but cannot establish which thread owns it.
"""
from dataclasses import dataclass, field
import hashlib
from typing import Callable
from uuid import uuid4

from app.application import ApplicationError, Principal
from app.metadata_models import (
    MetadataSnapshot, PersistentID, SHA256, SchemaSnapshot, SegmentInput,
    snapshot_bytes, validate_manual_input,
)
from app.services.metadata_store import _guard, _hex, _integer
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
