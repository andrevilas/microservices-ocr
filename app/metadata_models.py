"""Pure structural DTOs for the C1a manual metadata save contract."""

import json
import re
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

from .document_models import FieldName


MAX_REQUEST_BYTES = 512 * 1024
MAX_SAFE_INT = 2**53 - 1
MAX_SEGMENTS = 16
MAX_TOTAL_FIELDS = 200
MAX_TOTAL_EVIDENCE = 128


def _utf8(value: str) -> str:
    value.encode("utf-8", errors="strict")
    return value


def _id(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{32}", value, flags=re.ASCII) is None:
        raise ValueError("Invalid persistent ID.")
    return value


def _hash(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value, flags=re.ASCII) is None:
        raise ValueError("Invalid SHA-256.")
    return value


def _client_key(value: str) -> str:
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", value, flags=re.ASCII) is None:
        raise ValueError("Invalid client key.")
    return value


def _list(value):
    if type(value) is not list:
        raise ValueError("A JSON list is required.")
    return value


PersistentID = Annotated[StrictStr, AfterValidator(_id)]
SHA256 = Annotated[StrictStr, AfterValidator(_hash)]
ClientKey = Annotated[StrictStr, AfterValidator(_client_key)]
VersionOne = Annotated[StrictInt, Field(ge=1, le=1)]
Page = Annotated[StrictInt, Field(ge=1, le=300)]
Offset = Annotated[StrictInt, Field(ge=0, le=500000)]
TypeVersion = Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]
RawString = Annotated[StrictStr, StringConstraints(max_length=4000), AfterValidator(_utf8)]
RawInteger = Annotated[StrictInt, Field(ge=-MAX_SAFE_INT, le=MAX_SAFE_INT)]
Scalar = RawString | RawInteger | StrictBool
RawList = Annotated[list[Scalar], Field(max_length=100), BeforeValidator(_list)]
RawValue = Scalar | RawList | None
Quote = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=2000),
    AfterValidator(_utf8),
]
ItemIndex = Annotated[StrictInt, Field(ge=0, le=99)]


class _ClosedModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
        strict=True,
    )


class EvidenceRef(_ClosedModel):
    document_id: PersistentID
    artifact_version: VersionOne = 1
    text_version: VersionOne = 1
    page: Page
    text_sha256: SHA256
    start: Offset
    end: Offset
    quote: Quote
    item_index: ItemIndex | None = None

    @model_validator(mode="after")
    def _range(self):
        if self.start >= self.end or len(self.quote) != self.end - self.start:
            raise ValueError("Evidence range and quote length must agree.")
        return self


class ManualFieldInput(_ClosedModel):
    field_name: FieldName
    raw: RawValue
    origin: Literal["manual", "declared"] = "manual"
    evidence: Annotated[
        list[EvidenceRef], Field(max_length=128), BeforeValidator(_list)
    ] = Field(default_factory=list)

    @model_validator(mode="after")
    def _indices(self):
        if self.raw is None:
            if self.evidence:
                raise ValueError("Null values cannot have evidence.")
        elif isinstance(self.raw, list):
            if any(ref.item_index is None or ref.item_index >= len(self.raw) for ref in self.evidence):
                raise ValueError("List evidence requires a valid item index.")
        elif any(ref.item_index is not None for ref in self.evidence):
            raise ValueError("Scalar evidence cannot have an item index.")
        return self


class SegmentInput(_ClosedModel):
    client_key: ClientKey
    segment_id: PersistentID | None = None
    page_start: Page
    page_end: Page
    type_id: PersistentID
    type_version: TypeVersion
    fields: Annotated[
        list[ManualFieldInput], Field(max_length=100), BeforeValidator(_list)
    ] = Field(default_factory=list)

    @model_validator(mode="after")
    def _structure(self):
        if self.page_start > self.page_end:
            raise ValueError("Invalid segment page range.")
        names = [field.field_name for field in self.fields]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate field name.")
        return self


def _check_request_bytes(dump) -> None:
    # Keep only a bounded byte count, never a complete encoded request.
    encoder = json.JSONEncoder(
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    size = 0
    for chunk in encoder.iterencode(dump):
        size += len(chunk.encode("utf-8"))
        if size > MAX_REQUEST_BYTES:
            raise ValueError("Manual metadata request exceeds its byte limit.")


class SaveMetadataInput(_ClosedModel):
    expected_head: PersistentID | None
    expected_sha256: SHA256 | None
    segments: Annotated[
        list[SegmentInput], Field(min_length=1, max_length=16), BeforeValidator(_list)
    ]

    @model_validator(mode="after")
    def _structure(self):
        if (self.expected_head is None) != (self.expected_sha256 is None):
            raise ValueError("Both save preconditions must be supplied together.")
        keys = [segment.client_key for segment in self.segments]
        ids = [segment.segment_id for segment in self.segments if segment.segment_id is not None]
        if len(set(keys)) != len(keys) or len(set(ids)) != len(ids):
            raise ValueError("Duplicate segment identity.")
        for previous, current in zip(self.segments, self.segments[1:]):
            if previous.page_end >= current.page_start:
                raise ValueError("Segments must be ordered and non-overlapping.")
        if sum(len(segment.fields) for segment in self.segments) > MAX_TOTAL_FIELDS:
            raise ValueError("Too many fields in request.")
        if sum(len(field.evidence) for segment in self.segments for field in segment.fields) > MAX_TOTAL_EVIDENCE:
            raise ValueError("Too many evidence references in request.")
        _check_request_bytes(self.model_dump(mode="json", by_alias=True, warnings="error"))
        return self


class SaveMetadataReceipt(_ClosedModel):
    revision_id: PersistentID
    sha256: SHA256
    segment_ids: Annotated[dict[ClientKey, PersistentID], Field(max_length=16)]

    @model_validator(mode="after")
    def _unique_ids(self):
        if len(set(self.segment_ids.values())) != len(self.segment_ids):
            raise ValueError("Duplicate persistent segment ID.")
        return self


def validate_manual_input(value) -> SaveMetadataInput:
    """Return an independently revalidated request; expose only a fixed error."""
    try:
        if not isinstance(value, SaveMetadataInput) and type(value) is not dict:
            raise ValueError("A model or dictionary is required.")
        # Revalidate original nested instances before serialization can coerce them.
        result = SaveMetadataInput.model_validate(value, strict=True)
        # Detach nested mutable state only after strict recursive validation.
        dump = result.model_dump(mode="python", by_alias=True, warnings="error")
        result = SaveMetadataInput.model_validate(dump, strict=True)
        _check_request_bytes(result.model_dump(mode="json", by_alias=True, warnings="error"))
        return result
    except Exception:
        pass
    # Raise outside the handler, without retaining or chaining sensitive errors.
    raise ValueError("Invalid manual metadata input.")
