import hashlib
import json

import pytest
from pydantic import ValidationError

from app.services.rag_delivery_contract import (
    Artifact,
    DeliveryManifest,
    DeliveryReceipt,
    manifest_digest,
    match_receipt,
    parse_manifest,
    parse_receipt,
)


def _encode(payload):
    return json.dumps(payload).encode("utf-8")


def _artifact(role):
    return {
        "role": role,
        "sha256": {"pdf": "1", "pages": "2", "metadata": "3"}[role] * 64,
        "size_bytes": {"pdf": 10, "pages": 20, "metadata": 30}[role],
        "media_type": {
            "pdf": "application/pdf",
            "pages": "application/x-ndjson",
            "metadata": "application/json",
        }[role],
    }


def _manifest_data(kind="process_record", state="absent"):
    roles = ["pdf", "pages"]
    if state != "absent":
        roles.append("metadata")
    return {
        "contract_version": "ocr-rag/1",
        "delivery_id": "delivery-1",
        "destination_id": "destination-1",
        "job_id": "job-1",
        "source_revision": "revision-1",
        "source_kind": kind,
        "process_reference": "process-1" if kind == "process_record" else None,
        "metadata_state": state,
        "artifacts": [_artifact(role) for role in roles],
    }


def _receipt_data(manifest=None):
    if manifest is None:
        manifest = parse_manifest(_encode(_manifest_data()))
    return {
        "contract_version": "ocr-rag/1",
        "delivery_id": manifest.delivery_id,
        "destination_id": manifest.destination_id,
        "manifest_sha256": manifest_digest(manifest),
        "destination_document_id": "document-1",
        "destination_revision": "revision-2",
        "artifacts": manifest.model_dump(mode="json")["artifacts"],
        "storage_state": "stored",
        "indexing_state": "indexed",
    }


def _assert_invalid_manifest(payload):
    with pytest.raises(ValidationError):
        parse_manifest(_encode(payload))
    python_payload = {**payload, "artifacts": tuple(payload["artifacts"])}
    with pytest.raises(ValidationError):
        DeliveryManifest(**python_payload)


@pytest.mark.parametrize(
    "kind", ["process_record", "historical_reference", "normative_source"]
)
@pytest.mark.parametrize("state", ["absent", "draft", "approved"])
def test_valid_manifests_and_receipts(kind, state):
    manifest = parse_manifest(_encode(_manifest_data(kind, state)))
    assert isinstance(manifest.artifacts, tuple)
    assert len(manifest.artifacts) == (2 if state == "absent" else 3)
    assert manifest.process_reference == (
        "process-1" if kind == "process_record" else None
    )
    receipt = parse_receipt(_encode(_receipt_data(manifest)))
    assert match_receipt(manifest, receipt) is True
    assert DeliveryManifest(**manifest.model_dump(mode="python")) == manifest
    assert DeliveryReceipt(**receipt.model_dump(mode="python")) == receipt


def test_optional_process_reference_can_be_omitted():
    payload = _manifest_data("historical_reference")
    del payload["process_reference"]
    assert parse_manifest(_encode(payload)).process_reference is None
    payload["source_kind"] = "process_record"
    _assert_invalid_manifest(payload)


def test_digest_has_independent_known_canonical_serialization():
    manifest = parse_manifest(_encode(_manifest_data()))
    canonical = (
        '{"artifacts":[{"media_type":"application/x-ndjson","role":"pages",'
        '"sha256":"'
        + "2" * 64
        + '","size_bytes":20},{"media_type":"application/pdf","role":"pdf",'
        '"sha256":"'
        + "1" * 64
        + '","size_bytes":10}],"contract_version":"ocr-rag/1",'
        '"delivery_id":"delivery-1","destination_id":"destination-1",'
        '"job_id":"job-1","metadata_state":"absent",'
        '"process_reference":"process-1","source_kind":"process_record",'
        '"source_revision":"revision-1"}'
    )
    assert manifest_digest(manifest) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("state", ["absent", "draft", "approved"])
def test_artifact_order_does_not_change_digest_or_matching(state):
    payload = _manifest_data(state=state)
    original = parse_manifest(_encode(payload))
    payload["artifacts"].reverse()
    reordered = parse_manifest(_encode(payload))
    assert manifest_digest(original) == manifest_digest(reordered)
    receipt = parse_receipt(_encode(_receipt_data(reordered)))
    assert match_receipt(original, receipt) is True


