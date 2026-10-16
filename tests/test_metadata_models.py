"""Executable structural acceptance tests for C1a only."""

import builtins
import copy
import hashlib
import importlib.util
import io
import json
import pathlib
import socket
import sys
import threading
import time
import types
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app import metadata_models as models
from app.metadata_models import (
    EvidenceRef,
    ManualFieldInput,
    SaveMetadataInput,
    SaveMetadataReceipt,
    SegmentInput,
    validate_manual_input,
)


ID = "a" * 32
HASH = "b" * 64


def evidence(quote="x", **updates):
    data = dict(document_id=ID, page=1, text_sha256=HASH, start=0, end=len(quote), quote=quote)
    data.update(updates)
    return data


def field(raw="0012", **updates):
    data = dict(field_name="identifier", raw=raw)
    data.update(updates)
    return data


def segment(key="row", start=1, end=None, **updates):
    data = dict(client_key=key, page_start=start, page_end=start if end is None else end,
                type_id=ID, type_version=1)
    data.update(updates)
    return data


def request(**updates):
    data = dict(expected_head=None, expected_sha256=None, segments=[segment()])
    data.update(updates)
    return data


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def many_fields(count, raw=0):
    return [field(raw, field_name=f"f{i}") for i in range(count)]


def test_first_save_and_edit_preconditions():
    saved = validate_manual_input(request(segments=[segment("a"), segment("b", 3)]))
    assert [s.segment_id for s in saved.segments] == [None, None]
    assert [s.client_key for s in saved.segments] == ["a", "b"]
    edit = request(expected_head=ID, expected_sha256=HASH,
                   segments=[segment(segment_id="c" * 32)])
    assert validate_manual_input(edit).segments[0].segment_id == "c" * 32
    for key in ("expected_head", "expected_sha256"):
        missing = request()
        del missing[key]
        with pytest.raises(ValueError):
            validate_manual_input(missing)
    for updates in ({"expected_head": ID}, {"expected_sha256": HASH}):
        with pytest.raises(ValueError):
            validate_manual_input(request(**updates))


def test_receipt_correlates_and_limits_ids():
    receipt = SaveMetadataReceipt(revision_id=ID, sha256=HASH,
                                  segment_ids={"a": "c" * 32, "b": "d" * 32})
    assert receipt.segment_ids == {"a": "c" * 32, "b": "d" * 32}
    valid = {f"r{i}": f"{i:032x}" for i in range(16)}
    assert len(SaveMetadataReceipt(revision_id=ID, sha256=HASH, segment_ids=valid).segment_ids) == 16
    for mapping in ({"bad key": ID}, {"a": "A" * 32}, {"a": ID, "b": ID},
                    {**valid, "overflow": "f" * 32}, [("a", ID)], {1: ID}, {"a": 1}):
        with pytest.raises(ValidationError):
            SaveMetadataReceipt(revision_id=ID, sha256=HASH, segment_ids=mapping)
    for extra in ("content", "paths", "tokens", "owner"):
        with pytest.raises(ValidationError):
            SaveMetadataReceipt(revision_id=ID, sha256=HASH, segment_ids={}, **{extra: "x"})


def test_defaults_and_explicit_origins_preserve_raw_values():
    assert SegmentInput(**segment()).fields == []
    assert EvidenceRef(**evidence()).artifact_version == 1
    assert EvidenceRef(**evidence()).text_version == 1
    assert EvidenceRef(**evidence()).item_index is None
    for origin in ("manual", "declared"):
        result = ManualFieldInput(**field(origin=origin))
        assert result.raw == "0012"
        assert result.origin == origin
        assert result.evidence == []
    assert ManualFieldInput(**field()).origin == "manual"
    for raw in (None, 0, False, "", [], " 0012\r\n\x00 ", -(2**53 - 1), 2**53 - 1):
        result = ManualFieldInput(**field(raw))
        assert result.raw == raw
        assert type(result.raw) is type(raw)


@pytest.mark.parametrize("name", [
    "normalized", "status", "reviewer", "actor", "approved", "human_reviewed",
    "provenance", "execution_id", "hidden",
])
def test_authority_fields_are_forbidden(name):
    with pytest.raises(ValidationError):
        ManualFieldInput(**field(**{name: "SENSITIVE"}))


@pytest.mark.parametrize("origin", ["extracted", "inherited", True, 1])
def test_invalid_origins(origin):
    with pytest.raises(ValidationError):
        ManualFieldInput(**field(origin=origin))


@pytest.mark.parametrize("name", ["context_id", "schema_hash", "schema_document", "owner", "job_id"])
def test_segment_extras(name):
    with pytest.raises(ValidationError):
        SegmentInput(**segment(**{name: "x"}))


@pytest.mark.parametrize("cls,data", [
    (SaveMetadataInput, request(owner="x")),
    (SaveMetadataInput, request(job_id="x")),
    (EvidenceRef, evidence(extra="x")),
])
def test_other_models_are_closed(cls, data):
    with pytest.raises(ValidationError):
        cls(**data)


@pytest.mark.parametrize("raw", [
    1.0, float("nan"), float("inf"), float("-inf"), Decimal("1"),
    (1,), {1}, [[1]], {"value": 1}, [None], [1.0], 2**53, -(2**53),
    "x" * 4001, "\ud800", ["\udfff"], bytes(b"x"),
])
def test_invalid_raw_types_and_bounds(raw):
    with pytest.raises(ValidationError):
        ManualFieldInput(**field(raw))


