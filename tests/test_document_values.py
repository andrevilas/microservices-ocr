import copy

import pytest

from app.document_models import FieldSpec
from app.document_values import normalize_value, validate_values


def spec(type_, name="f", required=False, enum=None):
    return FieldSpec(name=name, type=type_, description="d", required=required, enum=enum)


def norm(type_, value, **kw):
    return normalize_value(spec(type_, **kw), value)


# --- integer / boolean ---
@pytest.mark.parametrize("v", [0, 1, -1, 2**53 - 1, -(2**53 - 1)])
def test_integer_ok(v):
    assert norm("integer", v) == v


@pytest.mark.parametrize("v", [True, False, 1.0, "1", 2**53, -(2**53), 9007199254740993, 10**400, -(10**400)])
def test_integer_rejected(v):
    with pytest.raises(ValueError) as e:
        norm("integer", v)
    assert not isinstance(e.value, OverflowError)


def test_boolean_strict():
    assert norm("boolean", True) is True
    assert norm("boolean", False) is False
    for v in (1, 0, "true", "false", "1"):
        with pytest.raises(ValueError):
            norm("boolean", v)


# --- decimal ---
@pytest.mark.parametrize(
    "raw,out",
    [
        ("0", "0"), ("-0", "0"), ("+0.000", "0"), ("000", "0"), ("0.0", "0"),
        ("+1.50", "1.5"), ("-001.2300", "-1.23"), ("007", "7"), ("0.5", "0.5"),
        ("10", "10"), ("100.000", "100"), ("-0.000000000001", "-0.000000000001"),
    ],
)
def test_decimal_canonical(raw, out):
    assert norm("decimal", raw) == out


def test_decimal_38_digits_12_places_exact():
    raw = "12345678901234567890123456.123456789012"  # 26 + 12 = 38 digits
    assert norm("decimal", raw) == raw
    assert norm("decimal", "-" + raw) == "-" + raw
    assert norm("decimal", "0.000000000001") == "0.000000000001"


@pytest.mark.parametrize(
    "raw",
    [
        "123456789012345678901234567.123456789012",  # 39 digits
        "1" * 39,
        "0.1234567890123",  # 13 places
        "0.0000000000001",
    ],
)
def test_decimal_limits(raw):
    with pytest.raises(ValueError):
        norm("decimal", raw)


def test_decimal_independent_of_global_context():
    import decimal

    ctx = decimal.getcontext()
    old = ctx.prec
    ctx.prec = 5
    try:
        raw = "12345678901234567890123456.123456789012"
        assert norm("decimal", raw) == raw
    finally:
        ctx.prec = old


@pytest.mark.parametrize(
    "raw",
    ["", " ", "1e3", "1E3", "NaN", "Infinity", "-Infinity", "inf", "1,5", " 1", "1 ", "1.", ".5", "--1", "+-1",
     "1_0", "١٢", "1" * 129, 1.5, 1.0, float("nan"), float("inf"), 1, True, b"1"],
)
def test_decimal_rejected(raw):
    with pytest.raises(ValueError):
        norm("decimal", raw)


# --- string / identifier ---
def test_identifier_preserves_zeros_and_text():
    assert norm("identifier", "0012") == "0012"
    assert norm("identifier", " A-0012 ") == " A-0012 "
    with pytest.raises(ValueError):
        norm("identifier", 12)


def test_string_rules():
    assert norm("string", "a\tb\nc") == "a\tb\nc"
    assert norm("string", "") == ""
    assert norm("string", "x" * 4000) == "x" * 4000
    for bad in ("x" * 4001, "a\rb", "a\x00b", "a\x7fb", "a\ud800b", 5, b"a"):
        with pytest.raises(ValueError):
            norm("string", bad)


def test_identifier_rejects_all_controls():
    assert norm("identifier", "") == ""
    for bad in ("a\tb", "a\nb", "a\x00", "a\ud800", "x" * 4001):
        with pytest.raises(ValueError):
            norm("identifier", bad)


# --- date ---
@pytest.mark.parametrize("v", ["2024-02-29", "2000-02-29", "1999-12-31", "0001-01-01", "9999-12-31"])
def test_date_ok(v):
    assert norm("date", v) == v


@pytest.mark.parametrize(
    "v",
    ["2023-02-29", "1900-02-29", "2024-13-01", "2024-00-10", "2024-04-31", "0000-01-01", "2024-1-1", "2024/01/01",
     "20240101", " 2024-01-01", "2024-01-01 ", "2024-01-01T00:00:00", "٢٠٢٤-٠١-٠١", "", 20240101],
)
def test_date_rejected(v):
    with pytest.raises(ValueError):
        norm("date", v)


