"""Pure write mapping and bounded encoding, plus synthetic HTTP integration."""
import json
import os

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app.document_mcp_http import document_body, document_request
from app.metadata_models import (
    MAX_REQUEST_BYTES, ApprovalInput, ApproveMetadataReceipt,
    SaveMetadataInput, SaveMetadataReceipt,
)
from app.security import require_principal
from test_document_http_basic import http
from test_document_metadata import env, persisted, who
from test_document_metadata_approval import ready

J, R, S, H, KEY = "a" * 32, "b" * 32, "c" * 32, "d" * 64, "mapper-write-0001"
CASES = [
    ("save_document_metadata", "PUT", "/metadata", SaveMetadataInput),
    ("approve_document_metadata", "POST", "/metadata/approve", ApprovalInput),
]


def payload(model):
    if model is ApprovalInput:
        return dict(revision_id=R, expected_head=R, expected_sha256=H,
                    reviewed_fields=[dict(segment_id=S, field_name="title")])
    return dict(expected_head=None, expected_sha256=None, segments=[dict(
        client_key="main", page_start=1, page_end=1, type_id=R, type_version=1,
        fields=[dict(field_name="title", raw=[0, False, "é🚀\n", None][:3]),
                dict(field_name="missing", raw=None)],
    )])


@pytest.mark.parametrize("op,method,suffix,model", CASES)
@pytest.mark.parametrize("typed", [False, True])
def test_write_mapping(op, method, suffix, model, typed):
    source = payload(model)
    dto = model.model_validate(source, strict=True)
    request = dto if typed else source
    result = document_request(op, (J, request, KEY))
    assert result == (method, f"/jobs/{J}" + suffix, {},
                      dto.model_dump(mode="json", by_alias=True, exclude_none=False),
                      {"Idempotency-Key": KEY})
    encoded = document_body(result[3])
    assert type(encoded) is bytes and json.loads(encoded) == result[3]
    if model is SaveMetadataInput:
        assert result[3]["expected_head"] is result[3]["expected_sha256"] is None
        fields = json.loads(encoded)["segments"][0]["fields"]
        assert type(fields[0]["raw"][0]) is int and fields[0]["raw"][0] == 0
        assert fields[0]["raw"][1] is False and fields[1]["raw"] is None
        assert fields[0]["origin"] == "manual" and fields[0]["evidence"] == []
        source["segments"][0]["fields"][0]["raw"].append("changed")
        assert len(result[3]["segments"][0]["fields"][0]["raw"]) == 3


@pytest.mark.parametrize("op,method,suffix,model", CASES)
@pytest.mark.parametrize("case", ["job", "key", "body", "missing", "extra", "container"])
def test_invalid_arguments(op, method, suffix, model, case):
    args = [J, payload(model), KEY]
    if case == "job": args[0] = "../PRIVATE"
    if case == "key": args[2] = "PRIVATE\r\nInjected: yes"
    if case == "body": args[1] = {"PRIVATE": "secret"}
    if case == "missing": args.pop()
    if case == "extra": args.append("PRIVATE")
    if case == "container": args = {"PRIVATE": args}
    with pytest.raises(ToolError) as caught:
        document_request(op, args)
    assert str(caught.value) == "validation_error: Invalid document request."


@pytest.mark.parametrize("key", [None, True, 12345678, "short", "x" * 129, "abcdefgh\n", "abcdefgh "])
def test_strict_keys(key):
    with pytest.raises(ToolError, match="^validation_error:"):
        document_request(CASES[0][0], [J, payload(SaveMetadataInput), key])


@pytest.mark.parametrize("op,method,suffix,model", CASES)
@pytest.mark.parametrize("mode", ["constructed", "mutated", "top"])
def test_unvalidated_models_are_rejected(op, method, suffix, model, mode):
    dto = model.model_validate(payload(model), strict=True)
    if mode == "top":
        dto = model.model_construct(**{**dto.model_dump(), "expected_head": "PRIVATE"})
    else:
        nested = dto.segments[0].fields if model is SaveMetadataInput else dto.reviewed_fields
        name, bad = ("raw", 1.5) if model is SaveMetadataInput else ("field_name", "../PRIVATE")
        if mode == "constructed":
            nested[0] = type(nested[0]).model_construct(**{**nested[0].model_dump(), name: bad})
        else:
            object.__setattr__(nested[0], name, bad)
    with pytest.raises(ToolError) as caught:
        document_request(op, [J, dto, KEY])
    assert str(caught.value) == "validation_error: Invalid document request."


