"""Synthetic public assessment tests using real composition and C1/C2a validation.

No filesystem, PDF, catalog, approval service, or persistence is involved.
Focused cache tests additionally exercise resource boundaries.
"""

from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import unittest
from unittest.mock import patch

import pytest

from app.document_models import FieldSpec, compose_schema
from app.metadata_evidence import (
    Blocker,
    CanonicalPageText,
    _PageCache,
    _verify_record_evidence,
    assess_snapshot,
)
from app.metadata_models import (
    ApprovalRecord,
    EvidenceRef,
    FieldValueRecord,
    ManualFieldInput,
    MetadataSnapshot,
    ProvenanceRecord,
    SchemaSnapshot,
    SegmentRecord,
    snapshot_bytes,
)
from app.metadata_validation import MetadataValidationError, normalize_manual_values


DOCUMENT = "a" * 32
SEGMENT = "b" * 32
CANONICAL = "c" * 64
NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


def page(text="A\r\n\x00😀e\u0301 Z", number=1):
    return CanonicalPageText(
        document_id=DOCUMENT,
        artifact_version=1,
        text_version=1,
        canonical_sha256=CANONICAL,
        page=number,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        text=text,
    )


def identity():
    return make_snapshot([])


def provenance():
    return ProvenanceRecord(
        actor_subject="user:1",
        auth_source="local_jwt",
        recorded_at=NOW,
    )


def reference(source, start=0, end=None, index=None):
    if end is None:
        end = len(source.text)
    return EvidenceRef(
        document_id=source.document_id,
        artifact_version=source.artifact_version,
        text_version=source.text_version,
        page=source.page,
        text_sha256=source.text_sha256,
        start=start,
        end=end,
        quote=source.text[start:end],
        item_index=index,
    )


def record(name="title", value="interpreted", evidence=(), status="reviewed", origin="manual"):
    sample = value[0] if type(value) is list and value else value
    kind = "boolean" if type(sample) is bool else "integer" if type(sample) is int else "string"
    field = spec(name, type=kind, cardinality="many" if type(value) is list else "one")
    normalized = normalize_manual_values(
        [field], [ManualFieldInput(field_name=name, raw=value)], provenance()
    )[0]
    source = provenance()
    if origin == "extracted":
        source = source.model_copy(update={"execution_id": "1" * 32})
    # Copies let negative fixtures reach the public revalidation boundary.
    return normalized.model_copy(update={
        "origin": origin,
        "status": status,
        "evidence": list(evidence),
        "provenance": source,
    })


def segment(records=()):
    return SegmentRecord(
        segment_id=SEGMENT,
        page_start=1,
        page_end=300,
        context_id="d" * 32,
        type_id="e" * 32,
        type_version=1,
        schema_sha256="f" * 64,
        fields=[],
    ).model_copy(update={"fields": list(records)})


def spec(name="title", **options):
    return FieldSpec.model_validate(
        {"name": name, "type": "string", "description": "Synthetic field", **options},
        strict=True,
    )


def make_snapshot(fields, records=(), *, segments=None, document_id=DOCUMENT):
    composed = compose_schema(list(fields), [])
    schema = SchemaSnapshot.model_validate({
        "schema_sha256": composed.sha256,
        "schema": json.loads(composed.canonical),
    }, strict=True)
    originals = list(segments) if segments is not None else [segment(records)]
    safe = [item.model_copy(update={
        "schema_sha256": composed.sha256, "fields": []
    }) for item in originals]
    baseline = MetadataSnapshot(
        revision_id="1" * 32,
        parent_revision_id=None,
        job_id=document_id,
        document_id=document_id,
        canonical_sha256=CANONICAL,
        input_pdf_sha256="2" * 64,
        output_pdf_sha256="3" * 64,
        page_count=300,
        created_at=NOW,
        author=provenance(),
        schemas=[schema],
        segments=safe,
        state="draft",
        approval=None,
    )
    # The complete baseline is real and valid. Inject records without prematurely
    # rejecting intentionally copied/constructed negative fixtures.
    return baseline.model_copy(update={"segments": [
        item.model_copy(update={"fields": list(original.fields)})
        for item, original in zip(baseline.segments, originals)
    ]})


