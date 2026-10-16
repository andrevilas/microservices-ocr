"""Template HTTP exchanges over real ASGI/SQLite, with explicit principal overrides.

These tests exercise authorization policy through Harness, not credential authentication.
"""
import json

import pytest

from app.application import Principal
from app.context_templates import ContextTemplateList, TemplateCloneReceipt
from test_catalog_http import OWNER, FULL, _sqlite_snapshot, api

LIST = "/context-templates"
CLONE = "/context-templates/{template_id}/clone"
READ_WRITE = frozenset({"contexts:read", "contexts:write"})


def templates(h):
    response, body = h.call("get", LIST, LIST, 200)
    ContextTemplateList.model_validate_json(response.text)
    return body["items"]


def clone_body(template):
    return {"expected_version": template["template_version"],
            "expected_sha256": template["template_sha256"]}


def clone_path(template):
    return f"/context-templates/{template['template_id']}/clone"


def clone(h, template, key, status=201):
    response, receipt = h.call("post", CLONE, clone_path(template), status,
                               key=key, json=clone_body(template))
    TemplateCloneReceipt.model_validate_json(response.text)
    assert response.headers["idempotency-replayed"] == ("true" if status == 200 else "false")
    assert (receipt["template_id"], receipt["template_version"], receipt["template_sha256"]) == (
        template["template_id"], template["template_version"], template["template_sha256"])
    assert receipt["context_revision"] == 1
    assert all(item["revision"] == 1 for item in receipt["types"])
    return response, receipt


def read_copy(h, template, receipt):
    cid = receipt["context_id"]
    _, context = h.call("get", "/contexts/{context_id}", f"/contexts/{cid}", 200)
    assert context["revision"] == 1 and context["body"] == template["context"]
    _, page = h.call("get", "/contexts/{context_id}/types", f"/contexts/{cid}/types", 200)
    assert {item["type_id"] for item in page["items"]} == {item["type_id"] for item in receipt["types"]}
    assert len(receipt["types"]) == len(template["types"]) == 1
    expected = {item["type_key"]: item["body"] for item in template["types"]}
    for item in receipt["types"]:
        tid = item["type_id"]
        _, record = h.call("get", "/document-types/{type_id}", f"/document-types/{tid}", 200)
        assert record["context_id"] == cid
        assert record["revision"] == 1 and record["body"] == expected[item["type_key"]]
        _, versions = h.call("get", "/document-types/{type_id}/versions", f"/document-types/{tid}/versions", 200)
        assert versions == {"items": [], "next_cursor": None}


def different_hash(template):
    return "0" * 64 if template["template_sha256"] != "0" * 64 else "1" * 64


def test_global_listing_is_complete_stable_and_never_seeds(api):
    h = api()
    assert h.call("get", "/contexts", "/contexts", 200)[1]["items"] == []
    before = _sqlite_snapshot(h)
    original = templates(h)
    assert len(original) == len({item["template_id"] for item in original}) == 3
    for who in (OWNER, Principal("user:2", 2, FULL), Principal("user:3", 3, FULL, is_admin=True)):
        h.who = who
        assert templates(h) == original
        assert templates(h) == original
        assert h.call("get", "/contexts", "/contexts", 200)[1]["items"] == []
    h.who = Principal("user:1", 1, frozenset({"contexts:write"}))
    response, error = h.call("get", LIST, LIST, 403)
    assert error["error"]["code"] == "insufficient_scope"
    assert _sqlite_snapshot(h) == before


