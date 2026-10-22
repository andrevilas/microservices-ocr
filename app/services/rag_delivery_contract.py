"""Pure, staged RAG delivery contract; no receiver transport is defined here.

Matching reports indexing readiness only. It is not authentication, artifact
byte verification, proof of durability, an ACL decision, or permission to expire
files. Callers must authenticate the receiver, verify actual artifact bytes,
enforce owner/process authorization, and enforce bounded retention externally.
Frozen models are not inherently trusted; public helpers revalidate their data.
"""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

_CONFIG = ConfigDict(
    frozen=True,
    extra="forbid",
    strict=True,
    revalidate_instances="always",
    validate_default=True,
    hide_input_in_errors=True,
)
_MAX_JSON_BYTES = 65536
_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "pages": "application/x-ndjson",
    "metadata": "application/json",
}
_SIZE_LIMITS = {
    "pdf": 512 * 1024 * 1024,
    "pages": 64 * 1024 * 1024,
    "metadata": 512 * 1024,
}


def _not_dot(value: str) -> str:
    if value in (".", ".."):
        raise ValueError("invalid_id")
    return value


SafeId = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_./-]+$",
    ),
    AfterValidator(_not_dot),
]
Sha256 = Annotated[
    str,
    StringConstraints(
        strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"
    ),
]


class Artifact(BaseModel):
    model_config = _CONFIG

    role: Literal["pdf", "pages", "metadata"]
    sha256: Sha256
    size_bytes: Annotated[int, Field(strict=True, gt=0)]
    media_type: Annotated[str, StringConstraints(strict=True, max_length=64)]

    @model_validator(mode="after")
    def check_descriptor(self) -> "Artifact":
        if self.media_type != _MEDIA_TYPES[self.role]:
            raise ValueError("artifact_media_type_mismatch")
        if self.size_bytes > _SIZE_LIMITS[self.role]:
            raise ValueError("artifact_size_exceeded")
        return self


Artifacts = Annotated[tuple[Artifact, ...], Field(min_length=2, max_length=3)]


def _check_roles(artifacts: tuple[Artifact, ...]) -> set[str]:
    roles = {artifact.role for artifact in artifacts}
    if len(roles) != len(artifacts):
        raise ValueError("duplicate_artifact_role")
    if not {"pdf", "pages"}.issubset(roles):
        raise ValueError("missing_artifact_role")
    return roles


class DeliveryManifest(BaseModel):
    model_config = _CONFIG

    contract_version: Literal["ocr-rag/1"]
    delivery_id: SafeId
    destination_id: SafeId
    job_id: SafeId
    source_revision: SafeId
    source_kind: Literal[
        "process_record", "historical_reference", "normative_source"
    ]
    process_reference: SafeId | None = None
    metadata_state: Literal["absent", "draft", "approved"]
    artifacts: Artifacts

    @model_validator(mode="after")
    def check_manifest(self) -> "DeliveryManifest":
        roles = _check_roles(self.artifacts)
        if self.source_kind == "process_record" and self.process_reference is None:
            raise ValueError("process_reference_required")
        if ("metadata" in roles) != (self.metadata_state != "absent"):
            raise ValueError("metadata_state_mismatch")
        return self


class DeliveryReceipt(BaseModel):
    model_config = _CONFIG

    contract_version: Literal["ocr-rag/1"]
    delivery_id: SafeId
    destination_id: SafeId
    manifest_sha256: Sha256
    destination_document_id: SafeId
    destination_revision: SafeId
    artifacts: Artifacts
    storage_state: Literal["stored"]
    indexing_state: Literal["pending", "indexed", "failed"]

    @model_validator(mode="after")
    def check_receipt(self) -> "DeliveryReceipt":
        _check_roles(self.artifacts)
        return self


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("invalid_json_constant")


def _checked_json(raw: bytes) -> str:
    if type(raw) is not bytes:
        raise ValueError("invalid_raw_type")
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError("json_size_exceeded")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ValueError("invalid_utf8") from None
    try:
        root = json.loads(
            text, object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except (json.JSONDecodeError, RecursionError):
        raise ValueError("invalid_json") from None
    if not isinstance(root, dict):
        raise ValueError("invalid_json_root")
    return text


def parse_manifest(raw: bytes) -> DeliveryManifest:
    return DeliveryManifest.model_validate_json(_checked_json(raw))


def parse_receipt(raw: bytes) -> DeliveryReceipt:
    return DeliveryReceipt.model_validate_json(_checked_json(raw))


def _validated_manifest(value: DeliveryManifest) -> DeliveryManifest:
    if not isinstance(value, DeliveryManifest):
        raise ValueError("invalid_manifest")
    return DeliveryManifest.model_validate(
        value.model_dump(mode="python", warnings=False)
    )


def _validated_receipt(value: DeliveryReceipt) -> DeliveryReceipt:
    if not isinstance(value, DeliveryReceipt):
        raise ValueError("invalid_receipt")
    return DeliveryReceipt.model_validate(
        value.model_dump(mode="python", warnings=False)
    )


def manifest_digest(manifest: DeliveryManifest) -> str:
    manifest = _validated_manifest(manifest)
    payload = manifest.model_dump(mode="json")
    payload["artifacts"] = sorted(payload["artifacts"], key=lambda item: item["role"])
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def match_receipt(manifest: DeliveryManifest, receipt: DeliveryReceipt) -> bool:
    """Validate correspondence and return indexing readiness, not trust."""
    manifest = _validated_manifest(manifest)
    receipt = _validated_receipt(receipt)
    if (
        receipt.delivery_id != manifest.delivery_id
        or receipt.destination_id != manifest.destination_id
        or receipt.manifest_sha256 != manifest_digest(manifest)
        or sorted(receipt.artifacts, key=lambda item: item.role)
        != sorted(manifest.artifacts, key=lambda item: item.role)
    ):
        raise ValueError("receipt_mismatch")
    return receipt.indexing_state == "indexed"
