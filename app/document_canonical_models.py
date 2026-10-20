"""Strict canonical text projections; PDF hashes identify persisted reports only."""
import hashlib
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, StrictBool, StrictInt, StrictStr, StringConstraints, model_validator

from app.metadata_models import _ClosedModel, _utf8, PersistentID, SHA256, Page, VersionOne
from app.results import MAX_PAGE_CHARS, MAX_TOTAL_CHARS
from app.services.canonical_text_service import MAX_CHUNK_CHARS, MAX_JSONL_BYTES


CanonicalOffset = Annotated[StrictInt, Field(ge=0, le=MAX_PAGE_CHARS)]
ChunkText = Annotated[
    StrictStr, StringConstraints(max_length=MAX_CHUNK_CHARS), AfterValidator(_utf8)
]
PageText = Annotated[
    StrictStr, StringConstraints(max_length=MAX_PAGE_CHARS), AfterValidator(_utf8)
]


def _check_text_hash(text: str, digest: str) -> None:
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
        raise ValueError("Canonical text hash mismatch.")


class CanonicalManifest(_ClosedModel):
    schema_version: Literal["ocr-canonical-pages/1"]
    normalization: Literal["identity-utf8-v1"]
    offset_unit: Literal["unicode_code_points"]
    job_id: PersistentID
    document_id: PersistentID
    artifact_version: VersionOne
    text_version: VersionOne
    page_count: Page
    total_characters: Annotated[StrictInt, Field(ge=0, le=MAX_TOTAL_CHARS)]
    size_bytes: Annotated[StrictInt, Field(ge=1, le=MAX_JSONL_BYTES)]
    sha256: SHA256
    input_pdf_sha256: SHA256
    output_pdf_sha256: SHA256
    parent_job_id: PersistentID | None = None

    @model_validator(mode="after")
    def _identity(self):
        if self.document_id != self.job_id or self.parent_job_id == self.job_id:
            raise ValueError("Invalid canonical document identity.")
        return self


class CanonicalPageChunk(_ClosedModel):
    job_id: PersistentID
    document_id: PersistentID
    artifact_version: VersionOne
    text_version: VersionOne
    page: Page
    page_count: Page
    text: ChunkText
    offset: CanonicalOffset
    end_offset: CanonicalOffset
    page_character_count: CanonicalOffset
    text_sha256: SHA256
    canonical_sha256: SHA256
    normalization: Literal["identity-utf8-v1"]
    offset_unit: Literal["unicode_code_points"]
    truncated: StrictBool
    next_page: Page | None
    next_offset: CanonicalOffset | None

    @model_validator(mode="after")
    def _structure(self):
        if self.document_id != self.job_id or self.page > self.page_count:
            raise ValueError("Invalid canonical chunk identity.")
        if not self.offset <= self.end_offset <= self.page_character_count:
            raise ValueError("Invalid canonical chunk range.")
        if self.end_offset - self.offset != len(self.text):
            raise ValueError("Canonical chunk length mismatch.")
        if self.truncated != (self.end_offset < self.page_character_count):
            raise ValueError("Invalid canonical chunk truncation.")
        if self.truncated:
            if not self.text or (self.next_page, self.next_offset) != (self.page, self.end_offset):
                raise ValueError("Invalid canonical chunk continuation.")
        elif self.page < self.page_count:
            if (self.next_page, self.next_offset) != (self.page + 1, 0):
                raise ValueError("Invalid canonical page continuation.")
        elif self.next_page is not None or self.next_offset is not None:
            raise ValueError("Final canonical chunks cannot have a continuation.")
        # Only a complete page can verify the page's text digest.
        if self.offset == 0 and self.end_offset == self.page_character_count:
            _check_text_hash(self.text, self.text_sha256)
        return self


class CanonicalPageText(_ClosedModel):
    document_id: PersistentID
    artifact_version: VersionOne
    text_version: VersionOne
    canonical_sha256: SHA256
    page: Page
    text_sha256: SHA256
    text: PageText

    @model_validator(mode="after")
    def _hash(self):
        _check_text_hash(self.text, self.text_sha256)
        return self