def test_clone_every_template_and_replay_after_edits_and_publication(api):
    h = api()
    catalog = templates(h)
    copies = []
    for index, template in enumerate(catalog):
        response, receipt = clone(h, template, f"template-clone-{index:04d}")
        read_copy(h, template, receipt)
        replay_response, replay = clone(h, template, f"template-clone-{index:04d}", 200)
        assert replay == receipt and replay_response.content == response.content
        copies.append((response, receipt))
    assert len({receipt["context_id"] for _, receipt in copies}) == 3
    assert len({item["type_id"] for _, receipt in copies for item in receipt["types"]}) == 3

    template = catalog[0]
    original_response, receipt = copies[0]
    _, another = clone(h, template, "template-clone-new-copy")
    assert another["context_id"] != receipt["context_id"]
    assert another["types"][0]["type_id"] != receipt["types"][0]["type_id"]
    read_copy(h, template, another)
    cid, tid = receipt["context_id"], receipt["types"][0]["type_id"]
    context_body = {**template["context"], "name": "Edited template context"}
    type_body = {**template["types"][0]["body"], "name": "Edited template type"}
    _, edited_context = h.call("put", "/contexts/{context_id}", f"/contexts/{cid}", 200,
                               json={"body": context_body, "expected_revision": 1})
    _, edited_type = h.call("put", "/document-types/{type_id}", f"/document-types/{tid}", 200,
                            json={"body": type_body, "expected_revision": 1})
    assert edited_context["revision"] == edited_type["revision"] == 2
    h.call("post", "/document-types/{type_id}/versions", f"/document-types/{tid}/versions", 201,
           key="template-edited-publish", json={"expected_revision": 2, "expected_context_revision": 2})
    before = _sqlite_snapshot(h)
    response, replay = clone(h, template, "template-clone-0000", 200)
    assert replay == receipt and response.content == original_response.content
    assert _sqlite_snapshot(h) == before
    assert h.call("get", "/contexts/{context_id}", f"/contexts/{cid}", 200)[1] == edited_context
    assert h.call("get", "/document-types/{type_id}", f"/document-types/{tid}", 200)[1] == edited_type
    assert templates(h) == catalog


def test_template_conflicts_unknown_ids_and_invalid_requests_do_not_write(api):
    h = api()
    template = templates(h)[0]
    body, path = clone_body(template), clone_path(template)
    clone(h, template, "template-conflict-used")
    before = _sqlite_snapshot(h)
    changed = ({**body, "expected_version": body["expected_version"] + 1},
               {**body, "expected_sha256": different_hash(template)})
    for payload in changed:
        _, error = h.call("post", CLONE, path, 409, key="template-conflict-used", json=payload)
        assert error["error"]["code"] == "idempotency_conflict"
        response, error = h.call("post", CLONE, path, 409, key="template-conflict-new", json=payload)
        assert error["error"]["code"] != "idempotency_conflict"
        assert "PRIVATE" not in response.text
    _, error = h.call("post", CLONE, "/context-templates/unknown-template/clone", 404,
                      key="template-unknown-id", json=body)
    assert error["error"]["code"] == "context_template_not_found"
    invalid = [{}, {**body, "owner": "PRIVATE-SENTINEL"}]
    invalid += [{**body, "expected_version": value} for value in (True, "1", 0, -1, None)]
    invalid += [{**body, "expected_sha256": value} for value in ("A" * 64, "0" * 63, True, None)]
    for payload in invalid:
        response, error = h.call("post", CLONE, path, 422, key="template-invalid-body", json=payload)
        assert error["error"]["code"] == "validation_error"
        assert "PRIVATE" not in response.text
    for identifier in ("Upper", "bad_id", "-bad", "bad--id", "a" * 65):
        h.call("post", CLONE, f"/context-templates/{identifier}/clone", 422,
               key="template-invalid-id", json=body)
    for headers in ({}, {"Idempotency-Key": "short"}, {"Idempotency-Key": "k" * 129}):
        response = h.client.post("/api/v1" + path, headers=headers, json=body)
        h.check(response, CLONE, "post", 422)
    for content, media, status in ((b'{"PRIVATE-SENTINEL":', "application/json", 422),
                                   (b"PRIVATE-SENTINEL", "text/plain", 415)):
        response = h.client.post("/api/v1" + path, content=content,
                                 headers={"Content-Type": media, "Idempotency-Key": "template-invalid-json"})
        h.check(response, CLONE, "post", status)
        assert "PRIVATE" not in response.text
    assert _sqlite_snapshot(h) == before


