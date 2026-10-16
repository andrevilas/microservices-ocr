"""Catalog HTTP routes over the real ASGI app and real SQLite stores (no service mocks on the happy path)."""
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from app.application import JobApplication, Principal
from app.catalog_models import ContextInput, TypeInput
from app.config import settings
from app.export_openapi import v1_schema
from app.main import create_app
from app.security import require_principal
from app.services.auth_service import create_token
from app.services.catalog_runtime import CatalogProvider
from app.services.storage_service import JobStore
from app.services.user_store import get_user_store

FULL = frozenset({"contexts:read", "contexts:write", "contexts:publish"})
OWNER = Principal("user:1", 1, FULL)
CTX = ContextInput.model_config["json_schema_extra"]["examples"][0]
TYP = TypeInput.model_config["json_schema_extra"]["examples"][0]
CTX2 = {**CTX, "name": "Contratos revisados",
        "common_fields": [*CTX["common_fields"], {"name": "data", "type": "date", "description": "Data."}]}
TYP2 = {**TYP, "name": "Contrato revisado"}
OPERATIONS = {
    "ocr_list_contexts": ("get", "/api/v1/contexts", ["contexts:read"]),
    "ocr_create_context": ("post", "/api/v1/contexts", ["contexts:read", "contexts:write"]),
    "ocr_get_context": ("get", "/api/v1/contexts/{context_id}", ["contexts:read"]),
    "ocr_update_context": ("put", "/api/v1/contexts/{context_id}", ["contexts:read", "contexts:write"]),
    "ocr_list_document_types": ("get", "/api/v1/contexts/{context_id}/types", ["contexts:read"]),
    "ocr_create_document_type": ("post", "/api/v1/contexts/{context_id}/types", ["contexts:read", "contexts:write"]),
    "ocr_get_document_type": ("get", "/api/v1/document-types/{type_id}", ["contexts:read"]),
    "ocr_update_document_type": ("put", "/api/v1/document-types/{type_id}", ["contexts:read", "contexts:write"]),
    "ocr_publish_document_type": ("post", "/api/v1/document-types/{type_id}/versions", ["contexts:read", "contexts:publish"]),
    "ocr_list_document_type_versions": ("get", "/api/v1/document-types/{type_id}/versions", ["contexts:read"]),
    "ocr_get_document_type_version": ("get", "/api/v1/document-types/{type_id}/versions/{version}", ["contexts:read"]),
    "ocr_deprecate_document_type_version": ("post", "/api/v1/document-types/{type_id}/versions/{version}/deprecate", ["contexts:read", "contexts:publish"]),
}
DEPRECATE = "/document-types/{type_id}/versions/{version}/deprecate"


class Harness:
    """One app + one store under root; open() again simulates a restart on the same files."""
    def __init__(self, root):
        self.root, self.who = root, OWNER
        self.open()

    def open(self):
        self.store = JobStore(root_dir=self.root)
        self.app = create_app(lambda: JobApplication(self.store))
        self.app.dependency_overrides[require_principal] = lambda: self.who
        self.client = TestClient(self.app)
        self.schema = v1_schema(self.app)

    def close(self):
        self.client.close()
        self.store._conn.close()

    def check(self, response, template, method, status=None):
        """Validate the real response (status, schema, headers) against the published OpenAPI."""
        assert response.status_code == (status or response.status_code)
        spec = self.schema["paths"]["/api/v1" + template][method]["responses"][str(response.status_code)]
        content = spec["content"][response.headers["content-type"].split(";")[0]]
        Draft202012Validator({"components": self.schema["components"], **content["schema"]}).validate(response.json())
        assert response.headers["cache-control"] == "no-store"
        assert len(response.headers["x-request-id"]) == 32
        return response.json()

    def call(self, method, template, path, status, *, key=None, **kwargs):
        headers = {"Idempotency-Key": key} if key else {}
        response = self.client.request(method, "/api/v1" + path, headers=headers, **kwargs)
        return response, self.check(response, template, method, status)


@pytest.fixture
def api(tmp_path):
    harnesses = []

    def make(name="a"):
        harnesses.append(Harness(tmp_path / name))
        return harnesses[-1]
    make.all = harnesses
    yield make
    for harness in harnesses:
        harness.close()


def make_catalog(h):
    """Context rev1 -> type rev1; returns ids."""
    _, ctx = h.call("post", "/contexts", "/contexts", 201, key="create-context-1", json=CTX)
    _, typ = h.call("post", "/contexts/{context_id}/types", f"/contexts/{ctx['context_id']}/types", 201,
                    key="create-type-0001", json=TYP)
    return ctx["context_id"], typ["type_id"]