@pytest.mark.parametrize(
    ("state", "roles"),
    [
        ("absent", ["pdf", "pdf", "pages"]),
        ("absent", ["pdf"]),
        ("absent", ["pages"]),
        ("draft", ["pages", "metadata"]),
        ("approved", ["pdf", "metadata"]),
        ("draft", ["pdf", "pages"]),
        ("approved", ["pdf", "pages"]),
        ("absent", ["pdf", "pages", "metadata"]),
        ("draft", ["pdf", "pages", "metadata", "metadata"]),
    ],
)
def test_manifest_role_and_metadata_rules(state, roles):
    payload = _manifest_data(state=state)
    payload["artifacts"] = [_artifact(role) for role in roles]
    _assert_invalid_manifest(payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("contract_version",), "ocr-rag/2"),
        (("source_kind",), "other"),
        (("metadata_state",), "other"),
        (("process_reference",), None),
        (("delivery_id",), True),
        (("job_id",), 1),
        (("extra",), "not-allowed"),
        (("artifacts", 0, "url"), "https://example.invalid/file"),
        (("artifacts", 0, "size_bytes"), True),
        (("artifacts", 0, "size_bytes"), 0),
        (("artifacts", 0, "size_bytes"), -1),
        (("artifacts", 0, "size_bytes"), 10.0),
        (("artifacts", 0, "size_bytes"), "10"),
        (("artifacts", 0, "sha256"), "A" * 64),
        (("artifacts", 0, "sha256"), "g" * 64),
        (("artifacts", 0, "sha256"), "a" * 63),
        (("artifacts", 0, "sha256"), "a" * 65),
        (("artifacts", 0, "sha256"), False),
        (("artifacts", 0, "media_type"), "application/json"),
        (("artifacts", 0, "role"), "other"),
    ],
)
def test_invalid_manifest_fields_in_json_and_python(path, value):
    payload = _manifest_data()
    target = payload
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = value
    _assert_invalid_manifest(payload)


@pytest.mark.parametrize(
    ("role", "limit"),
    [
        ("pdf", 512 * 1024 * 1024),
        ("pages", 64 * 1024 * 1024),
        ("metadata", 512 * 1024),
    ],
)
def test_exact_artifact_size_limits(role, limit):
    descriptor = _artifact(role)
    descriptor["size_bytes"] = limit
    assert Artifact(**descriptor).size_bytes == limit
    descriptor["size_bytes"] = limit + 1
    with pytest.raises(ValidationError):
        Artifact(**descriptor)
    payload = _manifest_data(state="draft")
    payload["artifacts"] = [
        descriptor if item["role"] == role else item for item in payload["artifacts"]
    ]
    _assert_invalid_manifest(payload)


@pytest.mark.parametrize(
    "field",
    ["delivery_id", "destination_id", "job_id", "source_revision", "process_reference"],
)
@pytest.mark.parametrize(
    "value", ["", ".", "..", "x" * 129, "has space", "é", "x\n", "x?", "x\\y"]
)
def test_manifest_ids_are_safe_bounded_ascii(field, value):
    payload = _manifest_data()
    payload[field] = value
    _assert_invalid_manifest(payload)


def test_safe_id_character_set_and_maximum_length():
    payload = _manifest_data()
    payload["delivery_id"] = "A_z.9/-"
    payload["source_revision"] = "x" * 128
    assert parse_manifest(_encode(payload)).delivery_id == "A_z.9/-"


@pytest.mark.parametrize(
    ("parser", "factory"),
    [(parse_manifest, _manifest_data), (parse_receipt, _receipt_data)],
)
def test_json_rejects_duplicate_keys_at_every_depth(parser, factory):
    raw = _encode(factory())
    cases = [
        raw[:-1] + b', "delivery_id": "delivery-1"}',
        raw.replace(b'"size_bytes": 10', b'"size_bytes": 10, "size_bytes": 10'),
        raw[:-1] + b', "extra": {"nested": {"x": 1, "x": 2}}}',
    ]
    for duplicate in cases:
        with pytest.raises(ValueError, match="^duplicate_json_key$"):
            parser(duplicate)


@pytest.mark.parametrize("parser", [parse_manifest, parse_receipt])
@pytest.mark.parametrize(
    "raw",
    [b"", b"{", b'{"x":1,}', b"[]", b"[{}]", b"null", b"true", b"1", b'"text"'],
)
def test_malformed_json_and_non_object_roots(parser, raw):
    with pytest.raises(ValueError):
        parser(raw)


@pytest.mark.parametrize(
    ("parser", "factory"),
    [(parse_manifest, _manifest_data), (parse_receipt, _receipt_data)],
)
@pytest.mark.parametrize("constant", [b"NaN", b"Infinity", b"-Infinity"])
def test_non_finite_json_tokens_rejected(parser, factory, constant):
    raw = _encode(factory())[:-1] + b', "extra": ' + constant + b"}"
    with pytest.raises(ValueError, match="^invalid_json_constant$"):
        parser(raw)


