"""Pure lossless content projections; no authorization, storage or transport I/O.

Callers MUST authorize the owner/job and supply verified storage sources first.
These helpers independently revalidate snapshots and hashes. Unsigned cursors
only address an already authorized resource; they never grant access. The 16 KiB
canonical DTO budget is independent of any future SDK envelope budget.
"""

import hashlib
from functools import wraps
from typing import Annotated

from pydantic import Field, StrictInt

from app.application import ApplicationError
from app.document_content_models import (
    MetadataEvidencePage, MetadataSchemaChunk, MetadataStringChunk,
    MetadataValuePage, Offset, Part,
)
from app.document_models import MAX_PAYLOAD_BYTES, FieldName
from app.metadata_models import (
    ActorSubject, ItemIndex, PersistentID, SHA256, _ClosedModel, _bounded_canonical,
)
from app.services.document_projection import (
    _ERRORS, _bounded_result, _error, _fits, _kind, _make, _query,
    _verified_snapshot, decode_cursor, encode_cursor,
)


class _FieldQuery(_ClosedModel):
    owner: ActorSubject
    segment_id: PersistentID
    field_name: FieldName


class _PageQuery(_FieldQuery):
    limit: Annotated[StrictInt, Field(ge=1, le=50)]


class _ChunkQuery(_ClosedModel):
    offset: Offset
    max_chars: Annotated[StrictInt, Field(ge=1, le=1000)]


class _StringQuery(_FieldQuery, _ChunkQuery):
    part: Part
    item_index: ItemIndex | None


class _SchemaQuery(_ChunkQuery):
    owner: ActorSubject
    schema_sha256: SHA256


