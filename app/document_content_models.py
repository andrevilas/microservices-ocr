"""Closed DTOs for lossless, bounded metadata content projections."""

from typing import Annotated, Literal

from pydantic import AfterValidator, Field, StrictInt, StrictStr, StringConstraints, model_validator

from app.document_models import MAX_PAYLOAD_BYTES, FieldName
from app.document_transport_models import Cursor, Position, ProjectionVersion, ValueKind
from app.metadata_models import (
    EvidenceRef, ItemIndex, PersistentID, RawInteger, SHA256,
    _ClosedModel, _bounded_canonical, _list, _utf8,
)


Offset = Annotated[StrictInt, Field(ge=0, le=MAX_PAYLOAD_BYTES)]
Part = Literal["raw", "normalized"]
Count = Annotated[StrictInt, Field(ge=0, le=100)]
ChunkText = Annotated[
    StrictStr, StringConstraints(max_length=1000), AfterValidator(_utf8)
]


def _inline(value):
    _bounded_canonical(value, 4096)
    return value


InlineString = Annotated[ChunkText, AfterValidator(_inline)]


def _sizes(characters, utf8_bytes):
    if not characters <= utf8_bytes <= 4 * characters:
        raise ValueError("Character and UTF-8 sizes disagree.")


class MetadataContentHeader(_ClosedModel):
    schema_version: ProjectionVersion = "ocr-document-metadata/1"
    job_id: PersistentID
    revision_id: PersistentID
    sha256: SHA256


class MetadataFieldContentHeader(MetadataContentHeader):
    segment_id: PersistentID
    field_name: FieldName


class MetadataStringReference(_ClosedModel):
    part: Part
    item_index: ItemIndex | None
    characters: Annotated[StrictInt, Field(ge=1, le=4000)]
    utf8_bytes: Annotated[StrictInt, Field(ge=1, le=16000)]
    sha256: SHA256

    @model_validator(mode="after")
    def _structure(self):
        _sizes(self.characters, self.utf8_bytes)
        return self


class MetadataNullComponent(_ClosedModel):
    kind: Literal["null"] = "null"
    value: None = None


class MetadataBooleanComponent(_ClosedModel):
    kind: Literal["boolean"] = "boolean"
    value: Annotated[bool, Field(strict=True)]


class MetadataIntegerComponent(_ClosedModel):
    kind: Literal["integer"] = "integer"
    value: RawInteger


class MetadataStringComponent(_ClosedModel):
    kind: Literal["string"] = "string"
    value: InlineString


class MetadataStringRefComponent(_ClosedModel):
    kind: Literal["string_ref"] = "string_ref"
    reference: MetadataStringReference


MetadataValueComponent = Annotated[
    MetadataNullComponent | MetadataBooleanComponent | MetadataIntegerComponent
    | MetadataStringComponent | MetadataStringRefComponent,
    Field(discriminator="kind"),
]


class MetadataValuePair(_ClosedModel):
    position: Position
    item_index: ItemIndex | None
    raw: MetadataValueComponent
    normalized: MetadataValueComponent

    @model_validator(mode="after")
    def _structure(self):
        if self.item_index is not None and self.item_index != self.position:
            raise ValueError("Item index must match stored position.")
        for part in ("raw", "normalized"):
            component = getattr(self, part)
            if component.kind == "string_ref":
                ref = component.reference
                if ref.part != part or ref.item_index != self.item_index:
                    raise ValueError("String reference coordinates disagree.")
        return self