@pytest.mark.parametrize(
    ("parser", "factory"),
    [(parse_manifest, _manifest_data), (parse_receipt, _receipt_data)],
)
def test_raw_size_boundary_and_invalid_utf8(parser, factory):
    raw = _encode(factory())
    padded = raw + b" " * (65536 - len(raw))
    parser(padded)
    with pytest.raises(ValueError, match="^json_size_exceeded$"):
        parser(padded + b" ")
    with pytest.raises(ValueError, match="^invalid_utf8$"):
        parser(raw[:-1] + b', "extra": "\xff"}')


@pytest.mark.parametrize("parser", [parse_manifest, parse_receipt])
@pytest.mark.parametrize("raw", [None, {}, [], 1, "{}", bytearray(b"{}")])
def test_parsers_reject_non_bytes(parser, raw):
    with pytest.raises(ValueError, match="^invalid_raw_type$"):
        parser(raw)


@pytest.mark.parametrize(
    ("state", "expected"), [("pending", False), ("failed", False), ("indexed", True)]
)
def test_readiness_states(state, expected):
    manifest = parse_manifest(_encode(_manifest_data()))
    payload = _receipt_data(manifest)
    payload["indexing_state"] = state
    assert match_receipt(manifest, parse_receipt(_encode(payload))) is expected


@pytest.mark.parametrize("state", ["pending", "failed", "indexed"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("destination_id", "other-destination"),
        ("delivery_id", "other-delivery"),
        ("manifest_sha256", "f" * 64),
    ],
)
def test_receipt_identity_mismatch_always_raises(field, value, state):
    manifest = parse_manifest(_encode(_manifest_data()))
    payload = _receipt_data(manifest)
    payload[field] = value
    payload["indexing_state"] = state
    with pytest.raises(ValueError, match="^receipt_mismatch$"):
        match_receipt(manifest, parse_receipt(_encode(payload)))


@pytest.mark.parametrize("role", ["pdf", "pages", "metadata"])
@pytest.mark.parametrize(("field", "value"), [("sha256", "f" * 64), ("size_bytes", 99)])
def test_each_artifact_hash_and_size_must_match(role, field, value):
    manifest = parse_manifest(_encode(_manifest_data(state="draft")))
    payload = _receipt_data(manifest)
    for descriptor in payload["artifacts"]:
        if descriptor["role"] == role:
            descriptor[field] = value
    with pytest.raises(ValueError, match="^receipt_mismatch$"):
        match_receipt(manifest, parse_receipt(_encode(payload)))


@pytest.mark.parametrize("state", ["absent", "draft"])
def test_receipt_metadata_descriptor_set_must_match_manifest(state):
    manifest = parse_manifest(_encode(_manifest_data(state=state)))
    payload = _receipt_data(manifest)
    if state == "absent":
        payload["artifacts"].append(_artifact("metadata"))
    else:
        payload["artifacts"] = payload["artifacts"][:2]
    with pytest.raises(ValueError, match="^receipt_mismatch$"):
        match_receipt(manifest, parse_receipt(_encode(payload)))


@pytest.mark.parametrize(
    "roles",
    [
        ["pdf"],
        ["pages"],
        ["pdf", "metadata"],
        ["pages", "metadata"],
        ["pdf", "pdf", "pages"],
        ["pdf", "pages", "metadata", "metadata"],
    ],
)
def test_receipt_requires_unique_pdf_and_pages(roles):
    payload = _receipt_data()
    payload["artifacts"] = [_artifact(role) for role in roles]
    with pytest.raises(ValidationError):
        parse_receipt(_encode(payload))
    payload["artifacts"] = tuple(payload["artifacts"])
    with pytest.raises(ValidationError):
        DeliveryReceipt(**payload)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("contract_version",), "ocr-rag/2"),
        (("delivery_id",), "."),
        (("destination_id",), ".."),
        (("destination_document_id",), "bad id"),
        (("destination_revision",), "x" * 129),
        (("destination_revision",), True),
        (("manifest_sha256",), "A" * 64),
        (("storage_state",), "missing"),
        (("indexing_state",), "ready"),
        (("signature",), "not-allowed"),
        (("artifacts", 0, "size_bytes"), True),
        (("artifacts", 0, "size_bytes"), 0),
        (("artifacts", 0, "size_bytes"), 512 * 1024 * 1024 + 1),
        (("artifacts", 0, "media_type"), "application/json"),
        (("artifacts", 0, "sha256"), "g" * 64),
        (("artifacts", 0, "credentials"), "not-allowed"),
    ],
)
def test_receipt_field_validation_in_json_and_python(path, value):
    payload = _receipt_data()
    target = payload
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        parse_receipt(_encode(payload))
    payload["artifacts"] = tuple(payload["artifacts"])
    with pytest.raises(ValidationError):
        DeliveryReceipt(**payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("delivery_id", ".."),
        ("process_reference", None),
        ("metadata_state", "approved"),
        ("contract_version", "other"),
    ],
)
def test_public_helpers_reject_constructed_invalid_manifests(field, value):
    manifest = parse_manifest(_encode(_manifest_data()))
    receipt = parse_receipt(_encode(_receipt_data(manifest)))
    payload = manifest.model_dump(mode="python")
    payload[field] = value
    unsafe = DeliveryManifest.model_construct(**payload)
    with pytest.raises(ValueError):
        manifest_digest(unsafe)
    with pytest.raises(ValueError):
        match_receipt(unsafe, receipt)


