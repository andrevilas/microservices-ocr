"""Pure structural DTOs for the C1a manual metadata save contract."""

import json
import re
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationInfo,
    model_validator,
)

from .document_models import MAX_PAYLOAD_BYTES, FieldName, FieldSpec, compose_schema


MAX_REQUEST_BYTES = 512 * 1024
MAX_SAFE_INT = 2**53 - 1
MAX_SEGMENTS = 16
MAX_TOTAL_FIELDS = 200
MAX_TOTAL_EVIDENCE = 128
MAX_SNAPSHOT_BYTES = 1024 * 1024


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


def _check_indices(raw, evidence) -> None:
    if raw is None:
        if evidence:
            raise ValueError("Null values cannot have evidence.")
    elif isinstance(raw, list):
        if any(ref.item_index is None or ref.item_index >= len(raw) for ref in evidence):
            raise ValueError("List evidence requires a valid item index.")
    elif any(ref.item_index is not None for ref in evidence):
        raise ValueError("Scalar evidence cannot have an item index.")


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
        _check_indices(self.raw, self.evidence)
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


def _bounded_canonical(value, limit: int) -> bytes:
    encoder = json.JSONEncoder(
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    result = bytearray()
    for chunk in encoder.iterencode(value):
        remaining = limit - len(result)
        # UTF-8 cannot use fewer bytes than the number of codepoints.
        if len(chunk) > remaining:
            raise ValueError("Canonical document exceeds its byte limit.")
        encoded = chunk.encode("utf-8", errors="strict")
        if len(encoded) > remaining:
            raise ValueError("Canonical document exceeds its byte limit.")
        result.extend(encoded)
    return bytes(result)


def _actor(value: str) -> str:
    if re.fullmatch(r"(?:user:[1-9][0-9]*|service:[0-9a-f]{32})", value, flags=re.ASCII) is None:
        raise ValueError("Invalid actor subject.")
    return value


def _user_actor(value: str) -> str:
    if re.fullmatch(r"user:[1-9][0-9]*", value, flags=re.ASCII) is None:
        raise ValueError("Invalid approval actor.")
    return value


def _datetime_input(value, info: ValidationInfo):
    if info.mode == "json":
        if type(value) is not str:
            raise ValueError("A timezone-aware datetime string is required.")
    elif not isinstance(value, datetime):
        raise ValueError("A timezone-aware datetime is required.")
    return value


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


ActorSubject = Annotated[StrictStr, AfterValidator(_actor)]
UserActorSubject = Annotated[StrictStr, AfterValidator(_user_actor)]
UTCDateTime = Annotated[
    AwareDatetime, BeforeValidator(_datetime_input), AfterValidator(_utc)
]


class ProvenanceRecord(_ClosedModel):
    actor_subject: ActorSubject
    auth_source: Literal["local_jwt", "oauth", "service", "legacy", "unknown"]
    recorded_at: UTCDateTime
    execution_id: PersistentID | None = None


class FieldValueRecord(_ClosedModel):
    field_name: FieldName
    raw: RawValue
    normalized: RawValue
    origin: Literal["manual", "declared", "inherited", "extracted"]
    status: Literal["not_found", "conflict", "needs_review", "reviewed"]
    evidence: Annotated[
        list[EvidenceRef], Field(max_length=MAX_TOTAL_EVIDENCE), BeforeValidator(_list)
    ] = Field(default_factory=list)
    provenance: ProvenanceRecord

    @model_validator(mode="after")
    def _structure(self):
        if (self.raw is None) != (self.normalized is None):
            raise ValueError("Raw and normalized nullability must agree.")
        if isinstance(self.raw, list) != isinstance(self.normalized, list):
            raise ValueError("Raw and normalized cardinality must agree.")
        if isinstance(self.raw, list) and len(self.raw) != len(self.normalized):
            raise ValueError("Raw and normalized list lengths must agree.")
        if self.status == "not_found" and (
            self.raw is not None or self.normalized is not None or self.evidence
        ):
            raise ValueError("Not-found fields cannot contain values or evidence.")
        _check_indices(self.raw, self.evidence)
        if (self.origin == "extracted") != (self.provenance.execution_id is not None):
            raise ValueError("Execution identity must agree with field origin.")
        return self


def _schema_document(value):
    if type(value) is not dict:
        raise ValueError("A schema envelope dictionary is required.")
    return value


class SchemaSnapshot(_ClosedModel):
    schema_sha256: SHA256
    schema_document: Annotated[
        dict[StrictStr, object], BeforeValidator(_schema_document)
    ] = Field(alias="schema")

    @model_validator(mode="after")
    def _schema(self):
        document = self.schema_document
        # Check the ORIGINAL envelope before FieldSpec defaults or composition.
        original = _bounded_canonical(document, MAX_PAYLOAD_BYTES)
        if set(document) != {"schema_version", "common_fields", "specific_fields", "fields"}:
            raise ValueError("Invalid composed schema envelope.")
        if type(document["schema_version"]) is not str or document["schema_version"] != "1":
            raise ValueError("Invalid composed schema version.")
        validated = {}
        for key in ("common_fields", "specific_fields", "fields"):
            items = _list(document[key])
            if len(items) > 100:
                raise ValueError("Too many schema fields.")
            validated[key] = [FieldSpec.model_validate(item, strict=True) for item in items]
        composed = compose_schema(validated["common_fields"], validated["specific_fields"])
        if original != composed.canonical or self.schema_sha256 != composed.sha256:
            raise ValueError("Composed schema bytes and hash must agree.")
        return self


class SegmentRecord(_ClosedModel):
    segment_id: PersistentID
    page_start: Page
    page_end: Page
    context_id: PersistentID
    type_id: PersistentID
    type_version: TypeVersion
    schema_sha256: SHA256
    fields: Annotated[
        list[FieldValueRecord], Field(max_length=100), BeforeValidator(_list)
    ] = Field(default_factory=list)

    @model_validator(mode="after")
    def _structure(self):
        if self.page_start > self.page_end:
            raise ValueError("Invalid segment page range.")
        names = [field.field_name for field in self.fields]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate field name.")
        return self


class ApprovalRecord(_ClosedModel):
    actor_subject: UserActorSubject
    auth_source: Literal["local_jwt", "oauth"]
    approved_at: UTCDateTime
    reviewed_revision_id: PersistentID
    reviewed_sha256: SHA256


class MetadataSnapshot(_ClosedModel):
    schema_version: Literal["ocr-document-metadata/1"] = "ocr-document-metadata/1"
    revision_id: PersistentID
    parent_revision_id: PersistentID | None
    job_id: PersistentID
    document_id: PersistentID
    artifact_version: VersionOne = 1
    text_version: VersionOne = 1
    canonical_sha256: SHA256
    input_pdf_sha256: SHA256
    output_pdf_sha256: SHA256
    page_count: Page
    parent_job_id: PersistentID | None = None
    created_at: UTCDateTime
    author: ProvenanceRecord
    schemas: Annotated[
        list[SchemaSnapshot], Field(min_length=1, max_length=MAX_SEGMENTS), BeforeValidator(_list)
    ]
    segments: Annotated[
        list[SegmentRecord], Field(min_length=1, max_length=MAX_SEGMENTS), BeforeValidator(_list)
    ]
    state: Literal["draft", "approved"]
    approval: ApprovalRecord | None = None

    @model_validator(mode="after")
    def _structure(self):
        if self.document_id != self.job_id:
            raise ValueError("Document and job identity must agree.")
        if self.revision_id == self.parent_revision_id or self.parent_job_id == self.job_id:
            raise ValueError("Self-referential lineage is forbidden.")
        if self.state == "draft":
            if self.approval is not None:
                raise ValueError("Draft snapshots cannot have approval.")
        elif self.approval is None or self.parent_revision_id != self.approval.reviewed_revision_id:
            raise ValueError("Approved snapshots require consistent review lineage.")
        hashes = [schema.schema_sha256 for schema in self.schemas]
        ids = [segment.segment_id for segment in self.segments]
        if len(set(hashes)) != len(hashes) or len(set(ids)) != len(ids):
            raise ValueError("Duplicate snapshot identity.")
        if set(hashes) != {segment.schema_sha256 for segment in self.segments}:
            raise ValueError("Every schema must be present and used.")
        for previous, current in zip(self.segments, self.segments[1:]):
            if previous.page_end >= current.page_start:
                raise ValueError("Segments must be ordered and non-overlapping.")
        if sum(len(segment.fields) for segment in self.segments) > MAX_TOTAL_FIELDS:
            raise ValueError("Too many fields in snapshot.")
        if sum(len(field.evidence) for segment in self.segments for field in segment.fields) > MAX_TOTAL_EVIDENCE:
            raise ValueError("Too many evidence references in snapshot.")
        for segment in self.segments:
            if segment.page_end > self.page_count:
                raise ValueError("Segment exceeds snapshot page count.")
            for field in segment.fields:
                for ref in field.evidence:
                    if (
                        ref.document_id != self.document_id
                        or ref.artifact_version != self.artifact_version
                        or ref.text_version != self.text_version
                        or not segment.page_start <= ref.page <= segment.page_end
                    ):
                        raise ValueError("Evidence does not belong to its snapshot segment.")
        _bounded_canonical(
            self.model_dump(mode="json", by_alias=True, warnings="error"), MAX_SNAPSHOT_BYTES
        )
        return self


def snapshot_bytes(value) -> bytes:
    """Return canonical immutable snapshot bytes; expose only a fixed error."""
    try:
        if not isinstance(value, MetadataSnapshot) and type(value) is not dict:
            raise ValueError("A snapshot model or dictionary is required.")
        # Validate original nested instances before any serializer can coerce them.
        result = MetadataSnapshot.model_validate(value, strict=True)
        dump = result.model_dump(mode="python", by_alias=True, warnings="error")
        result = MetadataSnapshot.model_validate(dump, strict=True)
        return _bounded_canonical(
            result.model_dump(mode="json", by_alias=True, warnings="error"), MAX_SNAPSHOT_BYTES
        )
    except Exception:
        pass
    raise ValueError("Invalid metadata snapshot.")
