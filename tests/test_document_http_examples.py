"""Contract and coherence checks for pure synthetic HTTP documentation fixtures."""

import hashlib
import importlib
import json
import unicodedata

import pytest

from app import document_http_examples as examples
from app.document_models import FieldSpec, compose_schema
from app.document_transport_models import MAX_PROJECTION_BYTES
from app.metadata_models import snapshot_bytes
from app.services.document_content_projection import schema_chunk


RESPONSE_MODELS = (
    examples.SaveMetadataReceipt, examples.ApproveMetadataReceipt,
    examples.MetadataView, examples.MetadataRevisionPage, examples.MetadataFieldPage,
    examples.MetadataValuePage, examples.MetadataEvidencePage,
    examples.MetadataStringChunk, examples.MetadataSchemaChunk,
    examples.CanonicalManifest, examples.CanonicalPageChunk, examples.CanonicalPageText,
    examples.MetadataSnapshot,
)
REQUEST_MODELS = (examples.SaveMetadataInput, examples.ApprovalInput)
PROJECTION_MODELS = (
    examples.MetadataView, examples.MetadataRevisionPage, examples.MetadataFieldPage,
    examples.MetadataValuePage, examples.MetadataEvidencePage,
    examples.MetadataStringChunk, examples.MetadataSchemaChunk,
)


def encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def sha(value):
    return hashlib.sha256(value).hexdigest()


def draft():
    return examples.MetadataSnapshot.model_validate_json(
        encoded(examples.metadata_json_example()), strict=True,
    )


@pytest.mark.parametrize("model", RESPONSE_MODELS)
def test_response_strict_json_roundtrip(model):
    data = examples.response_example(model)
    checked = model.model_validate_json(encoded(data), strict=True)
    assert checked.model_dump(mode="json", by_alias=True) == data


@pytest.mark.parametrize("model", REQUEST_MODELS)
def test_request_strict_json_roundtrip_and_repeatability(model):
    first = examples.request_example(model)
    checked = model.model_validate_json(encoded(first), strict=True)
    assert checked.model_dump(mode="json", by_alias=True) == first
    assert examples.request_example(model) == first
    if model is examples.ApprovalInput:
        pairs = [(item.segment_id, item.field_name) for item in checked.reviewed_fields]
        assert len(pairs) == len(set(pairs)) == 1


def test_save_snapshot_and_projection_coherence():
    source = draft()
    receipt = examples.response_example(examples.SaveMetadataReceipt)
    save = examples.request_example(examples.SaveMetadataInput)
    segment = source.segments[0]
    field = segment.fields[0]
    assert source.state == "draft" and source.approval is None
    assert source.author.actor_subject == "user:1"
    assert source.author.auth_source == "local_jwt"
    assert source.created_at.isoformat() == "2026-10-19T12:00:00+00:00"
    assert source.author.recorded_at == source.created_at
    assert receipt["revision_id"] == source.revision_id
    assert receipt["sha256"] == sha(snapshot_bytes(source))
    assert receipt["segment_ids"] == {"main": segment.segment_id}
    assert save["expected_head"] is None and save["expected_sha256"] is None
    request_segment = save["segments"][0]
    assert request_segment["client_key"] == "main"
    assert request_segment["segment_id"] is None
    assert request_segment["type_id"] == segment.type_id
    assert request_segment["type_version"] == segment.type_version
    assert (request_segment["page_start"], request_segment["page_end"]) == (1, 1)
    assert request_segment["fields"][0]["raw"] == field.raw
    assert request_segment["fields"][0]["evidence"] == [
        item.model_dump(mode="json", by_alias=True) for item in field.evidence
    ]
    view = examples.response_example(examples.MetadataView)
    assert view["sha256"] == receipt["sha256"]
    assert view["revision_id"] == source.revision_id
    assert view["canonical_sha256"] == source.canonical_sha256
    assert view["segments"][0]["segment_id"] == segment.segment_id
    assert view["schemas"][0]["schema_sha256"] == segment.schema_sha256
    assert receipt["sha256"] in view["content_path"]
    revisions = examples.response_example(examples.MetadataRevisionPage)
    assert revisions["job_id"] == source.job_id
    assert revisions["next_cursor"] is None
    revision = revisions["revisions"][0]
    assert revision["revision_id"] == source.revision_id
    assert revision["sha256"] == receipt["sha256"]
    assert revision["size_bytes"] == len(snapshot_bytes(source))
    descriptors = examples.response_example(examples.MetadataFieldPage)
    assert descriptors["fields"][0]["field_name"] == field.field_name
    assert descriptors["fields"][0]["status"] == "needs_review"
    assert descriptors["fields"][0]["value_count"] == 1
    assert descriptors["fields"][0]["evidence_count"] == 1
    values = examples.response_example(examples.MetadataValuePage)
    assert values["values"][0]["item_index"] is None
    assert values["values"][0]["raw"] == {"kind": "string", "value": field.raw}
    assert values["values"][0]["normalized"] == {
        "kind": "string", "value": field.normalized,
    }
    evidence = examples.response_example(examples.MetadataEvidencePage)
    assert evidence["evidence"] == [field.evidence[0].model_dump(mode="json", by_alias=True)]
    chunk = examples.response_example(examples.MetadataStringChunk)
    assert chunk["part"] == "raw" and chunk["item_index"] is None
    assert chunk["text"] == field.raw == field.normalized
    assert chunk["content_sha256"] == sha(field.raw.encode("utf-8"))