def test_raw_list_and_unicode_boundaries():
    value = ["0012", 0, False, "😀e\u0301\r\n\x00"]
    assert ManualFieldInput(**field(value)).raw == value
    assert len(ManualFieldInput(**field([True] * 100)).raw) == 100
    assert ManualFieldInput(**field("😀" * 4000)).raw == "😀" * 4000
    with pytest.raises(ValidationError):
        ManualFieldInput(**field([True] * 101))


@pytest.mark.parametrize("key,bad", [
    ("document_id", "A" * 32), ("document_id", "a" * 31),
    ("document_id", "a" * 32 + "\n"), ("document_id", 12),
    ("text_sha256", "B" * 64), ("text_sha256", "b" * 63),
    ("text_sha256", "b" * 64 + "\n"), ("text_sha256", False),
    ("artifact_version", True), ("artifact_version", "1"),
    ("artifact_version", 1.0), ("artifact_version", 2),
    ("text_version", False), ("text_version", 0), ("text_version", 1.0),
    ("page", True), ("page", "1"), ("page", 0), ("page", 301), ("page", 1.0),
    ("start", False), ("start", "0"), ("start", -1), ("start", 0.0),
    ("end", True), ("end", 500001), ("end", "1"),
    ("item_index", True), ("item_index", -1), ("item_index", 100),
])
def test_evidence_strict_types(key, bad):
    with pytest.raises(ValidationError):
        EvidenceRef(**evidence(**{key: bad}))


@pytest.mark.parametrize("key,bad", [
    ("type_id", "A" * 32), ("segment_id", 1),
    ("type_version", True), ("type_version", "1"), ("type_version", 1.0),
    ("type_version", 0), ("type_version", 2**63),
    ("page_start", True), ("page_end", 301),
    ("client_key", "1row"), ("client_key", "é"),
    ("client_key", "a" * 33), ("client_key", "a\n"), ("client_key", 1),
    ("fields", ()), ("fields", set()),
])
def test_segment_strict_types(key, bad):
    with pytest.raises(ValidationError):
        SegmentInput(**segment(**{key: bad}))


def test_page_offset_type_version_and_key_boundaries():
    assert EvidenceRef(**evidence(page=300, start=499999, end=500000)).end == 500000
    assert SegmentInput(**segment("A" + "_-0" * 10 + "z", 300,
                                  type_version=2**63 - 1)).page_end == 300
    for updates in ({"expected_head": True, "expected_sha256": HASH},
                    {"expected_head": ID, "expected_sha256": "B" * 64},
                    {"segments": tuple([segment()])}, {"segments": set()}):
        with pytest.raises(ValueError):
            validate_manual_input(request(**updates))


@pytest.mark.parametrize("quote", ["\r\n", "\x00", "😀", "e\u0301", "a\r\n\x00😀e\u0301", "x" * 2000])
def test_evidence_preserves_canonical_codepoints(quote):
    result = EvidenceRef(**evidence(quote, start=10, end=10 + len(quote)))
    assert result.quote == quote
    assert json.loads(canonical(result.model_dump(mode="json")))["quote"] == quote


@pytest.mark.parametrize("updates", [
    {"quote": "", "end": 0}, {"quote": "\ud800"}, {"quote": "x" * 2001, "end": 2001},
    {"end": 0}, {"start": 2, "end": 1}, {"end": 2}, {"quote": b"x"},
])
def test_bad_quote_and_range(updates):
    with pytest.raises(ValidationError):
        EvidenceRef(**evidence(**updates))


def test_evidence_item_indices_and_incomplete_drafts():
    assert ManualFieldInput(**field([1, False], evidence=[evidence(item_index=1)])).raw == [1, False]
    assert ManualFieldInput(**field([], evidence=[])).raw == []
    for raw, refs in ((None, [evidence()]), (0, [evidence(item_index=0)]),
                      (False, [evidence(item_index=0)]), ([1], [evidence()]),
                      ([1], [evidence(item_index=1)]), ([], [evidence(item_index=0)])):
        with pytest.raises(ValidationError):
            ManualFieldInput(**field(raw, evidence=refs))
    for bad in ((), set()):
        with pytest.raises(ValidationError):
            ManualFieldInput(**field(evidence=bad))


def test_segment_order_ranges_and_uniqueness():
    assert len(validate_manual_input(request(segments=[segment("a", 1, 2), segment("b", 5, 6)])).segments) == 2
    cases = [
        [segment("a", 3), segment("b", 1)],
        [segment("a", 1, 3), segment("b", 3, 4)],
        [segment("a", 1, 4), segment("b", 2)],
        [segment("a"), segment("a", 2)],
        [segment("a", segment_id=ID), segment("b", 2, segment_id=ID)],
        [segment("a", 2, 1)],
        [segment(fields=[field(), field()])],
    ]
    for segments in cases:
        with pytest.raises(ValueError):
            validate_manual_input(request(segments=segments))


