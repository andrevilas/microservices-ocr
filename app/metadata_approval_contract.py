"""Pure C5 persistence prerequisites; no storage or authorization authority.

Request identity hashes {"operation": "approve-v1", "body": validated_body}.
Reviewed fields retain their original order: selection ordering is significant.
No generated identity or timestamp participates in the request hash.

A receipt describes an unpersisted candidate, never committed approval proof.
These helpers do not verify current HEAD, literal evidence, or durable storage.
Approval policy and the future service remain responsible for those checks.
A future store must namespace keys by owner/job/operation/key and reauthorize
on every call, including replay, before returning any stored receipt.
"""

import hashlib
import json
import re

from .application import ApplicationError
from .metadata_approval import _guard, _input
from .metadata_models import ApproveMetadataReceipt, MetadataSnapshot, snapshot_bytes


@_guard
def approval_request_identity(key, body) -> tuple[str, str]:
    """Return key/request SHA-256 hashes without authorization or mutation.

    The operation versions the canonical envelope; reviewed_fields array order
    is preserved and significant. Dictionary member ordering is insignificant.
    """
    if type(key) is not str or re.fullmatch(
        r"[A-Za-z0-9._:-]{8,128}", key, flags=re.ASCII
    ) is None:
        raise ApplicationError(422, "invalid_idempotency_key", "Invalid idempotency key.")
    # Revalidate original nested instances before dumping and detaching them.
    request = _input(body)
    if request.revision_id != request.expected_head:
        raise ApplicationError(422, "invalid_metadata_input", "Invalid metadata input.")
    envelope = {
        "operation": "approve-v1",
        "body": request.model_dump(mode="json", warnings="error"),
    }
    encoded = json.dumps(
        envelope, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(key.encode("ascii")).hexdigest(), hashlib.sha256(encoded).hexdigest()


@_guard
def validate_approval_receipt(value) -> ApproveMetadataReceipt:
    """Strictly revalidate and detach a receipt; control exceptions propagate."""
    try:
        if type(value) is not dict and not isinstance(value, ApproveMetadataReceipt):
            raise ValueError("A receipt model or dictionary is required.")
        checked = ApproveMetadataReceipt.model_validate(value, strict=True)
        dump = checked.model_dump(mode="python", warnings="error")
        return ApproveMetadataReceipt.model_validate(dump, strict=True)
    except Exception:
        pass
    raise ApplicationError(422, "invalid_metadata_input", "Invalid metadata input.")


@_guard
def approval_receipt_for(snapshot) -> ApproveMetadataReceipt:
    """Describe an approved candidate, without asserting it has been committed.

    Only an exact trusted snapshot instance is accepted. Structural integrity
    and consistent author/reviewer lineage do not establish policy authority.
    """
    try:
        if type(snapshot) is not MetadataSnapshot:
            raise ValueError("A trusted snapshot instance is required.")
        encoded = snapshot_bytes(snapshot)
        checked = MetadataSnapshot.model_validate_json(encoded, strict=True)
        approval = checked.approval
        if (
            checked.state != "approved"
            or approval is None
            or checked.parent_revision_id != approval.reviewed_revision_id
            or checked.author.actor_subject != approval.actor_subject
            or checked.author.auth_source != approval.auth_source
            or checked.author.recorded_at != approval.approved_at
            or checked.created_at != approval.approved_at
            or checked.author.execution_id is not None
        ):
            raise ValueError("Inconsistent approval candidate.")
        return ApproveMetadataReceipt(
            operation="approve-v1",
            state="approved",
            revision_id=checked.revision_id,
            sha256=hashlib.sha256(encoded).hexdigest(),
            reviewed_revision_id=approval.reviewed_revision_id,
            reviewed_sha256=approval.reviewed_sha256,
        )
    except Exception:
        pass
    raise ApplicationError(503, "metadata_unavailable", "Metadata is unavailable.")
