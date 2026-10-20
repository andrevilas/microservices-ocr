"""Closed, bounded metadata projections. No storage or transport wiring."""

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, StrictInt, StrictStr, StringConstraints, model_validator

from .document_models import MAX_PAYLOAD_BYTES, FieldName
from .metadata_models import (
    MAX_SNAPSHOT_BYTES,
    ApprovalRecord,
    Page,
    PersistentID,
    ProvenanceRecord,
    SHA256,
    TypeVersion,
    UTCDateTime,
    VersionOne,
    _ClosedModel,
    _list,
)


MAX_PROJECTION_BYTES = 16384
ProjectionVersion = Literal["ocr-document-metadata/1"]
ValueKind = Literal["null", "list", "string", "integer", "boolean"]
Position = Annotated[StrictInt, Field(ge=0, le=99)]
Count = Annotated[StrictInt, Field(ge=0, le=100)]

_ID = r"[0-9a-f]{32}"
_HASH = r"[0-9a-f]{64}"
_BASE = rf"/api/v1/jobs/{_ID}/metadata/revisions/{_ID}"
_CONTENT = re.compile(rf"{_BASE}/content\?sha256={_HASH}", re.ASCII)
_FIELDS = re.compile(rf"{_BASE}/segments/{_ID}/fields\?sha256={_HASH}", re.ASCII)
_SCHEMA = re.compile(rf"{_BASE}/schemas/{_HASH}\?sha256={_HASH}", re.ASCII)
_TOKEN = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)


def _reference(value: str, pattern) -> str:
    value.encode("utf-8", errors="strict")
    if pattern.fullmatch(value) is None:
        raise ValueError("Invalid metadata reference.")
    return value


def _content_path(value: str) -> str:
    return _reference(value, _CONTENT)


def _fields_path(value: str) -> str:
    return _reference(value, _FIELDS)


def _schema_path(value: str) -> str:
    return _reference(value, _SCHEMA)


def _cursor(value: str) -> str:
    if _TOKEN.fullmatch(value) is None:
        raise ValueError("Invalid metadata cursor representation.")
    return value


ContentPath = Annotated[
    StrictStr, StringConstraints(max_length=320), AfterValidator(_content_path)
]
FieldsPath = Annotated[
    StrictStr, StringConstraints(max_length=320), AfterValidator(_fields_path)
]
SchemaPath = Annotated[
    StrictStr, StringConstraints(max_length=320), AfterValidator(_schema_path)
]
Cursor = Annotated[
    StrictStr, StringConstraints(min_length=1, max_length=2048), AfterValidator(_cursor)
]


class MetadataSchemaReference(_ClosedModel):
    schema_sha256: SHA256
    canonical_utf8_bytes: Annotated[StrictInt, Field(ge=1, le=MAX_PAYLOAD_BYTES)]
    schema_path: SchemaPath

    @model_validator(mode="after")
    def _identity(self):
        if f"/schemas/{self.schema_sha256}?" not in self.schema_path:
            raise ValueError("Schema reference identity must agree.")
        return self


class MetadataSegmentSummary(_ClosedModel):
    segment_id: PersistentID
    page_start: Page
    page_end: Page
    context_id: PersistentID
    type_id: PersistentID
    type_version: TypeVersion
    schema_sha256: SHA256
    field_count: Count
    fields_path: FieldsPath

    @model_validator(mode="after")
    def _structure(self):
        if self.page_start > self.page_end:
            raise ValueError("Invalid segment page range.")
        if f"/segments/{self.segment_id}/fields?" not in self.fields_path:
            raise ValueError("Segment reference identity must agree.")
        return self


