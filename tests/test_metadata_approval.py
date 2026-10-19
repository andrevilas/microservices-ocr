"""Pure C5 preparation tests using real schemas, normalization and evidence.

No storage, routes, providers or durable approval claims are involved.
"""

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.application import ApplicationError, Principal
from app.document_models import FieldSpec, compose_schema
from app.metadata_approval import ApprovalInput, ReviewedField, prepare_approval
from app.metadata_evidence import CanonicalPageText
from app.metadata_models import (
    ApprovalRecord,
    EvidenceRef,
    ManualFieldInput,
    MetadataSnapshot,
    ProvenanceRecord,
    SchemaSnapshot,
    SegmentRecord,
    snapshot_bytes,
)
from app.metadata_validation import normalize_manual_values


OWNER = 1
DOCUMENT = "a" * 32
SEGMENT = "b" * 32
REVISION = "c" * 32
CANONICAL = "d" * 64
OLD_TIME = datetime(2026, 10, 16, tzinfo=timezone.utc)
NEW_TIME = datetime(2026, 10, 19, 12, 30, tzinfo=timezone.utc)
NEW_ID = "6" * 32
TEXT = "A\r\n\x00😀e\u0301 literal"
SCOPES = frozenset({
    "metadata:approve", "metadata:read", "jobs:read", "artifacts:read",
})


def actor(**updates):
    return replace(
        Principal("user:1", 1, SCOPES, auth_source="local_jwt"), **updates
    )


def spec(name="title", **options):
    return FieldSpec.model_validate({
        "name": name, "type": "string", "description": "Synthetic field",
        **options,
    }, strict=True)


def page(text=TEXT):
    return CanonicalPageText(
        document_id=DOCUMENT, artifact_version=1, text_version=1,
        canonical_sha256=CANONICAL, page=1,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), text=text,
    )


def proof(index=None):
    source = page()
    return EvidenceRef(
        document_id=DOCUMENT, page=1, text_sha256=source.text_sha256,
        start=0, end=len(TEXT), quote=TEXT, item_index=index,
    )


def make_head(fields=None, entries=None, reviewed=()):
    if fields is None:
        fields = [spec(evidence_required=True)]
    if entries is None:
        entries = [ManualFieldInput(
            field_name="title", raw="interpretation", evidence=[proof()],
        )]
    author = ProvenanceRecord(
        actor_subject="user:1", auth_source="local_jwt", recorded_at=OLD_TIME,
    )
    composed = compose_schema(fields, [])
    schema = SchemaSnapshot.model_validate({
        "schema_sha256": composed.sha256,
        "schema": json.loads(composed.canonical),
    }, strict=True)
    records = normalize_manual_values(fields, entries, author)
    records = [record.model_copy(update={"status": "reviewed"})
               if record.field_name in reviewed else record for record in records]
    segment = SegmentRecord(
        segment_id=SEGMENT, page_start=1, page_end=1,
        context_id="e" * 32, type_id="f" * 32, type_version=1,
        schema_sha256=composed.sha256, fields=records,
    )
    return MetadataSnapshot(
        revision_id=REVISION, parent_revision_id=None,
        job_id=DOCUMENT, document_id=DOCUMENT, canonical_sha256=CANONICAL,
        input_pdf_sha256="1" * 64, output_pdf_sha256="2" * 64,
        page_count=1, created_at=OLD_TIME, author=author,
        schemas=[schema], segments=[segment], state="draft", approval=None,
    )


def body(head, selected=("title",)):
    return ApprovalInput(
        revision_id=head.revision_id,
        expected_head=head.revision_id,
        expected_sha256=hashlib.sha256(snapshot_bytes(head)).hexdigest(),
        reviewed_fields=[ReviewedField(segment_id=SEGMENT, field_name=name)
                         for name in selected],
    )


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


def reject(head, request, status, code, principal=None, owner=OWNER, source=None):
    if source is None:
        source = Mock(return_value=page())
    before = snapshot_bytes(head)
    with patch("app.metadata_approval.uuid4") as new_id, \
            patch("app.metadata_approval.datetime") as clock:
        assert_error(status, code, lambda: prepare_approval(
            actor() if principal is None else principal,
            owner, head, request, source,
        ))
        new_id.assert_not_called()
        clock.now.assert_not_called()
    assert snapshot_bytes(head) == before
    return source