# --- enum ---
def test_enum_exact():
    f = spec("enum", enum=["A", "b"])
    assert normalize_value(f, "A") == "A"
    for bad in ("a", "B", " A", "", 1, None.__class__):
        with pytest.raises(ValueError):
            normalize_value(f, bad)


def test_null_is_absence():
    for t in ("string", "identifier", "integer", "decimal", "boolean", "date"):
        assert norm(t, None) is None
    assert normalize_value(spec("enum", enum=["A"]), None) is None


def test_model_construct_not_trusted():
    bad = FieldSpec.model_construct(name="BAD NAME", type="integer", description="d")
    with pytest.raises(ValueError):
        normalize_value(bad, 1)
    with pytest.raises(ValueError):
        normalize_value("not a spec", 1)


# --- validate_values ---
FIELDS = [
    spec("string", "nome", required=True),
    spec("integer", "qtd"),
    spec("decimal", "valor", required=True),
    spec("date", "data"),
    spec("enum", "tipo", enum=["x", "y"]),
]


def test_draft_accepts_empty_missing_null():
    assert validate_values(FIELDS, {}) == {}
    assert validate_values(FIELDS, {"nome": None, "qtd": None}) == {"nome": None, "qtd": None}
    assert validate_values(FIELDS, {"nome": ""}) == {"nome": ""}


def test_returns_normalized_new_objects_without_mutation():
    values = {"valor": "+01.50", "qtd": 3}
    snapshot = copy.deepcopy(values)
    out = validate_values(FIELDS, values)
    assert out == {"valor": "1.5", "qtd": 3}
    assert values == snapshot
    assert out is not values
    assert "nome" not in out  # omitted fields are not filled


def test_complete_requires_required_only():
    with pytest.raises(ValueError):
        validate_values(FIELDS, {"valor": "1"}, complete=True)
    for blank in (None, "", "   ", "\t\n"):
        with pytest.raises(ValueError):
            validate_values(FIELDS, {"nome": blank, "valor": "1"}, complete=True)
    assert validate_values(FIELDS, {"nome": "n", "valor": "1.0"}, complete=True) == {"nome": "n", "valor": "1"}


def test_complete_does_not_force_optional_or_false_boolean():
    fields = [spec("boolean", "ok", required=True), spec("string", "opt")]
    assert validate_values(fields, {"ok": False, "opt": None}, complete=True) == {"ok": False, "opt": None}


def test_unknown_and_bad_keys_and_container():
    with pytest.raises(ValueError):
        validate_values(FIELDS, {"nope": 1})
    with pytest.raises(ValueError):
        validate_values(FIELDS, {1: "x"})
    for bad in (None, [], "x", [("nome", "x")]):
        with pytest.raises(ValueError):
            validate_values(FIELDS, bad)
    with pytest.raises(ValueError):
        validate_values("abc", {})


def test_duplicate_schema_names_rejected():
    with pytest.raises(ValueError):
        validate_values([spec("string", "a"), spec("integer", "a")], {})


def test_field_count_limit():
    many = [spec("string", f"f{i}") for i in range(101)]
    with pytest.raises(ValueError):
        validate_values(many, {})
    ok = [spec("string", f"f{i}") for i in range(100)]
    assert validate_values(ok, {"f99": "x"}) == {"f99": "x"}


def test_aggregate_payload_limit():
    fields = [spec("string", f"f{i}") for i in range(100)]
    values = {f"f{i}": "x" * 4000 for i in range(100)}  # ~400KB > 262144
    with pytest.raises(ValueError):
        validate_values(fields, values)
    values = {f"f{i}": "x" * 2000 for i in range(100)}  # ~200KB
    assert len(validate_values(fields, values)) == 100


def test_aggregate_limit_counts_utf8_bytes():
    fields = [spec("string", f"f{i}") for i in range(100)]
    values = {f"f{i}": "é" * 2000 for i in range(100)}  # 2 bytes each => ~400KB
    with pytest.raises(ValueError):
        validate_values(fields, values)


def mspec(type_="string", name="m", **kw):
    return FieldSpec(name=name, type=type_, description="d", cardinality="many", **kw)


def test_many_requires_strict_list():
    m = mspec("integer")
    for bad in (1, "1", (1,), {1}, {"a": 1}, b"x"):
        with pytest.raises(ValueError):
            validate_values([m], {"m": bad})
    assert validate_values([m], {"m": [1, 2]}) == {"m": [1, 2]}


def test_many_none_and_empty_are_absence_in_draft():
    m = mspec()
    assert validate_values([m], {"m": None}) == {"m": None}
    assert validate_values([m], {"m": []}) == {"m": []}
    assert validate_values([m], {}) == {}


