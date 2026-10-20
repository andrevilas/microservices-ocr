"""Pure phase-one metadata projections and opaque pagination positions.

Callers MUST authorize the owner/job and supply verified storage sources before
calling these helpers. These checks are neither ACL nor durability authority.
A cursor is an unsigned scope-bound position; it never grants access. Values,
evidence and schema content queries, and transport wiring, belong to later work.
The 16 KiB canonical projection budget does not establish an SDK envelope bound.
"""

import base64
import hashlib
import json
import re
from dataclasses import fields as dataclass_fields
from typing import Annotated, Literal

from pydantic import Field, StrictInt, TypeAdapter, model_validator

from app.application import ApplicationError
from app.document_models import MAX_PAYLOAD_BYTES
from app.document_transport_models import (
    MAX_PROJECTION_BYTES,
    MetadataFieldDescriptor,
    MetadataFieldPage,
    MetadataRevisionPage,
    MetadataRevisionSummary,
    MetadataSchemaReference,
    MetadataSegmentSummary,
    MetadataView,
)
from app.metadata_models import (
    ActorSubject,
    MetadataSnapshot,
    PersistentID,
    SHA256,
    _ClosedModel,
    _bounded_canonical,
    snapshot_bytes,
)
from app.services.metadata_store import MetadataRevision


MAX_CURSOR_CHARS = 2048
MAX_CURSOR_BYTES = 1536
MAX_REVISIONS = 32
_ID_ADAPTER = TypeAdapter(PersistentID)
_HASH_ADAPTER = TypeAdapter(SHA256)
_ALPHABET = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)


_ERRORS = {
    "metadata_unavailable": (503, "Stored metadata is unavailable."),
    "invalid_metadata_query": (422, "Invalid metadata query."),
    "invalid_metadata_cursor": (422, "Invalid metadata cursor."),
    "metadata_segment_not_found": (404, "Metadata segment was not found."),
    "metadata_response_too_large": (413, "Metadata response exceeds its byte limit."),
}


def _error(code):
    status, message = _ERRORS[code]
    return ApplicationError(status_code=status, code=code, message=message)


class _Scope(_ClosedModel):
    owner: ActorSubject
    job_id: PersistentID
    view: Literal["revisions", "fields"]
    revision_id: PersistentID | None = None
    sha256: SHA256 | None = None
    segment_id: PersistentID | None = None

    @model_validator(mode="after")
    def _shape(self):
        extras = (self.revision_id, self.sha256, self.segment_id)
        if self.view == "revisions":
            if any(value is not None for value in extras):
                raise ValueError("Revision scope cannot contain snapshot coordinates.")
        elif any(value is None for value in extras):
            raise ValueError("Field scope requires all snapshot coordinates.")
        return self


class _RevisionQuery(_ClosedModel):
    owner: ActorSubject
    job_id: PersistentID
    limit: Annotated[StrictInt, Field(ge=1, le=100)]


class _FieldQuery(_ClosedModel):
    owner: ActorSubject
    segment_id: PersistentID
    limit: Annotated[StrictInt, Field(ge=1, le=50)]


class _RevisionSource(MetadataRevisionSummary):
    owner: ActorSubject
    job_id: PersistentID


def _scope_hash(*, owner, job_id, view, revision_id, sha256, segment_id):
    scope = _Scope.model_validate(
        dict(owner=owner, job_id=job_id, view=view, revision_id=revision_id,
             sha256=sha256, segment_id=segment_id), strict=True
    )
    document = {
        "owner_sha256": hashlib.sha256(scope.owner.encode("utf-8", errors="strict")).hexdigest(),
        "job_id": scope.job_id,
        "view": scope.view,
        "view_version": 1,
        "revision_id": scope.revision_id,
        "sha256": scope.sha256,
        "segment_id": scope.segment_id,
    }
    return hashlib.sha256(_bounded_canonical(document, MAX_CURSOR_BYTES)).hexdigest()


def _position(value, view):
    if view == "revisions":
        return _ID_ADAPTER.validate_python(value, strict=True)
    if type(value) is not int or not 0 <= value <= 99:
        raise ValueError("Invalid field position.")
    return value


def _token(document):
    encoded = base64.urlsafe_b64encode(
        _bounded_canonical(document, MAX_CURSOR_BYTES)
    ).rstrip(b"=").decode("ascii")
    if len(encoded) > MAX_CURSOR_CHARS:
        raise ValueError("Cursor exceeds its encoded limit.")
    return encoded


def encode_cursor(position, *, owner, job_id, view, revision_id=None, sha256=None, segment_id=None):
    """Encode an unsigned position, binding the explicitly authorized scope."""
    try:
        scope = _scope_hash(owner=owner, job_id=job_id, view=view,
                            revision_id=revision_id, sha256=sha256, segment_id=segment_id)
        return _token({"v": 1, "scope": scope, "position": _position(position, view)})
    except Exception:
        pass
    raise _error("invalid_metadata_cursor")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key.")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Invalid JSON constant.")