def _safe(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        code = "metadata_unavailable"
        try:
            return function(*args, **kwargs)
        except ApplicationError as error:
            candidate = getattr(error, "code", None)
            if type(candidate) is str and candidate in _ERRORS:
                code = candidate
        except Exception:
            pass
        # Raise outside handlers: no sensitive cause or exception context survives.
        raise _error(code)
    return guarded


def _field(query, snapshot, sha256):
    source, digest = _verified_snapshot(query.owner, snapshot, sha256)
    segment = next((item for item in source.segments if item.segment_id == query.segment_id), None)
    if segment is None:
        raise _error("metadata_segment_not_found")
    field = next((item for item in segment.fields if item.field_name == query.field_name), None)
    if field is None:
        raise _error("metadata_field_not_found")
    header = dict(job_id=source.job_id, revision_id=source.revision_id, sha256=digest,
                  segment_id=segment.segment_id, field_name=field.field_name)
    return field, header


def _component(value, part, item_index):
    kind = _kind(value)
    if kind == "string":
        inline = len(value) <= 1000
        if inline:
            try:
                _bounded_canonical(value, 4096)
            except ValueError:
                inline = False
        if not inline:
            raw = value.encode("utf-8", errors="strict")
            return dict(kind="string_ref", reference=dict(
                part=part, item_index=item_index, characters=len(value),
                utf8_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest(),
            ))
    return dict(kind=kind, value=value)


def _page(model, header, key, count, query, after, view, item_at):
    scope = dict(owner=query.owner, view=view, **header)
    position = decode_cursor(after, **scope)
    if position is not None and position >= count:
        raise _error("invalid_metadata_cursor")
    start = 0 if position is None else position + 1
    base = {**header, "total_count": count}
    if start == count:
        return _bounded_result(_make(model, {**base, key: [], "next_cursor": None}))
    items, best = [], None
    for index in range(start, min(count, start + query.limit)):
        items.append(item_at(index))
        cursor = encode_cursor(index, **scope) if index + 1 < count else None
        candidate = _make(model, {**base, key: items, "next_cursor": cursor})
        if _fits(candidate):
            best = candidate
    if best is None:
        raise _error("metadata_response_too_large")
    return best


@_safe

def value_page(owner, snapshot, sha256, segment_id, field_name, *, limit=20, after=None):
    """Page raw/normalized pairs without coercion, truncation or normalization."""
    query = _query(_PageQuery, dict(owner=owner, segment_id=segment_id,
                                    field_name=field_name, limit=limit))
    field, header = _field(query, snapshot, sha256)
    raw_kind, normalized_kind = _kind(field.raw), _kind(field.normalized)
    count = len(field.raw) if raw_kind == "list" else 0 if raw_kind == "null" else 1

    def item_at(position):
        item_index = position if raw_kind == "list" else None
        raw = field.raw[position] if item_index is not None else field.raw
        normalized = field.normalized[position] if item_index is not None else field.normalized
        return dict(position=position, item_index=item_index,
                    raw=_component(raw, "raw", item_index),
                    normalized=_component(normalized, "normalized", item_index))

    # Kinds are response data, never cursor coordinates.
    class_header = {**header, "raw_kind": raw_kind, "normalized_kind": normalized_kind}
    return _value_pack(class_header, header, count, query, after, item_at)


def _value_pack(response_header, scope_header, count, query, after, item_at):
    # Reuse the common packer while keeping response-only kinds out of scope.
    def factory(data):
        return _make(MetadataValuePage, {**data, **response_header})

    class _ValueModel:
        @staticmethod
        def model_validate(data, strict=True):
            return factory(data)

    # _make expects the supplied model to revalidate its detached Python dump.
    return _page(_ValueModel, scope_header, "values", count, query, after, "values", item_at)


@_safe

def evidence_page(owner, snapshot, sha256, segment_id, field_name, *, limit=10, after=None):
    """Page complete evidence in stored order, including duplicate references."""
    query = _query(_PageQuery, dict(owner=owner, segment_id=segment_id,
                                    field_name=field_name, limit=limit))
    field, header = _field(query, snapshot, sha256)
    return _page(MetadataEvidencePage, header, "evidence", len(field.evidence),
                 query, after, "evidence", lambda index: field.evidence[index])


def _slice(text, query):
    if query.offset > len(text):
        raise _error("invalid_metadata_query")
    end = min(len(text), query.offset + query.max_chars)
    return dict(total_characters=len(text), offset=query.offset, end=end,
                next_offset=None if end == len(text) else end, text=text[query.offset:end])


@_safe

def string_chunk(owner, snapshot, sha256, segment_id, field_name, *, part,
                 item_index=None, offset=0, max_chars=1000):
    """Slice original string codepoints; offsets never address encoded bytes."""
    query = _query(_StringQuery, dict(owner=owner, segment_id=segment_id,
        field_name=field_name, part=part, item_index=item_index,
        offset=offset, max_chars=max_chars))
    field, header = _field(query, snapshot, sha256)
    value = getattr(field, query.part)
    if type(value) is list:
        if query.item_index is None or query.item_index >= len(value):
            raise _error("invalid_metadata_query")
        value = value[query.item_index]
    elif query.item_index is not None:
        raise _error("invalid_metadata_query")
    if type(value) is not str:
        raise _error("invalid_metadata_query")
    chunk = _slice(value, query)
    raw = value.encode("utf-8", errors="strict")
    return _bounded_result(_make(MetadataStringChunk, dict(
        **header, **chunk, part=query.part, item_index=query.item_index,
        utf8_bytes=len(raw), content_sha256=hashlib.sha256(raw).hexdigest(),
    )))


@_safe

def schema_chunk(owner, snapshot, sha256, schema_sha256, *, offset=0, max_chars=1000):
    """Slice the exact canonical JSON of a frozen schema, without parsing slices."""
    query = _query(_SchemaQuery, dict(owner=owner, schema_sha256=schema_sha256,
                                     offset=offset, max_chars=max_chars))
    source, digest = _verified_snapshot(query.owner, snapshot, sha256)
    schema = next((item for item in source.schemas if item.schema_sha256 == query.schema_sha256), None)
    if schema is None:
        raise _error("metadata_schema_not_found")
    raw = _bounded_canonical(schema.schema_document, MAX_PAYLOAD_BYTES)
    if hashlib.sha256(raw).hexdigest() != schema.schema_sha256:
        raise _error("metadata_unavailable")
    chunk = _slice(raw.decode("utf-8", errors="strict"), query)
    return _bounded_result(_make(MetadataSchemaChunk, dict(
        job_id=source.job_id, revision_id=source.revision_id, sha256=digest,
        schema_sha256=schema.schema_sha256, canonical_utf8_bytes=len(raw), **chunk,
    )))