def test_many_max_items_and_items_validated():
    m = mspec("integer", max_items=2)
    assert validate_values([m], {"m": [1, 2]}) == {"m": [1, 2]}
    for bad in ([1, 2, 3], [1, None], [None], [True], ["1"], [[1]], [2**53]):
        with pytest.raises(ValueError):
            validate_values([m], {"m": bad})


def test_many_normalizes_each_item_without_mutation():
    m = mspec("decimal")
    values = {"m": ["01.00", "+2.50"]}
    snapshot = copy.deepcopy(values)
    out = validate_values([m], values)
    assert out == {"m": ["1", "2.5"]}
    assert out["m"] is not values["m"] and values == snapshot


def test_normalize_value_stays_scalar_for_many():
    assert normalize_value(mspec("integer"), 3) == 3
    with pytest.raises(ValueError):
        normalize_value(mspec("integer"), [3])


def test_many_complete_requires_non_empty_when_required():
    m = mspec(required=True)
    for missing in ({}, {"m": None}, {"m": []}):
        with pytest.raises(ValueError):
            validate_values([m], missing, complete=True)
    assert validate_values([m], {"m": ["a"]}, complete=True) == {"m": ["a"]}
    assert validate_values([mspec()], {"m": []}, complete=True) == {"m": []}


def cond(equals, target_type="string", **kw):
    return [
        spec(target_type, "a", **kw),
        FieldSpec(name="b", type="string", description="d", required_when={"field": "a", "equals": equals}),
    ]


def test_required_when_invalid_references_raise_even_in_draft():
    dep = FieldSpec(name="b", type="string", description="d", required_when={"field": "zz", "equals": "x"})
    with pytest.raises(ValueError):
        validate_values([dep], {})
    with pytest.raises(ValueError):
        validate_values([mspec(name="a"), FieldSpec(name="b", type="string", description="d",
                                                   required_when={"field": "a", "equals": "x"})], {})
    bad_self = FieldSpec.model_construct(name="b", type="string", description="d",
                                         required_when={"field": "b", "equals": "x"})
    with pytest.raises(ValueError):
        validate_values([bad_self], {})


@pytest.mark.parametrize("target_type,equals", [("integer", "1"), ("integer", True), ("boolean", 0),
                                                ("boolean", "true"), ("decimal", 1), ("date", "2024-02-30"),
                                                ("decimal", "abc"), ("enum", "zz")])
def test_required_when_incompatible_equals_raises(target_type, equals):
    fields = cond(equals, target_type, enum=["x"] if target_type == "enum" else None)
    for complete in (False, True):
        with pytest.raises(ValueError):
            validate_values(fields, {}, complete=complete)


def test_required_when_invalid_condition_does_not_change_payload():
    fields = cond("1", "integer")
    values = {"a": 1}
    snapshot = copy.deepcopy(values)
    with pytest.raises(ValueError):
        validate_values(fields, values)
    assert values == snapshot


def test_required_when_draft_never_requires():
    assert validate_values(cond("x"), {"a": "x"}) == {"a": "x"}


def test_required_when_complete_conditional():
    fields = cond("x")
    with pytest.raises(ValueError):
        validate_values(fields, {"a": "x"}, complete=True)
    for blank in (None, "", "  "):
        with pytest.raises(ValueError):
            validate_values(fields, {"a": "x", "b": blank}, complete=True)
    assert validate_values(fields, {"a": "x", "b": "v"}, complete=True) == {"a": "x", "b": "v"}
    # different value, absent target, or null target do not activate the condition
    assert validate_values(fields, {"a": "y"}, complete=True) == {"a": "y"}
    assert validate_values(fields, {}, complete=True) == {}
    assert validate_values(fields, {"a": None}, complete=True) == {"a": None}


def test_required_when_compares_normalized_decimal():
    fields = cond("01.00", "decimal")
    with pytest.raises(ValueError):
        validate_values(fields, {"a": "1.0"}, complete=True)
    with pytest.raises(ValueError):
        validate_values(fields, {"a": "+001"}, complete=True)
    assert validate_values(fields, {"a": "2"}, complete=True) == {"a": "2"}


def test_required_when_bool_is_not_int():
    off = cond(False, "boolean")
    with pytest.raises(ValueError):
        validate_values(off, {"a": False}, complete=True)
    assert validate_values(off, {"a": True}, complete=True) == {"a": True}
    zero = cond(0, "integer")
    with pytest.raises(ValueError):
        validate_values(zero, {"a": 0}, complete=True)
    assert validate_values(zero, {"a": 1}, complete=True) == {"a": 1}


