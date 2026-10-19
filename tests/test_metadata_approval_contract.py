"""C5 candidate protocol tests; no persistence or committed approval claims."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.application import ApplicationError, Principal
from app.document_models import FieldSpec, compose_schema
from app.metadata_approval import ApprovalInput, ReviewedField, prepare_approval
from app.metadata_approval_contract import (
    ApproveMetadataReceipt,
    approval_receipt_for,
    approval_request_identity,
    validate_approval_receipt,
)
from app.metadata_models import (
    ManualFieldInput,
    MetadataSnapshot,
    ProvenanceRecord,
    SaveMetadataReceipt,
    SchemaSnapshot,
    SegmentRecord,
    snapshot_bytes,
)
from app.metadata_validation import normalize_manual_values


KEY = "approval:request-1"
SEGMENT = "b" * 32
SCOPES = frozenset({
    "metadata:approve", "metadata:read", "jobs:read", "artifacts:read",
})


@pytest.fixture
def draft():
    author = ProvenanceRecord(
        actor_subject="user:1", auth_source="local_jwt",
        recorded_at=datetime(2026, 10, 16, tzinfo=timezone.utc),
    )
    fields = [FieldSpec(name=name, type="string", description="Synthetic field")
              for name in ("title", "other")]
    composed = compose_schema(fields, [])
    schema = SchemaSnapshot.model_validate({
        "schema_sha256": composed.sha256,
        "schema": json.loads(composed.canonical),
    }, strict=True)
    records = normalize_manual_values(fields, [
        ManualFieldInput(field_name="title", raw="one"),
        ManualFieldInput(field_name="other", raw="two"),
    ], author)
    return MetadataSnapshot(
        revision_id="c" * 32, parent_revision_id=None,
        job_id="a" * 32, document_id="a" * 32,
        canonical_sha256="d" * 64,
        input_pdf_sha256="1" * 64, output_pdf_sha256="2" * 64,
        page_count=1, created_at=author.recorded_at, author=author,
        schemas=[schema], segments=[SegmentRecord(
            segment_id=SEGMENT, page_start=1, page_end=1,
            context_id="e" * 32, type_id="f" * 32, type_version=1,
            schema_sha256=composed.sha256, fields=records,
        )], state="draft",
    )


@pytest.fixture
def request(draft):
    return ApprovalInput(
        revision_id=draft.revision_id, expected_head=draft.revision_id,
        expected_sha256=hashlib.sha256(snapshot_bytes(draft)).hexdigest(),
        reviewed_fields=[ReviewedField(segment_id=SEGMENT, field_name=name)
                         for name in ("title", "other")],
    )


@pytest.fixture
def candidate(draft, request):
    source = Mock(side_effect=AssertionError("No evidence pages are needed."))
    principal = Principal("user:1", 1, SCOPES, auth_source="local_jwt")
    result = prepare_approval(principal, 1, draft, request, source)
    source.assert_not_called()
    return result


def assert_error(status, code, operation):
    with pytest.raises(ApplicationError) as caught:
        operation()
    error = caught.value
    assert (error.status, error.code) == (status, code)
    assert error.__context__ is None
    assert error.__cause__ is None
    assert "SENSITIVE_MARKER" not in str(error)
    assert "SENSITIVE_MARKER" not in repr(error)
    return error


@pytest.fixture
def receipt_data(candidate):
    return approval_receipt_for(candidate).model_dump(mode="python")


def test_identity_matches_precise_versioned_canonical_envelope(request):
    data = request.model_dump(mode="python")
    before = request.model_dump(mode="python")
    # Construct the independent expected envelope explicitly, including order.
    expected = {
        "operation": "approve-v1",
        "body": {
            "revision_id": request.revision_id,
            "expected_head": request.expected_head,
            "expected_sha256": request.expected_sha256,
            "reviewed_fields": [
                {"segment_id": SEGMENT, "field_name": "title"},
                {"segment_id": SEGMENT, "field_name": "other"},
            ],
        },
    }
    canonical = json.dumps(
        expected, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    identity = approval_request_identity(KEY, request)
    assert identity == (
        hashlib.sha256(KEY.encode("ascii")).hexdigest(),
        hashlib.sha256(canonical).hexdigest(),
    )
    reordered = dict(reversed(list(data.items())))
    reordered["reviewed_fields"] = [dict(reversed(list(item.items())))
                                    for item in data["reviewed_fields"]]
    assert approval_request_identity(KEY, reordered) == identity
    assert approval_request_identity(KEY, request) == identity
    assert request.model_dump(mode="python") == before
    assert data == before


@pytest.mark.parametrize("change", ["selection", "order", "hash", "revision"])
def test_body_and_selection_order_are_identity_significant(request, change):
    data = request.model_dump(mode="python")
    if change == "selection":
        data["reviewed_fields"].pop()
    elif change == "order":
        data["reviewed_fields"].reverse()
    elif change == "hash":
        data["expected_sha256"] = "9" * 64
    else:
        data["revision_id"] = data["expected_head"] = "9" * 32
    original = approval_request_identity(KEY, request)
    changed = approval_request_identity(KEY, data)
    assert changed[0] == original[0]
    assert changed[1] != original[1]
    other_key = approval_request_identity("another-key", request)
    assert other_key[0] != original[0]
    assert other_key[1] == original[1]


@pytest.mark.parametrize("key", [
    "", "a" * 7, "a" * 129, "abcdefgh\n", "abc defg", "abc/defg",
    "abcédefg", "abc😀defg", "abc\x00defg", None, True, False, 12345678,
    b"abcdefgh", [], {},
])
def test_invalid_keys_are_strict_and_sanitized(key, request):
    assert_error(422, "invalid_idempotency_key",
                 lambda: approval_request_identity(key, request))


def test_string_subclass_is_not_an_exact_key(request):
    class Key(str):
        pass
    assert_error(422, "invalid_idempotency_key",
                 lambda: approval_request_identity(Key(KEY), request))


@pytest.mark.parametrize("key", ["a" * 8, "a" * 128, "aA09._:-"])
def test_key_boundaries_and_allowed_characters(key, request):
    assert approval_request_identity(key, request)[0] == hashlib.sha256(key.encode()).hexdigest()


def test_request_revision_and_head_must_identify_same_draft(request):
    data = request.model_dump(mode="python")
    data["expected_head"] = "9" * 32
    assert_error(422, "invalid_metadata_input",
                 lambda: approval_request_identity(KEY, data))


@pytest.mark.parametrize("tamper", ["nested", "construct", "tuple", "extra", "duplicate"])
def test_request_original_data_cannot_be_laundered(request, tamper):
    if tamper == "nested":
        request.reviewed_fields[0] = ReviewedField.model_construct(
            segment_id=True, field_name="title",
        )
    elif tamper == "construct":
        request = ApprovalInput.model_construct(**{
            **request.__dict__, "expected_sha256": "SENSITIVE_MARKER",
        })
    elif tamper == "tuple":
        request = request.model_copy(update={"reviewed_fields": tuple(request.reviewed_fields)})
    elif tamper == "extra":
        request = request.model_copy(update={"actor": "SENSITIVE_MARKER"})
    else:
        request.reviewed_fields.append(request.reviewed_fields[0])
    assert_error(422, "invalid_metadata_input",
                 lambda: approval_request_identity(KEY, request))


def test_receipt_schema_requires_explicit_closed_discriminators(receipt_data):
    schema = ApproveMetadataReceipt.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(receipt_data)
    assert schema["properties"]["operation"]["const"] == "approve-v1"
    assert schema["properties"]["state"]["const"] == "approved"
    assert "default" not in schema["properties"]["operation"]
    assert "default" not in schema["properties"]["state"]
    for name in ("operation", "state"):
        missing = dict(receipt_data)
        missing.pop(name)
        assert_error(422, "invalid_metadata_input", lambda: validate_approval_receipt(missing))


def test_receipt_roundtrip_detaches_without_mutation(receipt_data):
    before = dict(receipt_data)
    receipt = validate_approval_receipt(receipt_data)
    detached = validate_approval_receipt(receipt)
    assert detached == receipt
    assert detached is not receipt
    assert receipt_data == before
    restored = ApproveMetadataReceipt.model_validate_json(receipt.model_dump_json(), strict=True)
    assert restored == receipt
    assert validate_approval_receipt(restored) == receipt


@pytest.mark.parametrize("changes", [
    {"operation": "save-v1"}, {"state": "draft"}, {"sha256": True},
    {"revision_id": "SENSITIVE_MARKER"}, {"extra": "SENSITIVE_MARKER"},
    {"reviewed_revision_id": None},
])
def test_receipt_rejects_wrong_discriminators_types_and_unknown_fields(receipt_data, changes):
    receipt_data.update(changes)
    assert_error(422, "invalid_metadata_input",
                 lambda: validate_approval_receipt(receipt_data))


def test_receipt_requires_distinct_child_and_rejects_save_receipts(receipt_data):
    receipt_data["revision_id"] = receipt_data["reviewed_revision_id"]
    assert_error(422, "invalid_metadata_input", lambda: validate_approval_receipt(receipt_data))
    save = SaveMetadataReceipt(revision_id="1" * 32, sha256="2" * 64, segment_ids={})
    for value in (save, save.model_dump(mode="python"), None, "SENSITIVE_MARKER"):
        assert_error(422, "invalid_metadata_input", lambda: validate_approval_receipt(value))


@pytest.mark.parametrize("tamper", ["construct", "copy", "extra"])
def test_mutated_receipt_models_are_revalidated(receipt_data, tamper):
    if tamper == "construct":
        value = ApproveMetadataReceipt.model_construct(**{
            **receipt_data, "sha256": "SENSITIVE_MARKER",
        })
    else:
        value = ApproveMetadataReceipt(**receipt_data).model_copy(update=(
            {"state": "draft"} if tamper == "copy" else {"extra": "SENSITIVE_MARKER"}
        ))
    assert_error(422, "invalid_metadata_input", lambda: validate_approval_receipt(value))


def test_real_policy_candidate_receipt_hash_and_unpersisted_contract(draft, request, candidate):
    before = snapshot_bytes(candidate)
    original_draft = snapshot_bytes(draft)
    original_request = request.model_dump(mode="python")
    receipt = approval_receipt_for(candidate)
    assert receipt.operation == "approve-v1"
    assert receipt.state == "approved"
    assert receipt.revision_id == candidate.revision_id
    assert receipt.sha256 == hashlib.sha256(before).hexdigest()
    assert receipt.reviewed_revision_id == draft.revision_id
    assert receipt.reviewed_sha256 == hashlib.sha256(original_draft).hexdigest()
    assert snapshot_bytes(candidate) == before
    assert snapshot_bytes(draft) == original_draft
    assert request.model_dump(mode="python") == original_request
    # Describing a policy candidate has not approved the source draft. The
    # receipt is neither an approval request nor a trusted snapshot or commit.
    assert draft.state == "draft" and draft.approval is None
    assert_error(503, "metadata_unavailable", lambda: approval_receipt_for(draft))
    assert_error(503, "metadata_unavailable", lambda: approval_receipt_for(receipt))
    assert_error(422, "invalid_metadata_input", lambda: approval_request_identity(KEY, receipt))
    with pytest.raises(ValidationError):
        MetadataSnapshot.model_validate(receipt.model_dump(mode="python"), strict=True)


@pytest.mark.parametrize("tamper", [
    "parent", "reviewed", "actor", "auth", "recorded", "created", "execution",
    "nested", "construct", "dict", "subclass",
])
def test_corrupt_or_inconsistent_candidate_is_unavailable(candidate, tamper):
    if tamper == "parent":
        candidate = candidate.model_copy(update={"parent_revision_id": "9" * 32})
    elif tamper == "reviewed":
        candidate = candidate.model_copy(update={"approval": candidate.approval.model_copy(
            update={"reviewed_revision_id": "9" * 32},
        )})
    elif tamper in ("actor", "auth", "recorded", "execution"):
        updates = {
            "actor": {"actor_subject": "user:2"},
            "auth": {"auth_source": "oauth"},
            "recorded": {"recorded_at": candidate.created_at + timedelta(seconds=1)},
            "execution": {"execution_id": "9" * 32},
        }
        candidate = candidate.model_copy(update={"author": candidate.author.model_copy(
            update=updates[tamper],
        )})
    elif tamper == "created":
        candidate = candidate.model_copy(update={
            "created_at": candidate.created_at + timedelta(seconds=1),
        })
    elif tamper == "nested":
        candidate.segments[0].fields[0] = candidate.segments[0].fields[0].model_copy(
            update={"field_name": "SENSITIVE_MARKER"},
        )
    elif tamper == "construct":
        candidate = MetadataSnapshot.model_construct(**{
            **candidate.__dict__, "page_count": True,
        })
    elif tamper == "dict":
        candidate = candidate.model_dump(mode="python", by_alias=True)
    else:
        class SnapshotSubclass(MetadataSnapshot):
            pass
        candidate = SnapshotSubclass.model_validate(
            candidate.model_dump(mode="python", by_alias=True), strict=True,
        )
    assert_error(503, "metadata_unavailable", lambda: approval_receipt_for(candidate))


def test_snapshot_dependency_failure_is_sanitized(monkeypatch, candidate):
    def fail(value):
        raise RuntimeError("SENSITIVE_MARKER")
    monkeypatch.setattr("app.metadata_approval_contract.snapshot_bytes", fail)
    error = assert_error(503, "metadata_unavailable", lambda: approval_receipt_for(candidate))
    assert error.message == "Metadata is unavailable."


@pytest.mark.parametrize("control", [KeyboardInterrupt(), SystemExit(), GeneratorExit()])
def test_snapshot_control_exception_identity_propagates(monkeypatch, candidate, control):
    before = snapshot_bytes(candidate)
    def interrupt(value):
        raise control
    monkeypatch.setattr("app.metadata_approval_contract.snapshot_bytes", interrupt)
    with pytest.raises(type(control)) as caught:
        approval_receipt_for(candidate)
    assert caught.value is control
    assert snapshot_bytes(candidate) == before