def test_list_and_aggregate_quotas():
    assert len(validate_manual_input(request(segments=[segment(f"r{i}", i + 1) for i in range(16)])).segments) == 16
    for segments in ([], [segment(f"r{i}", i + 1) for i in range(17)],
                     [segment(fields=many_fields(101))]):
        with pytest.raises(ValueError):
            validate_manual_input(request(segments=segments))
    two_hundred = [segment("a", fields=many_fields(100)), segment("b", 2, fields=many_fields(100))]
    assert sum(len(s.fields) for s in validate_manual_input(request(segments=two_hundred)).segments) == 200
    with pytest.raises(ValueError):
        validate_manual_input(request(segments=two_hundred + [segment("c", 3, fields=[field()])]))
    refs = [evidence()] * 128
    assert len(ManualFieldInput(**field(evidence=refs)).evidence) == 128
    with pytest.raises(ValidationError):
        ManualFieldInput(**field(evidence=refs + [evidence()]))
    accepted = request(segments=[segment(fields=[field(evidence=refs)])])
    validate_manual_input(accepted)
    accepted["segments"][0]["fields"].append(field(field_name="other", evidence=[evidence()]))
    with pytest.raises(ValueError):
        validate_manual_input(accepted)


def test_exact_canonical_request_byte_boundary():
    data = request(segments=[segment("a", fields=many_fields(70, "x" * 3600)),
                             segment("b", 2, fields=many_fields(70, "x" * 3600))])
    dump = validate_manual_input(data).model_dump(mode="json", by_alias=True, warnings="error")
    remaining = models.MAX_REQUEST_BYTES - len(canonical(dump))
    assert remaining > 0
    for seg in dump["segments"]:
        for item in seg["fields"]:
            added = min(4000 - len(item["raw"]), remaining)
            item["raw"] += "x" * added
            remaining -= added
    assert remaining == 0
    assert len(canonical(dump)) == 512 * 1024
    assert validate_manual_input(dump)
    item = next(item for seg in dump["segments"] for item in seg["fields"] if len(item["raw"]) < 4000)
    item["raw"] += "x"
    with pytest.raises(ValueError):
        validate_manual_input(dump)
    with pytest.raises(ValidationError):
        SaveMetadataInput.model_validate(dump)


def test_canonical_byte_count_matches_json_for_escaping_and_unicode(monkeypatch):
    dump = validate_manual_input(request(segments=[segment(fields=[
        field("😀\r\n\x00e\u0301", evidence=[evidence("😀\r\n\x00e\u0301")])
    ])])).model_dump(mode="json", by_alias=True, warnings="error")
    size = len(canonical(dump))
    monkeypatch.setattr(models, "MAX_REQUEST_BYTES", size)
    models._check_request_bytes(dump)
    monkeypatch.setattr(models, "MAX_REQUEST_BYTES", size - 1)
    with pytest.raises(ValueError):
        models._check_request_bytes(dump)


def test_revalidation_detaches_nested_lists_and_models():
    original = validate_manual_input(request(segments=[segment(fields=[
        field(["0012", False], evidence=[evidence(item_index=0)])
    ])]))
    cloned = validate_manual_input(original)
    assert cloned is not original
    assert cloned.segments is not original.segments
    assert cloned.segments[0].fields is not original.segments[0].fields
    left = original.segments[0].fields[0]
    right = cloned.segments[0].fields[0]
    assert left.raw is not right.raw
    assert left.evidence is not right.evidence
    left.raw.append("new")
    left.evidence.clear()
    assert right.raw == ["0012", False]
    assert len(right.evidence) == 1
    source = request(segments=[segment(fields=[field([1])])])
    saved = validate_manual_input(source)
    source["segments"][0]["fields"][0]["raw"].append(2)
    assert saved.segments[0].fields[0].raw == [1]
    with pytest.raises(ValidationError):
        saved.expected_head = ID


def test_mutated_and_constructed_models_cannot_bypass_helper():
    value = validate_manual_input(request())
    value.segments.append(value.segments[0])
    with pytest.raises(ValueError):
        validate_manual_input(value)
    invalid = SegmentInput.model_construct(**segment(page_start=True))
    forged = SaveMetadataInput.model_construct(expected_head=None, expected_sha256=None, segments=[invalid])
    with pytest.raises(ValueError):
        validate_manual_input(forged)
    good = SaveMetadataInput.model_construct(expected_head=None, expected_sha256=None,
                                             segments=[SegmentInput(**segment())])
    assert validate_manual_input(good) is not good
    nested = validate_manual_input(request(segments=[segment(fields=[field([1])])]))
    nested.segments[0].fields[0].raw.append(None)
    with pytest.raises(ValueError):
        validate_manual_input(nested)