def test_clone_scope_checks_precede_body_read_and_stream_limit_precedes_decode(api):
    h = api()
    template = templates(h)[0]
    path = "/api/v1" + clone_path(template)
    before = _sqlite_snapshot(h)
    headers = {"Content-Type": "application/json", "Idempotency-Key": "template-denied-body"}
    for scopes in (frozenset({"contexts:read"}), frozenset({"contexts:write"}),
                   frozenset({"contexts:publish"})):
        h.who = Principal("user:1", 1, scopes)
        for payload in (b'{"PRIVATE-SENTINEL":', b"PRIVATE-SENTINEL" + b" " * (2 * 1024 * 1024)):
            response = h.client.post(path, content=payload, headers=headers)
            assert h.check(response, CLONE, "post", 403)["error"]["code"] == "insufficient_scope"
            assert "PRIVATE" not in response.text
    assert _sqlite_snapshot(h) == before
    h.who = Principal("user:1", 1, READ_WRITE)

    def oversized():
        yield b"PRIVATE-SENTINEL"
        for _ in range(2):
            yield b" " * (1024 * 1024)

    response = h.client.post(path, content=oversized(), headers=headers)
    assert h.check(response, CLONE, "post", 413)["error"]["code"] == "request_too_large"
    assert "PRIVATE" not in response.text
    assert _sqlite_snapshot(h) == before
    # Header matching is case-insensitive; publish is not needed for a clone.
    response = h.client.post(path, json=clone_body(template), headers={"idempotency-key": "template-read-write"})
    receipt = h.check(response, CLONE, "post", 201)
    TemplateCloneReceipt.model_validate_json(response.text)
    assert response.headers["idempotency-replayed"] == "false"
    read_copy(h, template, receipt)
    tid = receipt["types"][0]["type_id"]
    before = _sqlite_snapshot(h)
    _, error = h.call("post", "/document-types/{type_id}/versions", f"/document-types/{tid}/versions", 403,
                      key="template-denied-publish", json={"expected_revision": 1, "expected_context_revision": 1})
    assert error["error"]["code"] == "insufficient_scope"
    assert _sqlite_snapshot(h) == before


@pytest.mark.parametrize("is_admin", [False, True])
def test_global_templates_clone_into_each_owner_without_admin_bypass(api, is_admin):
    h = api()
    template = templates(h)[0]
    _, first = clone(h, template, "template-owner-shared-key")
    h.who = Principal("user:2", 2, READ_WRITE, is_admin=is_admin)
    assert templates(h)[0] == template
    _, second = clone(h, template, "template-owner-shared-key")
    assert first["context_id"] != second["context_id"]
    assert first["types"][0]["type_id"] != second["types"][0]["type_id"]
    read_copy(h, template, second)
    _, error = h.call("get", "/contexts/{context_id}", f"/contexts/{first['context_id']}", 404)
    assert error["error"]["code"] == "context_not_found"
    _, error = h.call("get", "/document-types/{type_id}", f"/document-types/{first['types'][0]['type_id']}", 404)
    assert error["error"]["code"] == "type_not_found"
    h.who = OWNER
    read_copy(h, template, first)
    h.call("get", "/contexts/{context_id}", f"/contexts/{second['context_id']}", 404)
    h.call("get", "/document-types/{type_id}", f"/document-types/{second['types'][0]['type_id']}", 404)


def test_template_clone_uses_only_the_injected_store(api):
    first, second = api("first"), api("second")
    template = templates(first)[0]
    assert templates(second)[0] == template
    before = _sqlite_snapshot(second)
    _, receipt = clone(first, template, "template-isolated-store")
    read_copy(first, template, receipt)
    assert second.call("get", "/contexts", "/contexts", 200)[1]["items"] == []
    second.call("get", "/contexts/{context_id}", f"/contexts/{receipt['context_id']}", 404)
    second.call("get", "/document-types/{type_id}", f"/document-types/{receipt['types'][0]['type_id']}", 404)
    assert _sqlite_snapshot(second) == before
