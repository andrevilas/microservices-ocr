"""Executable structural acceptance tests for C1a only."""

import builtins
import copy
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