@pytest.mark.parametrize("method", ["construct", "copy"])
@pytest.mark.parametrize("target,attribute,bad", [
    ("segment", "page_start", True),
    ("segment", "page_end", True),
    ("segment", "type_version", True),
    ("ref", "artifact_version", True),
    ("ref", "text_version", True),
    ("ref", "page", True),
    ("ref", "start", False),
    ("ref", "end", True),
    ("ref", "item_index", False),
    ("root", "segments", "tuple"),
    ("segment", "fields", "tuple"),
    ("field", "evidence", "tuple"),
    ("field", "raw", "tuple"),
])
def test_nested_forged_models_are_strictly_revalidated(method, target, attribute, bad):
    original = validate_manual_input(request(segments=[segment(fields=[
        field([True], evidence=[evidence(item_index=0)])
    ])]))
    nodes = {
        "root": original,
        "segment": original.segments[0],
        "field": original.segments[0].fields[0],
        "ref": original.segments[0].fields[0].evidence[0],
    }
    node = nodes[target]
    replacement = tuple(getattr(node, attribute)) if bad == "tuple" else bad
    updates = {attribute: replacement}
    if method == "construct":
        forged = type(node).model_construct(**{**node.__dict__, **updates})
    else:
        forged = node.model_copy(update=updates)
    assert type(getattr(forged, attribute)) is type(replacement)
    if target == "ref":
        forged = nodes["field"].model_copy(update={"evidence": [forged]})
    if target in ("ref", "field"):
        forged = nodes["segment"].model_copy(update={"fields": [forged]})
    if target != "root":
        forged = original.model_copy(update={"segments": [forged]})
    with pytest.raises(ValueError) as caught:
        validate_manual_input(forged)
    assert str(caught.value) == "Invalid manual metadata input."
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("mutation", ["raw_null", "raw_float", "evidence_bool", "evidence_scalar_index"])
def test_mutated_nested_lists_are_revalidated(mutation):
    original = validate_manual_input(request(segments=[segment(fields=[
        field([True], evidence=[evidence(item_index=0)])
    ])]))
    item = original.segments[0].fields[0]
    if mutation == "raw_null":
        item.raw.append(None)
    elif mutation == "raw_float":
        item.raw.append(1.0)
    elif mutation == "evidence_bool":
        item.evidence.append(item.evidence[0].model_copy(update={"page": True}))
    else:
        item.evidence.append(EvidenceRef(**evidence()))
    with pytest.raises(ValueError):
        validate_manual_input(original)


@pytest.mark.parametrize("source_kind", ["instance", "dictionary"])
def test_valid_boolean_clone_detaches_every_nested_list(source_kind):
    original = validate_manual_input(request(segments=[segment(fields=[
        field([True, False, 1, 0], evidence=[evidence(item_index=0)]),
        field(False, field_name="other"),
    ])]))
    source = original if source_kind == "instance" else {
        "expected_head": None, "expected_sha256": None, "segments": original.segments,
    }
    cloned = validate_manual_input(source)
    assert cloned == original
    assert cloned is not original
    assert cloned.segments is not original.segments
    for left_segment, right_segment in zip(original.segments, cloned.segments):
        assert right_segment is not left_segment
        assert right_segment.fields is not left_segment.fields
        for left, right in zip(left_segment.fields, right_segment.fields):
            assert right is not left
            assert right.evidence is not left.evidence
            if isinstance(left.raw, list):
                assert right.raw is not left.raw
                assert [type(value) for value in right.raw] == [bool, bool, int, int]
            else:
                assert right.raw is False
            for left_ref, right_ref in zip(left.evidence, right.evidence):
                assert right_ref is not left_ref
    original.segments[0].fields[0].raw.clear()
    original.segments[0].fields[0].evidence.clear()
    original.segments[0].fields.clear()
    original.segments.clear()
    assert cloned.segments[0].fields[0].raw == [True, False, 1, 0]
    assert len(cloned.segments[0].fields[0].evidence) == 1
    assert cloned.segments[0].fields[1].raw is False


def test_fixed_public_errors_do_not_retain_sensitive_failures():
    marker = "SENSITIVE_RAW_QUOTE_PROVENANCE"
    cases = [request(segments=[segment(fields=[field(marker, provenance=marker)])]),
             request(segments=[segment(fields=[field(marker, evidence=[evidence(marker, end=1)])])]),
             request(segments=[segment(fields=[field([marker, None])])]), object()]
    for data in cases:
        with pytest.raises(ValueError) as caught:
            validate_manual_input(data)
        assert str(caught.value) == "Invalid manual metadata input."
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None
        assert marker not in str(caught.value)
    with pytest.raises(ValidationError) as caught:
        ManualFieldInput(**field(marker, provenance=marker))
    assert marker not in str(caught.value)


def test_import_and_validation_have_no_external_effects(monkeypatch):
    # Read/compile before installing guards: module loading itself needs source I/O.
    source = pathlib.Path(models.__file__).read_text(encoding="utf-8")
    code = compile(source, models.__file__, "exec")
    probe = types.ModuleType("app._metadata_models_purity_probe")
    probe.__package__ = "app"
    monkeypatch.setitem(sys.modules, probe.__name__, probe)
    payload = request(segments=[segment(fields=[field(evidence=[evidence()])])])

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected external effect")

    for target, names in (
        (builtins, ("open",)), (io, ("open",)),
        (pathlib.Path, ("open", "read_text", "read_bytes", "write_text", "write_bytes")),
        (socket, ("socket", "create_connection", "getaddrinfo")),
        (threading.Thread, ("start",)),
        (time, ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "sleep")),
        (uuid, ("uuid1", "uuid3", "uuid4", "uuid5")),
    ):
        for name in names:
            monkeypatch.setattr(target, name, forbidden)
    exec(code, probe.__dict__)
    saved = probe.validate_manual_input(payload)
    assert saved.segments[0].segment_id is None
    assert saved.segments[0].fields[0].raw == "0012"
    assert probe.SaveMetadataReceipt(revision_id=ID, sha256=HASH, segment_ids={"row": ID})


STAMP = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)


def provenance(**updates):
    data = dict(actor_subject="user:1", auth_source="local_jwt", recorded_at=STAMP)
    data.update(updates)
    return data


def stored_field(raw="0012", **updates):
    data = dict(field_name="identifier", raw=raw, normalized=copy.deepcopy(raw),
                origin="manual", status="needs_review", evidence=[], provenance=provenance())
    data.update(updates)
    return data


