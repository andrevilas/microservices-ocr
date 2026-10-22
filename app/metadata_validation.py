"""Pure validation and copying of manual metadata; no approval or authority checks."""
import re
from collections.abc import Sequence

from app.document_models import MAX_FIELDS, FieldSpec, revalidate_field
from app.document_values import _conditions, validate_values
from app.metadata_models import FieldValueRecord, ManualFieldInput, ProvenanceRecord


_CODES = frozenset({
    "metadata_invalid_input", "metadata_invalid_schema", "metadata_invalid_value",
    "unsupported_metadata_state", "unsupported_metadata_origin",
    "evidence_invalid", "canonical_source_invalid",
})
_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)
_MAX_EVIDENCE = 128


class MetadataValidationError(ValueError):
    """A closed error code and optional validated, non-sensitive schema name."""

    def __init__(self, code, field_name=None):
        if type(code) is not str or code not in _CODES:
            raise ValueError("Invalid metadata error code.")
        if field_name is not None and (
            type(field_name) is not str or _FIELD_NAME.fullmatch(field_name) is None
        ):
            raise ValueError("Invalid metadata field name.")
        self.code = code
        self.field_name = field_name
        super().__init__("Metadata validation failed.")


def _copy_model(model, value):
    # Validate the original first: dumping a malformed constructed instance can
    # erase distinctions that strict validation must reject.
    failed = False
    copied = None
    try:
        validated = model.model_validate(value, strict=True)
        copied = model.model_validate(validated.model_dump(mode="python"), strict=True)
    except (ValueError, TypeError):
        failed = True
    if failed:
        raise MetadataValidationError("metadata_invalid_input")
    return copied


def _schema(fields):
    if (
        isinstance(fields, (str, bytes))
        or not isinstance(fields, Sequence)
        or len(fields) > MAX_FIELDS
    ):
        raise MetadataValidationError("metadata_invalid_schema")
    failed = False
    specs = None
    try:
        specs = [revalidate_field(field) for field in fields]
        names = [field.name for field in specs]
        if len(set(names)) != len(names):
            failed = True
        else:
            # Reuse the existing condition normalizer, including typed literals.
            _conditions(specs)
    except (ValueError, TypeError):
        failed = True
    if failed:
        raise MetadataValidationError("metadata_invalid_schema")
    return specs


def _entries(model, entries, specs):
    if type(entries) is not list or len(entries) > MAX_FIELDS:
        raise MetadataValidationError("metadata_invalid_input")
    names = {field.name for field in specs}
    copied = []
    seen = set()
    evidence_count = 0
    for entry in entries:
        item = _copy_model(model, entry)
        if item.field_name not in names or item.field_name in seen:
            raise MetadataValidationError("metadata_invalid_input")
        seen.add(item.field_name)
        evidence_count += len(item.evidence)
        if evidence_count > _MAX_EVIDENCE:
            raise MetadataValidationError("metadata_invalid_input")
        copied.append(item)
    return copied


def _normalized(specs, entries):
    failed = False
    result = None
    try:
        result = validate_values(
            specs, {entry.field_name: entry.raw for entry in entries}, complete=False
        )
    except (ValueError, TypeError):
        failed = True
    if failed:
        raise MetadataValidationError("metadata_invalid_value")
    return result


def _same(left, right):
    if type(left) is not type(right):
        return False
    if type(left) is list:
        return len(left) == len(right) and all(_same(a, b) for a, b in zip(left, right))
    return left == right


def normalize_manual_values(fields, entries, provenance) -> list[FieldValueRecord]:
    """Copy explicit manual inputs into unapproved records, ordered by schema."""
    specs = _schema(fields)
    items = _entries(ManualFieldInput, entries, specs)
    source = _copy_model(ProvenanceRecord, provenance)
    if source.execution_id is not None:
        raise MetadataValidationError("metadata_invalid_input")
    normalized = _normalized(specs, items)
    by_name = {item.field_name: item for item in items}
    result = []
    for field in specs:
        item = by_name.get(field.name)
        if item is None:
            continue
        result.append(_copy_model(FieldValueRecord, {
            "field_name": field.name,
            "raw": item.raw,
            "normalized": normalized[field.name],
            "origin": item.origin,
            "status": "not_found" if item.raw is None else "needs_review",
            "evidence": [e.model_dump(mode="python") for e in item.evidence],
            "provenance": source.model_dump(mode="python"),
        }))
    return result


def validate_value_records(fields, records) -> None:
    """Validate E2 drafts, without requiring completeness or granting approval."""
    specs = _schema(fields)
    items = _entries(FieldValueRecord, records, specs)
    for item in items:
        if item.origin not in ("manual", "declared"):
            raise MetadataValidationError("unsupported_metadata_origin", item.field_name)
        if item.status not in ("not_found", "needs_review", "reviewed"):
            raise MetadataValidationError("unsupported_metadata_state", item.field_name)
    normalized = _normalized(specs, items)
    for item in items:
        if not _same(item.normalized, normalized[item.field_name]):
            raise MetadataValidationError("metadata_invalid_value", item.field_name)
        if (item.raw is None) != (item.status == "not_found"):
            raise MetadataValidationError("metadata_invalid_value", item.field_name)
