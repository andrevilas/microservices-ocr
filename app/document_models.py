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

_CONFIG = dict(extra="forbid", frozen=True, hide_input_in_errors=True, revalidate_instances="always")


class FieldSpec(BaseModel):
    model_config = ConfigDict(**_CONFIG)
    name: FieldName
    type: FieldType
    description: Description
    required: StrictBool = False
    critical: StrictBool = False
    evidence_required: StrictBool = False
    enum: Annotated[
        list[EnumValue] | None,
        BeforeValidator(lambda v: v if v is None else _must_be_list(v)),
        Field(min_length=1, max_length=MAX_ENUM_VALUES),
    ] = None

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


def _snapshot(fields: Sequence[FieldSpec]) -> list[dict]:
    if isinstance(fields, (str, bytes)) or not isinstance(fields, Sequence):
        raise ValueError("Fields must be a sequence of FieldSpec.")
    out = []
    for f in fields:
        if not isinstance(f, FieldSpec):
            raise ValueError("Fields must be FieldSpec instances.")
        # Re-validate from a dump so later mutation of the source or model_construct bypasses are caught.
        out.append(FieldSpec.model_validate(f.model_dump(mode="json")).model_dump(mode="json", exclude_none=True))
    return out


def compose_schema(common: Sequence[FieldSpec], specific: Sequence[FieldSpec]) -> ComposedSchema:
    common_s, specific_s = _snapshot(common), _snapshot(specific)
    fields = common_s + specific_s
    if len(fields) > MAX_FIELDS:
        raise ValueError(f"Composed schema exceeds {MAX_FIELDS} fields.")
    names = [f["name"] for f in fields]
    if len(set(names)) != len(names):
        raise ValueError("Duplicate field name in composition.")
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