def frozen_schema(description="Identifier", count=1):
    composed = models.compose_schema([
        models.FieldSpec(name="identifier" if count == 1 else f"f{i}",
                         type="string", description=description)
        for i in range(count)
    ], [])
    return dict(schema_sha256=composed.sha256, schema=json.loads(composed.canonical))


def stored_segment(schema_hash, number=1, **updates):
    data = dict(segment_id=f"{number:032x}", page_start=number, page_end=number,
                context_id=ID, type_id=ID, type_version=1, schema_sha256=schema_hash,
                fields=[stored_field(evidence=[evidence(page=number)])])
    data.update(updates)
    return data


def snapshot(**updates):
    schema = frozen_schema()
    data = dict(revision_id="c" * 32, parent_revision_id=None, job_id=ID, document_id=ID,
                canonical_sha256=HASH, input_pdf_sha256=HASH, output_pdf_sha256=HASH,
                page_count=2, created_at=STAMP, author=provenance(), schemas=[schema],
                segments=[stored_segment(schema["schema_sha256"], i) for i in (1, 2)],
                state="draft")
    data.update(updates)
    return data


def approval(**updates):
    data = dict(actor_subject="user:2", auth_source="oauth", approved_at=STAMP,
                reviewed_revision_id="d" * 32, reviewed_sha256=HASH)
    data.update(updates)
    return data


def test_snapshot_provenance_and_approval_are_strict_and_closed():
    assert models.ProvenanceRecord(**provenance()).execution_id is None
    for source in ("local_jwt", "oauth", "service", "legacy", "unknown"):
        assert models.ProvenanceRecord(**provenance(auth_source=source)).auth_source == source
    assert models.ProvenanceRecord(**provenance(actor_subject="service:" + ID)).actor_subject == "service:" + ID
    for actor in ("user:0", "user:-1", "user:01", "user:1\n", "user:١", "service:" + "A" * 32, True):
        with pytest.raises(ValidationError):
            models.ProvenanceRecord(**provenance(actor_subject=actor))
    for updates in ({"auth_source": "token"}, {"execution_id": True},
                    {"recorded_at": STAMP.replace(tzinfo=None)},
                    {"recorded_at": STAMP.isoformat()}, {"recorded_at": 0},
                    {"email": "SENSITIVE"}):
        with pytest.raises(ValidationError):
            models.ProvenanceRecord(**provenance(**updates))
    assert models.ApprovalRecord(**approval(auth_source="local_jwt"))
    assert models.ApprovalRecord(**approval())
    for updates in ({"actor_subject": "service:" + ID}, {"actor_subject": "user:0"},
                    {"auth_source": "service"}, {"auth_source": "legacy"},
                    {"approved_at": STAMP.replace(tzinfo=None)},
                    {"approved_at": STAMP.isoformat()}, {"reviewed_revision_id": True},
                    {"reviewed_sha256": "B" * 64}, {"human_verified": True}, {"mfa": True}):
        with pytest.raises(ValidationError):
            models.ApprovalRecord(**approval(**updates))


def test_snapshot_dates_normalize_and_roundtrip_without_python_string_coercion():
    offset = timezone(timedelta(hours=-3))
    local = STAMP.astimezone(offset)
    first = snapshot(state="approved", parent_revision_id="d" * 32, approval=approval())
    second = copy.deepcopy(first)
    second["created_at"] = local
    second["author"]["recorded_at"] = local
    second["approval"]["approved_at"] = local
    for seg in second["segments"]:
        seg["fields"][0]["provenance"]["recorded_at"] = local
    expected = models.snapshot_bytes(first)
    assert models.snapshot_bytes(second) == expected
    restored = models.MetadataSnapshot.model_validate_json(expected, strict=True)
    assert restored.created_at == STAMP
    assert restored.created_at.utcoffset() == timedelta(0)
    assert models.snapshot_bytes(restored) == expected
    wire = json.loads(expected)
    with pytest.raises(ValueError):
        models.snapshot_bytes(wire)
    for invalid in (0, True, "2026-10-16T12:00:00"):
        broken = copy.deepcopy(wire)
        broken["created_at"] = invalid
        with pytest.raises(ValidationError):
            models.MetadataSnapshot.model_validate_json(canonical(broken), strict=True)
    wire["created_at"] = local.isoformat()
    assert models.snapshot_bytes(models.MetadataSnapshot.model_validate_json(canonical(wire), strict=True)) == expected


@pytest.mark.parametrize("raw,normalized", [
    (None, None), (False, True), ("0012", 12), ([], []), (["0012", False], [12, False]),
])
def test_snapshot_field_structural_cardinality(raw, normalized):
    value = models.FieldValueRecord(**stored_field(raw, normalized=normalized))
    assert value.raw == raw
    assert value.normalized == normalized
    assert type(value.raw) is type(raw)
    assert type(value.normalized) is type(normalized)


@pytest.mark.parametrize("updates", [
    {"raw": None, "normalized": "0012"}, {"normalized": None}, {"normalized": []},
    {"raw": [1], "normalized": [1, 2]}, {"raw": [1], "normalized": 1},
    {"normalized": 1.0}, {"raw": (1,), "normalized": [1]},
    {"normalized": [None]}, {"status": "not_found"}, {"evidence": ()},
    {"origin": "extracted"}, {"alternatives": []}, {"human_reviewed": True},
])
def test_invalid_snapshot_field_shapes(updates):
    with pytest.raises(ValidationError):
        models.FieldValueRecord(**stored_field(**updates))


