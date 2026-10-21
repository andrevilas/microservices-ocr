"""Contract models for assisted extraction runs (E3): ocr-extraction-run/1.

A run is a persisted, auditable PROPOSAL. It never changes metadata by itself:
applying it creates an ordinary draft revision (origin=extracted) that still
needs explicit review and approval by a user identity. Evidence references use
the same shape as metadata evidence and are verified against canonical text.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from app.metadata_models import EvidenceRef, RawValue

ID = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CONTRACT_VERSION = "ocr-extraction-run/1"
RunState = Literal["queued", "running", "completed", "failed"]
OutcomeCode = Literal[
    "completed", "abstained", "invalid_output", "output_truncated", "provider_timeout", "provider_unavailable",
    "provider_quota", "provider_error", "provider_policy", "run_timeout", "budget_exceeded", "document_too_large",
    "interrupted", "source_changed",
]
RejectionReason = Literal[
    "unknown_field", "malformed_value", "quote_not_found", "invalid_value", "value_not_in_quote", "max_items_exceeded",
    "candidates_truncated", "unknown_type", "suspicious_source",
]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CreateExtractionInput(_Closed):
    """Exactly one of type_id (extract fields) or context_id (classify into a published type)."""
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    type_id: ID | None = None
    type_version: Annotated[StrictInt, Field(ge=1)] | None = None
    context_id: ID | None = None
    page_start: Annotated[StrictInt, Field(ge=1, le=300)] | None = None
    page_end: Annotated[StrictInt, Field(ge=1, le=300)] | None = None
    retry: StrictBool = False

    @model_validator(mode="after")
    def _shape(self):
        if (self.type_id is None) == (self.context_id is None):
            raise ValueError("Give exactly one of type_id or context_id.")
        if self.context_id is not None and self.type_version is not None:
            raise ValueError("type_version applies only to extraction.")
        if (self.page_start is None) != (self.page_end is None):
            raise ValueError("Give both page_start and page_end, or neither.")
        if self.page_start is not None and self.page_start > self.page_end:
            raise ValueError("Invalid page range.")
        return self


class ApplyExtractionInput(_Closed):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    expected_head: ID | None
    expected_sha256: SHA256 | None

    @model_validator(mode="after")
    def _pair(self):
        if (self.expected_head is None) != (self.expected_sha256 is None):
            raise ValueError("Both preconditions must be supplied together.")
        return self


class ExtractionUsage(_Closed):
    calls: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    latency_ms: int = Field(ge=0)


class ExtractionCandidate(_Closed):
    raw: RawValue
    normalized: RawValue
    evidence: list[EvidenceRef] = Field(max_length=5)
    methods: list[Literal["llm", "deterministic"]] = Field(min_length=1, max_length=2)


class FieldProposal(_Closed):
    field_name: str = Field(min_length=1, max_length=64)
    status: Literal["needs_review", "not_found", "conflict"]
    raw: RawValue = None
    normalized: RawValue = None
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=128)
    candidates: list[ExtractionCandidate] = Field(default_factory=list, max_length=5)
    methods: list[Literal["llm", "deterministic"]] = Field(default_factory=list, max_length=2)


class TypeProposal(_Closed):
    type_id: ID
    type_version: int = Field(ge=1)
    type_name: str
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=1)


class Rejection(_Closed):
    field_name: str | None = None
    reason: RejectionReason
    count: int = Field(ge=1)


class ExtractionView(_Closed):
    contract: Literal["ocr-extraction-run/1"] = CONTRACT_VERSION
    run_id: ID
    job_id: ID
    kind: Literal["extract", "classify"]
    state: RunState
    outcome: OutcomeCode | None = None
    context_id: ID | None = None
    type_id: ID | None = None
    type_version: int | None = None
    schema_sha256: SHA256 | None = None
    canonical_sha256: SHA256
    page_start: int = Field(ge=1)
    page_end: int = Field(ge=1)
    model: str
    prompt_version: str
    created_at: datetime
    finished_at: datetime | None = None
    usage: ExtractionUsage
    fields: list[FieldProposal] | None = None
    classification: TypeProposal | None = None
    rejections: list[Rejection] = Field(default_factory=list)
    applied_revision_ids: list[ID] = Field(default_factory=list)


class ExtractionReceipt(_Closed):
    run: ExtractionView
    replayed: bool


class ExtractionList(_Closed):
    items: list[ExtractionView]
