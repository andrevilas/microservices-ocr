"""C2a regressions using the real schema, DTOs, and scalar normalizers."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import localcontext

import pytest

from app.document_models import FieldSpec, RequiredWhen
from app.document_values import missing_required_fields, validate_values
from app.metadata_models import EvidenceRef, FieldValueRecord, ManualFieldInput, ProvenanceRecord
from app.metadata_validation import (
    MetadataValidationError,
    normalize_manual_values,
    validate_value_records,
)


def field(name="value", kind="string", **kwargs):
    return FieldSpec(name=name, type=kind, description="Test", **kwargs)


def provenance():
    return ProvenanceRecord(
        actor_subject="user:1", auth_source="local_jwt",
        recorded_at=datetime(2026, 10, 16, tzinfo=timezone.utc),
    )


def entry(raw, name="value", **kwargs):
    return ManualFieldInput(field_name=name, raw=raw, **kwargs)


def evidence(**kwargs):
    data = dict(
        document_id="a" * 32, page=1, text_sha256="b" * 64,
        start=0, end=1, quote="x",
    )
    data.update(kwargs)
    return EvidenceRef(**data)


def record(raw, normalized, **kwargs):
    data = dict(
        field_name="value", raw=raw, normalized=normalized,
        origin="manual", status="needs_review", provenance=provenance(),
    )
    data.update(kwargs)
    return FieldValueRecord(**data)


def assert_error(code, operation):
    with pytest.raises(MetadataValidationError) as caught:
        operation()
    assert caught.value.code == code
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert str(caught.value) == "Metadata validation failed."
    return caught.value


@pytest.mark.parametrize("kind,raw,expected", [
    ("string", "  text\n", "  text\n"),
    ("identifier", "0012", "0012"),
    ("integer", 0, 0),
    ("decimal", "+001.2300", "1.23"),
    ("boolean", False, False),
    ("date", "2024-02-29", "2024-02-29"),
    ("enum", "a", "a"),
])
def test_all_scalar_types_and_many(kind, raw, expected):
    kwargs = {"enum": ["a", "b"]} if kind == "enum" else {}
    for many in (False, True):
        schema = [field(kind=kind, cardinality="many" if many else "one", **kwargs)]
        value = [raw, raw] if many else raw
        inputs = [entry(value, origin="declared")]
        before = deepcopy(inputs[0].model_dump(mode="python"))
        with localcontext() as context:
            context.prec = 1
            output = normalize_manual_values(schema, inputs, provenance())
        result = output[0]
        assert result.raw == value
        assert type(result.raw) is type(value)
        assert result.normalized == ([expected, expected] if many else expected)
        assert result.status == "needs_review"
        assert result.origin == "declared"
        assert inputs[0].model_dump(mode="python") == before
        validate_value_records(schema, output)


@pytest.mark.parametrize("kind,raw,kwargs", [
    ("integer", True, {}), ("integer", "1", {}),
    ("decimal", "1,23", {}), ("date", "2023-02-29", {}),
    ("enum", "c", {"enum": ["a", "b"]}),
    ("identifier", "x\n", {}), ("boolean", 1, {}),
])
def test_invalid_scalar_values(kind, raw, kwargs):
    assert_error("metadata_invalid_value", lambda: normalize_manual_values(
        [field(kind=kind, **kwargs)], [entry(raw)], provenance(),
    ))


@pytest.mark.parametrize("raw", [1.0, [None], [["x"]], ("x",)])
def test_raw_dto_rejects_float_null_nested_and_tuple(raw):
    assert_error("metadata_invalid_input", lambda: normalize_manual_values(
        [field(cardinality="many")], [{"field_name": "value", "raw": raw}], provenance(),
    ))


def test_lists_keep_order_duplicates_and_real_limit():
    schema = [field(kind="decimal", cardinality="many", max_items=3)]
    raw = ["2.00", "1.00", "2.00"]
    result = normalize_manual_values(schema, [entry(raw)], provenance())
    assert result[0].raw == raw
    assert result[0].normalized == ["2", "1", "2"]
    assert result[0].raw is not raw
    assert_error("metadata_invalid_value", lambda: normalize_manual_values(
        schema, [entry(raw + ["3"])], provenance(),
    ))
    assert_error("metadata_invalid_value", lambda: normalize_manual_values(
        schema, [entry("2")], provenance(),
    ))


def test_omitted_null_blank_empty_zero_false_and_schema_order():
    schema = [
        field("omitted", required=True), field("absent"), field("blank"),
        field("empty", cardinality="many"), field("zero", "integer"),
        field("flag", "boolean"),
    ]
    inputs = [entry(False, "flag"), entry(0, "zero"), entry([], "empty"),
              entry(" \t", "blank"), entry(None, "absent")]
    result = normalize_manual_values(schema, inputs, provenance())
    assert [r.field_name for r in result] == ["absent", "blank", "empty", "zero", "flag"]
    assert result[0].status == "not_found"
    assert result[0].normalized is None
    assert result[0].evidence == []
    assert all(r.status == "needs_review" for r in result[1:])
    assert type(result[-2].normalized) is int
    assert type(result[-1].normalized) is bool
    validate_value_records(schema, result)
    assert missing_required_fields(schema, {r.field_name: r.raw for r in result}) == ("omitted",)


@pytest.mark.parametrize("records", [False, True])
def test_unknown_duplicate_and_segment_entry_limit(records):
    schema = [field()]
    make = (lambda name: record("x", "x", field_name=name)) if records else (lambda name: entry("x", name))
    operation = (lambda items: validate_value_records(schema, items)) if records else (
        lambda items: normalize_manual_values(schema, items, provenance())
    )
    for items in ([make("unknown")], [make("value"), make("value")], [make("value")] * 101):
        assert_error("metadata_invalid_input", lambda: operation(items))


def test_original_instances_are_checked_before_dump():
    bad_input = entry(1)
    object.__setattr__(bad_input, "raw", 1.0)
    assert_error("metadata_invalid_input", lambda: normalize_manual_values(
        [field(kind="integer")], [bad_input], provenance(),
    ))
    constructed = ManualFieldInput.model_construct(field_name="value", raw=("x",), origin="manual", evidence=[])
    assert_error("metadata_invalid_input", lambda: normalize_manual_values(
        [field(cardinality="many")], [constructed], provenance(),
    ))
    bad_schema = field()
    object.__setattr__(bad_schema, "required", 1)
    assert_error("metadata_invalid_schema", lambda: normalize_manual_values([bad_schema], [], provenance()))
    bad_provenance = provenance()
    object.__setattr__(bad_provenance, "recorded_at", "2026-10-16T00:00:00Z")
    assert_error("metadata_invalid_input", lambda: normalize_manual_values([field()], [], bad_provenance))
    bad_record = record(1, 1)
    object.__setattr__(bad_record, "normalized", 1.0)
    assert_error("metadata_invalid_input", lambda: validate_value_records([field(kind="integer")], [bad_record]))


@pytest.mark.parametrize("raw,normalized,kind,kwargs", [
    (1, True, "integer", {}),
    (False, 0, "boolean", {}),
    ([1], [True], "integer", {"cardinality": "many"}),
    (["a", "b"], ["b", "a"], "string", {"cardinality": "many"}),
    ("+001.2300", "+001.2300", "decimal", {}),
])
def test_normalized_must_match_recursively_with_exact_types(raw, normalized, kind, kwargs):
    assert_error("metadata_invalid_value", lambda: validate_value_records(
        [field(kind=kind, **kwargs)], [record(raw, normalized)],
    ))


def test_inherited_origin_remains_structurally_readable_but_not_operational():
    value = record("x", "x", origin="inherited")
    assert_error("unsupported_metadata_origin", lambda: validate_value_records([field()], [value]))


@pytest.mark.parametrize("origin", ["inherited", "extracted"])
def test_manual_input_cannot_claim_a_reserved_origin(origin):
    assert_error("metadata_invalid_input", lambda: normalize_manual_values(
        [field()], [{"field_name": "value", "raw": "x", "origin": origin}], provenance(),
    ))


def test_extracted_origin_is_operational_with_its_execution():
    source = provenance().model_copy(update={"execution_id": "c" * 32})
    validate_value_records([field()], [record("x", "x", origin="extracted", provenance=source)])
    validate_value_records([field()], [record(None, None, origin="extracted", status="conflict",
                                              provenance=source)])


def test_conflict_only_as_an_extracted_record_without_value():
    source = provenance().model_copy(update={"execution_id": "c" * 32})
    assert_error("unsupported_metadata_state", lambda: validate_value_records(
        [field()], [record("x", "x", status="conflict")],
    ))
    assert_error("unsupported_metadata_state", lambda: validate_value_records(
        [field()], [record(None, None, status="conflict")],
    ))
    assert_error("unsupported_metadata_state", lambda: validate_value_records(
        [field()], [record("x", "x", origin="extracted", status="conflict", provenance=source)],
    ))
    validate_value_records([field()], [record("x", "x", status="reviewed")])
    assert normalize_manual_values([field()], [entry("x")], provenance())[0].status == "needs_review"


def test_null_status_consistency():
    assert_error("metadata_invalid_value", lambda: validate_value_records(
        [field()], [record(None, None)],
    ))
    validate_value_records([field()], [record(None, None, status="not_found")])
    assert_error("metadata_invalid_input", lambda: validate_value_records(
        [field()], [{**record("x", "x").model_dump(mode="python"), "status": "not_found"}],
    ))


def test_copies_provenance_evidence_and_raw_without_aliases():
    source = provenance()
    value = entry(["x"], evidence=[evidence(item_index=0)])
    before = deepcopy(value.model_dump(mode="python"))
    result = normalize_manual_values([field(cardinality="many")], [value], source)[0]
    assert result.provenance is not source
    assert result.evidence is not value.evidence
    assert result.evidence[0] is not value.evidence[0]
    assert value.model_dump(mode="python") == before
    value.raw.append("y")
    value.evidence.clear()
    object.__setattr__(source, "actor_subject", "user:2")
    assert result.raw == ["x"]
    assert result.normalized == ["x"]
    assert len(result.evidence) == 1
    assert result.provenance.actor_subject == "user:1"


def test_manual_provenance_rejects_execution_id_even_without_entries():
    source = provenance().model_dump(mode="python")
    source["execution_id"] = "c" * 32
    assert_error("metadata_invalid_input", lambda: normalize_manual_values([field()], [], source))


def test_segment_total_evidence_limit():
    schema = [field("first"), field("second")]
    inputs = [entry("x", "first", evidence=[evidence()] * 64),
              entry("x", "second", evidence=[evidence()] * 64)]
    output = normalize_manual_values(schema, inputs, provenance())
    validate_value_records(schema, output)
    inputs[1].evidence.append(evidence())
    assert_error("metadata_invalid_input", lambda: normalize_manual_values(schema, inputs, provenance()))
    output[1].evidence.append(evidence())
    assert_error("metadata_invalid_input", lambda: validate_value_records(schema, output))


def test_missing_required_enumerates_all_in_schema_order():
    schema = [
        field("first", required=True),
        field("items", cardinality="many", required=True),
        field("flag", "boolean", required=True),
        field("count", "integer", required=True),
        field("critical_only", critical=True),
    ]
    values = {"first": " \t", "items": ["x", " "], "flag": False, "count": 0}
    before = deepcopy(values)
    assert missing_required_fields(schema, values) == ("first", "items")
    assert validate_values(schema, values) == values
    with pytest.raises(ValueError):
        validate_values(schema, values, complete=True)
    assert values == before
    values.update(first="x", items=["x"])
    assert missing_required_fields(schema, values) == ()
    validate_values(schema, values, complete=True)


@pytest.mark.parametrize("raw", [None, "", " \n"])
def test_required_scalar_missing(raw):
    schema = [field(required=True)]
    assert missing_required_fields(schema, {"value": raw}) == ("value",)
    with pytest.raises(ValueError):
        validate_values(schema, {"value": raw}, complete=True)


@pytest.mark.parametrize("raw", [None, [], [""], ["x", " "]])
def test_required_many_missing(raw):
    schema = [field(required=True, cardinality="many")]
    assert missing_required_fields(schema, {"value": raw}) == ("value",)
    with pytest.raises(ValueError):
        validate_values(schema, {"value": raw}, complete=True)


@pytest.mark.parametrize("kind,actual,literal", [
    ("integer", 0, 0), ("boolean", False, False),
    ("decimal", "+001.2300", "1.230"),
])
def test_conditional_requirements_use_normalized_targets(kind, actual, literal):
    schema = [field("target", kind), field("dependent", required_when=RequiredWhen(field="target", equals=literal))]
    assert missing_required_fields(schema, {"target": actual}) == ("dependent",)
    assert missing_required_fields(schema, {}) == ()
    assert missing_required_fields(schema, {"target": None}) == ()
    with pytest.raises(ValueError):
        validate_values(schema, {"target": actual}, complete=True)
    validate_values(schema, {"target": actual, "dependent": "x"}, complete=True)


def test_required_or_condition_and_false_condition():
    schema = [field("target", "integer"), field("dependent", required=True,
        required_when=RequiredWhen(field="target", equals=1))]
    assert missing_required_fields(schema, {"target": 0}) == ("dependent",)
    schema[1] = field("dependent", required_when=RequiredWhen(field="target", equals=1))
    assert missing_required_fields(schema, {"target": 0}) == ()


@pytest.mark.parametrize("target,reference,literal", [
    (field("target"), "unknown", "x"),
    (field("target"), "dependent", "x"),
    (field("target", cardinality="many"), "target", "x"),
    (field("target", "integer"), "target", True),
    (field("target", "boolean"), "target", 0),
    (field("target", "date"), "target", "2023-02-29"),
])
def test_invalid_conditions_fail_in_drafts(target, reference, literal):
    dependent = field("dependent").model_copy(update={
        "required_when": RequiredWhen(field=reference, equals=literal),
    })
    schema = [target, dependent]
    with pytest.raises(ValueError):
        missing_required_fields(schema, {})
    with pytest.raises(ValueError):
        validate_values(schema, {})
    assert_error("metadata_invalid_schema", lambda: normalize_manual_values(schema, [], provenance()))
    assert_error("metadata_invalid_schema", lambda: validate_value_records(schema, []))


def test_helper_rejects_invalid_values_and_schema():
    for schema, values in [
        ([field()], {"unknown": "x"}),
        ([field(kind="integer")], {"value": True}),
        ([field(), field()], {}),
        ([field(cardinality="many")], {"value": [None]}),
    ]:
        with pytest.raises(ValueError):
            missing_required_fields(schema, values)
    assert_error("metadata_invalid_schema", lambda: normalize_manual_values([field(), field()], [], provenance()))
    assert_error("metadata_invalid_schema", lambda: normalize_manual_values([field(f"f{i}") for i in range(101)], [], provenance()))


def test_errors_never_include_sensitive_values_or_exception_context():
    marker = "SECRET_MARKER_987"
    error = assert_error("metadata_invalid_value", lambda: normalize_manual_values(
        [field(kind="integer")], [entry(marker)], provenance(),
    ))
    assert marker not in repr(error)
    error = assert_error("metadata_invalid_input", lambda: normalize_manual_values(
        [field()], [{"field_name": "value", "raw": "x", "extra": marker}], provenance(),
    ))
    assert marker not in repr(error)
    with pytest.raises(ValueError) as caught:
        MetadataValidationError(marker)
    assert marker not in str(caught.value)
    with pytest.raises(ValueError) as caught:
        MetadataValidationError("metadata_invalid_input", marker)
    assert marker not in str(caught.value)