def test_snapshot_field_states_execution_and_evidence_indices():
    for status in ("conflict", "needs_review", "reviewed"):
        assert models.FieldValueRecord(**stored_field(status=status)).status == status
    assert models.FieldValueRecord(**stored_field(None, status="not_found"))
    with pytest.raises(ValidationError):
        models.FieldValueRecord(**stored_field(None, status="not_found", evidence=[evidence()]))
    for origin in ("manual", "declared", "inherited"):
        assert models.FieldValueRecord(**stored_field(origin=origin))
        with pytest.raises(ValidationError):
            models.FieldValueRecord(**stored_field(origin=origin, provenance=provenance(execution_id=ID)))
    assert models.FieldValueRecord(**stored_field(origin="extracted", provenance=provenance(execution_id=ID)))
    assert models.FieldValueRecord(**stored_field([1], evidence=[evidence(item_index=0)]))
    for raw, refs in (([1], [evidence()]), ([1], [evidence(item_index=1)]),
                      (0, [evidence(item_index=0)]), (None, [evidence()])):
        with pytest.raises(ValidationError):
            models.FieldValueRecord(**stored_field(raw, evidence=refs))


def test_schema_snapshot_preserves_original_canonical_envelope_and_alias():
    source = frozen_schema()
    result = models.SchemaSnapshot(**source)
    assert result.model_dump(mode="json", by_alias=True) == source
    assert "schema_document" not in result.model_dump(by_alias=True)
    assert hashlib.sha256(canonical(source["schema"])).hexdigest() == source["schema_sha256"]
    broken = copy.deepcopy(source)
    broken["schema_document"] = broken.pop("schema")
    with pytest.raises(ValidationError):
        models.SchemaSnapshot(**broken)


@pytest.mark.parametrize("mutation", [
    "extra", "ref", "version", "fields", "hash", "tuple", "default", "null",
    "bool", "nested_tuple", "oversize",
])
def test_schema_snapshot_rejects_noncanonical_or_oversized_envelopes(mutation):
    source = frozen_schema(count=40) if mutation == "oversize" else frozen_schema()
    document = source["schema"]
    if mutation == "extra":
        document["extra"] = "SENSITIVE"
    elif mutation == "ref":
        document["$ref"] = "https://invalid.example/schema"
    elif mutation == "version":
        document["schema_version"] = 1
    elif mutation == "fields":
        document["fields"] = []
    elif mutation == "hash":
        source["schema_sha256"] = "0" * 64
    elif mutation == "tuple":
        document["common_fields"] = tuple(document["common_fields"])
    elif mutation in ("default", "null", "bool", "nested_tuple"):
        for key in ("common_fields", "fields"):
            item = document[key][0]
            if mutation == "default":
                del item["label"]
            elif mutation == "null":
                item["required_when"] = None
            elif mutation == "bool":
                item["required"] = 1
            else:
                item["examples"] = ()
    else:
        for key in ("common_fields", "fields"):
            for item in document[key]:
                item["description"] = "x" * 4000
        assert len(canonical(document)) > models.MAX_PAYLOAD_BYTES
    # Even the correct hash of a malformed original must not legitimize it.
    if mutation != "hash":
        source["schema_sha256"] = hashlib.sha256(canonical(document)).hexdigest()
    with pytest.raises(ValidationError):
        models.SchemaSnapshot(**source)


def test_schema_original_byte_limit_is_checked_before_composition(monkeypatch):
    source = frozen_schema()
    source["schema"]["unexpected"] = "x" * (models.MAX_PAYLOAD_BYTES + 1)

    def forbidden(*args, **kwargs):
        raise AssertionError("Composition ran before the original envelope limit")

    monkeypatch.setattr(models, "compose_schema", forbidden)
    with pytest.raises(ValidationError):
        models.SchemaSnapshot(**source)


def test_snapshot_shared_schema_and_exact_deterministic_bytes():
    source = snapshot()
    original = copy.deepcopy(source)
    model = models.MetadataSnapshot.model_validate(source, strict=True)
    encoded = models.snapshot_bytes(source)
    assert encoded == canonical(model.model_dump(mode="json", by_alias=True, warnings="error"))
    assert encoded == models.snapshot_bytes(model) == models.snapshot_bytes(source)
    assert not encoded.endswith(b"\n")
    assert source == original
    wire = json.loads(encoded)
    assert len(wire["schemas"]) == 1
    assert len(wire["segments"]) == 2
    assert "schema" in wire["schemas"][0]
    assert "client_key" not in wire["segments"][0]
    digest = hashlib.sha256(encoded).hexdigest()
    assert len(digest) == 64
    assert digest.encode() not in encoded
    source["segments"][0]["fields"][0]["raw"] = "changed"
    assert encoded == models.snapshot_bytes(model)
    assert models.snapshot_bytes(source) != encoded