class MetadataValuePage(MetadataFieldContentHeader):
    raw_kind: ValueKind
    normalized_kind: ValueKind
    total_count: Count
    values: Annotated[list[MetadataValuePair], Field(max_length=50)]
    next_cursor: Cursor | None

    @model_validator(mode="before")
    @classmethod
    def _lists(cls, value):
        if type(value) is dict and "values" in value:
            _list(value["values"])
        return value

    @model_validator(mode="after")
    def _structure(self):
        for kind in ("null", "list"):
            if (self.raw_kind == kind) != (self.normalized_kind == kind):
                raise ValueError("Value cardinalities disagree.")
        expected = 0 if self.raw_kind == "null" else 1
        if self.raw_kind != "list" and self.total_count != expected:
            raise ValueError("Scalar or null count disagrees.")
        positions = [pair.position for pair in self.values]
        if positions:
            if positions != list(range(positions[0], positions[0] + len(positions))):
                raise ValueError("Values must retain consecutive stored positions.")
            if positions[-1] >= self.total_count:
                raise ValueError("Value position exceeds total count.")
            if (self.next_cursor is None) != (positions[-1] == self.total_count - 1):
                raise ValueError("Terminal value page disagrees with total count.")
        elif self.next_cursor is not None:
            raise ValueError("An empty page must be terminal.")
        for pair in self.values:
            expected_index = pair.position if self.raw_kind == "list" else None
            if pair.item_index != expected_index:
                raise ValueError("Value index disagrees with cardinality.")
            for part in ("raw", "normalized"):
                component = getattr(pair, part)
                kind = "string" if component.kind == "string_ref" else component.kind
                if kind == "null":
                    raise ValueError("Null fields have no value pairs.")
                if self.raw_kind != "list" and kind != getattr(self, part + "_kind"):
                    raise ValueError("Component kind disagrees with scalar kind.")
        return self


class MetadataEvidencePage(MetadataFieldContentHeader):
    total_count: Annotated[StrictInt, Field(ge=0, le=128)]
    evidence: Annotated[list[EvidenceRef], Field(max_length=50)]
    next_cursor: Cursor | None

    @model_validator(mode="before")
    @classmethod
    def _lists(cls, value):
        if type(value) is dict and "evidence" in value:
            _list(value["evidence"])
        return value

    @model_validator(mode="after")
    def _structure(self):
        if len(self.evidence) > self.total_count:
            raise ValueError("Evidence exceeds total count.")
        if not self.evidence and self.next_cursor is not None:
            raise ValueError("An empty page must be terminal.")
        if self.next_cursor is not None and len(self.evidence) >= self.total_count:
            raise ValueError("A complete evidence collection must be terminal.")
        return self


class _Chunk(MetadataContentHeader):
    total_characters: Offset
    offset: Offset
    end: Offset
    next_offset: Offset | None
    text: ChunkText

    @model_validator(mode="after")
    def _bounds(self):
        if not self.offset <= self.end <= self.total_characters:
            raise ValueError("Invalid chunk bounds.")
        if self.end - self.offset != len(self.text):
            raise ValueError("Chunk text and bounds disagree.")
        expected = None if self.end == self.total_characters else self.end
        if self.next_offset != expected:
            raise ValueError("Invalid continuation offset.")
        if self.end == self.offset and self.end != self.total_characters:
            raise ValueError("A nonterminal chunk must advance.")
        return self


class MetadataStringChunk(_Chunk):
    segment_id: PersistentID
    field_name: FieldName
    part: Part
    item_index: ItemIndex | None
    utf8_bytes: Annotated[StrictInt, Field(ge=0, le=16000)]
    content_sha256: SHA256

    @model_validator(mode="after")
    def _size(self):
        if self.total_characters > 4000:
            raise ValueError("String exceeds stored string limit.")
        _sizes(self.total_characters, self.utf8_bytes)
        if len(self.text.encode("utf-8")) > self.utf8_bytes:
            raise ValueError("Chunk exceeds source UTF-8 size.")
        return self


class MetadataSchemaChunk(_Chunk):
    schema_sha256: SHA256
    canonical_utf8_bytes: Annotated[StrictInt, Field(ge=1, le=MAX_PAYLOAD_BYTES)]

    @model_validator(mode="after")
    def _size(self):
        _sizes(self.total_characters, self.canonical_utf8_bytes)
        if len(self.text.encode("utf-8")) > self.canonical_utf8_bytes:
            raise ValueError("Chunk exceeds canonical schema size.")
        return self