@pytest.mark.parametrize("changes", [
    {"actor_subject": "user:1"}, {"auth_source": "oauth"},
    {"status": "reviewed"}, {"normalized": "value"},
    {"human_verified": True}, {"approved_at": NEW_TIME},
    {"revision_id": "A" * 32}, {"expected_head": "g" * 32},
    {"expected_sha256": "D" * 64}, {"revision_id": 123},
    {"reviewed_fields": ()}, {"reviewed_fields": {}},
    {"reviewed_fields": None},
])
def test_closed_strict_input(changes):
    data = body(make_head()).model_dump(mode="python")
    data.update(changes)
    with pytest.raises(ValidationError):
        ApprovalInput.model_validate(data, strict=True)


@pytest.mark.parametrize("selection", [
    [{"segment_id": SEGMENT, "field_name": "title", "status": "reviewed"}],
    [{"segment_id": True, "field_name": "title"}],
    [{"segment_id": SEGMENT, "field_name": 1}],
    [{"segment_id": SEGMENT, "field_name": "Invalid"}],
    [{"segment_id": SEGMENT, "field_name": "title"}] * 2,
    [{"segment_id": SEGMENT, "field_name": f"field_{index}"}
     for index in range(201)],
])
def test_reviewed_field_shape_duplicates_and_limit(selection):
    data = body(make_head()).model_dump(mode="python")
    data["reviewed_fields"] = selection
    with pytest.raises(ValidationError):
        ApprovalInput.model_validate(data, strict=True)


def test_empty_and_maximum_selection_are_structurally_valid():
    data = body(make_head(), ()).model_dump(mode="python")
    assert ApprovalInput.model_validate(data, strict=True).reviewed_fields == []
    data["reviewed_fields"] = [
        {"segment_id": SEGMENT, "field_name": f"field_{index}"}
        for index in range(200)
    ]
    assert len(ApprovalInput.model_validate(data, strict=True).reviewed_fields) == 200


@pytest.mark.parametrize("tamper", [
    "copy", "construct", "nested", "duplicate", "tuple", "extra", "wire",
])
def test_public_boundary_revalidates_mutated_models(tamper):
    head = make_head()
    request = body(head)
    if tamper == "copy":
        request = request.model_copy(update={"revision_id": True})
    elif tamper == "construct":
        request = ApprovalInput.model_construct(**{
            **request.__dict__, "expected_sha256": "SENSITIVE_MARKER",
        })
    elif tamper == "nested":
        request.reviewed_fields[0] = request.reviewed_fields[0].model_copy(
            update={"segment_id": True}
        )
    elif tamper == "duplicate":
        request.reviewed_fields.append(request.reviewed_fields[0])
    elif tamper == "tuple":
        request = request.model_copy(update={
            "reviewed_fields": tuple(request.reviewed_fields),
        })
    elif tamper == "extra":
        request = request.model_copy(update={"human_verified": True})
    else:
        request = request.model_dump(mode="python")
        request["reviewed_fields"][0]["normalized"] = "SENSITIVE_MARKER"
    source = reject(head, request, 422, "invalid_metadata_input")
    source.assert_not_called()


@pytest.mark.parametrize("scope", sorted(SCOPES))
@pytest.mark.parametrize("admin", [False, True])
def test_every_scope_mandatory_without_admin_bypass(scope, admin):
    head = make_head()
    source = reject(
        head, body(head), 403, "insufficient_scope",
        principal=actor(scopes=SCOPES - {scope}, is_admin=admin),
    )
    source.assert_not_called()


@pytest.mark.parametrize("principal,owner", [
    (actor(kind="service", auth_source="service"), OWNER),
    (actor(kind="service", auth_source="oauth"), OWNER),
    (actor(kind="legacy", auth_source="local_jwt"), OWNER),
    (actor(auth_source="service"), OWNER),
    (actor(auth_source="legacy"), OWNER),
    (actor(auth_source="unknown"), OWNER),
    (actor(subject="user:01"), OWNER),
    (actor(subject="user:0"), OWNER),
    (actor(subject="user:+1"), OWNER),
    (actor(subject="user:1\n"), OWNER),
    (actor(subject="service:" + "1" * 32), OWNER),
    (actor(subject="user:2"), OWNER),
    (actor(user_id=True), OWNER),
    (actor(user_id=None), OWNER),
    (actor(is_admin=True), 2),
    (actor(), True),
    (actor(), "user:1"),
    (actor(), 0),
    (SimpleNamespace(subject="user:1", user_id=1, scopes=SCOPES,
                     kind="user", auth_source="local_jwt"), OWNER),
])
def test_identity_owner_kind_and_auth_denials(principal, owner):
    head = make_head()
    source = reject(
        head, body(head), 403, "metadata_approval_forbidden",
        principal=principal, owner=owner,
    )
    source.assert_not_called()