@pytest.mark.parametrize("mutation", [
    "schema_duplicate", "schema_unused", "schema_missing", "segment_duplicate",
    "field_duplicate", "overlap", "order", "range", "page_count", "document",
    "self_revision", "self_job", "draft_approval", "approved_missing", "approved_parent",
    "extra", "version_bool", "segments_tuple", "schemas_tuple", "fields_tuple",
])
def test_invalid_snapshot_structure_and_lineage(mutation):
    source = snapshot()
    if mutation == "schema_duplicate":
        source["schemas"].append(copy.deepcopy(source["schemas"][0]))
    elif mutation == "schema_unused":
        source["schemas"].append(frozen_schema("Unused"))
    elif mutation == "schema_missing":
        source["segments"][0]["schema_sha256"] = "0" * 64
    elif mutation == "segment_duplicate":
        source["segments"][1]["segment_id"] = source["segments"][0]["segment_id"]
    elif mutation == "field_duplicate":
        source["segments"][0]["fields"] *= 2
    elif mutation == "overlap":
        source["segments"][0]["page_end"] = 2
    elif mutation == "order":
        source["segments"].reverse()
    elif mutation == "range":
        source["segments"][0].update(page_start=2, page_end=1)
    elif mutation == "page_count":
        source["page_count"] = 1
    elif mutation == "document":
        source["document_id"] = "e" * 32
    elif mutation == "self_revision":
        source["parent_revision_id"] = source["revision_id"]
    elif mutation == "self_job":
        source["parent_job_id"] = ID
    elif mutation == "draft_approval":
        source["approval"] = approval()
    elif mutation == "approved_missing":
        source["state"] = "approved"
    elif mutation == "approved_parent":
        source.update(state="approved", approval=approval(), parent_revision_id="e" * 32)
    elif mutation == "extra":
        source["owner"] = "SENSITIVE"
    elif mutation == "version_bool":
        source["artifact_version"] = True
    elif mutation == "segments_tuple":
        source["segments"] = tuple(source["segments"])
    elif mutation == "schemas_tuple":
        source["schemas"] = tuple(source["schemas"])
    else:
        source["segments"][0]["fields"] = tuple(source["segments"][0]["fields"])
    with pytest.raises(ValueError):
        models.snapshot_bytes(source)


@pytest.mark.parametrize("updates", [
    {"document_id": "e" * 32}, {"artifact_version": 2}, {"text_version": True}, {"page": 2},
])
def test_snapshot_evidence_is_structurally_bound_to_its_segment(updates):
    source = snapshot()
    source["segments"][0]["fields"][0]["evidence"] = [evidence(**updates)]
    with pytest.raises(ValueError):
        models.snapshot_bytes(source)


@pytest.mark.parametrize("method", ["construct", "copy"])
@pytest.mark.parametrize("target,attribute,bad", [
    ("root", "artifact_version", True), ("root", "page_count", 2.0),
    ("root", "created_at", STAMP.isoformat()), ("root", "segments", "tuple"),
    ("segment", "type_version", True), ("segment", "fields", "tuple"),
    ("field", "raw", "tuple"), ("field", "normalized", "tuple"),
    ("field", "evidence", "tuple"), ("ref", "page", True),
    ("provenance", "recorded_at", STAMP.isoformat()),
])
def test_snapshot_forged_instances_are_validated_before_dump(method, target, attribute, bad):
    source = snapshot()
    source["segments"][0]["fields"] = [stored_field([True], evidence=[evidence(item_index=0)])]
    root = models.MetadataSnapshot.model_validate(source, strict=True)
    nodes = {"root": root, "segment": root.segments[0], "field": root.segments[0].fields[0],
             "ref": root.segments[0].fields[0].evidence[0],
             "provenance": root.segments[0].fields[0].provenance}
    node = nodes[target]
    replacement = tuple(getattr(node, attribute)) if bad == "tuple" else bad
    updates = {attribute: replacement}
    forged = (type(node).model_construct(**{**node.__dict__, **updates}) if method == "construct"
              else node.model_copy(update=updates))
    if target == "ref":
        forged = nodes["field"].model_copy(update={"evidence": [forged]})
    elif target == "provenance":
        forged = nodes["field"].model_copy(update={"provenance": forged})
    if target in ("ref", "provenance", "field"):
        forged = nodes["segment"].model_copy(update={"fields": [forged]})
    if target != "root":
        forged = root.model_copy(update={"segments": [forged, root.segments[1]]})
    with pytest.raises(ValueError) as caught:
        models.snapshot_bytes(forged)
    assert str(caught.value) == "Invalid metadata snapshot."
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.parametrize("mutation", ["raw", "evidence", "schema", "schema_tuple", "segments", "approval"])
def test_snapshot_mutable_state_is_revalidated_and_previous_bytes_remain_immutable(mutation):
    source = snapshot(state="approved", parent_revision_id="d" * 32, approval=approval())
    source["segments"][0]["fields"] = [stored_field([True], evidence=[evidence(item_index=0)])]
    model = models.MetadataSnapshot.model_validate(source, strict=True)
    saved = models.snapshot_bytes(model)
    if mutation == "raw":
        model.segments[0].fields[0].raw.append(None)
    elif mutation == "evidence":
        model.segments[0].fields[0].evidence.append(EvidenceRef(**evidence()))
    elif mutation == "schema":
        model.schemas[0].schema_document["fields"].clear()
    elif mutation == "schema_tuple":
        document = model.schemas[0].schema_document
        document["common_fields"] = tuple(document["common_fields"])
    elif mutation == "segments":
        model.segments.append(model.segments[0])
    else:
        model = model.model_copy(update={"approval": model.approval.model_copy(update={"approved_at": True})})
    with pytest.raises(ValueError):
        models.snapshot_bytes(model)
    assert isinstance(saved, bytes)
    restored = models.MetadataSnapshot.model_validate_json(saved, strict=True)
    assert models.snapshot_bytes(restored) == saved


