"""Closed declarative catalog DTOs and canonical schema composition. Pure: no I/O, no import effects."""
import hashlib
import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    model_validator,
)

MAX_PAYLOAD_BYTES = 262144
MAX_FIELDS = 100
MAX_ENUM_VALUES = 100
MAX_TEXT = 4000
MAX_NAME = 120
SCHEMA_VERSION = "1"

FieldType = Literal["string", "identifier", "integer", "decimal", "boolean", "date", "enum"]
_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}", re.ASCII)


def _check_text(value: str, allow_tab_lf: bool) -> str:
    for ch in value:
        if 0xD800 <= ord(ch) <= 0xDFFF:
            raise ValueError("Isolated surrogate code point is not allowed.")
        if unicodedata.category(ch) == "Cc" and not (allow_tab_lf and ch in "\t\n"):
            raise ValueError("Control characters are not allowed.")
    return value


def _text_ok(value: str) -> str:
    return _check_text(value, True)


def _name_ok(value: str) -> str:
    _check_text(value, False)
    # Policy: names are kept verbatim; leading/trailing whitespace (incl. blank-only) is rejected, never stripped.
    if value != value.strip():
        raise ValueError("Name must not have leading or trailing whitespace.")
    return value


def _field_name_ok(value: str) -> str:
    if not _FIELD_NAME.fullmatch(value):
        raise ValueError("Field name must match [a-z][a-z0-9_]{0,63}.")
    return value


def _must_be_list(value):
    if not isinstance(value, list):
        raise ValueError("Must be a list.")
    return value


Text = Annotated[StrictStr, StringConstraints(max_length=MAX_TEXT), AfterValidator(_text_ok)]
Description = Annotated[StrictStr, StringConstraints(min_length=1, max_length=MAX_TEXT), AfterValidator(_text_ok)]
EnumValue = Annotated[StrictStr, StringConstraints(min_length=1, max_length=MAX_TEXT), AfterValidator(_text_ok)]
CatalogName = Annotated[
    StrictStr, StringConstraints(min_length=1, max_length=MAX_NAME), AfterValidator(_name_ok)
]
FieldName = Annotated[StrictStr, StringConstraints(max_length=64), AfterValidator(_field_name_ok)]

MAX_INT = 2**53 - 1
MAX_EXAMPLES = 3
MAX_ITEMS_LIMIT = 100


def _equals_ok(value):
    if type(value) is str:
        if len(value) > MAX_TEXT:
            raise ValueError("equals text is too long.")
        _check_text(value, True)
    elif type(value) is int:
        if value > MAX_INT or value < -MAX_INT:
            raise ValueError("equals integer out of range.")
    elif type(value) is not bool:
        raise ValueError("equals must be a string, boolean or integer.")
    return value


EqualsValue = Annotated[StrictStr | StrictBool | StrictInt, AfterValidator(_equals_ok)]

_CONFIG = dict(extra="forbid", frozen=True, hide_input_in_errors=True, revalidate_instances="always")


class RequiredWhen(BaseModel):
    """Closed structural condition: the field is required when another (single-valued) field equals a literal."""

    model_config = ConfigDict(**_CONFIG)
    field: FieldName
    equals: EqualsValue