def test_authentication_precedes_untrusted_input_and_head_validation():
    source = Mock()
    with patch("app.metadata_approval.uuid4") as new_id:
        assert_error(403, "metadata_approval_forbidden", lambda: prepare_approval(
            None, OWNER, object(), {"actor": "SENSITIVE_MARKER"}, source,
        ))
        new_id.assert_not_called()
    source.assert_not_called()


def test_head_author_owner_is_checked_without_inferring_principal():
    head = make_head()
    head = head.model_copy(update={
        "author": head.author.model_copy(update={"actor_subject": "user:2"}),
    })
    source = reject(head, body(head), 403, "metadata_approval_forbidden")
    source.assert_not_called()


@pytest.mark.parametrize("tamper", ["copy", "construct", "nested", "dict"])
def test_corrupted_or_nonmodel_trusted_head_is_503(tamper):
    head = make_head()
    request = body(head)
    if tamper == "copy":
        head = head.model_copy(update={"page_count": True})
    elif tamper == "construct":
        head = MetadataSnapshot.model_construct(**{
            **head.__dict__, "page_count": True,
        })
    elif tamper == "nested":
        head.segments[0].fields[0].evidence[0] = proof().model_copy(
            update={"start": True}
        )
    else:
        head = head.model_dump(mode="python", by_alias=True)
    source = Mock()
    with patch("app.metadata_approval.uuid4") as new_id:
        assert_error(503, "metadata_unavailable", lambda: prepare_approval(
            actor(), OWNER, head, request, source,
        ))
        new_id.assert_not_called()
    source.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"revision_id": "9" * 32},
    {"expected_head": "9" * 32},
    {"expected_sha256": "9" * 64},
    {"revision_id": "9" * 32, "expected_head": "9" * 32},
])
def test_exact_revision_and_hash_preconditions(changes):
    head = make_head()
    source = reject(
        head, body(head).model_copy(update=changes),
        409, "metadata_revision_conflict",
    )
    source.assert_not_called()


def test_same_revision_changed_content_rejects_stale_hash():
    head = make_head()
    request = body(head)
    changed = head.model_copy(update={"output_pdf_sha256": "8" * 64})
    source = reject(changed, request, 409, "metadata_revision_conflict")
    source.assert_not_called()


def test_already_approved_cannot_be_approved_again_even_with_exact_hash():
    head = make_head(reviewed=("title",))
    head = head.model_copy(update={
        "state": "approved", "parent_revision_id": "7" * 32,
        "approval": ApprovalRecord(
            actor_subject="user:1", auth_source="local_jwt",
            approved_at=OLD_TIME, reviewed_revision_id="7" * 32,
            reviewed_sha256="8" * 64,
        ),
    })
    source = reject(head, body(head), 409, "metadata_approval_conflict")
    source.assert_not_called()


@pytest.mark.parametrize("pair", [
    ("9" * 32, "title"), (SEGMENT, "unknown"),
])
def test_unknown_segment_or_field_rejected(pair):
    head = make_head()
    request = body(head).model_copy(update={
        "reviewed_fields": [ReviewedField(segment_id=pair[0], field_name=pair[1])],
    })
    source = reject(head, request, 422, "invalid_metadata_input")
    source.assert_not_called()


def test_cannot_mark_absent_or_null_field_reviewed():
    fields = [spec(), spec("absent")]
    head = make_head(fields, [ManualFieldInput(field_name="title", raw=None)])
    for selected in (("title",), ("absent",)):
        source = reject(head, body(head, selected), 422, "invalid_metadata_input")
        source.assert_not_called()