def test_required_when_two_dependents_and_required_or_condition():
    fields = [
        spec("string", "a"),
        FieldSpec(name="b", type="string", description="d", required_when={"field": "a", "equals": "x"}),
        FieldSpec(name="c", type="string", description="d", required_when={"field": "a", "equals": "x"}),
        spec("string", "d", required=True),
    ]
    with pytest.raises(ValueError):
        validate_values(fields, {"a": "x", "b": "1", "d": "1"}, complete=True)
    assert validate_values(fields, {"a": "x", "b": "1", "c": "2", "d": "1"}, complete=True)["c"] == "2"
    with pytest.raises(ValueError):
        validate_values(fields, {"a": "y"}, complete=True)  # d still unconditionally required


def test_required_when_many_dependent_needs_non_empty_list():
    fields = [
        spec("boolean", "a"),
        mspec(name="b", required_when={"field": "a", "equals": True}),
    ]
    with pytest.raises(ValueError):
        validate_values(fields, {"a": True, "b": []}, complete=True)
    assert validate_values(fields, {"a": True, "b": ["v"]}, complete=True)["b"] == ["v"]


COND_MANY = [
    spec("string", "a"),
    mspec(name="b", required_when={"field": "a", "equals": "x"}),
]


@pytest.mark.parametrize("blanks", [[" ", ""], ["ok", " "], [""], ["\t\n"], [" ", "ok"]])
def test_complete_required_many_rejects_blank_items(blanks):
    with pytest.raises(ValueError):
        validate_values([mspec(required=True)], {"m": blanks}, complete=True)  # unconditional
    with pytest.raises(ValueError):
        validate_values(COND_MANY, {"a": "x", "b": blanks}, complete=True)  # conditional


def test_complete_required_many_accepts_meaningful_items():
    assert validate_values([mspec(required=True)], {"m": ["ok", "x y"]}, complete=True) == {"m": ["ok", "x y"]}
    assert validate_values(COND_MANY, {"a": "x", "b": ["ok"]}, complete=True)["b"] == ["ok"]


@pytest.mark.parametrize("type_,items", [("integer", [0, 1]), ("boolean", [False, True]), ("boolean", [False])])
def test_complete_required_many_accepts_zero_and_false(type_, items):
    m = mspec(type_, required=True)
    assert validate_values([m], {"m": items}, complete=True) == {"m": items}


def test_blank_many_items_remain_allowed_in_draft_and_when_optional_or_inactive():
    required = mspec(required=True)
    assert validate_values([required], {"m": [" ", ""]}) == {"m": [" ", ""]}
    assert validate_values([mspec()], {"m": [" ", ""]}) == {"m": [" ", ""]}
    assert validate_values([mspec()], {"m": [" "]}, complete=True) == {"m": [" "]}
    assert validate_values(COND_MANY, {"a": "y", "b": [" "]}, complete=True)["b"] == [" "]


def _warnings_for(call):
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(ValueError) as e:
            call()
    return caught, e.value


def test_model_construct_serializer_warning_becomes_sanitized_error():
    secret = "SYNTHETIC_PRIVATE"
    bad = FieldSpec.model_construct(name="b", type="string", description="d",
                                    required_when={"field": "b", "equals": secret})
    caught, err = _warnings_for(lambda: validate_values([bad], {}))
    assert not caught
    assert str(err) == "Invalid field specification."
    assert err.__cause__ is None and err.__context__ is None
    assert secret not in str(err)


def test_surrogate_in_constructed_field_is_sanitized_error():
    bad = FieldSpec.model_construct(name="a\ud800SEGREDO", type="string", description="d")
    caught, err = _warnings_for(lambda: validate_values([bad], {}))
    assert not caught
    assert str(err) == "Invalid field specification."
    assert "SEGREDO" not in str(err)


def test_errors_do_not_echo_values():
    secret = "SEGREDO-123456"
    cases = [
        (spec("integer"), secret),
        (spec("decimal"), secret),
        (spec("date"), secret),
        (spec("boolean"), secret),
        (spec("enum", enum=["a"]), secret),
        (spec("identifier"), secret + "\x00"),
        (spec("string"), secret + "\ud800"),
    ]
    for f, v in cases:
        with pytest.raises(ValueError) as e:
            normalize_value(f, v)
        assert "SEGREDO" not in str(e.value)
    with pytest.raises(ValueError) as e:
        validate_values(FIELDS, {"qtd": 10**400})
    assert "1000000" not in str(e.value)


def test_validate_values_error_on_bad_value_in_mapping():
    with pytest.raises(ValueError):
        validate_values(FIELDS, {"qtd": True})
    with pytest.raises(ValueError):
        validate_values(FIELDS, {"tipo": "z"})
