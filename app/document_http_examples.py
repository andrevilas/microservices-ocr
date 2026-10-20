"""Pure synthetic documentation fixtures for future document HTTP contracts.

These examples imply no actual human review, persisted approval, real PDF/A
artifact, authorization, or storage operation. Projection bounds concern DTOs,
not a real MCP/SDK response envelope. Importing this module builds no fixtures.
"""

import hashlib
import json
from datetime import datetime, timezone

from .document_canonical_models import (
    CanonicalManifest, CanonicalPageChunk, CanonicalPageText,
)
from .document_content_models import (
    MetadataEvidencePage, MetadataSchemaChunk, MetadataStringChunk, MetadataValuePage,
)
from .document_models import FieldSpec, compose_schema
from .document_transport_models import (
    MetadataFieldPage, MetadataRevisionPage, MetadataRevisionSummary, MetadataView,
)
from .metadata_approval import ApprovalInput
from .metadata_approval_contract import ApproveMetadataReceipt
from .metadata_models import (
    ApprovalRecord, EvidenceRef, FieldValueRecord, ManualFieldInput, MetadataSnapshot,
    ProvenanceRecord, SaveMetadataInput, SaveMetadataReceipt, SchemaSnapshot,
    SegmentInput, SegmentRecord, snapshot_bytes,
)
from .services.document_content_projection import (
    evidence_page, schema_chunk, string_chunk, value_page,
)
from .services.document_projection import field_page, metadata_view