@pytest.mark.parametrize("selected", [("title",), ()])
@pytest.mark.parametrize("status,origin", [
    ("conflict", "manual"),
    ("conflict", "declared"),
    ("needs_review", "inherited"),
    ("reviewed", "inherited"),
    ("needs_review", "extracted"),
    ("reviewed", "extracted"),
])
def test_original_unsupported_record_semantics_fail_before_review_or_pages(
    selected, status, origin,
):
    head = make_head()
    record = head.segments[0].fields[0]
    provenance = record.provenance.model_copy(update={
        "execution_id": "7" * 32 if origin == "extracted" else None,
    })
    head.segments[0].fields[0] = record.model_copy(update={
        "status": status, "origin": origin, "provenance": provenance,
    })
    # These populated records are structurally valid C1 models, but C2 rejects
    # their original status/origin even when the request selects them for review.
    head = MetadataSnapshot.model_validate(head, strict=True)
    before = snapshot_bytes(head)
    source = reject(head, body(head, selected), 503, "metadata_unavailable")
    source.assert_not_called()
    assert snapshot_bytes(head) == before
    assert head.segments[0].fields[0].status == status
    assert head.segments[0].fields[0].origin == origin


def test_partial_review_is_not_ready_and_does_not_mutate_head():
    fields = [spec(), spec("other")]
    head = make_head(fields, [
        ManualFieldInput(field_name="title", raw="one"),
        ManualFieldInput(field_name="other", raw="two"),
    ])
    source = reject(head, body(head), 409, "metadata_not_ready")
    source.assert_not_called()
    assert all(record.status == "needs_review" for record in head.segments[0].fields)


def test_missing_required_field_blocks_even_with_empty_review_set():
    head = make_head([spec(required=True)], [])
    source = reject(head, body(head, ()), 409, "metadata_not_ready")
    source.assert_not_called()


@pytest.mark.parametrize("option", ["critical", "evidence_required"])
def test_each_significant_item_requires_evidence(option):
    fields = [spec(type="boolean", cardinality="many", **{option: True})]
    head = make_head(fields, [ManualFieldInput(
        field_name="title", raw=[False, True], evidence=[proof(0)],
    )])
    source = reject(head, body(head), 409, "metadata_not_ready")
    source.assert_called_once_with(1)


def test_optional_false_quote_is_rejected_even_alongside_valid_proof():
    bad = proof().model_copy(update={"quote": "X" + TEXT[1:]})
    head = make_head([spec()], [ManualFieldInput(
        field_name="title", raw="SENSITIVE_MARKER", evidence=[proof(), bad],
    )])
    source = reject(head, body(head), 422, "evidence_invalid")
    source.assert_called_once_with(1)


def test_canonical_callback_application_error_status_code_message_preserved():
    head = make_head()
    expected = ApplicationError(503, "result_integrity_error", "Result integrity failed.")
    source = Mock(side_effect=expected)
    with patch("app.metadata_approval.uuid4") as new_id:
        error = assert_error(503, expected.code, lambda: prepare_approval(
            actor(), OWNER, head, body(head), source,
        ))
        assert error.message == expected.message
        assert error is not expected
        new_id.assert_not_called()
    source.assert_called_once_with(1)


@pytest.mark.parametrize("failure", [RuntimeError("SENSITIVE_MARKER"),
                                     KeyError("SENSITIVE_MARKER")])
def test_arbitrary_callback_failure_is_sanitized(failure):
    head = make_head()
    source = Mock(side_effect=failure)
    reject(head, body(head), 503, "canonical_source_invalid", source=source)


def test_malformed_canonical_page_is_503():
    head = make_head()
    reject(head, body(head), 503, "canonical_source_invalid",
           source=Mock(return_value=replace(page(), canonical_sha256="0" * 64)))


@pytest.mark.parametrize("control", [KeyboardInterrupt(), SystemExit(), GeneratorExit()])
def test_control_exception_identity_propagates(control):
    head = make_head()
    before = snapshot_bytes(head)
    source = Mock(side_effect=control)
    with patch("app.metadata_approval.uuid4") as new_id:
        with pytest.raises(type(control)) as caught:
            prepare_approval(actor(), OWNER, head, body(head), source)
        assert caught.value is control
        new_id.assert_not_called()
    assert snapshot_bytes(head) == before