def test_exact_jsonl_hashes_unicode_offsets_and_canonical_models():
    source = draft()
    jsonl = examples.canonical_jsonl_example().encode("utf-8")
    assert jsonl.endswith(b"\n") and jsonl.count(b"\n") == 1
    record = json.loads(jsonl)
    assert jsonl == encoded(record) + b"\n"
    text = record["text"]
    assert "e\u0301" in text and any(ord(char) > 0xFFFF for char in text)
    assert unicodedata.normalize("NFC", text) != text
    assert len(text.encode("utf-8")) > len(text)
    assert record == {
        "schema_version": "ocr-canonical-page/1",
        "normalization": "identity-utf8-v1", "offset_unit": "unicode_code_points",
        "job_id": source.job_id, "document_id": source.document_id,
        "artifact_version": 1, "text_version": 1, "page": 1, "source": "ocr",
        "text": text, "char_start": 0, "char_end": len(text),
        "text_sha256": sha(text.encode("utf-8")),
        "text_utf8_bytes": len(text.encode("utf-8")),
        "input_pdf_sha256": source.input_pdf_sha256,
        "output_pdf_sha256": source.output_pdf_sha256,
    }
    manifest = examples.response_example(examples.CanonicalManifest)
    page = examples.response_example(examples.CanonicalPageText)
    chunk = examples.response_example(examples.CanonicalPageChunk)
    assert manifest["sha256"] == source.canonical_sha256 == sha(jsonl)
    assert manifest["size_bytes"] == len(jsonl)
    assert manifest["total_characters"] == len(text)
    assert manifest["page_count"] == source.page_count == 1
    assert manifest["input_pdf_sha256"] == source.input_pdf_sha256
    assert manifest["output_pdf_sha256"] == source.output_pdf_sha256
    for data in (manifest, page, chunk):
        assert data["document_id"] == source.document_id == source.job_id
        assert data["artifact_version"] == data["text_version"] == 1
    for data in (page, chunk):
        assert data["canonical_sha256"] == manifest["sha256"]
        assert data["text_sha256"] == record["text_sha256"]
        assert data["text"] == text and data["page"] == 1
    assert "job_id" not in page and "schema_version" not in page
    assert chunk["offset"] == 0
    assert chunk["end_offset"] == chunk["page_character_count"] == len(text)
    assert chunk["truncated"] is False
    assert chunk["next_page"] is None and chunk["next_offset"] is None
    ref = source.segments[0].fields[0].evidence[0]
    assert ref.start == 0 and ref.end == len(text)
    assert text[ref.start:ref.end] == ref.quote == text
    assert ref.text_sha256 == record["text_sha256"]
    assert ref.document_id == source.document_id and ref.page == 1


def test_schema_recomposition_and_lossless_chunk_reconstruction():
    source = draft()
    schema = source.schemas[0]
    document = schema.schema_document
    composed = compose_schema(
        [FieldSpec.model_validate(item, strict=True) for item in document["common_fields"]],
        [FieldSpec.model_validate(item, strict=True) for item in document["specific_fields"]],
    )
    assert composed.canonical == encoded(document)
    assert composed.sha256 == schema.schema_sha256 == sha(composed.canonical)
    digest = sha(snapshot_bytes(source))
    chunk = examples.response_example(examples.MetadataSchemaChunk)
    pieces = []
    while True:
        pieces.append(chunk["text"])
        offset = chunk["next_offset"]
        if offset is None:
            break
        assert offset > chunk["offset"]
        chunk = schema_chunk(
            "user:1", source, digest, schema.schema_sha256,
            offset=offset, max_chars=1000,
        ).model_dump(mode="json", by_alias=True)
    assert "".join(pieces).encode("utf-8") == composed.canonical


