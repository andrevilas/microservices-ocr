"""C5 policy and preparation of an unpersisted approval candidate.

``owner`` is the trusted positive integer owner user ID. ``head`` must be the
verified current HEAD read by a future DocumentMetadataService under its owner
check and artifact lease. This module neither loads HEAD nor establishes that a
supplied snapshot is current.

An approval records authorized_principal_review: an authorized authenticated
principal explicitly selected present fields for review. OAuth and local JWT
identity do not establish human verification or MFA.

The returned candidate conveys no durability authority. Before atomic commit,
the service must reauthorize, check grants, current HEAD/CAS, TTL and quota while
holding the required transaction and lease. There are no writes here.
"""

from functools import wraps
import hashlib
from datetime import datetime, timezone
from typing import Annotated
from uuid import uuid4

from pydantic import BeforeValidator, Field, model_validator

from .application import ApplicationError, Principal
from .document_models import FieldName, FieldSpec
from .metadata_evidence import assess_snapshot
from .metadata_models import (
    ApprovalRecord,
    MetadataSnapshot,
    PersistentID,
    ProvenanceRecord,
    SHA256,
    _ClosedModel,
    _list,
    snapshot_bytes,
)
from .metadata_validation import MetadataValidationError, validate_value_records


_ERRORS = {
    "metadata_approval_forbidden": (403, "Metadata approval is forbidden."),
    "invalid_metadata_input": (422, "Invalid metadata input."),
    "metadata_revision_conflict": (409, "Metadata revision has changed."),
    "metadata_approval_conflict": (409, "Metadata is already approved."),
    "metadata_not_ready": (409, "Metadata is not ready for approval."),
    "evidence_invalid": (422, "Invalid metadata evidence."),
    "canonical_source_invalid": (503, "Canonical text is unavailable."),
    "metadata_unavailable": (503, "Metadata is unavailable."),
}
_SCOPES = (
    "metadata:approve", "metadata:read", "jobs:read", "artifacts:read",
)


def _error(code):
    status, message = _ERRORS[code]
    return ApplicationError(status, code, message)


def _fail(code):
    raise _error(code)


def _guard(method):
    """Discard unexpected exception details; allow control exceptions through."""
    @wraps(method)
    def guarded(*args, **kwargs):
        failure = None
        try:
            return method(*args, **kwargs)
        except ApplicationError as exc:
            failure = ApplicationError(exc.status, exc.code, exc.message)
        except Exception:
            failure = _error("metadata_unavailable")
        try:
            raise failure from None
        finally:
            failure.__context__ = None
            failure.__cause__ = None
    return guarded


class ReviewedField(_ClosedModel):
    segment_id: PersistentID
    field_name: FieldName


class ApprovalInput(_ClosedModel):
    revision_id: PersistentID
    expected_head: PersistentID
    expected_sha256: SHA256
    reviewed_fields: Annotated[
        list[ReviewedField], Field(max_length=200), BeforeValidator(_list)
    ]

    @model_validator(mode="after")
    def _unique(self):
        pairs = [(item.segment_id, item.field_name) for item in self.reviewed_fields]
        if len(set(pairs)) != len(pairs):
            raise ValueError("Duplicate reviewed field.")
        return self


def _authorize(principal, owner):
    if (
        type(principal) is not Principal
        or type(owner) is not int
        or owner <= 0
    ):
        _fail("metadata_approval_forbidden")
    if (
        type(principal.kind) is not str
        or principal.kind != "user"
        or type(principal.user_id) is not int
        or principal.user_id != owner
        or type(principal.subject) is not str
        or principal.subject != f"user:{owner}"
        or type(principal.auth_source) is not str
        or principal.auth_source not in ("local_jwt", "oauth")
        or type(principal.scopes) is not frozenset
        or any(type(scope) is not str for scope in principal.scopes)
    ):
        _fail("metadata_approval_forbidden")
    # Administrative status supplies no exemption from any required grant.
    for scope in _SCOPES:
        principal.require(scope)


def _input(body):
    failed = False
    result = None
    try:
        if not isinstance(body, ApprovalInput) and type(body) is not dict:
            raise ValueError("A model or dictionary is required.")
        # Validate original nested instances before serialization can coerce them.
        result = ApprovalInput.model_validate(body, strict=True)
        result = ApprovalInput.model_validate(
            result.model_dump(mode="python", warnings="error"), strict=True
        )
    except Exception:
        failed = True
    if failed:
        _fail("invalid_metadata_input")
    return result


