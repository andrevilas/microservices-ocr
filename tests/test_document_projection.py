"""Pure C6 projections: source integrity, bounded pages and scoped positions."""

import base64
import builtins
import copy
import hashlib
import json
import os
import socket
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.application import ApplicationError
from app.document_models import MAX_PAYLOAD_BYTES, FieldSpec, compose_schema
from app.document_transport_models import MAX_PROJECTION_BYTES, MetadataFieldPage, MetadataView
from app.metadata_models import (
    ApprovalRecord,
    EvidenceRef,
    FieldValueRecord,
    MetadataSnapshot,
    ProvenanceRecord,
    SchemaSnapshot,
    SegmentRecord,
    snapshot_bytes,
)
from app.services import document_projection as projection
from app.services.metadata_store import MetadataRevision


OWNER = "user:1"
NOW = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
MARKER = "SENSITIVE_VALUE_漢字_Ω"


def ident(number):
    return f"{number:032x}"


def provenance(actor=OWNER):
    return ProvenanceRecord(actor_subject=actor, auth_source="local_jwt", recorded_at=NOW)


def make_snapshot(values=None, *, segments=1, large_schema=False, field_actor=OWNER):
    if values is None:
        values = [0, False, None, [], MARKER]
    count = 30 if large_schema else max(1, len(values))
    composed = compose_schema([
        FieldSpec(name=f"field_{index}", type="string",
                  description="D" * 4000 if large_schema else "Synthetic")
        for index in range(count)
    ], [])
    if large_schema:
        assert 230000 < len(composed.canonical) <= MAX_PAYLOAD_BYTES
    records = []
    for index in range(segments):
        selected = values if segments == 1 else ([None] * (13 if index < 15 else 5))
        fields = []
        for position, value in enumerate(selected):
            evidence = []
            if value == MARKER:
                evidence = [EvidenceRef(
                    document_id=ident(1), page=index + 1, text_sha256="e" * 64,
                    start=0, end=len(MARKER), quote=MARKER,
                )]
            fields.append(FieldValueRecord(
                field_name=f"field_{position}", raw=value, normalized=copy.deepcopy(value),
                origin="manual", status="needs_review", evidence=evidence,
                provenance=provenance(field_actor),
            ))
        records.append(SegmentRecord(
            segment_id=ident(100 + index), page_start=index + 1, page_end=index + 1,
            context_id=ident(20), type_id=ident(30), type_version=1,
            schema_sha256=composed.sha256, fields=fields,
        ))
    return MetadataSnapshot(
        revision_id=ident(2), parent_revision_id=None, job_id=ident(1), document_id=ident(1),
        canonical_sha256="a" * 64, input_pdf_sha256="b" * 64, output_pdf_sha256="c" * 64,
        page_count=segments, created_at=NOW, author=provenance(),
        schemas=[SchemaSnapshot(schema=json.loads(composed.canonical), schema_sha256=composed.sha256)],
        segments=records, state="draft",
    )


def digest(snapshot):
    return hashlib.sha256(snapshot_bytes(snapshot)).hexdigest()


def canonical(model):
    return json.dumps(model.model_dump(mode="json", by_alias=True), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def failure(operation, status, code):
    with pytest.raises(ApplicationError) as caught:
        operation()
    error = caught.value
    assert getattr(error, "status_code", getattr(error, "status", None)) == status
    assert error.code == code
    assert error.message == projection._ERRORS[code][1]
    assert error.__cause__ is None and error.__context__ is None
    assert MARKER not in str(error) + repr(error) + repr(vars(error))
    return error


def field_scope(snapshot, sha=None):
    return dict(owner=OWNER, job_id=snapshot.job_id, view="fields",
                revision_id=snapshot.revision_id, sha256=sha or digest(snapshot),
                segment_id=snapshot.segments[0].segment_id)


def revisions(count=32):
    return [MetadataRevision(
        revision_id=ident(1000 + index), owner=OWNER, job_id=ident(1),
        parent_revision_id=None if index == 0 else ident(999 + index),
        sha256=f"{index:064x}", size_bytes=100, state="draft", created_at=NOW,
    ) for index in range(count)]


def encode_raw(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def cursor_document(token):
    return json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))


