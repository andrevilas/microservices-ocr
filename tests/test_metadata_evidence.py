"""Synthetic unit tests; no filesystem, PDF, catalog, or approval service.

The identity-only cache fixture deliberately uses model_construct: it is not a
valid snapshot fixture and is never passed to assess_snapshot. Full composed C1
snapshot integration requires the composition interface absent from this dispatch.
"""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import hashlib
import unittest
from unittest.mock import patch

from app.document_models import FieldSpec
from app.metadata_evidence import (
    Blocker,
    CanonicalPageText,
    _PageCache,
    _field_blockers,
    _verify_record_evidence,
    assess_snapshot,
)
from app.metadata_models import (
    EvidenceRef,
    FieldValueRecord,
    MetadataSnapshot,
    ProvenanceRecord,
    SegmentRecord,
)
from app.metadata_validation import MetadataValidationError, validate_value_records


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
    return MetadataSnapshot.model_construct(
        document_id=DOCUMENT,
        artifact_version=1,
        text_version=1,
        canonical_sha256=CANONICAL,
        page_count=300,
    )


def provenance():
    return ProvenanceRecord(
        actor_subject="user:synthetic",
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
    return FieldValueRecord(
        field_name=name,
        raw=value,
        normalized=value,
        origin=origin,
        status=status,
        evidence=list(evidence),
        provenance=provenance(),
    )


def segment(records=()):
    return SegmentRecord(
        segment_id=SEGMENT,
        page_start=1,
        page_end=300,
        context_id="d" * 32,
        type_id="e" * 32,
        type_version=1,
        schema_sha256="f" * 64,
        fields=list(records),
    )


def spec(name="title", **options):
    return FieldSpec.model_validate({"name": name, "type": "text", **options}, strict=True)


class ErrorAssertions(unittest.TestCase):
    def assert_metadata_error(self, code, operation):
        with self.assertRaises(MetadataValidationError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn("SENSITIVE_MARKER", str(caught.exception))
        return caught.exception


class LiteralEvidenceTests(ErrorAssertions):
    def verify(self, source_page, value, source=None):
        cache = _PageCache(identity(), source or (lambda number: source_page))
        _verify_record_evidence(cache, segment(), value)

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
            bad = reference(canonical).model_copy(update={"quote": changed})
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
        for updates in (
            {"document_id": "0" * 32},
            {"artifact_version": 2},
            {"text_version": 2},
            {"text_sha256": "0" * 64},
            {"start": -1},
            {"end": 500},
            {"item_index": 0},
        ):
            with self.subTest(updates=updates):
                bad = reference(canonical).model_copy(update=updates)
                self.assert_metadata_error(
                    "evidence_invalid", lambda: self.verify(canonical, record(evidence=[bad]))
                )

    def test_segment_boundary_and_list_index(self):
        canonical = page("literal", 2)
        value = record(value=["one", "two"], evidence=[reference(canonical, index=1)])
        self.verify(canonical, value)
        narrow = segment().model_copy(update={"page_end": 1})
        self.assert_metadata_error(
            "evidence_invalid",
            lambda: _verify_record_evidence(_PageCache(identity(), lambda n: canonical), narrow, value),
        )
        for index in (None, -1, 2, True):
            bad = reference(canonical, index=1).model_copy(update={"item_index": index})
            self.assert_metadata_error(
                "evidence_invalid",
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
        self.assert_metadata_error("evidence_invalid", lambda: self.verify(canonical, invalid))


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
                "evidence_invalid", lambda: _PageCache(identity(), lambda n: returned).get(1)
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
        validate_value_records(fields, records)
        return _field_blockers(SEGMENT, fields, records)

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
        for value in ("", " \t\r\n"):
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


if __name__ == "__main__":
    unittest.main()