def decode_cursor(token, *, owner, job_id, view, revision_id=None, sha256=None, segment_id=None):
    """Validate scope even for None; adapters can call this before loading a snapshot.

    Returned positions must only address the resource already authorized by the
    caller. They must never select another owner, job, revision or segment.
    """
    try:
        expected = _scope_hash(owner=owner, job_id=job_id, view=view,
                               revision_id=revision_id, sha256=sha256, segment_id=segment_id)
        if token is None:
            return None
        # Check characters and decoded size before allocating decoded input.
        if type(token) is not str or not 0 < len(token) <= MAX_CURSOR_CHARS:
            raise ValueError("Invalid cursor length.")
        if len(token) % 4 == 1 or (len(token) * 6) // 8 > MAX_CURSOR_BYTES:
            raise ValueError("Invalid decoded cursor length.")
        if _ALPHABET.fullmatch(token) is None:
            raise ValueError("Invalid cursor alphabet.")
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        if len(raw) > MAX_CURSOR_BYTES:
            raise ValueError("Decoded cursor exceeds its limit.")
        if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != token:
            raise ValueError("Noncanonical base64 encoding.")
        document = json.loads(raw.decode("utf-8", errors="strict"),
                              object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        if type(document) is not dict or set(document) != {"v", "scope", "position"}:
            raise ValueError("Invalid cursor envelope.")
        if type(document["v"]) is not int or document["v"] != 1:
            raise ValueError("Invalid cursor version.")
        scope = _HASH_ADAPTER.validate_python(document["scope"], strict=True)
        position = _position(document["position"], view)
        if scope != expected or _bounded_canonical(document, MAX_CURSOR_BYTES) != raw:
            raise ValueError("Invalid cursor scope or canonical form.")
        return position
    except Exception:
        pass
    raise _error("invalid_metadata_cursor")


def _query(model, data):
    try:
        return model.model_validate(data, strict=True)
    except Exception:
        pass
    raise _error("invalid_metadata_query")


def _verified_snapshot(owner, value, sha256):
    try:
        if not isinstance(value, MetadataSnapshot):
            raise ValueError("A snapshot model is required.")
        digest = _HASH_ADAPTER.validate_python(sha256, strict=True)
        # snapshot_bytes revalidates ORIGINAL instances recursively before any dump.
        canonical = snapshot_bytes(value)
        if hashlib.sha256(canonical).hexdigest() != digest:
            raise ValueError("Snapshot hash mismatch.")
        result = MetadataSnapshot.model_validate_json(canonical, strict=True)
        if snapshot_bytes(result) != canonical or result.author.actor_subject != owner:
            raise ValueError("Snapshot owner or canonical form mismatch.")
        return result, digest
    except Exception:
        pass
    raise _error("metadata_unavailable")


def _revision_sources(owner, job_id, revisions):
    try:
        if type(revisions) not in (list, tuple) or len(revisions) > MAX_REVISIONS:
            raise ValueError("A bounded complete revision collection is required.")
        result = []
        seen = set()
        for revision in revisions:
            if type(revision) is not MetadataRevision:
                raise ValueError("An actual stored revision is required.")
            # Read original attributes without asdict, JSON or other coercion.
            data = {field.name: getattr(revision, field.name) for field in dataclass_fields(revision)}
            attributes = getattr(revision, "__dict__", None)
            if attributes is not None and set(attributes) != set(data):
                raise ValueError("Unexpected revision attributes.")
            checked = _RevisionSource.model_validate(data, strict=True)
            if checked.owner != owner or checked.job_id != job_id or checked.revision_id in seen:
                raise ValueError("Revision scope or identity mismatch.")
            seen.add(checked.revision_id)
            result.append(checked)
        return sorted(result, key=lambda revision: revision.revision_id)
    except Exception:
        pass
    raise _error("metadata_unavailable")


def _make(model, data):
    try:
        result = model.model_validate(data, strict=True)
        # Detach mutable containers after strict validation, never before it.
        return model.model_validate(
            result.model_dump(mode="python", by_alias=True, warnings="error"), strict=True
        )
    except Exception:
        pass
    raise _error("metadata_unavailable")


def _fits(model):
    try:
        _bounded_canonical(
            model.model_dump(mode="json", by_alias=True, warnings="error"), MAX_PROJECTION_BYTES
        )
        return True
    except ValueError:
        return False


def _bounded_result(model):
    if not _fits(model):
        raise _error("metadata_response_too_large")
    return model


def _pack(model, header, key, items, start, limit, cursor_at):
    if start == len(items):
        return _bounded_result(_make(model, {**header, key: [], "next_cursor": None}))
    best = None
    # Consider the terminal candidate too: removing its cursor can save bytes.
    for end in range(start + 1, min(len(items), start + limit) + 1):
        cursor = cursor_at(end - 1) if end < len(items) else None
        candidate = _make(model, {**header, key: items[start:end], "next_cursor": cursor})
        if _fits(candidate):
            best = candidate
    if best is None:
        raise _error("metadata_response_too_large")
    return best


def metadata_view(owner, snapshot, sha256):
    """Return a detached header and references, without values or schema content."""
    # The owner is query syntax; all snapshot identities are validated source data.
    query = _query(_OwnerQuery, {"owner": owner})
    source, digest = _verified_snapshot(query.owner, snapshot, sha256)
    base = f"/api/v1/jobs/{source.job_id}/metadata/revisions/{source.revision_id}"
    suffix = f"?sha256={digest}"
    segments = []
    for segment in source.segments:
        segments.append(_make(MetadataSegmentSummary, dict(
            segment_id=segment.segment_id, page_start=segment.page_start, page_end=segment.page_end,
            context_id=segment.context_id, type_id=segment.type_id, type_version=segment.type_version,
            schema_sha256=segment.schema_sha256, field_count=len(segment.fields),
            fields_path=base + f"/segments/{segment.segment_id}/fields" + suffix,
        )))
    schemas = []
    for schema in source.schemas:
        canonical = _bounded_canonical(schema.schema_document, MAX_PAYLOAD_BYTES)
        schemas.append(_make(MetadataSchemaReference, dict(
            schema_sha256=schema.schema_sha256, canonical_utf8_bytes=len(canonical),
            schema_path=base + f"/schemas/{schema.schema_sha256}" + suffix,
        )))
    header = {name: getattr(source, name) for name in (
        "schema_version", "revision_id", "parent_revision_id", "job_id", "document_id",
        "artifact_version", "text_version", "canonical_sha256", "input_pdf_sha256",
        "output_pdf_sha256", "page_count", "parent_job_id", "created_at", "author", "state", "approval",
    )}
    return _bounded_result(_make(MetadataView, {
        **header, "sha256": digest, "content_path": base + "/content" + suffix,
        "segments": segments, "schemas": schemas,
    }))


class _OwnerQuery(_ClosedModel):
    owner: ActorSubject


def revision_page(owner, job_id, revisions, *, limit=25, after=None):
    """Page a complete, already authorized collection of at most 32 revisions."""
    query = _query(_RevisionQuery, dict(owner=owner, job_id=job_id, limit=limit))
    scope = dict(owner=query.owner, job_id=query.job_id, view="revisions")
    position = decode_cursor(after, **scope)
    source = _revision_sources(query.owner, query.job_id, revisions)
    start = 0
    if position is not None:
        indices = {revision.revision_id: index for index, revision in enumerate(source)}
        if position not in indices:
            raise _error("invalid_metadata_cursor")
        start = indices[position] + 1
    summaries = [_make(MetadataRevisionSummary, {
        name: getattr(revision, name) for name in (
            "revision_id", "parent_revision_id", "sha256", "size_bytes", "state", "created_at"
        )
    }) for revision in source]
    return _pack(MetadataRevisionPage, {"job_id": query.job_id}, "revisions", summaries,
                 start, query.limit,
                 lambda index: encode_cursor(source[index].revision_id, **scope))


def _kind(value):
    if value is None:
        return "null"
    if type(value) is list:
        return "list"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is str:
        return "string"
    raise _error("metadata_unavailable")


def field_page(owner, snapshot, sha256, segment_id, *, limit=20, after=None):
    """Page descriptors in immutable stored order; never include field values."""
    query = _query(_FieldQuery, dict(owner=owner, segment_id=segment_id, limit=limit))
    source, digest = _verified_snapshot(query.owner, snapshot, sha256)
    scope = dict(owner=query.owner, job_id=source.job_id, view="fields",
                 revision_id=source.revision_id, sha256=digest, segment_id=query.segment_id)
    position = decode_cursor(after, **scope)
    segment = next((segment for segment in source.segments if segment.segment_id == query.segment_id), None)
    if segment is None:
        raise _error("metadata_segment_not_found")
    if position is not None and position >= len(segment.fields):
        raise _error("invalid_metadata_cursor")
    start = 0 if position is None else position + 1
    descriptors = []
    for index, field in enumerate(segment.fields):
        count = 0 if field.raw is None else len(field.raw) if type(field.raw) is list else 1
        descriptors.append(_make(MetadataFieldDescriptor, dict(
            position=index, field_name=field.field_name, origin=field.origin, status=field.status,
            provenance=field.provenance, raw_kind=_kind(field.raw), normalized_kind=_kind(field.normalized),
            value_count=count, evidence_count=len(field.evidence),
        )))
    return _pack(MetadataFieldPage, dict(job_id=source.job_id, revision_id=source.revision_id,
                                       sha256=digest, segment_id=segment.segment_id),
                 "fields", descriptors, start, query.limit,
                 lambda index: encode_cursor(index, **scope))