def forbidden_source(number):
    raise AssertionError("uncited source requested")


def public_error(code, snapshot, source=forbidden_source):
    with pytest.raises(MetadataValidationError) as caught:
        assess_snapshot(snapshot, source)
    error = caught.value
    assert error.code == code
    assert error.__cause__ is None
    assert error.__context__ is None
    assert "SENSITIVE_MARKER" not in repr(error)
    assert "SENSITIVE_MARKER" not in str(error)
    return error


class ErrorAssertions(unittest.TestCase):
    def assert_metadata_error(self, code, operation):
        with self.assertRaises(MetadataValidationError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertIsNone(caught.exception.__context__)
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("SENSITIVE_MARKER", repr(caught.exception))
        self.assertNotIn("SENSITIVE_MARKER", str(caught.exception))
        return caught.exception


class LiteralEvidenceTests(ErrorAssertions):
    def verify(self, source_page, value, source=None):
        fields = [spec(value.field_name, cardinality="many" if type(value.raw) is list else "one")]
        return assess_snapshot(
            make_snapshot(fields, [value]),
            source if source is not None else lambda number: source_page,
        )

    def test_unicode_crlf_nul_and_codepoint_offsets(self):
        canonical = page()
        for start, end in ((0, 3), (3, 4), (4, 5), (5, 7)):
            with self.subTest(start=start):
                self.verify(canonical, record(evidence=[reference(canonical, start, end)]))

    def test_interpretation_need_not_be_substring(self):
        canonical = page("texto citado")
        self.verify(canonical, record(value="interpretação distinta", evidence=[reference(canonical)]))

    def test_wrong_quote_even_alongside_valid_reference(self):
        canonical = page("literal")
        good = reference(canonical)
        bad = good.model_copy(update={"quote": "LITERAL"})
        self.assert_metadata_error(
            "evidence_invalid",
            lambda: self.verify(canonical, record(evidence=[good, bad])),
        )

    def test_no_trim_or_unicode_or_newline_normalization(self):
        for original, changed in ((" x ", "x"), ("e\u0301", "é"), ("\r\n", "\n")):
            canonical = page(original)
            bad = reference(canonical).model_copy(update={"quote": changed, "end": len(changed)})
            self.assert_metadata_error(
                "evidence_invalid", lambda: self.verify(canonical, record(evidence=[bad]))
            )

    def test_utf16_offsets_are_not_codepoint_offsets(self):
        canonical = page("😀x")
        bad = reference(canonical, 1, 2).model_copy(update={"start": 2, "end": 3})
        self.assert_metadata_error(
            "evidence_invalid", lambda: self.verify(canonical, record(evidence=[bad]))
        )

    def test_reference_identity_hash_and_versions(self):
        canonical = page("literal")
        for updates, code in (
            ({"document_id": "0" * 32}, "metadata_invalid_input"),
            ({"artifact_version": 2}, "metadata_invalid_input"),
            ({"text_version": 2}, "metadata_invalid_input"),
            ({"text_sha256": "0" * 64}, "evidence_invalid"),
            ({"start": -1}, "metadata_invalid_input"),
            ({"end": 500}, "metadata_invalid_input"),
            ({"item_index": 0}, "metadata_invalid_input"),
        ):
            with self.subTest(updates=updates):
                bad = reference(canonical).model_copy(update=updates)
                self.assert_metadata_error(
                    code, lambda: self.verify(canonical, record(evidence=[bad]))
                )

    def test_segment_boundary_and_list_index(self):
        canonical = page("literal", 2)
        value = record(value=["one", "two"], evidence=[reference(canonical, index=1)])
        self.assertTrue(self.verify(canonical, value).approvable)
        narrow = segment([value]).model_copy(update={"page_end": 1})
        public_error(
            "metadata_invalid_input",
            make_snapshot([spec(cardinality="many")], segments=[narrow]),
        )
        for index in (None, -1, 2, True):
            bad = reference(canonical, index=1).model_copy(update={"item_index": index})
            self.assert_metadata_error(
                "metadata_invalid_input",
                lambda: self.verify(canonical, value.model_copy(update={"evidence": [bad]})),
            )

    def test_empty_or_truncated_page_cannot_support_reference(self):
        original = page("literal")
        for returned in (page(""), page("lit")):
            self.assert_metadata_error(
                "evidence_invalid",
                lambda: self.verify(returned, record(evidence=[reference(original)])),
            )

    def test_null_cannot_have_evidence(self):
        canonical = page("literal")
        invalid = record().model_copy(update={"raw": None, "normalized": None, "evidence": [reference(canonical)]})
        self.assert_metadata_error("metadata_invalid_input", lambda: self.verify(canonical, invalid))


class SourceTests(ErrorAssertions):
    def test_one_call_and_one_hash_per_distinct_page(self):
        pages = {1: page("one"), 2: page("two", 2)}
        calls = []

        def source(number):
            calls.append(number)
            return pages[number]

        cache = _PageCache(identity(), source)
        real_sha256 = hashlib.sha256
        with patch("app.metadata_evidence.hashlib.sha256", wraps=real_sha256) as sha:
            for name, number in (("a", 1), ("b", 1), ("c", 2), ("d", 2)):
                value = record(name, evidence=[reference(pages[number]), reference(pages[number])])
                _verify_record_evidence(cache, segment(), value)
            self.assertEqual(sha.call_count, 2)
        self.assertEqual(calls, [1, 2])

    def test_no_evidence_never_calls_source(self):
        def forbidden(number):
            self.fail("uncited source requested")

        _verify_record_evidence(_PageCache(identity(), forbidden), segment(), record())

    def test_caches_are_local_to_operation(self):
        calls = []
        canonical = page("literal")

        def source(number):
            calls.append(number)
            return canonical

        for owner in ("owner-one", "owner-two"):
            cache = _PageCache(identity(), source)
            self.assertEqual(cache.get(1), canonical, owner)
        self.assertEqual(calls, [1, 1])

    def test_callback_exception_is_sanitized_outside_except(self):
        def failed(number):
            raise RuntimeError("SENSITIVE_MARKER")

        self.assert_metadata_error("canonical_source_invalid", lambda: _PageCache(identity(), failed).get(1))

    def test_sanitized_callback_error_propagates(self):
        expected = MetadataValidationError("canonical_source_invalid")

        def failed(number):
            raise expected

        with self.assertRaises(MetadataValidationError) as caught:
            _PageCache(identity(), failed).get(1)
        self.assertIs(caught.exception, expected)

    def test_malformed_callback_values(self):
        canonical = page("literal")
        for returned in (
            None,
            {},
            replace(canonical, page=True),
            replace(canonical, artifact_version=True),
            replace(canonical, text_version=2),
            replace(canonical, document_id="invalid"),
            replace(canonical, canonical_sha256="invalid"),
            replace(canonical, text_sha256="invalid"),
            replace(canonical, text=b"literal"),
            replace(canonical, text="\ud800SENSITIVE_MARKER"),
            replace(canonical, text_sha256="0" * 64),
        ):
            with self.subTest(return_type=type(returned).__name__):
                self.assert_metadata_error(
                    "canonical_source_invalid",
                    lambda: _PageCache(identity(), lambda n: returned).get(1),
                )

    def test_well_formed_unrelated_page_is_not_evidence(self):
        canonical = page("literal")
        for returned in (
            replace(canonical, document_id="0" * 32),
            replace(canonical, canonical_sha256="0" * 64),
            replace(canonical, page=2),
        ):
            self.assert_metadata_error(
                "canonical_source_invalid", lambda: _PageCache(identity(), lambda n: returned).get(1)
            )

    def test_absent_page_is_operation_failure(self):
        def absent(number):
            raise KeyError(number)

        self.assert_metadata_error("canonical_source_invalid", lambda: _PageCache(identity(), absent).get(1))

    def test_page_and_total_limits(self):
        oversized = page("x" * 500_001)
        self.assert_metadata_error(
            "canonical_source_invalid", lambda: _PageCache(identity(), lambda n: oversized).get(1)
        )
        text = "x" * 500_000
        calls = []

        def source(number):
            calls.append(number)
            return page(text, number)

        cache = _PageCache(identity(), source)
        for number in range(1, 11):
            cache.get(number)
        self.assert_metadata_error("canonical_source_invalid", lambda: cache.get(11))
        self.assertEqual(calls, list(range(1, 12)))
        for number in (0, 301, True):
            self.assert_metadata_error("evidence_invalid", lambda: cache.get(number))

    def test_page_dataclass_is_frozen(self):
        canonical = page()
        with self.assertRaises(FrozenInstanceError):
            canonical.text = "replacement"


class ReadinessRuleTests(ErrorAssertions):
    def blockers(self, fields, records):
        return assess_snapshot(
            make_snapshot(fields, records), lambda number: page("literal", number)
        ).blockers

    def test_omitted_required_field_and_optional_null(self):
        fields = [spec("required_title", required=True), spec("optional", critical=True)]
        values = [record("optional", None, status="not_found")]
        self.assertEqual(
            self.blockers(fields, values),
            [Blocker("required_missing", SEGMENT, "required_title")],
        )
        self.assertEqual(self.blockers([spec(critical=True)], []), [])

    def test_conditional_required_uses_existing_helper(self):
        fields = [
            spec("kind"),
            spec("detail", required_when={"field": "kind", "equals": "special"}),
        ]
        self.assertEqual(
            self.blockers(fields, [record("kind", "special")]),
            [Blocker("required_missing", SEGMENT, "detail")],
        )
        self.assertEqual(self.blockers(fields, [record("kind", "ordinary")]), [])

    def test_empty_optional_and_required_are_distinct(self):
        for value in ("", " \t\n"):
            self.assertEqual(
                self.blockers([spec()], [record(value=value)]),
                [Blocker("empty_value", SEGMENT, "title")],
            )
            self.assertEqual(
                self.blockers([spec(required=True)], [record(value=value)]),
                [Blocker("required_missing", SEGMENT, "title")],
            )

    def test_reviewed_is_readiness_not_an_automatic_transition(self):
        value = record(status="needs_review")
        before = value.model_dump()
        self.assertEqual(
            self.blockers([spec()], [value]),
            [Blocker("review_required", SEGMENT, "title")],
        )
        self.assertEqual(value.model_dump(), before)
        self.assertEqual(self.blockers([spec()], [record()]), [])

    def test_critical_and_explicit_evidence_required(self):
        for options in ({"critical": True}, {"evidence_required": True}):
            for origin in ("manual", "declared"):
                self.assertEqual(
                    self.blockers([spec(**options)], [record(origin=origin)]),
                    [Blocker("evidence_missing", SEGMENT, "title")],
                )

    def test_each_list_item_needs_explicit_reference(self):
        canonical = page("literal")
        fields = [spec(cardinality="many", evidence_required=True)]
        value = record(value=["one", "two"], evidence=[reference(canonical, index=0)])
        self.assertEqual(
            self.blockers(fields, [value]),
            [Blocker("evidence_missing", SEGMENT, "title", 1)],
        )
        complete = value.model_copy(update={
            "evidence": [reference(canonical, index=0), reference(canonical, index=1)]
        })
        self.assertEqual(self.blockers(fields, [complete]), [])

    def test_blank_list_item_does_not_hide_other_items(self):
        fields = [spec(cardinality="many", evidence_required=True)]
        value = record(value=[" ", "meaningful"], status="needs_review")
        self.assertEqual(
            self.blockers(fields, [value]),
            [
                Blocker("empty_value", SEGMENT, "title"),
                Blocker("evidence_missing", SEGMENT, "title", 1),
                Blocker("review_required", SEGMENT, "title"),
            ],
        )
        fields = [spec(cardinality="many", evidence_required=True, required=True)]
        self.assertEqual(
            self.blockers(fields, [value]),
            [
                Blocker("evidence_missing", SEGMENT, "title", 1),
                Blocker("required_missing", SEGMENT, "title"),
                Blocker("review_required", SEGMENT, "title"),
            ],
        )

    def test_empty_list(self):
        self.assertEqual(
            self.blockers([spec(cardinality="many")], [record(value=[])]),
            [Blocker("empty_value", SEGMENT, "title")],
        )

    def test_zero_and_false_are_significant(self):
        for field_type, scalar in (("integer", 0), ("boolean", False)):
            fields = [spec(type=field_type, cardinality="many", required=True, evidence_required=True)]
            value = record(value=[scalar], status="needs_review")
            self.assertEqual(
                self.blockers(fields, [value]),
                [
                    Blocker("evidence_missing", SEGMENT, "title", 0),
                    Blocker("review_required", SEGMENT, "title"),
                ],
            )

    def test_reserved_states_and_origins_remain_errors(self):
        for value, code in (
            (record(origin="inherited"), "unsupported_metadata_origin"),
            (record(origin="extracted"), "unsupported_metadata_origin"),
            (record(status="conflict"), "unsupported_metadata_state"),
        ):
            self.assert_metadata_error(code, lambda: self.blockers([spec()], [value]))

    def test_invalid_type_and_divergent_normalized_are_not_blockers(self):
        for value in (
            record(value=True),
            record().model_copy(update={"normalized": "different"}),
        ):
            self.assert_metadata_error(
                "metadata_invalid_value", lambda: self.blockers([spec()], [value])
            )

    def test_schema_field_order_and_no_sensitive_values_in_results(self):
        fields = [spec("z", critical=True), spec("a", required=True)]
        values = [record("z", "SENSITIVE_MARKER", status="needs_review")]
        expected = [
            Blocker("evidence_missing", SEGMENT, "z"),
            Blocker("review_required", SEGMENT, "z"),
            Blocker("required_missing", SEGMENT, "a"),
        ]
        self.assertEqual(self.blockers(fields, values), expected)
        self.assertEqual(self.blockers(fields, values), expected)
        self.assertNotIn("SENSITIVE_MARKER", repr(expected))
        self.assertEqual(len(expected), len(set(expected)))

    def test_structurally_invalid_snapshot_fails_without_source(self):
        def forbidden(number):
            self.fail("source called before snapshot validation")

        self.assert_metadata_error("metadata_invalid_input", lambda: assess_snapshot({}, forbidden))


def test_public_happy_path_roundtrip_and_no_automatic_transition():
    canonical = page("literal")
    original = make_snapshot(
        [spec(evidence_required=True)], [record(evidence=[reference(canonical)])]
    )
    encoded = snapshot_bytes(original)
    snapshot = MetadataSnapshot.model_validate_json(encoded, strict=True)
    result = assess_snapshot(snapshot, lambda number: canonical)
    assert asdict(result) == {"approvable": True, "blockers": []}
    assert snapshot_bytes(snapshot) == encoded
    assert snapshot.state == "draft"
    assert snapshot.approval is None
    assert snapshot.segments[0].fields[0].status == "reviewed"


@pytest.mark.parametrize("status", ["reviewed", "needs_review"])
def test_public_decimal_interpretation_is_not_literal_truth(status):
    canonical = page("R$1,23")
    field = spec(type="decimal", evidence_required=True)
    value = normalize_manual_values(
        [field],
        [ManualFieldInput(field_name="title", raw="+001.2300", evidence=[reference(canonical)])],
        provenance(),
    )[0].model_copy(update={"status": status})
    assert value.normalized == "1.23"
    snapshot = make_snapshot([field], [value])
    before = snapshot_bytes(snapshot)
    result = assess_snapshot(snapshot, lambda number: canonical)
    expected = [] if status == "reviewed" else [Blocker("review_required", SEGMENT, "title")]
    assert result.blockers == expected
    assert result.approvable == (status == "reviewed")
    assert snapshot_bytes(snapshot) == before


@pytest.mark.parametrize("records", [[], [record(value=None, status="not_found")]])
@pytest.mark.parametrize("options", [{}, {"critical": True}, {"evidence_required": True}])
def test_public_optional_absence_never_loads_text(records, options):
    calls = []

    def source(number):
        calls.append(number)
        raise AssertionError("uncited source requested")

    result = assess_snapshot(make_snapshot([spec(**options)], records), source)
    assert result.approvable
    assert result.blockers == []
    assert calls == []


def test_public_incomplete_draft_and_redacted_result():
    marker = "SENSITIVE_MARKER"
    canonical = page(marker)
    fields = [spec(description=marker), spec("missing", required=True)]
    snapshot = make_snapshot(fields, [record(value=marker, status="needs_review", evidence=[reference(canonical)])])
    before = snapshot_bytes(snapshot)
    result = assess_snapshot(snapshot, lambda number: canonical)
    assert asdict(result) == {
        "approvable": False,
        "blockers": [
            {"code": "review_required", "segment_id": SEGMENT, "field_name": "title", "item_index": None},
            {"code": "required_missing", "segment_id": SEGMENT, "field_name": "missing", "item_index": None},
        ],
    }
    assert marker not in repr(result)
    assert snapshot_bytes(snapshot) == before


@pytest.mark.parametrize("bad_first", [False, True])
@pytest.mark.parametrize("critical", [False, True])
def test_public_checks_all_proofs_even_optional_with_valid_proof(bad_first, critical):
    canonical = page("literal")
    good = reference(canonical)
    bad = good.model_copy(update={"quote": "LITERAL"})
    evidence = [bad, good] if bad_first else [good, bad]
    public_error(
        "evidence_invalid",
        make_snapshot([spec(critical=critical)], [record(evidence=evidence)]),
        lambda number: canonical,
    )


@pytest.mark.parametrize("updates", [
    {"document_id": "0" * 32},
    {"canonical_sha256": "0" * 64},
    {"page": 2},
])
def test_public_well_formed_unrelated_source(updates):
    canonical = page("literal")
    snapshot = make_snapshot([spec()], [record(evidence=[reference(canonical)])])
    public_error("canonical_source_invalid", snapshot, lambda number: replace(canonical, **updates))


@pytest.mark.parametrize("case", [
    "none", "dict", "page_bool", "artifact_bool", "text_bool", "artifact_version",
    "text_version", "document", "canonical_hash", "text_hash", "bytes", "surrogate",
    "digest_mismatch", "oversized",
])
def test_public_malformed_source_aborts(case):
    canonical = page("literal")
    snapshot = make_snapshot([spec()], [record(evidence=[reference(canonical)])])
    updates = {
        "page_bool": {"page": True},
        "artifact_bool": {"artifact_version": True},
        "text_bool": {"text_version": True},
        "artifact_version": {"artifact_version": 2},
        "text_version": {"text_version": 2},
        "document": {"document_id": "invalid"},
        "canonical_hash": {"canonical_sha256": "invalid"},
        "text_hash": {"text_sha256": "invalid"},
        "bytes": {"text": b"literal"},
        "surrogate": {"text": "\ud800SENSITIVE_MARKER"},
        "digest_mismatch": {"text_sha256": "0" * 64},
    }
    if case == "none":
        returned = None
    elif case == "dict":
        returned = asdict(canonical)
    elif case == "oversized":
        returned = page("x" * 500_001)
    else:
        returned = replace(canonical, **updates[case])
    public_error("canonical_source_invalid", snapshot, lambda number: returned)


@pytest.mark.parametrize("exception_type", [RuntimeError, KeyError])
def test_public_callback_failure_is_sanitized(exception_type):
    canonical = page("literal")
    snapshot = make_snapshot([spec()], [record(evidence=[reference(canonical)])])

    def source(number):
        raise exception_type("SENSITIVE_MARKER")

    public_error("canonical_source_invalid", snapshot, source)


def test_public_sanitized_callback_error_is_preserved():
    canonical = page("literal")
    snapshot = make_snapshot([spec()], [record(evidence=[reference(canonical)])])
    expected = MetadataValidationError("canonical_source_invalid")

    def source(number):
        raise expected

    assert public_error("canonical_source_invalid", snapshot, source) is expected


@pytest.mark.parametrize("page_numbers", [(1, 1), (1, 2)])
def test_public_cache_loads_and_hashes_only_once_per_cited_page(page_numbers):
    pages = {number: page(f"literal page {number}", number) for number in set(page_numbers)}
    fields = [spec("first", evidence_required=True), spec("second", evidence_required=True)]
    records = [
        record(name, evidence=[reference(pages[number]), reference(pages[number])])
        for name, number in zip(("first", "second"), page_numbers)
    ]
    snapshot = make_snapshot(fields, records)
    calls = []

    def source(number):
        calls.append(number)
        return pages[number]

    real_sha256 = hashlib.sha256
    with patch("app.metadata_evidence.hashlib.sha256", wraps=real_sha256) as hashes:
        assert assess_snapshot(snapshot, source).approvable
    assert calls == list(dict.fromkeys(page_numbers))
    # C1 also hashes schemas: count only these exact UTF-8 page payloads.
    for canonical in pages.values():
        assert sum(call.args == (canonical.text.encode("utf-8"),) for call in hashes.call_args_list) == 1


def test_public_cache_is_operation_local_across_documents_and_owners():
    calls = []
    for document_id, actor in ((DOCUMENT, "user:1"), (DOCUMENT, "user:1"), ("0" * 32, "user:2")):
        canonical = replace(page("literal"), document_id=document_id)
        snapshot = make_snapshot(
            [spec()], [record(evidence=[reference(canonical)])], document_id=document_id
        ).model_copy(update={"author": provenance().model_copy(update={"actor_subject": actor})})

        def source(number):
            calls.append((document_id, actor, number))
            return canonical

        assert assess_snapshot(snapshot, source).approvable
    assert calls == [(DOCUMENT, "user:1", 1), (DOCUMENT, "user:1", 1), ("0" * 32, "user:2", 1)]


@pytest.mark.parametrize("kind,scalar", [("boolean", False), ("integer", 0)])
@pytest.mark.parametrize("required", [False, True])
def test_public_typed_conditions_and_significant_false_zero(kind, scalar, required):
    fields = [
        spec("trigger", type=kind, required=True),
        spec("detail", required=required, required_when={"field": "trigger", "equals": scalar}),
    ]
    result = assess_snapshot(make_snapshot(fields, [record("trigger", scalar)]), forbidden_source)
    assert result.blockers == [Blocker("required_missing", SEGMENT, "detail")]


def test_public_condition_uses_normalized_decimal_literal():
    fields = [
        spec("amount", type="decimal"),
        spec("detail", required_when={"field": "amount", "equals": "+001.2300"}),
    ]
    value = normalize_manual_values(
        fields, [ManualFieldInput(field_name="amount", raw="1.230")], provenance()
    )[0].model_copy(update={"status": "reviewed"})
    assert value.normalized == "1.23"
    result = assess_snapshot(make_snapshot(fields, [value]), forbidden_source)
    assert result.blockers == [Blocker("required_missing", SEGMENT, "detail")]


def test_public_required_is_not_disabled_by_false_condition():
    fields = [spec("kind"), spec("detail", required=True, required_when={"field": "kind", "equals": "special"})]
    result = assess_snapshot(make_snapshot(fields, [record("kind", "ordinary")]), forbidden_source)
    assert result.blockers == [Blocker("required_missing", SEGMENT, "detail")]


@pytest.mark.parametrize("option", ["critical", "evidence_required"])
@pytest.mark.parametrize("kind,values", [("string", ["one", "two"]), ("integer", [0, 1]), ("boolean", [False, True])])
def test_public_many_requires_each_items_own_proof(option, kind, values):
    canonical = page("literal")
    field = spec(type=kind, cardinality="many", **{option: True})
    value = record(value=values, evidence=[reference(canonical, index=0)])
    result = assess_snapshot(make_snapshot([field], [value]), lambda number: canonical)
    assert result.blockers == [Blocker("evidence_missing", SEGMENT, "title", 1)]
    complete = value.model_copy(update={"evidence": [reference(canonical, index=0), reference(canonical, index=1)]})
    assert assess_snapshot(make_snapshot([field], [complete]), lambda number: canonical).approvable


@pytest.mark.parametrize("bad_state", ["needs_review", "false_proof"])
def test_public_approved_state_does_not_bypass_assessment(bad_state):
    canonical = page("literal")
    evidence = reference(canonical)
    if bad_state == "false_proof":
        evidence = evidence.model_copy(update={"quote": "LITERAL"})
    value = record(status="needs_review" if bad_state == "needs_review" else "reviewed", evidence=[evidence])
    snapshot = make_snapshot([spec(evidence_required=True)], [value]).model_copy(update={
        "state": "approved",
        "parent_revision_id": "4" * 32,
        "approval": ApprovalRecord(
            actor_subject="user:1", auth_source="local_jwt", approved_at=NOW,
            reviewed_revision_id="4" * 32, reviewed_sha256="5" * 64,
        ),
    })
    before = snapshot_bytes(snapshot)
    if bad_state == "false_proof":
        public_error("evidence_invalid", snapshot, lambda number: canonical)
    else:
        result = assess_snapshot(snapshot, lambda number: canonical)
        assert not result.approvable
        assert result.blockers == [Blocker("review_required", SEGMENT, "title")]
    assert snapshot_bytes(snapshot) == before
    assert snapshot.state == "approved"


@pytest.mark.parametrize("tamper", ["copied", "constructed", "nested", "wire"])
def test_public_revalidates_structural_tampering_before_source(tamper):
    canonical = page("literal")
    snapshot = make_snapshot([spec()], [record(evidence=[reference(canonical)])])
    if tamper == "copied":
        snapshot = snapshot.model_copy(update={"page_count": True})
    elif tamper == "constructed":
        snapshot = MetadataSnapshot.model_construct(**{**snapshot.__dict__, "page_count": True})
    elif tamper == "nested":
        snapshot.segments[0].fields[0].evidence.append(reference(canonical).model_copy(update={"start": True}))
    else:
        snapshot = snapshot.model_dump(mode="python", by_alias=True)
        snapshot["segments"][0]["fields"][0]["provenance"]["actor_subject"] = "SENSITIVE_MARKER"
    calls = []

    def source(number):
        calls.append(number)
        return canonical

    public_error("metadata_invalid_input", snapshot, source)
    assert calls == []


@pytest.mark.parametrize("kind,raw,wrong", [("string", "original", "different"), ("integer", 0, False)])
def test_public_semantic_tampering_precedes_all_source_calls(kind, raw, wrong):
    canonical = page("literal")
    first = segment([record(value=raw, evidence=[reference(canonical)])]).model_copy(update={"page_end": 1})
    second = segment([record(value=raw).model_copy(update={"normalized": wrong})]).model_copy(update={
        "segment_id": "9" * 32, "page_start": 2, "page_end": 2,
    })
    snapshot = make_snapshot([spec(type=kind)], segments=[first, second])
    calls = []

    def source(number):
        calls.append(number)
        return canonical

    public_error("metadata_invalid_value", snapshot, source)
    assert calls == []


def test_public_segment_schema_code_index_order_is_deterministic_and_deduplicated():
    canonical = page("literal", 2)
    fields = [spec("z", cardinality="many", critical=True), spec("a", required=True)]
    first = segment([record("z", ["one", "two"], status="needs_review")]).model_copy(update={"page_end": 1})
    second = segment([record("z", ["one", "two"], status="needs_review", evidence=[
        reference(canonical, index=0), reference(canonical, index=0)
    ])]).model_copy(update={"segment_id": "9" * 32, "page_start": 2, "page_end": 2})
    snapshot = make_snapshot(fields, segments=[first, second])
    assert len(snapshot.schemas) == 1
    before = snapshot_bytes(snapshot)
    expected = [
        Blocker("evidence_missing", SEGMENT, "z", 0),
        Blocker("evidence_missing", SEGMENT, "z", 1),
        Blocker("review_required", SEGMENT, "z"),
        Blocker("required_missing", SEGMENT, "a"),
        Blocker("evidence_missing", "9" * 32, "z", 1),
        Blocker("review_required", "9" * 32, "z"),
        Blocker("required_missing", "9" * 32, "a"),
    ]
    for _ in range(2):
        calls = []

        def source(number):
            calls.append(number)
            return canonical

        result = assess_snapshot(snapshot, source)
        assert not result.approvable
        assert result.blockers == expected
        assert len(result.blockers) == len(set(result.blockers))
        assert calls == [2]
        assert snapshot_bytes(snapshot) == before


if __name__ == "__main__":
    unittest.main()