def test_snapshot_fixed_errors_do_not_chain_or_echo_sensitive_content():
    marker = "SENSITIVE_RAW_QUOTE_PROVENANCE"
    source = snapshot()
    source["segments"][0]["fields"][0].update(raw=marker, provenance={"secret": marker})
    for value in (source, object(), json.loads(models.snapshot_bytes(snapshot()))):
        with pytest.raises(ValueError) as caught:
            models.snapshot_bytes(value)
        assert str(caught.value) == "Invalid metadata snapshot."
        assert marker not in str(caught.value)
        assert caught.value.__cause__ is None
        assert caught.value.__context__ is None


def test_snapshot_field_and_evidence_aggregate_limits():
    source = snapshot()
    for seg in source["segments"]:
        seg["fields"] = [stored_field(0, field_name=f"f{i}") for i in range(100)]
    assert models.snapshot_bytes(source)
    extra = stored_segment(source["schemas"][0]["schema_sha256"], 3, fields=[stored_field()])
    source.update(page_count=3)
    source["segments"].append(extra)
    with pytest.raises(ValueError):
        models.snapshot_bytes(source)
    source = snapshot()
    source["segments"][0]["fields"] = [stored_field(evidence=[evidence()] * 128)]
    source["segments"][1]["fields"] = []
    assert models.snapshot_bytes(source)
    source["segments"][0]["fields"].append(stored_field(field_name="other", evidence=[evidence()]))
    with pytest.raises(ValueError):
        models.snapshot_bytes(source)
    with pytest.raises(ValidationError):
        models.FieldValueRecord(**stored_field(evidence=[evidence()] * 129))
    with pytest.raises(ValidationError):
        models.SegmentRecord(**stored_segment(source["schemas"][0]["schema_sha256"],
            fields=[stored_field(field_name=f"f{i}") for i in range(101)]))


def test_snapshot_segment_and_schema_list_limits():
    source = snapshot(page_count=16)
    digest = source["schemas"][0]["schema_sha256"]
    source["segments"] = [stored_segment(digest, i, fields=[]) for i in range(1, 17)]
    assert models.snapshot_bytes(source)
    source["segments"].append(stored_segment(digest, 17, fields=[]))
    source["page_count"] = 17
    with pytest.raises(ValueError):
        models.snapshot_bytes(source)
    for key in ("schemas", "segments"):
        empty = snapshot()
        empty[key] = []
        with pytest.raises(ValueError):
            models.snapshot_bytes(empty)
    schemas = [frozen_schema(str(i)) for i in range(17)]
    with pytest.raises(ValueError):
        models.snapshot_bytes(snapshot(schemas=schemas))


def test_snapshot_distinct_valid_schemas_cannot_exceed_global_byte_limit():
    schemas = [frozen_schema(str(i) + "x" * 2000, count=40) for i in range(7)]
    for item in schemas:
        assert len(canonical(item["schema"])) <= models.MAX_PAYLOAD_BYTES
        assert models.SchemaSnapshot(**item)
    assert sum(len(canonical(item)) for item in schemas) > models.MAX_SNAPSHOT_BYTES
    source = snapshot(page_count=7, schemas=schemas, segments=[
        stored_segment(item["schema_sha256"], i, fields=[])
        for i, item in enumerate(schemas, 1)
    ])
    with pytest.raises(ValidationError):
        models.MetadataSnapshot.model_validate(source, strict=True)
    with pytest.raises(ValueError):
        models.snapshot_bytes(source)
    assert len(source["schemas"]) == 7


def test_snapshot_exact_byte_boundary_with_unicode_and_escaping(monkeypatch):
    source = snapshot()
    source["segments"][0]["fields"][0].update(raw="😀\r\n\x00e\u0301", normalized=False)
    encoded = models.snapshot_bytes(source)
    monkeypatch.setattr(models, "MAX_SNAPSHOT_BYTES", len(encoded))
    assert models.snapshot_bytes(source) == encoded
    monkeypatch.setattr(models, "MAX_SNAPSHOT_BYTES", len(encoded) - 1)
    with pytest.raises(ValueError):
        models.snapshot_bytes(source)
    with pytest.raises(ValidationError):
        models.MetadataSnapshot.model_validate(source, strict=True)


def test_snapshot_validation_and_serialization_have_no_external_effects(monkeypatch):
    source = snapshot()
    expected = models.snapshot_bytes(source)

    def forbidden(*args, **kwargs):
        raise AssertionError("Unexpected external effect")

    for target, names in (
        (builtins, ("open",)), (io, ("open",)),
        (pathlib.Path, ("open", "read_text", "read_bytes", "write_text", "write_bytes")),
        (socket, ("socket", "create_connection", "getaddrinfo")),
        (threading.Thread, ("start",)),
        (time, ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter", "sleep")),
        (uuid, ("uuid1", "uuid3", "uuid4", "uuid5")),
    ):
        for name in names:
            monkeypatch.setattr(target, name, forbidden)
    assert models.snapshot_bytes(source) == expected
    assert models.snapshot_bytes(models.MetadataSnapshot.model_validate_json(expected, strict=True)) == expected