def encode_document(document):
    return encode_raw(json.dumps(document, sort_keys=True, separators=(",", ":")).encode())


def test_views_exclude_content_preserve_kinds_and_return_detached_models():
    source = make_snapshot()
    sha = digest(source)
    view = projection.metadata_view(OWNER, source, sha)
    page = projection.field_page(OWNER, source, sha, source.segments[0].segment_id)
    assert [field.raw_kind for field in page.fields] == ["integer", "boolean", "null", "list", "string"]
    assert [field.value_count for field in page.fields] == [1, 1, 0, 0, 1]
    assert [field.evidence_count for field in page.fields] == [0, 0, 0, 0, 1]
    assert [field.position for field in page.fields] == list(range(5))
    assert page.next_cursor is None
    for result in (view, page):
        text = canonical(result).decode()
        assert MARKER not in text
        assert all(f'"{key}":' not in text for key in ("raw", "normalized", "evidence", "schema", "quote"))
        assert len(canonical(result)) <= MAX_PROJECTION_BYTES
    schema_bytes = json.dumps(source.schemas[0].schema_document, sort_keys=True,
                              separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    assert view.schemas[0].canonical_utf8_bytes == len(schema_bytes)
    assert hashlib.sha256(schema_bytes).hexdigest() == view.schemas[0].schema_sha256
    assert view.sha256 == sha and view.content_path.endswith("/content?sha256=" + sha)
    assert view.segments[0].fields_path.endswith("/fields?sha256=" + sha)
    assert view.schemas[0].schema_path.endswith("/schemas/" + view.schemas[0].schema_sha256 + "?sha256=" + sha)
    second = projection.metadata_view(OWNER, source, sha)
    assert view == second and view is not second
    assert view.author is not source.author
    view.segments.clear()
    page.fields.clear()
    assert len(second.segments) == len(source.segments) == 1
    assert len(source.segments[0].fields) == 5
    assert len(projection.field_page(OWNER, source, sha, source.segments[0].segment_id).fields) == 5


def test_approved_lineage_and_large_frozen_schema_remain_small():
    source = make_snapshot(segments=16, large_schema=True)
    assert sum(len(segment.fields) for segment in source.segments) == 200
    parent = ident(3)
    approved = source.model_dump(mode="python", by_alias=True)
    approved.update(parent_revision_id=parent, state="approved", approval=ApprovalRecord(
        actor_subject=OWNER, auth_source="oauth", approved_at=NOW,
        reviewed_revision_id=parent, reviewed_sha256="d" * 64,
    ))
    source = MetadataSnapshot.model_validate(approved, strict=True)
    view = projection.metadata_view(OWNER, source, digest(source))
    assert view.approval == source.approval and view.approval is not source.approval
    assert view.parent_revision_id == parent and view.state == "approved"
    assert len(view.segments) == 16 and sum(segment.field_count for segment in view.segments) == 200
    assert len(canonical(view)) <= MAX_PROJECTION_BYTES
    assert "D" * 100 not in canonical(view).decode()


@pytest.mark.parametrize("damage", ["hash", "hash_type", "owner", "construct", "nested", "extra", "schema"])
def test_corrupt_sources_are_fixed_503_before_serialization_coercion(damage):
    source = make_snapshot()
    sha = digest(source)
    owner = OWNER
    if damage == "hash":
        sha = "f" * 64
    elif damage == "hash_type":
        sha = MARKER
    elif damage == "owner":
        owner = "user:2"
    elif damage == "construct":
        data = source.model_dump(mode="python", by_alias=True)
        data["page_count"] = True
        source = MetadataSnapshot.model_construct(**data)
    elif damage == "nested":
        source.segments[0].fields[0].__dict__["raw"] = 0.0
    elif damage == "extra":
        source.__dict__["unexpected"] = MARKER
    else:
        source.schemas[0].schema_document["fields"][0]["required"] = 0
    failure(lambda: projection.metadata_view(owner, source, sha), 503, "metadata_unavailable")
    failure(lambda: projection.field_page(owner, source, sha, ident(100)), 503, "metadata_unavailable")


def test_all_32_revisions_reconstruct_once_with_exclusive_keysets():
    source = list(reversed(revisions()))
    default = projection.revision_page(OWNER, ident(1), tuple(source))
    assert len(default.revisions) == 25 and default.next_cursor is not None
    found, after = [], None
    while True:
        page = projection.revision_page(OWNER, ident(1), source, limit=7, after=after)
        assert len(canonical(page)) <= MAX_PROJECTION_BYTES
        assert all("owner" not in item.model_dump() and "job_id" not in item.model_dump() for item in page.revisions)
        found.extend(item.revision_id for item in page.revisions)
        after = page.next_cursor
        if after is None:
            break
        assert page.revisions
    assert found == sorted(item.revision_id for item in source) and len(set(found)) == 32
    scope = dict(owner=OWNER, job_id=ident(1), view="revisions")
    last = projection.encode_cursor(found[-1], **scope)
    terminal = projection.revision_page(OWNER, ident(1), source, after=last)
    assert terminal.revisions == [] and terminal.next_cursor is None
    assert projection.revision_page(OWNER, ident(1), []).next_cursor is None
    missing = projection.encode_cursor(ident(9999), **scope)
    failure(lambda: projection.revision_page(OWNER, ident(1), source, after=missing), 422, "invalid_metadata_cursor")


@pytest.mark.parametrize("damage", ["quota", "duplicate", "owner", "job", "bool", "datetime", "dictionary", "extra"])
def test_revision_collection_requires_actual_strict_owned_sources(damage):
    source = revisions()
    if damage == "quota":
        source = revisions(33)
    elif damage == "duplicate":
        source[-1] = source[0]
    elif damage in ("owner", "job", "bool", "datetime"):
        key, value = {
            "owner": ("owner", "user:2"), "job": ("job_id", ident(2)),
            "bool": ("size_bytes", True), "datetime": ("created_at", NOW.isoformat()),
        }[damage]
        source[0] = replace(source[0], **{key: value})
    elif damage == "dictionary":
        source[0] = dict(revision_id=ident(1000))
    else:
        if hasattr(source[0], "__dict__"):
            source[0].__dict__["unexpected"] = MARKER
        else:
            source[0] = object()
    failure(lambda: projection.revision_page(OWNER, ident(1), source), 503, "metadata_unavailable")


def test_field_order_budget_packing_and_explicit_terminal_pages():
    source = make_snapshot(values=[MARKER] * 9, field_actor="user:" + "7" * 7000)
    sha = digest(source)
    segment = source.segments[0].segment_id
    default = projection.field_page(OWNER, source, sha, segment)
    assert 0 < len(default.fields) < 9 and default.next_cursor is not None
    positions, after = [], None
    while True:
        page = projection.field_page(OWNER, source, sha, segment, limit=9, after=after)
        assert len(canonical(page)) <= MAX_PROJECTION_BYTES
        positions.extend(field.position for field in page.fields)
        after = page.next_cursor
        if after is None:
            break
        assert page.fields
    assert positions == list(range(9))
    scope = field_scope(source, sha)
    last = projection.encode_cursor(8, **scope)
    terminal = projection.field_page(OWNER, source, sha, segment, after=last)
    assert terminal.fields == [] and terminal.next_cursor is None
    outside = projection.encode_cursor(9, **scope)
    failure(lambda: projection.field_page(OWNER, source, sha, segment, after=outside), 422, "invalid_metadata_cursor")
    empty = make_snapshot(values=[])
    page = projection.field_page(OWNER, empty, digest(empty), empty.segments[0].segment_id)
    assert page.fields == [] and page.next_cursor is None
    normal = make_snapshot(values=[None] * 30)
    assert len(projection.field_page(OWNER, normal, digest(normal), normal.segments[0].segment_id).fields) == 20


def test_single_descriptor_and_fixed_header_over_budget_have_fixed_413():
    source = make_snapshot(values=[None], field_actor="user:" + "8" * 20000)
    failure(lambda: projection.field_page(OWNER, source, digest(source), source.segments[0].segment_id),
            413, "metadata_response_too_large")
    data = source.model_dump(mode="python", by_alias=True)
    huge_owner = "user:" + "9" * 20000
    data["author"] = provenance(huge_owner)
    source = MetadataSnapshot.model_validate(data, strict=True)
    failure(lambda: projection.metadata_view(huge_owner, source, digest(source)),
            413, "metadata_response_too_large")


@pytest.mark.parametrize("limit", [True, False, 0, -1, 10**100, 1.0, "2", None])
def test_query_limits_are_exact_bounded_integers(limit):
    source = make_snapshot()
    failure(lambda: projection.revision_page(OWNER, ident(1), revisions(), limit=limit), 422, "invalid_metadata_query")
    failure(lambda: projection.field_page(OWNER, source, digest(source), ident(100), limit=limit), 422, "invalid_metadata_query")


def test_query_scope_and_limit_edges_and_missing_segment():
    source = make_snapshot()
    sha = digest(source)
    assert len(projection.revision_page(OWNER, ident(1), revisions(), limit=100).revisions) == 32
    assert len(projection.field_page(OWNER, source, sha, ident(100), limit=50).fields) == 5
    failure(lambda: projection.revision_page(OWNER, ident(1), revisions(), limit=101), 422, "invalid_metadata_query")
    failure(lambda: projection.field_page(OWNER, source, sha, ident(100), limit=51), 422, "invalid_metadata_query")
    failure(lambda: projection.revision_page(OWNER, MARKER, []), 422, "invalid_metadata_query")
    failure(lambda: projection.field_page(OWNER, source, sha, MARKER), 422, "invalid_metadata_query")
    failure(lambda: projection.metadata_view(MARKER, source, sha), 422, "invalid_metadata_query")
    failure(lambda: projection.field_page(OWNER, source, sha, ident(999)), 404, "metadata_segment_not_found")


def test_cursor_scopes_are_validated_without_a_token_and_never_encode_owner():
    source = make_snapshot()
    scope = field_scope(source)
    token = projection.encode_cursor(0, **scope)
    assert projection.decode_cursor(token, **scope) == 0
    assert projection.decode_cursor(None, **scope) is None
    document = cursor_document(token)
    assert set(document) == {"v", "scope", "position"} and OWNER not in json.dumps(document)
    for key, value in {
        "owner": "user:2", "job_id": ident(9), "revision_id": ident(9),
        "sha256": "f" * 64, "segment_id": ident(9),
    }.items():
        failure(lambda key=key, value=value: projection.decode_cursor(token, **{**scope, key: value}),
                422, "invalid_metadata_cursor")
    revision_scope = dict(owner=OWNER, job_id=source.job_id, view="revisions")
    failure(lambda: projection.decode_cursor(token, **revision_scope), 422, "invalid_metadata_cursor")
    revision_token = projection.encode_cursor(ident(7), **revision_scope)
    failure(lambda: projection.decode_cursor(revision_token, **scope), 422, "invalid_metadata_cursor")
    for bad in (
        {**scope, "job_id": MARKER}, {**scope, "revision_id": None},
        {**scope, "view": "values"}, {**revision_scope, "sha256": "a" * 64},
    ):
        failure(lambda bad=bad: projection.decode_cursor(None, **bad), 422, "invalid_metadata_cursor")
    failure(lambda: projection.encode_cursor(True, **scope), 422, "invalid_metadata_cursor")


@pytest.mark.parametrize("damage", [
    "duplicate", "oversize", "decoded", "padding", "alphabet", "padbits", "utf8",
    "extra", "version_bool", "version", "scope_type", "position_bool", "position_float",
    "position_negative", "position_large", "position_string", "noncanonical", "array", "nan",
])
def test_cursor_rejects_noncanonical_and_malformed_envelopes(damage):
    scope = field_scope(make_snapshot())
    token = projection.encode_cursor(0, **scope)
    document = cursor_document(token)
    if damage == "duplicate":
        raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        token = encode_raw(b'{"v":1,' + raw[1:])
    elif damage == "oversize":
        token = "A" * 2049
    elif damage == "decoded":
        token = encode_raw(b" " * 1537)
    elif damage == "padding":
        token += "="
    elif damage == "alphabet":
        token = "!" + token[1:]
    elif damage == "padbits":
        # A different final character with identical significant decoded bits.
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        while len(token) % 4 == 0:
            document["position"] += 1
            token = encode_document(document)
        bits = 4 if len(token) % 4 == 2 else 2
        final = alphabet.index(token[-1])
        token = token[:-1] + alphabet[final | ((1 << bits) - 1)]
    elif damage == "utf8":
        token = encode_raw(b"\xff")
    elif damage == "noncanonical":
        token = encode_raw(json.dumps(document, indent=1).encode())
    elif damage == "array":
        token = encode_raw(b"[]")
    elif damage == "nan":
        token = encode_raw(b'{"position":NaN,"scope":"' + document["scope"].encode() + b'","v":1}')
    else:
        key, value = {
            "extra": ("extra", MARKER), "version_bool": ("v", True), "version": ("v", 2),
            "scope_type": ("scope", 1), "position_bool": ("position", True),
            "position_float": ("position", 0.0), "position_negative": ("position", -1),
            "position_large": ("position", 100), "position_string": ("position", ident(1)),
        }[damage]
        document[key] = value
        token = encode_document(document)
    failure(lambda: projection.decode_cursor(token, **scope), 422, "invalid_metadata_cursor")


def test_dtos_are_closed_strict_and_references_cannot_be_arbitrary_urls():
    source = make_snapshot()
    view = projection.metadata_view(OWNER, source, digest(source))
    data = view.model_dump(mode="python")
    for change in (
        {"extra": MARKER}, {"schema_version": "other/1"}, {"page_count": True},
        {"content_path": "https://example.invalid/"}, {"segments": tuple(data["segments"])},
        {"content_path": "\ud800"},
    ):
        with pytest.raises(ValidationError):
            MetadataView.model_validate({**data, **change}, strict=True)
    assert MetadataView.model_json_schema()["additionalProperties"] is False
    assert MetadataFieldPage.model_json_schema()["additionalProperties"] is False
    page = projection.field_page(OWNER, source, digest(source), ident(100))
    altered = page.model_dump(mode="python")
    altered["fields"][0]["value_count"] = False
    with pytest.raises(ValidationError):
        MetadataFieldPage.model_validate(altered, strict=True)


def test_functions_do_not_perform_io_and_control_exceptions_keep_identity(monkeypatch):
    source = make_snapshot()
    sha = digest(source)
    rows = revisions(2)
    scope = field_scope(source, sha)
    def forbidden(*args, **kwargs):
        raise AssertionError("Projection attempted external I/O")
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(os, "open", forbidden)
        patch.setattr(os, "stat", forbidden)
        patch.setattr(os, "urandom", forbidden)
        patch.setattr(socket, "socket", forbidden)
        projection.metadata_view(OWNER, source, sha)
        projection.revision_page(OWNER, source.job_id, rows)
        projection.field_page(OWNER, source, sha, ident(100))
        assert projection.decode_cursor(projection.encode_cursor(0, **scope), **scope) == 0
    for signal in (KeyboardInterrupt(), SystemExit(), GeneratorExit()):
        def interrupted(value, signal=signal):
            raise signal
        with monkeypatch.context() as patch:
            patch.setattr(projection, "snapshot_bytes", interrupted)
            with pytest.raises(type(signal)) as caught:
                projection.metadata_view(OWNER, source, sha)
            assert caught.value is signal