def _head(head):
    failed = False
    detached = None
    encoded = None
    try:
        if type(head) is not MetadataSnapshot:
            raise ValueError("A trusted snapshot instance is required.")
        encoded = snapshot_bytes(head)
        detached = MetadataSnapshot.model_validate_json(encoded, strict=True)
    except Exception:
        failed = True
    if failed:
        _fail("metadata_unavailable")
    return detached, hashlib.sha256(encoded).hexdigest()


def _validate_original_records(draft):
    """Reject invalid trusted HEAD semantics before changing review statuses.

    Validation failures reach _guard as fixed metadata_unavailable errors.
    This checks records only and never requests canonical pages.
    """
    fields_by_hash = {
        schema.schema_sha256: [
            FieldSpec.model_validate(value, strict=True)
            for value in schema.schema_document["fields"]
        ]
        for schema in draft.schemas
    }
    for segment in draft.segments:
        validate_value_records(fields_by_hash[segment.schema_sha256], segment.fields)


def _review(draft, selection):
    records = {
        (segment.segment_id, record.field_name): record
        for segment in draft.segments
        for record in segment.fields
    }
    selected = {(item.segment_id, item.field_name) for item in selection}
    for pair in selected:
        record = records.get(pair)
        if (
            record is None
            or record.raw is None
            or record.normalized is None
            or record.status == "not_found"
        ):
            _fail("invalid_metadata_input")
    return draft.model_copy(update={
        "segments": [segment.model_copy(update={
            "fields": [
                record.model_copy(update={"status": "reviewed"})
                if (segment.segment_id, record.field_name) in selected else record
                for record in segment.fields
            ],
        }) for segment in draft.segments],
    })


def _assess(draft, page_source):
    # C2 sanitizes arbitrary callback exceptions. Retain trusted application
    # errors separately so their status/code/message survive that boundary.
    callback_failure = []

    def source(number):
        failed = False
        try:
            return page_source(number)
        except ApplicationError as exc:
            callback_failure.append(exc)
            failed = True
        if failed:
            raise MetadataValidationError("canonical_source_invalid")

    validation_code = None
    result = None
    try:
        result = assess_snapshot(draft, source)
    except MetadataValidationError as exc:
        validation_code = exc.code
    if callback_failure:
        raise callback_failure[0]
    if validation_code == "evidence_invalid":
        _fail("evidence_invalid")
    if validation_code == "canonical_source_invalid":
        _fail("canonical_source_invalid")
    if validation_code is not None:
        # Other failures concern the trusted HEAD's structure/schema/values.
        _fail("metadata_unavailable")
    if not result.approvable:
        _fail("metadata_not_ready")


@_guard
def prepare_approval(principal, owner, head, body, page_source) -> MetadataSnapshot:
    """Authorize and validate a detached draft, then prepare an approved child.

    No caller-supplied actor, time or new revision identity is accepted. Existing
    reviewed statuses from the trusted persisted HEAD remain unchanged unless
    explicitly selected; they are not a claim of human verification.
    """
    _authorize(principal, owner)
    request = _input(body)
    draft, reviewed_hash = _head(head)
    if draft.author.actor_subject != f"user:{owner}":
        _fail("metadata_approval_forbidden")
    if draft.state != "draft" or draft.approval is not None:
        _fail("metadata_approval_conflict")
    if (
        request.revision_id != draft.revision_id
        or request.expected_head != draft.revision_id
        or request.expected_sha256 != reviewed_hash
    ):
        _fail("metadata_revision_conflict")

    _validate_original_records(draft)
    reviewed = _review(draft, request.reviewed_fields)
    _assess(reviewed, page_source)

    # Only now allocate the new server identity and approval timestamp.
    revision_id = uuid4().hex
    now = datetime.now(timezone.utc)
    data = reviewed.model_dump(mode="python", by_alias=True, warnings="error")
    data.update({
        "revision_id": revision_id,
        "parent_revision_id": draft.revision_id,
        "created_at": now,
        "author": ProvenanceRecord(
            actor_subject=principal.subject,
            auth_source=principal.auth_source,
            recorded_at=now,
            execution_id=None,
        ),
        "state": "approved",
        "approval": ApprovalRecord(
            actor_subject=principal.subject,
            auth_source=principal.auth_source,
            approved_at=now,
            reviewed_revision_id=draft.revision_id,
            reviewed_sha256=reviewed_hash,
        ),
    })
    candidate = MetadataSnapshot.model_validate(data, strict=True)
    # A final canonical roundtrip also detaches all nested mutable containers.
    return MetadataSnapshot.model_validate_json(snapshot_bytes(candidate), strict=True)