@pytest.mark.parametrize("auth_source", ["local_jwt", "oauth"])
def test_success_exact_lineage_actor_time_literal_evidence_and_independence(auth_source):
    head = make_head()
    before = snapshot_bytes(head)
    request = body(head)
    request_before = request.model_dump(mode="python")
    source = Mock(return_value=page())
    events = []

    def read(number):
        events.append("page")
        return page()

    def allocate():
        events.append("uuid")
        return UUID(NEW_ID)

    source.side_effect = read
    with patch("app.metadata_approval.uuid4", side_effect=allocate) as new_id, \
            patch("app.metadata_approval.datetime", wraps=datetime) as clock:
        clock.now.return_value = NEW_TIME
        candidate = prepare_approval(
            actor(auth_source=auth_source), OWNER, head, request, source,
        )
        new_id.assert_called_once_with()
        clock.now.assert_called_once_with(timezone.utc)
    assert events == ["page", "uuid"]
    assert candidate.revision_id == NEW_ID != head.revision_id
    assert candidate.parent_revision_id == head.revision_id
    assert candidate.state == "approved"
    assert candidate.created_at == NEW_TIME
    assert candidate.created_at.tzinfo == timezone.utc
    assert candidate.author == ProvenanceRecord(
        actor_subject="user:1", auth_source=auth_source, recorded_at=NEW_TIME,
        execution_id=None,
    )
    assert candidate.approval == ApprovalRecord(
        actor_subject="user:1", auth_source=auth_source, approved_at=NEW_TIME,
        reviewed_revision_id=head.revision_id,
        reviewed_sha256=hashlib.sha256(before).hexdigest(),
    )
    old = head.segments[0].fields[0]
    new = candidate.segments[0].fields[0]
    assert new.status == "reviewed"
    assert old.status == "needs_review"
    assert new.model_dump(exclude={"status"}) == old.model_dump(exclude={"status"})
    assert new.evidence[0].quote == TEXT
    assert new.evidence[0].end == len(TEXT)
    assert new.provenance.auth_source == "local_jwt"
    assert new.provenance.recorded_at == OLD_TIME
    expected = head.model_dump(mode="python", by_alias=True)
    actual = candidate.model_dump(mode="python", by_alias=True)
    for key in ("revision_id", "parent_revision_id", "created_at", "author", "state", "approval", "segments"):
        expected.pop(key)
        actual.pop(key)
    assert actual == expected
    assert snapshot_bytes(head) == before
    assert request.model_dump(mode="python") == request_before
    # Frozen DTOs still contain mutable containers; none may alias HEAD.
    new.evidence.clear()
    candidate.schemas[0].schema_document["fields"][0]["description"] = "changed"
    candidate.segments[0].fields.clear()
    candidate.schemas.clear()
    assert snapshot_bytes(head) == before
    assert request.model_dump(mode="python") == request_before


def test_existing_reviewed_fields_may_remain_with_empty_selection():
    head = make_head(reviewed=("title",))
    before = snapshot_bytes(head)
    source = Mock(return_value=page())
    candidate = prepare_approval(actor(), OWNER, head, body(head, ()), source)
    assert candidate.state == "approved"
    assert candidate.segments[0].fields == head.segments[0].fields
    assert candidate.segments[0].fields is not head.segments[0].fields
    assert snapshot_bytes(head) == before
    source.assert_called_once_with(1)


def test_empty_optional_draft_needs_no_pages_and_can_prepare_candidate():
    head = make_head([spec()], [])
    source = Mock(side_effect=AssertionError("uncited page requested"))
    candidate = prepare_approval(actor(), OWNER, head, body(head, ()), source)
    assert candidate.state == "approved"
    assert candidate.segments[0].fields == []
    source.assert_not_called()


def test_nested_list_values_are_detached_and_field_provenance_preserved():
    head = make_head([spec(cardinality="many")], [ManualFieldInput(
        field_name="title", raw=["one", "two"],
    )])
    before = snapshot_bytes(head)
    candidate = prepare_approval(
        actor(auth_source="oauth"), OWNER, head, body(head), Mock(),
    )
    field = candidate.segments[0].fields[0]
    assert field.provenance == head.segments[0].fields[0].provenance
    field.raw.append("three")
    field.normalized.clear()
    assert snapshot_bytes(head) == before


def test_unexpected_failure_is_fixed_503_without_sensitive_chain():
    head = make_head(reviewed=("title",))
    with patch("app.metadata_approval.uuid4", side_effect=RuntimeError("SENSITIVE_MARKER")):
        assert_error(503, "metadata_unavailable", lambda: prepare_approval(
            actor(), OWNER, head, body(head, ()), Mock(return_value=page()),
        ))
