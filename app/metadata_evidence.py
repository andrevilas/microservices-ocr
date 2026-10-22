"""Literal evidence and semantic readiness; no approval or persistence authority."""

from dataclasses import dataclass
import hashlib
from typing import Callable

from .document_models import FieldSpec
from .document_values import missing_required_fields
from .metadata_models import MetadataSnapshot, snapshot_bytes
from .metadata_validation import MetadataValidationError, validate_value_records


@dataclass(frozen=True)
class CanonicalPageText:
    document_id: str
    artifact_version: int
    text_version: int
    canonical_sha256: str
    page: int
    text_sha256: str
    text: str


@dataclass(frozen=True)
class Blocker:
    code: str
    segment_id: str
    field_name: str
    item_index: int | None = None


@dataclass(frozen=True)
class ReadinessResult:
    approvable: bool
    blockers: list[Blocker]


PageSource = Callable[[int], CanonicalPageText]
_MAX_PAGE_CHARS = 500_000
_MAX_TOTAL_CHARS = 5_000_000
_MAX_PAGES = 300


def _hex_string(value, length):
    return (
        type(value) is str
        and len(value) == length
        and all(char in "0123456789abcdef" for char in value)
    )


class _PageCache:
    """Operation-local cache, bounded by the total loaded codepoint budget."""

    def __init__(self, snapshot, source):
        self._document_id = snapshot.document_id
        self._artifact_version = snapshot.artifact_version
        self._text_version = snapshot.text_version
        self._canonical_sha256 = snapshot.canonical_sha256
        self._page_count = snapshot.page_count
        self._source = source
        self._pages = {}
        self._total_chars = 0

    def get(self, number):
        if type(number) is not int or not 1 <= number <= self._page_count:
            raise MetadataValidationError("evidence_invalid")
        if number in self._pages:
            return self._pages[number]
        if len(self._pages) >= _MAX_PAGES:
            raise MetadataValidationError("canonical_source_invalid")

        failed = False
        try:
            page = self._source(number)
        except MetadataValidationError:
            raise
        except Exception:
            failed = True
        if failed:
            raise MetadataValidationError("canonical_source_invalid")

        if type(page) is not CanonicalPageText:
            raise MetadataValidationError("canonical_source_invalid")
        if not (
            _hex_string(page.document_id, 32)
            and _hex_string(page.canonical_sha256, 64)
            and _hex_string(page.text_sha256, 64)
            and type(page.artifact_version) is int
            and page.artifact_version == 1
            and type(page.text_version) is int
            and page.text_version == 1
            and type(page.page) is int
            and 1 <= page.page <= _MAX_PAGES
            and type(page.text) is str
        ):
            raise MetadataValidationError("canonical_source_invalid")
        size = len(page.text)
        if size > _MAX_PAGE_CHARS or self._total_chars + size > _MAX_TOTAL_CHARS:
            raise MetadataValidationError("canonical_source_invalid")

        invalid_unicode = False
        try:
            encoded = page.text.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            invalid_unicode = True
        if invalid_unicode:
            raise MetadataValidationError("canonical_source_invalid")
        digest = hashlib.sha256(encoded).hexdigest()
        if digest != page.text_sha256:
            raise MetadataValidationError("canonical_source_invalid")
        if (
            page.document_id != self._document_id
            or page.artifact_version != self._artifact_version
            or page.text_version != self._text_version
            or page.canonical_sha256 != self._canonical_sha256
            or page.page != number
        ):
            raise MetadataValidationError("canonical_source_invalid")
        self._total_chars += size
        self._pages[number] = page
        return page


def _verify_record_evidence(cache, segment, record):
    """Check every supplied reference, including those on optional fields."""
    for reference in record.evidence:
        if not segment.page_start <= reference.page <= segment.page_end:
            raise MetadataValidationError("evidence_invalid", record.field_name)
        if record.raw is None:
            raise MetadataValidationError("evidence_invalid", record.field_name)
        if type(record.raw) is list:
            if (
                type(reference.item_index) is not int
                or not 0 <= reference.item_index < len(record.raw)
            ):
                raise MetadataValidationError("evidence_invalid", record.field_name)
        elif reference.item_index is not None:
            raise MetadataValidationError("evidence_invalid", record.field_name)
        page = cache.get(reference.page)
        if (
            reference.document_id != page.document_id
            or reference.artifact_version != page.artifact_version
            or reference.text_version != page.text_version
            or reference.text_sha256 != page.text_sha256
            or not 0 <= reference.start < reference.end <= len(page.text)
            or page.text[reference.start:reference.end] != reference.quote
        ):
            raise MetadataValidationError("evidence_invalid", record.field_name)


def _blank(value):
    return type(value) is str and not value.strip()


def _field_blockers(segment_id, fields, records):
    """Readiness only; callers must first complete strong record validation."""
    by_name = {record.field_name: record for record in records}
    values = {record.field_name: record.normalized for record in records}
    missing = set(missing_required_fields(fields, values))
    result = []
    for field in fields:
        pending = set()
        if field.name in missing:
            pending.add(("required_missing", None))
        record = by_name.get(field.name)
        if record is not None and record.normalized is not None:
            value = record.normalized
            if field.cardinality == "many":
                significant = [index for index, item in enumerate(value) if not _blank(item)]
                empty = not value or len(significant) != len(value)
            else:
                significant = [] if _blank(value) else [None]
                empty = not significant
            if empty and field.name not in missing:
                pending.add(("empty_value", None))
            if significant:
                if record.status != "reviewed":
                    pending.add(("review_required", None))
                if field.critical or field.evidence_required:
                    supported = {reference.item_index for reference in record.evidence}
                    for index in significant:
                        if index not in supported:
                            pending.add(("evidence_missing", index))
        for code, index in sorted(
            pending, key=lambda item: (item[0], -1 if item[1] is None else item[1])
        ):
            result.append(Blocker(code, segment_id, field.name, index))
    return result


def assess_snapshot(snapshot, page_source: PageSource) -> ReadinessResult:
    """Revalidate and assess without granting approval or modifying the input.

    The trusted adapter supplies complete canonical pages from one preparation.
    No pages are requested without evidence. Callback failures abort the operation.
    An approvable result conveys semantic readiness only, including for inputs
    whose stored state is already 'approved'.
    """
    invalid = False
    try:
        validated = MetadataSnapshot.model_validate_json(snapshot_bytes(snapshot), strict=True)
    except Exception:
        invalid = True
    if invalid:
        raise MetadataValidationError("metadata_invalid_input")

    fields_by_hash = {}
    for schema in validated.schemas:
        if schema.schema_sha256 in fields_by_hash:
            continue
        invalid = False
        try:
            fields = [
                FieldSpec.model_validate(value, strict=True)
                for value in schema.schema_document["fields"]
            ]
        except Exception:
            invalid = True
        if invalid:
            raise MetadataValidationError("metadata_invalid_schema")
        fields_by_hash[schema.schema_sha256] = fields

    # Complete semantic validation before invoking the canonical source.
    for segment in validated.segments:
        validate_value_records(fields_by_hash[segment.schema_sha256], segment.fields)

    cache = _PageCache(validated, page_source)
    blockers = []
    for segment in validated.segments:
        for record in segment.fields:
            _verify_record_evidence(cache, segment, record)
        blockers.extend(
            _field_blockers(
                segment.segment_id,
                fields_by_hash[segment.schema_sha256],
                segment.fields,
            )
        )
    return ReadinessResult(approvable=not blockers, blockers=blockers)