def test_approval_request_and_receipt_describe_consistent_synthetic_child():
    source = draft()
    digest = sha(snapshot_bytes(source))
    request = examples.request_example(examples.ApprovalInput)
    receipt = examples.response_example(examples.ApproveMetadataReceipt)
    assert request["revision_id"] == request["expected_head"] == source.revision_id
    assert request["expected_sha256"] == receipt["reviewed_sha256"] == digest
    assert request["reviewed_fields"] == [{
        "segment_id": source.segments[0].segment_id, "field_name": "titulo",
    }]
    assert receipt["reviewed_revision_id"] == source.revision_id
    assert receipt["revision_id"] != source.revision_id
    data = source.model_dump(mode="python", by_alias=True)
    data.update(
        revision_id=receipt["revision_id"], parent_revision_id=source.revision_id,
        state="approved", approval=examples.ApprovalRecord(
            actor_subject=source.author.actor_subject, auth_source=source.author.auth_source,
            approved_at=source.created_at, reviewed_revision_id=source.revision_id,
            reviewed_sha256=digest,
        ),
    )
    data["segments"][0]["fields"][0]["status"] = "reviewed"
    child = examples.MetadataSnapshot.model_validate(data, strict=True)
    assert receipt["sha256"] == sha(snapshot_bytes(child))
    assert child.author.recorded_at == child.approval.approved_at == child.created_at
    assert child.author.execution_id is None


@pytest.mark.parametrize("model", PROJECTION_MODELS)
def test_projection_canonical_byte_budget(model):
    assert len(encoded(examples.response_example(model))) <= MAX_PROJECTION_BYTES


def test_nested_mutation_does_not_escape_any_helper():
    response = examples.response_example(examples.MetadataEvidencePage)
    expected_response = encoded(response)
    response["evidence"][0]["quote"] = "changed"
    response["evidence"].clear()
    assert encoded(examples.response_example(examples.MetadataEvidencePage)) == expected_response
    save = examples.request_example(examples.SaveMetadataInput)
    expected_save = encoded(save)
    save["segments"][0]["fields"][0]["evidence"][0]["quote"] = "changed"
    save["segments"].append(save["segments"][0])
    assert encoded(examples.request_example(examples.SaveMetadataInput)) == expected_save
    approval = examples.request_example(examples.ApprovalInput)
    expected_approval = encoded(approval)
    approval["reviewed_fields"].append(approval["reviewed_fields"][0])
    assert encoded(examples.request_example(examples.ApprovalInput)) == expected_approval
    download = examples.metadata_json_example()
    expected_download = encoded(download)
    download["schemas"][0]["schema"]["fields"][0]["description"] = "changed"
    download["segments"][0]["fields"][0]["evidence"].clear()
    assert encoded(examples.metadata_json_example()) == expected_download
    assert encoded(examples.response_example(examples.MetadataSnapshot)) == expected_download


def test_reloads_are_deterministic():
    def capture():
        return (
            [encoded(examples.response_example(model)) for model in RESPONSE_MODELS],
            [encoded(examples.request_example(model)) for model in REQUEST_MODELS],
            encoded(examples.metadata_json_example()), examples.canonical_jsonl_example(),
        )

    expected = capture()
    for _ in range(2):
        importlib.reload(examples)
        assert capture() == expected


@pytest.mark.parametrize("unsupported", [None, object, {}, [], str])
@pytest.mark.parametrize("helper", [examples.response_example, examples.request_example])
def test_unsupported_models_have_fixed_error(helper, unsupported):
    with pytest.raises(ValueError) as error:
        helper(unsupported)
    assert str(error.value) == "Unsupported example model."


def test_request_and_response_registries_are_distinct():
    for helper, unsupported in (
        (examples.request_example, examples.SaveMetadataReceipt),
        (examples.response_example, examples.SaveMetadataInput),
    ):
        with pytest.raises(ValueError, match="^Unsupported example model\\.$"):
            helper(unsupported)