_RESPONSE_MODELS = (
    SaveMetadataReceipt, ApproveMetadataReceipt, MetadataView, MetadataRevisionPage,
    MetadataFieldPage, MetadataValuePage, MetadataEvidencePage, MetadataStringChunk,
    MetadataSchemaChunk, CanonicalManifest, CanonicalPageChunk, CanonicalPageText,
    MetadataSnapshot,
)
_REQUEST_MODELS = (SaveMetadataInput, ApprovalInput)
_UNSUPPORTED = "Unsupported example model."


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _fixtures():
    # Fixed, distinct identities and time; never production artifact identities.
    job, revision, approved_revision, context, type_id, segment = (
        digit * 32 for digit in "123456"
    )
    now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
    owner = "user:1"
    text = "Edital sintético e\u0301 🚀"
    text_bytes = text.encode("utf-8")
    text_hash = _sha(text_bytes)
    input_hash = _sha(b"synthetic-input-pdf-label-only")
    output_hash = _sha(b"synthetic-output-pdf-label-only")
    normalization = "identity-utf8-v1"
    offset_unit = "unicode_code_points"
    record = dict(
        schema_version="ocr-canonical-page/1", normalization=normalization,
        offset_unit=offset_unit, job_id=job, document_id=job,
        artifact_version=1, text_version=1, page=1, source="ocr", text=text,
        char_start=0, char_end=len(text), text_sha256=text_hash,
        text_utf8_bytes=len(text_bytes), input_pdf_sha256=input_hash,
        output_pdf_sha256=output_hash,
    )
    jsonl = _canonical(record) + b"\n"
    canonical_hash = _sha(jsonl)
    composed = compose_schema([], [FieldSpec(
        name="titulo", label="T\u00edtulo", type="string", required=True,
        description="Título sintético para documentação.",
    )])
    provenance = ProvenanceRecord(
        actor_subject=owner, auth_source="local_jwt", recorded_at=now,
    )
    evidence = EvidenceRef(
        document_id=job, page=1, text_sha256=text_hash,
        start=0, end=len(text), quote=text,
    )
    field = FieldValueRecord(
        field_name="titulo", raw=text, normalized=text, origin="manual",
        status="needs_review", evidence=[evidence], provenance=provenance,
    )
    draft = MetadataSnapshot(
        revision_id=revision, parent_revision_id=None, job_id=job, document_id=job,
        canonical_sha256=canonical_hash, input_pdf_sha256=input_hash,
        output_pdf_sha256=output_hash, page_count=1, created_at=now,
        author=provenance, state="draft",
        schemas=[SchemaSnapshot.model_validate({
            "schema_sha256": composed.sha256,
            "schema": json.loads(composed.canonical),
        }, strict=True)],
        segments=[SegmentRecord(
            segment_id=segment, page_start=1, page_end=1, context_id=context,
            type_id=type_id, type_version=1, schema_sha256=composed.sha256,
            fields=[field],
        )],
    )
    draft_bytes = snapshot_bytes(draft)
    draft_hash = _sha(draft_bytes)
    save = SaveMetadataInput(
        expected_head=None, expected_sha256=None,
        segments=[SegmentInput(
            client_key="main", page_start=1, page_end=1,
            type_id=type_id, type_version=1,
            fields=[ManualFieldInput(
                field_name="titulo", raw=text, evidence=[evidence],
            )],
        )],
    )
    approve = ApprovalInput.model_validate(dict(
        revision_id=revision, expected_head=revision, expected_sha256=draft_hash,
        reviewed_fields=[dict(segment_id=segment, field_name="titulo")],
    ), strict=True)

    # Construct a synthetic child directly, without invoking approval policy.
    child_data = draft.model_dump(mode="python", by_alias=True)
    child_data.update(
        revision_id=approved_revision, parent_revision_id=revision, state="approved",
        approval=ApprovalRecord(
            actor_subject=owner, auth_source="local_jwt", approved_at=now,
            reviewed_revision_id=revision, reviewed_sha256=draft_hash,
        ),
    )
    child_data["segments"][0]["fields"][0]["status"] = "reviewed"
    child = MetadataSnapshot.model_validate(child_data, strict=True)
    responses = {
        MetadataSnapshot: draft,
        SaveMetadataReceipt: SaveMetadataReceipt(
            revision_id=revision, sha256=draft_hash, segment_ids={"main": segment},
        ),
        ApproveMetadataReceipt: ApproveMetadataReceipt(
            operation="approve-v1", state="approved", revision_id=approved_revision,
            sha256=_sha(snapshot_bytes(child)), reviewed_revision_id=revision,
            reviewed_sha256=draft_hash,
        ),
        MetadataView: metadata_view(owner, draft, draft_hash),
        MetadataRevisionPage: MetadataRevisionPage(
            job_id=job, revisions=[MetadataRevisionSummary(
                revision_id=revision, parent_revision_id=None, sha256=draft_hash,
                size_bytes=len(draft_bytes), state="draft", created_at=now,
            )], next_cursor=None,
        ),
        MetadataFieldPage: field_page(owner, draft, draft_hash, segment),
        MetadataValuePage: value_page(owner, draft, draft_hash, segment, "titulo"),
        MetadataEvidencePage: evidence_page(owner, draft, draft_hash, segment, "titulo"),
        MetadataStringChunk: string_chunk(
            owner, draft, draft_hash, segment, "titulo", part="raw",
            item_index=None, offset=0, max_chars=1000,
        ),
        MetadataSchemaChunk: schema_chunk(
            owner, draft, draft_hash, composed.sha256, offset=0, max_chars=1000,
        ),
        CanonicalManifest: CanonicalManifest(
            schema_version="ocr-canonical-pages/1", normalization=normalization,
            offset_unit=offset_unit, job_id=job, document_id=job,
            artifact_version=1, text_version=1, page_count=1,
            total_characters=len(text), size_bytes=len(jsonl), sha256=canonical_hash,
            input_pdf_sha256=input_hash, output_pdf_sha256=output_hash,
            parent_job_id=None,
        ),
        CanonicalPageText: CanonicalPageText(
            document_id=job, artifact_version=1, text_version=1,
            canonical_sha256=canonical_hash, page=1, text_sha256=text_hash, text=text,
        ),
        CanonicalPageChunk: CanonicalPageChunk(
            job_id=job, document_id=job, artifact_version=1, text_version=1,
            page=1, page_count=1, text=text, offset=0, end_offset=len(text),
            page_character_count=len(text), text_sha256=text_hash,
            canonical_sha256=canonical_hash, normalization=normalization,
            offset_unit=offset_unit, truncated=False, next_page=None, next_offset=None,
        ),
    }
    return responses, {SaveMetadataInput: save, ApprovalInput: approve}, jsonl.decode("utf-8")


def response_example(model) -> dict:
    """Return a fresh JSON-compatible response example for an exact model class."""
    if not any(model is supported for supported in _RESPONSE_MODELS):
        raise ValueError(_UNSUPPORTED)
    responses, _, _ = _fixtures()
    return responses[model].model_dump(mode="json", by_alias=True)


def request_example(model) -> dict:
    """Return a fresh JSON-compatible manual-save or approval request example."""
    if not any(model is supported for supported in _REQUEST_MODELS):
        raise ValueError(_UNSUPPORTED)
    _, requests, _ = _fixtures()
    return requests[model].model_dump(mode="json", by_alias=True)


def canonical_jsonl_example() -> str:
    """Return the exact single-page synthetic download, including its final LF."""
    return _fixtures()[2]


def metadata_json_example() -> dict:
    """Return the synthetic draft whose canonical hash is in the save receipt."""
    return response_example(MetadataSnapshot)