def test_full_lifecycle_replays_snapshots_and_persistence(api):
    h = api()
    r, ctx = h.call("post", "/contexts", "/contexts", 201, key="create-context-1", json=CTX)
    assert r.headers["idempotency-replayed"] == "false"
    cid = ctx["context_id"]
    _, typ = h.call("post", "/contexts/{context_id}/types", f"/contexts/{cid}/types", 201, key="create-type-0001", json=TYP)
    tid = typ["type_id"]
    _, current = h.call("get", "/document-types/{type_id}", f"/document-types/{tid}", 200)
    assert current["revision"] == 1 and current["body"]["name"] == TYP["name"]
    _, updated = h.call("put", "/document-types/{type_id}", f"/document-types/{tid}", 200,
                        json={"body": TYP2, "expected_revision": 1})
    assert updated["revision"] == 2 and updated["body"]["name"] == TYP2["name"]
    _, stale = h.call("put", "/document-types/{type_id}", f"/document-types/{tid}", 409,
                      json={"body": TYP, "expected_revision": 1})
    assert stale["error"]["code"] == "type_revision_conflict"

    publish = {"expected_revision": 2, "expected_context_revision": 1}
    r, first = h.call("post", "/document-types/{type_id}/versions", f"/document-types/{tid}/versions", 201,
                      key="publish-0000001", json=publish)
    assert r.headers["idempotency-replayed"] == "false"
    assert first["version"] == 1 and "schema" not in first and "schema_document" not in first
    _, version = h.call("get", "/document-types/{type_id}/versions/{version}", f"/document-types/{tid}/versions/1", 200)
    assert "schema" in version and "schema_document" not in version
    canonical = json.dumps(version["schema"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert hashlib.sha256(canonical).hexdigest() == version["schema_sha256"] == first["schema_sha256"]

    # Editing the context afterwards never touches the published snapshot or its hash.
    _, edited = h.call("put", "/contexts/{context_id}", f"/contexts/{cid}", 200, json={"body": CTX2, "expected_revision": 1})
    assert edited["revision"] == 2 and edited["body"]["name"] == CTX2["name"]
    _, again = h.call("get", "/document-types/{type_id}/versions/{version}", f"/document-types/{tid}/versions/1", 200)
    assert again == version and again["context_name"] == CTX["name"]

    # Replays return the original body (200) even after later edits; conflicting reuse is 409.
    r, replay = h.call("post", "/document-types/{type_id}/versions", f"/document-types/{tid}/versions", 200,
                       key="publish-0000001", json=publish)
    assert replay == first and r.headers["idempotency-replayed"] == "true"
    _, conflict = h.call("post", "/document-types/{type_id}/versions", f"/document-types/{tid}/versions", 409,
                         key="publish-0000001", json={**publish, "expected_context_revision": 2})
    assert conflict["error"]["code"] == "idempotency_conflict"
    r, ctx_replay = h.call("post", "/contexts", "/contexts", 200, key="create-context-1", json=CTX)
    assert ctx_replay == ctx and r.headers["idempotency-replayed"] == "true"
    _, conflict = h.call("post", "/contexts", "/contexts", 409, key="create-context-1", json=CTX2)
    assert conflict["error"]["code"] == "idempotency_conflict"
    r, type_replay = h.call("post", "/contexts/{context_id}/types", f"/contexts/{cid}/types", 200, key="create-type-0001", json=TYP)
    assert type_replay == typ and r.headers["idempotency-replayed"] == "true"

    _, stale_type = h.call("post", "/document-types/{type_id}/versions", f"/document-types/{tid}/versions", 409,
                           key="publish-0000002", json={"expected_revision": 1, "expected_context_revision": 2})
    assert stale_type["error"]["code"] == "type_revision_conflict"
    _, stale_ctx = h.call("post", "/document-types/{type_id}/versions", f"/document-types/{tid}/versions", 409,
                          key="publish-0000003", json={"expected_revision": 2, "expected_context_revision": 1})
    assert stale_ctx["error"]["code"] == "context_revision_conflict"
    _, second = h.call("post", "/document-types/{type_id}/versions", f"/document-types/{tid}/versions", 201,
                       key="publish-0000004", json={"expected_revision": 2, "expected_context_revision": 2})
    assert second["version"] == 2 and second["context_name"] == CTX2["name"]
    assert second["schema_sha256"] != first["schema_sha256"]

    # Listings carry summaries only.
    _, versions = h.call("get", "/document-types/{type_id}/versions", f"/document-types/{tid}/versions?limit=1", 200)
    assert [v["version"] for v in versions["items"]] == [1] and versions["next_cursor"] == 1
    assert all("schema" not in item for item in versions["items"])
    _, versions = h.call("get", "/document-types/{type_id}/versions", f"/document-types/{tid}/versions?after=1", 200)
    assert [v["version"] for v in versions["items"]] == [2] and versions["next_cursor"] is None
    _, contexts = h.call("get", "/contexts", "/contexts", 200)
    assert [c["context_id"] for c in contexts["items"]] == [cid] and "common_fields" not in contexts["items"][0]
    _, types = h.call("get", "/contexts/{context_id}/types", f"/contexts/{cid}/types", 200)
    assert [t["type_id"] for t in types["items"]] == [tid] and "specific_fields" not in types["items"][0]

    h.close()
    h.open()  # restart on the same files
    _, reloaded = h.call("get", "/document-types/{type_id}/versions/{version}", f"/document-types/{tid}/versions/1", 200)
    assert reloaded == version
    _, context = h.call("get", "/contexts/{context_id}", f"/contexts/{cid}", 200)
    assert context["revision"] == 2 and context["body"]["name"] == CTX2["name"]


def _sqlite_snapshot(h):
    """Capture logical rows in the real stores, including persisted idempotency records."""
    snapshot = {}
    for path in sorted(h.root.rglob("*")):
        if not path.is_file():
            continue
        with path.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                continue
        connection = sqlite3.connect(path)
        try:
            for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
                quoted = '"' + table.replace('"', '""') + '"'
                cursor = connection.execute(f"SELECT * FROM {quoted}")
                snapshot[(str(path.relative_to(h.root)), table)] = (
                    tuple(column[0] for column in cursor.description),
                    tuple(sorted(cursor.fetchall(), key=repr)))
        finally:
            connection.close()
    assert snapshot
    return snapshot


def _immutable_rows(snapshot, canonical):
    rows = {(table, row) for table, (_, records) in snapshot.items() for row in records
            if canonical in row or canonical.decode("utf-8") in row}
    assert rows, "The stored canonical envelope must be present in the real SQLite snapshot."
    return rows


def _published_catalog(h):
    cid, tid = make_catalog(h)
    _, summary = h.call("post", "/document-types/{type_id}/versions", f"/document-types/{tid}/versions", 201,
                        key="deprecate-publish-1", json={"expected_revision": 1, "expected_context_revision": 1})
    _, record = h.call("get", "/document-types/{type_id}/versions/{version}", f"/document-types/{tid}/versions/1", 200)
    assert record["lifecycle"] == {"state": "published", "revision": 1, "changed_at": summary["created_at"]}
    return cid, tid, summary, record


def test_deprecation_receipt_lifecycle_cas_and_persistence(api):
    h = api()
    cid, tid, published, original = _published_catalog(h)
    base = f"/document-types/{tid}/versions"
    path = base + "/1/deprecate"
    body = {"expected_lifecycle_revision": 1}
    canonical = json.dumps(original["schema"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert hashlib.sha256(canonical).hexdigest() == published["schema_sha256"]
    immutable = _immutable_rows(_sqlite_snapshot(h), canonical)

    response, receipt = h.call("post", DEPRECATE, path, 200, key="deprecate-key-001", json=body)
    assert response.headers["idempotency-replayed"] == "false"
    assert set(receipt) == {"type_id", "version", "schema_sha256", "lifecycle"}
    assert (receipt["type_id"], receipt["version"], receipt["schema_sha256"]) == (tid, 1, published["schema_sha256"])
    assert receipt["lifecycle"]["state"] == "deprecated" and receipt["lifecycle"]["revision"] == 2
    assert receipt["lifecycle"]["changed_at"].endswith(("Z", "+00:00"))
    _, current = h.call("get", "/document-types/{type_id}/versions/{version}", base + "/1", 200)
    assert current == {**original, "lifecycle": receipt["lifecycle"]}
    _, page = h.call("get", "/document-types/{type_id}/versions", base, 200)
    assert page == {"items": [{**published, "lifecycle": receipt["lifecycle"]}], "next_cursor": None}
    assert _immutable_rows(_sqlite_snapshot(h), canonical) == immutable

    persisted = _sqlite_snapshot(h)
    for key, revision, code in (
        ("deprecate-key-002", 1, "type_lifecycle_revision_conflict"),
        ("deprecate-key-003", 2, "type_version_already_deprecated"),
        ("deprecate-key-001", 2, "idempotency_conflict"),
    ):
        _, error = h.call("post", DEPRECATE, path, 409, key=key,
                          json={"expected_lifecycle_revision": revision})
        assert error["error"]["code"] == code
        assert _sqlite_snapshot(h) == persisted

    h.close()
    h.open()
    h.call("put", "/contexts/{context_id}", f"/contexts/{cid}", 200,
           json={"body": CTX2, "expected_revision": 1})
    h.call("put", "/document-types/{type_id}", f"/document-types/{tid}", 200,
           json={"body": TYP2, "expected_revision": 1})
    persisted = _sqlite_snapshot(h)
    response, replay = h.call("post", DEPRECATE, path, 200, key="deprecate-key-001", json=body)
    assert replay == receipt and response.headers["idempotency-replayed"] == "true"
    response, historical = h.call("post", "/document-types/{type_id}/versions", base, 200,
                                  key="deprecate-publish-1", json={"expected_revision": 1, "expected_context_revision": 1})
    assert historical == published and "lifecycle" not in historical
    assert response.headers["idempotency-replayed"] == "true"
    assert _sqlite_snapshot(h) == persisted
    assert _immutable_rows(persisted, canonical) == immutable
    _, reloaded = h.call("get", "/document-types/{type_id}/versions/{version}", base + "/1", 200)
    assert reloaded == current
    _, second = h.call("post", "/document-types/{type_id}/versions", base, 201,
                       key="deprecate-publish-2", json={"expected_revision": 2, "expected_context_revision": 2})
    assert second["version"] == 2
    _, second_view = h.call("get", "/document-types/{type_id}/versions/{version}", base + "/2", 200)
    assert second_view["lifecycle"] == {"state": "published", "revision": 1, "changed_at": second["created_at"]}


def test_deprecation_denied_scopes_precede_body_parsing(api):
    h = api()
    _, tid, _, _ = _published_catalog(h)
    persisted = _sqlite_snapshot(h)
    for who in (
        Principal("user:1", 1, frozenset({"contexts:read", "contexts:write"})),
        Principal("user:1", 1, frozenset({"contexts:publish"})),
        Principal("service:limited", None, frozenset({"jobs:read"})),
    ):
        h.who = who
        for payload in (b'{"PRIVATE-SENTINEL":', b" " * (2 * 1024 * 1024 + 1)):
            response = h.client.post(f"/api/v1/document-types/{tid}/versions/1/deprecate", content=payload,
                                     headers={"Content-Type": "application/json", "Idempotency-Key": "denied-deprecate-1"})
            assert h.check(response, DEPRECATE, "post", 403)["error"]["code"] == "insufficient_scope"
            assert "PRIVATE" not in response.text
    assert _sqlite_snapshot(h) == persisted


def test_deprecation_input_and_version_boundaries(api):
    h = api()
    _, tid, _, _ = _published_catalog(h)
    path = f"/document-types/{tid}/versions/1/deprecate"
    body = {"expected_lifecycle_revision": 1}
    persisted = _sqlite_snapshot(h)
    invalid = [{"expected_lifecycle_revision": value} for value in (True, "1", 0, -1, 2**31, None)]
    invalid += [{**body, name: "PRIVATE-SENTINEL"} for name in ("owner", "state", "reason")]
    invalid.append({})
    for payload in invalid:
        response, _ = h.call("post", DEPRECATE, path, 422, key="invalid-deprecate-1", json=payload)
        assert "PRIVATE" not in response.text
    for headers in ({}, {"Idempotency-Key": "short"}):
        h.check(h.client.post("/api/v1" + path, json=body, headers=headers), DEPRECATE, "post", 422)
    for content, media, status in ((b'{"PRIVATE-SENTINEL":', "application/json", 422),
                                   (b"PRIVATE-SENTINEL", "text/plain", 415)):
        response = h.client.post("/api/v1" + path, content=content,
                                 headers={"Content-Type": media, "Idempotency-Key": "invalid-deprecate-1"})
        h.check(response, DEPRECATE, "post", status)
        assert "PRIVATE" not in response.text

    def oversized():
        yield b"PRIVATE-SENTINEL"
        for _ in range(2):
            yield b" " * (1024 * 1024)
    response = h.client.post("/api/v1" + path, content=oversized(),
                             headers={"Content-Type": "application/json", "Idempotency-Key": "invalid-deprecate-1"})
    assert h.check(response, DEPRECATE, "post", 413)["error"]["code"] == "request_too_large"
    assert "PRIVATE" not in response.text
    for version in ("latest", "0", "-1", str(2**63)):
        h.call("post", DEPRECATE, f"/document-types/{tid}/versions/{version}/deprecate", 422,
               key="invalid-deprecate-1", json=body)
    for missing_type, version in ((tid, 2), ("0" * 32, 1)):
        _, error = h.call("post", DEPRECATE, f"/document-types/{missing_type}/versions/{version}/deprecate", 404,
                          key="missing-deprecate-1", json=body)
        assert error["error"]["code"] == "type_version_not_found"
    assert _sqlite_snapshot(h) == persisted


def test_deprecation_corrupt_snapshot_is_sanitized_and_does_not_write(api, caplog):
    h = api()
    _, tid, _, record = _published_catalog(h)
    canonical = json.dumps(record["schema"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    clean = _sqlite_snapshot(h)
    locations = [(database, table, columns[index], value)
                 for (database, table), (columns, rows) in clean.items() for row in rows
                 for index, value in enumerate(row) if value == canonical or value == canonical.decode("utf-8")]
    assert len(locations) == 1
    database, table, column, original = locations[0]
    quote = lambda name: '"' + name.replace('"', '""') + '"'
    statement = f"UPDATE {quote(table)} SET {quote(column)}=? WHERE {quote(column)}=?"
    sentinel = b"PRIVATE-SENTINEL" if isinstance(original, bytes) else "PRIVATE-SENTINEL"
    connection = sqlite3.connect(h.root / database)
    try:
        assert connection.execute(statement, (sentinel, original)).rowcount == 1
        connection.commit()
        corrupted = _sqlite_snapshot(h)
        try:
            response, error = h.call("post", DEPRECATE, f"/document-types/{tid}/versions/1/deprecate", 503,
                                     key="corrupt-deprecate-1", json={"expected_lifecycle_revision": 1})
            assert error["error"]["code"] == "storage_unavailable"
            assert "PRIVATE" not in response.text + caplog.text
            assert _sqlite_snapshot(h) == corrupted
        finally:
            connection.execute(statement, (original, sentinel))
            connection.commit()
    finally:
        connection.close()
    assert _sqlite_snapshot(h) == clean


def test_context_pagination(api):
    h = api()
    ids = {h.call("post", "/contexts", "/contexts", 201, key=f"create-context-{n}", json=CTX)[1]["context_id"] for n in range(3)}
    seen, after = [], None
    while True:
        _, page = h.call("get", "/contexts", "/contexts?limit=2" + (f"&after={after}" if after else ""), 200)
        seen += [item["context_id"] for item in page["items"]]
        after = page["next_cursor"]
        if after is None:
            break
    assert seen == sorted(ids) and len(seen) == 3


def test_owner_isolation_and_scopes(api):
    h = api()
    cid, tid = make_catalog(h)
    h.call("post", "/document-types/{type_id}/versions", f"/document-types/{tid}/versions", 201, key="publish-0000001",
           json={"expected_revision": 1, "expected_context_revision": 1})
    update = {"body": CTX, "expected_revision": 1}
    publish = {"expected_revision": 1, "expected_context_revision": 1}
    ctx_nf, typ_nf, ver_nf = "context_not_found", "type_not_found", "type_version_not_found"
    foreign_requests = [
        ("get", "/contexts/{context_id}", f"/contexts/{cid}", None, None, ctx_nf),
        ("put", "/contexts/{context_id}", f"/contexts/{cid}", None, update, ctx_nf),
        ("get", "/contexts/{context_id}/types", f"/contexts/{cid}/types", None, None, ctx_nf),
        ("post", "/contexts/{context_id}/types", f"/contexts/{cid}/types", "foreign-type-01", TYP, ctx_nf),
        ("get", "/document-types/{type_id}", f"/document-types/{tid}", None, None, typ_nf),
        ("put", "/document-types/{type_id}", f"/document-types/{tid}", None, {**update, "body": TYP}, typ_nf),
        ("post", "/document-types/{type_id}/versions", f"/document-types/{tid}/versions", "foreign-publish1", publish, typ_nf),
        ("get", "/document-types/{type_id}/versions", f"/document-types/{tid}/versions", None, None, typ_nf),
        ("get", "/document-types/{type_id}/versions/{version}", f"/document-types/{tid}/versions/1", None, None, ver_nf),
        ("post", DEPRECATE, f"/document-types/{tid}/versions/1/deprecate", "foreign-deprecate1",
         {"expected_lifecycle_revision": 1}, ver_nf),
    ]
    foreign_snapshot = _sqlite_snapshot(h)
    for who in (Principal("user:2", 2, FULL), Principal("user:3", 3, FULL, is_admin=True)):  # admin is not special
        h.who = who
        for method, template, path, key, body, expected_code in foreign_requests:
            _, error = h.call(method, template, path, 404, key=key, **({"json": body} if body is not None else {}))
            assert error["error"]["code"] == expected_code
        assert h.call("get", "/contexts", "/contexts", 200)[1]["items"] == []
    assert _sqlite_snapshot(h) == foreign_snapshot
    h.who = OWNER
    assert len(h.call("get", "/contexts/{context_id}/types", f"/contexts/{cid}/types", 200)[1]["items"]) == 1

    # Missing scopes: 403 before the body is read (an unreadable body would otherwise be 415/413/422).
    junk = {"content": b"{not json", "headers": {"Content-Type": "text/plain"}}
    for scopes, method, template, path, key in (
        (frozenset({"contexts:read"}), "post", "/contexts", "/contexts", "denied-key-0001"),
        (frozenset({"contexts:read"}), "put", "/contexts/{context_id}", f"/contexts/{cid}", None),
        (frozenset({"contexts:write"}), "post", "/contexts", "/contexts", "denied-key-0002"),
        (frozenset({"contexts:read", "contexts:write"}), "post", "/document-types/{type_id}/versions", f"/document-types/{tid}/versions", "denied-key-0003"),
        (frozenset({"contexts:read", "contexts:publish"}), "post", "/contexts/{context_id}/types", f"/contexts/{cid}/types", "denied-key-0004"),
    ):
        h.who = Principal("user:1", 1, scopes)
        response = h.client.request(method, "/api/v1" + path, content=junk["content"],
                                    headers={**junk["headers"], **({"Idempotency-Key": key} if key else {})})
        assert h.check(response, template, method, 403)["error"]["code"] == "insufficient_scope"
    for method, template, path in (("get", "/contexts", "/contexts"), ("get", "/contexts/{context_id}", f"/contexts/{cid}"),
                                   ("get", "/document-types/{type_id}", f"/document-types/{tid}"),
                                   ("get", "/document-types/{type_id}/versions/{version}", f"/document-types/{tid}/versions/1")):
        h.who = Principal("user:1", 1, frozenset({"jobs:read", "contexts:write"}))
        h.check(h.client.request(method, "/api/v1" + path), template, method, 403)
    h.who = OWNER
    assert len(h.call("get", "/contexts", "/contexts", 200)[1]["items"]) == 1  # nothing was created


def test_body_limits_from_first_exposure(api):
    h = api()
    key = {"Idempotency-Key": "limit-key-0001"}

    def field(n, text):
        return {"name": f"f{n}", "type": "string", "description": text}

    # > 64 KiB (the default JSON cap elsewhere) is accepted here.
    big = {"name": "Grande", "common_fields": [field(n, "x" * 3000) for n in range(30)]}
    assert len(json.dumps(big)) > 65536
    h.call("post", "/contexts", "/contexts", 201, key="limit-key-0001", json=big)

    # > 2 MiB while streaming (chunked): 413 and nothing is created.
    def chunks():
        for _ in range(3):
            yield b" " * (1024 * 1024)
    response = h.client.post("/api/v1/contexts", content=chunks(), headers={**key, "Content-Type": "application/json"})
    assert h.check(response, "/contexts", "post", 413)["error"]["code"] == "request_too_large"

    # Canonical payload over 262144 bytes is a 422 even though the raw body is far below 2 MiB; no value echoed.
    huge = {"name": "Enorme", "common_fields": [field(n, "PRIVATE-SENTINEL" + "y" * 3984) for n in range(100)]}
    raw = json.dumps(huge)
    assert 262144 < len(raw) < 2 * 1024 * 1024
    response = h.client.post("/api/v1/contexts", content=raw, headers={"Idempotency-Key": "limit-key-0002", "Content-Type": "application/json"})
    assert h.check(response, "/contexts", "post", 422)["error"]["code"] == "validation_error"
    assert "PRIVATE" not in response.text

    # Media type, malformed JSON and unknown fields.
    response = h.client.post("/api/v1/contexts", content=json.dumps(CTX), headers={"Idempotency-Key": "limit-key-0003", "Content-Type": "text/plain"})
    assert h.check(response, "/contexts", "post", 415)["error"]["code"] == "unsupported_media_type"
    for payload in ('{"name": PRIVATE-SENTINEL', json.dumps({**CTX, "PRIVATE-NAME": "PRIVATE-VALUE"})):
        response = h.client.post("/api/v1/contexts", content=payload, headers={"Idempotency-Key": "limit-key-0004", "Content-Type": "application/json"})
        h.check(response, "/contexts", "post", 422)
        assert "PRIVATE" not in response.text
    assert len(h.call("get", "/contexts", "/contexts", 200)[1]["items"]) == 1  # only the >64 KiB context

    cid, tid = make_catalog(h)
    for bad in ('"1"', "true", "0", "null", "2147483648"):
        response = h.client.put(f"/api/v1/contexts/{cid}", content=f'{{"body": {json.dumps(CTX)}, "expected_revision": {bad}}}',
                                headers={"Content-Type": "application/json"})
        h.check(response, "/contexts/{context_id}", "put", 422)
    for template, path in (("/contexts", "/contexts?limit=0"), ("/contexts", "/contexts?limit=101"),
                           ("/contexts", "/contexts?after=not-a-cursor"),
                           ("/document-types/{type_id}/versions", f"/document-types/{tid}/versions?after=0"),
                           ("/document-types/{type_id}/versions", f"/document-types/{tid}/versions?after={2**63}"),
                           ("/document-types/{type_id}/versions/{version}", f"/document-types/{tid}/versions/0"),
                           ("/document-types/{type_id}/versions/{version}", f"/document-types/{tid}/versions/{2**63}")):
        h.check(h.client.get("/api/v1" + path), template, "get", 422)
    for headers in ({}, {"Idempotency-Key": "short"}):
        h.check(h.client.post("/api/v1/contexts", json=CTX, headers=headers), "/contexts", "post", 422)
    h.check(h.client.get(f"/api/v1/document-types/{tid}/versions/9"), "/document-types/{type_id}/versions/{version}", "get", 404)
    h.check(h.client.get("/api/v1/contexts/not-an-id"), "/contexts/{context_id}", "get", 404)


def test_routing_boundary(api):
    h = api()
    for method, path, allow in (("delete", "/contexts", "GET, POST"), ("put", "/contexts", "GET, POST"),
                                ("delete", "/contexts/" + "0" * 32, "GET, PUT"), ("patch", "/document-types/" + "0" * 32, "GET, PUT"),
                                ("put", "/document-types/" + "0" * 32 + "/versions", "GET, POST"),
                                ("post", "/document-types/" + "0" * 32 + "/versions/1", "GET"),
                                ("get", "/document-types/" + "0" * 32 + "/versions/1/deprecate", "POST")):
        response = h.client.request(method, "/api/v1" + path)
        assert h.check(response, "/contexts", "get", 405)["error"]["code"] == "method_not_allowed"
        assert response.headers["allow"] == allow
    response = h.client.get("/api/v1/contexts/" + "0" * 32 + "/unknown")
    assert h.check(response, "/contexts", "get", 404)["error"]["code"] == "route_not_found"


def test_openapi_contract_and_wire_name(api):
    h = api()
    found = {}
    for path, methods in h.schema["paths"].items():
        for method, operation in methods.items():
            if operation["operationId"] in OPERATIONS:
                found[operation["operationId"]] = (method, path, operation)
    assert set(found) == set(OPERATIONS)
    for operation_id, (method, path, scopes) in OPERATIONS.items():
        found_method, found_path, operation = found[operation_id]
        assert (found_method, found_path) == (method, path)
        assert operation["x-required-scopes"] == scopes and operation["x-human-admin-required"] is False
        assert operation["tags"] == ["Catalog"] and operation["security"] == [{"PrincipalBearer": []}]
        for status in ("401", "403", "405", "408", "413", "415", "422", "429", "500", "503"):
            assert status in operation["responses"]
        if method in ("post", "put"):
            assert "application/json" in operation["requestBody"]["content"]
    for operation_id in ("ocr_create_context", "ocr_create_document_type", "ocr_publish_document_type"):
        responses = found[operation_id][2]["responses"]
        assert "200" in responses and "201" in responses
        assert "Idempotency-Replayed" in responses["200"]["headers"] and "Idempotency-Replayed" in responses["201"]["headers"]
    deprecation = found["ocr_deprecate_document_type_version"][2]
    assert "200" in deprecation["responses"] and "201" not in deprecation["responses"]
    success = deprecation["responses"]["200"]
    assert "Idempotency-Replayed" in success["headers"]
    assert "deprecated" in success["description"].lower() and "replayed" in success["description"].lower()
    assert "409" in deprecation["responses"] and "503" in deprecation["responses"]
    key = next(parameter for parameter in deprecation["parameters"] if parameter["name"] == "Idempotency-Key")
    assert key["required"] and key["in"] == "header"
    schemas = h.schema["components"]["schemas"]
    assert schemas["TypeDeprecateInput"]["additionalProperties"] is False
    assert schemas["TypeDeprecateInput"]["examples"] == [{"expected_lifecycle_revision": 1}]
    example = success["content"]["application/json"]["example"]
    assert set(example) == {"type_id", "version", "schema_sha256", "lifecycle"}
    assert example["lifecycle"] == {"state": "deprecated", "revision": 2, "changed_at": "2026-10-16T12:00:00Z"}
    Draft202012Validator({"components": h.schema["components"],
                           **success["content"]["application/json"]["schema"]}).validate(example)
    properties = h.schema["components"]["schemas"]["TypeVersionRecord"]["properties"]
    assert "schema" in properties and "schema_document" not in properties
    code = "import app.catalog_models, app.routes.catalog"
    result = subprocess.run([sys.executable, "-W", "always", "-c", code], capture_output=True, text=True, timeout=60,
                            env={**os.environ, "OCR_TMP_DIR": str(api.all[0].root / "no-runtime")})
    assert result.returncode == 0, result.stderr
    assert "shadow" not in result.stderr.lower()


def test_isolated_apps_and_provider_on_state(api):
    first, second = api("a"), api("b")
    assert isinstance(first.app.state.catalog_provider, CatalogProvider)
    assert first.app.state.catalog_provider is not second.app.state.catalog_provider
    first.call("post", "/contexts", "/contexts", 201, key="create-context-1", json=CTX)
    assert len(first.call("get", "/contexts", "/contexts", 200)[1]["items"]) == 1
    assert second.call("get", "/contexts", "/contexts", 200)[1]["items"] == []
    services = first.app.state.catalog_provider()
    assert services is first.app.state.catalog_provider()  # cached by store, not rebuilt per JobApplication
    assert services is not second.app.state.catalog_provider()


def test_create_app_and_openapi_do_not_resolve_storage():
    calls = []
    app = create_app(lambda: calls.append("resolved"))
    assert app.openapi()["paths"]["/api/v1/contexts"] and calls == []


def legacy_token(user_id, secret):
    """Synthetic JWT signed like the old issuer: no ocr_authz_version/scopes claims."""
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=").decode()
    signing = encode({"alg": "HS256", "typ": "JWT"}) + "." + encode({"sub": user_id, "email": "legacy@test.invalid", "role": "user", "exp": int(time.time()) + 600})
    signature = hmac.new(secret.encode(), signing.encode(), hashlib.sha256).digest()
    return signing + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def test_legacy_jwt_without_new_claims_is_denied(tmp_path, api):
    user = get_user_store().create_user("Legacy", f"{uuid4().hex}@test.invalid", "synthetic-test-password")
    store = JobStore(root_dir=tmp_path)
    app = create_app(lambda: JobApplication(store))  # real authentication, no override
    client = TestClient(app)
    try:
        legacy = {"Authorization": "Bearer " + legacy_token(user["id"], settings.jwt_secret)}
        current = {"Authorization": "Bearer " + create_token(user["id"], user["email"], user["role"], settings.jwt_secret)}
        assert client.get("/api/v1/contexts", headers=legacy).status_code == 403
        assert client.post("/api/v1/contexts", json=CTX, headers={**legacy, "Idempotency-Key": "legacy-key-0001"}).status_code == 403
        assert client.get("/api/v1/contexts").status_code == 401
        assert client.get("/api/v1/contexts", headers=current).status_code == 200
        h = api("legacy-deprecation")
        h.app.dependency_overrides.pop(require_principal)
        response = h.client.post("/api/v1/document-types/" + "0" * 32 + "/versions/1/deprecate",
                                 json={"expected_lifecycle_revision": 1},
                                 headers={**legacy, "Idempotency-Key": "legacy-deprecate-1"})
        assert h.check(response, DEPRECATE, "post", 403)["error"]["code"] == "insufficient_scope"
    finally:
        client.close()
        store._conn.close()
