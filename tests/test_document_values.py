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