def test_encoder_boundaries_and_unicode():
    assert document_body(None) is None
    sample = {"text": "é🚀\n\t\u0000", "zero": 0, "false": False, "null": None}
    encoded = document_body(sample)
    assert json.loads(encoded) == sample and "é🚀".encode() in encoded
    assert b"\\n\\t\\u0000" in encoded
    assert len(document_body({"x": "a" * (MAX_REQUEST_BYTES - 8)})) == MAX_REQUEST_BYTES
    assert len(document_body({"x": "é" * ((MAX_REQUEST_BYTES - 8) // 2)})) == MAX_REQUEST_BYTES
    with pytest.raises(ToolError, match="^request_too_large:"):
        document_body({"x": "a" * (MAX_REQUEST_BYTES - 7)})
    with pytest.raises(ToolError, match="^request_too_large:"):
        document_body({"x": "é" * ((MAX_REQUEST_BYTES - 8) // 2) + "a"})


@pytest.mark.parametrize("bad", [{"x": "\ud800PRIVATE"}, {"x": object()}, {"x": float("nan")}, "PRIVATE"])
def test_invalid_encoding_is_sanitized(bad):
    with pytest.raises(ToolError) as caught:
        document_body(bad)
    assert str(caught.value) == "invalid_request: Invalid document body."


def test_cycles_and_recursion_are_sanitized():
    cyclic = {}; cyclic["self"] = cyclic
    deep = {}
    for _ in range(2000): deep = {"child": deep}
    for bad in (cyclic, deep):
        with pytest.raises(ToolError, match="^invalid_request:"):
            document_body(bad)


@pytest.mark.skipif(os.name != "posix", reason="MetadataFiles requires POSIX")
def test_real_http_writes(env, http, ready):
    e, app, client, services = http
    assert e is env is ready
    snapshot = persisted(e, e.draft).model_dump(mode="json", by_alias=True)
    request = dict(expected_head=e.draft.revision_id, expected_sha256=e.draft.sha256, segments=[])
    for index, segment in enumerate(snapshot["segments"]):
        entry = {key: segment[key] for key in (
            "segment_id", "page_start", "page_end", "type_id", "type_version")}
        entry.update(client_key=f"s{index}", fields=[
            {key: field[key] for key in ("field_name", "raw", "evidence")}
            for field in segment["fields"]])
        request["segments"].append(entry)

    def send(op, body, key=KEY):
        method, path, query, mapped, headers = document_request(op, [e.job.job_id, body, key])
        return client.request(method, "/api/v1" + path, params=query,
                              content=document_body(mapped),
                              headers={**headers, "Content-Type": "application/json"})

    first = send(CASES[0][0], request)
    assert first.status_code == 201, first.text
    saved = SaveMetadataReceipt.model_validate_json(first.content)
    replay = send(CASES[0][0], request)
    assert replay.status_code == 200 and replay.json() == first.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    approval = {**e.approval_body, "revision_id": saved.revision_id,
                "expected_head": saved.revision_id, "expected_sha256": saved.sha256}
    approver = who(scopes={"jobs:read", "artifacts:read", "metadata:read", "metadata:approve"})
    app.dependency_overrides[require_principal] = lambda: approver
    approved = send(CASES[1][0], approval, "mapper-approve-0001")
    assert approved.status_code == 201, approved.text
    receipt = ApproveMetadataReceipt.model_validate_json(approved.content)
    assert receipt.reviewed_revision_id == saved.revision_id and receipt.reviewed_sha256 == saved.sha256
    assert persisted(e, receipt).parent_revision_id == saved.revision_id
    replay = send(CASES[1][0], approval, "mapper-approve-0001")
    assert replay.status_code == 200 and replay.json() == approved.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    app.dependency_overrides[require_principal] = lambda: e.owner
    historical = send(CASES[0][0], request)
    assert historical.status_code == 200 and historical.json() == first.json()
    assert historical.headers["Idempotency-Replayed"] == "true"
    assert send(CASES[0][0], request, "mapper-stale-0001").status_code == 409
    head = client.get(f"/api/v1/jobs/{e.job.job_id}/metadata")
    assert head.status_code == 200 and head.json()["revision_id"] == receipt.revision_id
    assert not e.jobs._artifact_leases