class MetadataView(_ClosedModel):
    schema_version: ProjectionVersion = "ocr-document-metadata/1"
    revision_id: PersistentID
    parent_revision_id: PersistentID | None
    job_id: PersistentID
    document_id: PersistentID
    artifact_version: VersionOne
    text_version: VersionOne
    canonical_sha256: SHA256
    input_pdf_sha256: SHA256
    output_pdf_sha256: SHA256
    page_count: Page
    parent_job_id: PersistentID | None
    created_at: UTCDateTime
    author: ProvenanceRecord
    state: Literal["draft", "approved"]
    approval: ApprovalRecord | None
    sha256: SHA256
    content_path: ContentPath
    segments: Annotated[
        list[MetadataSegmentSummary], BeforeValidatorPlaceholder := Field(min_length=1, max_length=16)
    ]
    schemas: Annotated[list[MetadataSchemaReference], Field(min_length=1, max_length=16)]

    @model_validator(mode="before")
    @classmethod
    def _lists(cls, value):
        if type(value) is dict:
            for name in ("segments", "schemas"):
                if name in value:
                    _list(value[name])
        return value

    @model_validator(mode="after")
    def _structure(self):
        if self.document_id != self.job_id:
            raise ValueError("Document and job identity must agree.")
        if self.revision_id == self.parent_revision_id or self.parent_job_id == self.job_id:
            raise ValueError("Self-referential lineage is forbidden.")
        if self.state == "draft":
            if self.approval is not None:
                raise ValueError("Draft views cannot have approval.")
        elif self.approval is None or self.approval.reviewed_revision_id != self.parent_revision_id:
            raise ValueError("Approved views require consistent review lineage.")
        base = f"/api/v1/jobs/{self.job_id}/metadata/revisions/{self.revision_id}"
        suffix = f"?sha256={self.sha256}"
        if self.content_path != base + "/content" + suffix:
            raise ValueError("Content reference scope must agree.")
        hashes = [schema.schema_sha256 for schema in self.schemas]
        ids = [segment.segment_id for segment in self.segments]
        if len(set(hashes)) != len(hashes) or len(set(ids)) != len(ids):
            raise ValueError("Duplicate projection identity.")
        if set(hashes) != {segment.schema_sha256 for segment in self.segments}:
            raise ValueError("Every schema reference must be present and used.")
        if sum(segment.field_count for segment in self.segments) > 200:
            raise ValueError("Too many projected fields.")
        for previous, current in zip(self.segments, self.segments[1:]):
            if previous.page_end >= current.page_start:
                raise ValueError("Segments must be ordered and non-overlapping.")
        for segment in self.segments:
            if segment.page_end > self.page_count:
                raise ValueError("Segment exceeds page count.")
            if segment.fields_path != base + f"/segments/{segment.segment_id}/fields" + suffix:
                raise ValueError("Fields reference scope must agree.")
        for schema in self.schemas:
            if schema.schema_path != base + f"/schemas/{schema.schema_sha256}" + suffix:
                raise ValueError("Schema reference scope must agree.")
        return self


class MetadataRevisionSummary(_ClosedModel):
    revision_id: PersistentID
    parent_revision_id: PersistentID | None
    sha256: SHA256
    size_bytes: Annotated[StrictInt, Field(ge=1, le=MAX_SNAPSHOT_BYTES)]
    state: Literal["draft", "approved"]
    created_at: UTCDateTime

    @model_validator(mode="after")
    def _lineage(self):
        if self.revision_id == self.parent_revision_id:
            raise ValueError("Self-referential lineage is forbidden.")
        return self


class MetadataRevisionPage(_ClosedModel):
    schema_version: ProjectionVersion = "ocr-document-metadata/1"
    job_id: PersistentID
    revisions: Annotated[list[MetadataRevisionSummary], Field(max_length=32)]
    next_cursor: Cursor | None

    @model_validator(mode="before")
    @classmethod
    def _lists(cls, value):
        if type(value) is dict and "revisions" in value:
            _list(value["revisions"])
        return value

    @model_validator(mode="after")
    def _ordering(self):
        ids = [revision.revision_id for revision in self.revisions]
        if ids != sorted(set(ids)):
            raise ValueError("Revision summaries must be strictly ordered.")
        if not self.revisions and self.next_cursor is not None:
            raise ValueError("An empty page must be terminal.")
        return self


class MetadataFieldDescriptor(_ClosedModel):
    position: Position
    field_name: FieldName
    origin: Literal["manual", "declared", "inherited", "extracted"]
    status: Literal["not_found", "conflict", "needs_review", "reviewed"]
    provenance: ProvenanceRecord
    raw_kind: ValueKind
    normalized_kind: ValueKind
    value_count: Count
    evidence_count: Annotated[StrictInt, Field(ge=0, le=128)]

    @model_validator(mode="after")
    def _structure(self):
        if (self.raw_kind == "null") != (self.normalized_kind == "null"):
            raise ValueError("Projected nullability must agree.")
        if (self.raw_kind == "list") != (self.normalized_kind == "list"):
            raise ValueError("Projected cardinality must agree.")
        if self.raw_kind == "null":
            if self.value_count != 0 or self.evidence_count != 0:
                raise ValueError("Null fields cannot contain values or evidence.")
        elif self.raw_kind != "list" and self.value_count != 1:
            raise ValueError("Scalar fields require one value.")
        if self.status == "not_found" and self.raw_kind != "null":
            raise ValueError("Not-found fields must be null.")
        if (self.origin == "extracted") != (self.provenance.execution_id is not None):
            raise ValueError("Execution identity must agree with field origin.")
        return self


class MetadataFieldPage(_ClosedModel):
    schema_version: ProjectionVersion = "ocr-document-metadata/1"
    job_id: PersistentID
    revision_id: PersistentID
    sha256: SHA256
    segment_id: PersistentID
    fields: Annotated[list[MetadataFieldDescriptor], Field(max_length=50)]
    next_cursor: Cursor | None

    @model_validator(mode="before")
    @classmethod
    def _lists(cls, value):
        if type(value) is dict and "fields" in value:
            _list(value["fields"])
        return value

    @model_validator(mode="after")
    def _ordering(self):
        positions = [field.position for field in self.fields]
        if positions and positions != list(range(positions[0], positions[0] + len(positions))):
            raise ValueError("Field descriptors must retain consecutive stored positions.")
        names = [field.field_name for field in self.fields]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate field name.")
        if not self.fields and self.next_cursor is not None:
            raise ValueError("An empty page must be terminal.")
        return self