class FieldSpec(BaseModel):
    model_config = ConfigDict(**_CONFIG)
    name: FieldName
    type: FieldType
    description: Description
    # None means "use name as label"; composition materializes the resolved label.
    label: CatalogName | None = None
    required: StrictBool = False
    critical: StrictBool = False
    evidence_required: StrictBool = False
    # sensitivity/indexable/destination are contracts for E4; nothing is inferred from them here.
    sensitivity: Literal["public", "internal", "restricted"] = "internal"
    indexable: StrictBool = False
    cardinality: Literal["one", "many"] = "one"
    max_items: Annotated[int, Field(strict=True, ge=1, le=MAX_ITEMS_LIMIT)] = 20
    normalization: Literal["type-default"] = "type-default"
    destination: Literal["sidecar", "xmp"] = "sidecar"
    # Synthetic, descriptive text samples only; never proven values.
    examples: Annotated[list[Text], BeforeValidator(_must_be_list), Field(max_length=MAX_EXAMPLES)] = Field(
        default_factory=list
    )
    required_when: RequiredWhen | None = None
    enum: (
        Annotated[
            list[EnumValue],
            BeforeValidator(_must_be_list),
            Field(min_length=1, max_length=MAX_ENUM_VALUES),
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def _self_reference(self):
        if self.required_when is not None and self.required_when.field == self.name:
            raise ValueError("required_when must not reference its own field.")
        return self

    @model_validator(mode="after")
    def _enum_rules(self):
        if self.type == "enum":
            if self.enum is None:
                raise ValueError("Enum fields require enum values.")
            if len(set(self.enum)) != len(self.enum):
                raise ValueError("Duplicate enum values.")
        elif self.enum is not None:
            raise ValueError("enum is only allowed for type=enum.")
        return self


def _fields_list(value):
    return _must_be_list(value)


FieldList = Annotated[list[FieldSpec], BeforeValidator(_fields_list), Field(max_length=MAX_FIELDS)]


def _no_duplicate_names(fields) -> None:
    names = [f.name for f in fields]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate field name.")


def _canonical_bytes(data) -> bytes:
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _check_payload(model: BaseModel) -> None:
    if len(_canonical_bytes(model.model_dump(mode="json"))) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"Serialized payload exceeds {MAX_PAYLOAD_BYTES} bytes.")


class ContextCreate(BaseModel):
    model_config = ConfigDict(**_CONFIG)
    name: CatalogName
    description: Text = ""
    common_fields: FieldList = Field(default_factory=list)

    @model_validator(mode="after")
    def _limits(self):
        _no_duplicate_names(self.common_fields)
        _check_payload(self)
        return self


class TypeDraft(BaseModel):
    model_config = ConfigDict(**_CONFIG)
    name: CatalogName
    description: Text = ""
    specific_fields: FieldList = Field(default_factory=list)

    @model_validator(mode="after")
    def _limits(self):
        _no_duplicate_names(self.specific_fields)
        _check_payload(self)
        return self


@dataclass(frozen=True)
class ComposedSchema:
    """Immutable snapshot: canonical bytes and the SHA-256 (hex) of exactly those bytes."""

    canonical: bytes
    sha256: str


def _check_sequence(fields) -> None:
    if isinstance(fields, (str, bytes)) or not isinstance(fields, Sequence):
        raise ValueError("Fields must be a sequence of FieldSpec.")


def revalidate_field(field: FieldSpec) -> FieldSpec:
    """Re-validate from a dump so model_construct bypasses and later mutation are caught.

    Serializer warnings are promoted to errors and every failure is replaced by a fixed message,
    so neither warnings nor exceptions can echo field content.
    """
    try:
        return FieldSpec.model_validate(field.model_dump(mode="json", warnings="error"))
    except Exception:
        raise ValueError("Invalid field specification.") from None


def _snapshot(fields: Sequence[FieldSpec]) -> list[dict]:
    _check_sequence(fields)
    out = []
    for f in fields:
        if not isinstance(f, FieldSpec):
            raise ValueError("Fields must be FieldSpec instances.")
        checked = revalidate_field(f)
        try:
            dumped = checked.model_dump(mode="json", exclude_none=True, warnings="error")
        except Exception:
            raise ValueError("Invalid field specification.") from None
        dumped.setdefault("label", dumped["name"])
        out.append(dumped)
    return out


def _check_conditions(fields: list[dict]) -> None:
    by_name = {f["name"]: f for f in fields}
    for f in fields:
        cond = f.get("required_when")
        if cond is None:
            continue
        target = by_name.get(cond["field"])
        if target is None:
            raise ValueError("required_when references an unknown field.")
        if target["name"] == f["name"]:
            raise ValueError("required_when must not reference its own field.")
        if target["cardinality"] != "one":
            raise ValueError("required_when must reference a single-valued field.")


def compose_schema(common: Sequence[FieldSpec], specific: Sequence[FieldSpec]) -> ComposedSchema:
    _check_sequence(common)
    _check_sequence(specific)
    if len(common) + len(specific) > MAX_FIELDS:  # reject before any serialization/copy
        raise ValueError(f"Composed schema exceeds {MAX_FIELDS} fields.")
    common_s, specific_s = _snapshot(common), _snapshot(specific)
    fields = common_s + specific_s
    names = [f["name"] for f in fields]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate field name in composition.")
    _check_conditions(fields)
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "common_fields": common_s,
        "specific_fields": specific_s,
        "fields": fields,
    }
    data = _canonical_bytes(envelope)
    if len(data) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"Composed schema exceeds {MAX_PAYLOAD_BYTES} bytes.")
    return ComposedSchema(canonical=data, sha256=hashlib.sha256(data).hexdigest())
