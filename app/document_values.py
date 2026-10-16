"""Exact scalar validation of document values against FieldSpec. Pure: no I/O, no import effects."""
import json
import re
import unicodedata
from collections.abc import Sequence
from datetime import date

from app.document_models import MAX_FIELDS, MAX_PAYLOAD_BYTES, MAX_TEXT, FieldSpec

MAX_INT = 2**53 - 1
MAX_DECIMAL_CHARS = 128
MAX_DECIMAL_DIGITS = 38
MAX_DECIMAL_PLACES = 12

_DECIMAL = re.compile(r"([+-]?)([0-9]+)(?:\.([0-9]+))?", re.ASCII)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


def _fail(field: FieldSpec, reason: str):
    # Never echo the offending value: only the (non-sensitive) schema field name and a fixed reason.
    raise ValueError(f"Invalid value for field '{field.name}': {reason}.")


def _text(field: FieldSpec, value, allow_tab_lf: bool) -> str:
    if type(value) is not str:
        _fail(field, "must be a string")
    if len(value) > MAX_TEXT:
        _fail(field, f"exceeds {MAX_TEXT} characters")
    for ch in value:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            _fail(field, "contains a surrogate code point")
        if unicodedata.category(ch) == "Cc" and not (allow_tab_lf and ch in "\t\n"):
            _fail(field, "contains control characters")
    return value


def _integer(field: FieldSpec, value) -> int:
    if type(value) is not int:
        _fail(field, "must be an integer")
    if value > MAX_INT or value < -MAX_INT:  # pure int comparison: no float, no OverflowError
        _fail(field, "integer out of range")
    return value


def _decimal(field: FieldSpec, value) -> str:
    if type(value) is not str:
        _fail(field, "must be a decimal string")
    if len(value) > MAX_DECIMAL_CHARS:
        _fail(field, "decimal text too long")
    m = _DECIMAL.fullmatch(value)
    if m is None:
        _fail(field, "malformed decimal")
    sign, int_part, frac = m.group(1), m.group(2), (m.group(3) or "")
    # Exact digit-string arithmetic: independent of the global Decimal context.
    int_part = int_part.lstrip("0")
    frac = frac.rstrip("0")
    if len((int_part + frac).lstrip("0")) > MAX_DECIMAL_DIGITS:
        _fail(field, "too many significant digits")
    if len(frac) > MAX_DECIMAL_PLACES:
        _fail(field, "too many decimal places")
    if not int_part and not frac:
        return "0"
    out = int_part or "0"
    if frac:
        out += "." + frac
    return ("-" if sign == "-" else "") + out


def _date(field: FieldSpec, value) -> str:
    if type(value) is not str or _DATE.fullmatch(value) is None:
        _fail(field, "must be a date in YYYY-MM-DD format")
    try:
        date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    except ValueError:
        _fail(field, "not a real calendar date")
    return value


def _revalidated(field) -> FieldSpec:
    if not isinstance(field, FieldSpec):
        raise ValueError("Fields must be FieldSpec instances.")
    try:
        # Re-validate from a dump so model_construct bypasses and later mutation are caught.
        return FieldSpec.model_validate(field.model_dump(mode="json"))
    except Exception:
        raise ValueError("Invalid field specification.") from None


def _normalize(field: FieldSpec, value):
    if value is None:
        return None
    kind = field.type
    if kind == "string":
        return _text(field, value, True)
    if kind == "identifier":
        return _text(field, value, False)
    if kind == "integer":
        return _integer(field, value)
    if kind == "decimal":
        return _decimal(field, value)
    if kind == "boolean":
        if type(value) is not bool:
            _fail(field, "must be a boolean")
        return value
    if kind == "date":
        return _date(field, value)
    if kind == "enum":
        if type(value) is not str or value not in (field.enum or []):
            _fail(field, "not an allowed enum value")
        return value
    _fail(field, "unsupported type")


def normalize_value(field: FieldSpec, value):
    """Return the canonical form of one value (None for absence); raise ValueError otherwise."""
    return _normalize(_revalidated(field), value)


def validate_values(fields: Sequence[FieldSpec], values: dict, *, complete: bool = False) -> dict:
    """Validate a mapping of values against a schema; return a new dict, never mutating the input.

    complete=True only checks that required fields are structurally present and non-blank.
    """
    if isinstance(fields, (str, bytes)) or not isinstance(fields, Sequence):
        raise ValueError("Fields must be a sequence of FieldSpec.")
    if len(fields) > MAX_FIELDS:
        raise ValueError(f"Schema exceeds {MAX_FIELDS} fields.")
    if not isinstance(values, dict):
        raise ValueError("Values must be a dict.")
    if len(values) > MAX_FIELDS:
        raise ValueError(f"Values exceed {MAX_FIELDS} entries.")
    if any(type(k) is not str for k in values):
        raise ValueError("Value keys must be strings.")
    specs = [_revalidated(f) for f in fields]
    names = [s.name for s in specs]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate field name in schema.")
    if set(values) - set(names):
        raise ValueError("Unknown fields in values.")
    result: dict = {}
    for spec in specs:
        present = spec.name in values
        raw = values.get(spec.name)
        if complete and spec.required:
            if raw is None or (type(raw) is str and not raw.strip()):
                _fail(spec, "required value is missing")
        if not present:
            continue
        result[spec.name] = _normalize(spec, raw)
    try:
        size = len(json.dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, UnicodeEncodeError):
        raise ValueError("Values are not serializable.") from None
    if size > MAX_PAYLOAD_BYTES:
        raise ValueError(f"Serialized values exceed {MAX_PAYLOAD_BYTES} bytes.")
    return result