@pytest.mark.parametrize("owner", ["manifest", "receipt"])
@pytest.mark.parametrize(
    ("field", "value"),
    [("size_bytes", True), ("sha256", "bad"), ("media_type", "application/json"), ("role", "metadata")],
)
def test_constructed_invalid_nested_descriptors_are_revalidated(owner, field, value):
    manifest = parse_manifest(_encode(_manifest_data()))
    receipt = parse_receipt(_encode(_receipt_data(manifest)))
    descriptor = manifest.artifacts[0].model_dump(mode="python")
    descriptor[field] = value
    unsafe_artifact = Artifact.model_construct(**descriptor)
    artifacts = (unsafe_artifact, manifest.artifacts[1])
    if owner == "manifest":
        payload = manifest.model_dump(mode="python")
        payload["artifacts"] = artifacts
        unsafe = DeliveryManifest.model_construct(**payload)
        with pytest.raises(ValueError):
            manifest_digest(unsafe)
        with pytest.raises(ValueError):
            match_receipt(unsafe, receipt)
    else:
        payload = receipt.model_dump(mode="python")
        payload["artifacts"] = artifacts
        unsafe = DeliveryReceipt.model_construct(**payload)
        with pytest.raises(ValueError):
            match_receipt(manifest, unsafe)
    with pytest.raises(ValidationError):
        DeliveryManifest(**{**manifest.model_dump(mode="python"), "artifacts": artifacts})


@pytest.mark.parametrize(
    ("field", "value"),
    [("destination_revision", True), ("storage_state", "missing"), ("indexing_state", "ready")],
)
def test_constructed_invalid_receipt_fields_are_revalidated(field, value):
    manifest = parse_manifest(_encode(_manifest_data()))
    payload = _receipt_data(manifest)
    payload["artifacts"] = tuple(payload["artifacts"])
    payload[field] = value
    with pytest.raises(ValueError):
        match_receipt(manifest, DeliveryReceipt.model_construct(**payload))


def test_python_artifact_collections_remain_strict():
    manifest = parse_manifest(_encode(_manifest_data()))
    receipt = parse_receipt(_encode(_receipt_data(manifest)))
    payload = manifest.model_dump(mode="python")
    payload["artifacts"] = list(manifest.artifacts)
    with pytest.raises(ValidationError):
        DeliveryManifest(**payload)
    with pytest.raises(ValueError):
        manifest_digest(DeliveryManifest.model_construct(**payload))
    payload = receipt.model_dump(mode="python")
    payload["artifacts"] = list(receipt.artifacts)
    with pytest.raises(ValueError):
        match_receipt(manifest, DeliveryReceipt.model_construct(**payload))


def test_valid_constructed_models_are_revalidated_successfully():
    manifest = parse_manifest(_encode(_manifest_data()))
    receipt = parse_receipt(_encode(_receipt_data(manifest)))
    reconstructed = DeliveryManifest.model_construct(**manifest.model_dump(mode="python"))
    reconstructed_receipt = DeliveryReceipt.model_construct(**receipt.model_dump(mode="python"))
    assert manifest_digest(reconstructed) == manifest_digest(manifest)
    assert match_receipt(reconstructed, reconstructed_receipt) is True


def test_helpers_reject_arbitrary_objects():
    manifest = parse_manifest(_encode(_manifest_data()))
    receipt = parse_receipt(_encode(_receipt_data(manifest)))
    with pytest.raises(ValueError, match="^invalid_manifest$"):
        manifest_digest({})
    with pytest.raises(ValueError, match="^invalid_manifest$"):
        match_receipt(None, receipt)
    with pytest.raises(ValueError, match="^invalid_receipt$"):
        match_receipt(manifest, {})


def test_models_and_nested_artifacts_are_frozen():
    manifest = parse_manifest(_encode(_manifest_data()))
    receipt = parse_receipt(_encode(_receipt_data(manifest)))
    for model, field, value in [
        (manifest, "delivery_id", "other"),
        (receipt, "indexing_state", "pending"),
        (manifest.artifacts[0], "size_bytes", 99),
    ]:
        with pytest.raises(ValidationError, match="frozen_instance"):
            setattr(model, field, value)
