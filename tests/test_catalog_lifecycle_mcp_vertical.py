"""Lifecycle parity through real SDK HTTP/stdio clients, in auto and legacy modes.

Uses synthetic local identities and persisted stores. This does not validate desktop/CLI
applications, an external OAuth issuer, DNS/TLS, or production deployment.
"""
import asyncio
import hashlib

import pytest

from test_catalog_http import legacy_token
from test_catalog_mcp_boundaries import reduced_token
from test_catalog_mcp_vertical import (CTX, TYP, SCENARIO_TIMEOUT, Tools, canonical,
                                       catalog_accounts, rest, text_documents)  # noqa: F401
from test_mcp_vertical import connected, runtime  # noqa: F401


@pytest.mark.parametrize("transport,mode", [
    ("http", "auto"), ("http", "legacy"), ("stdio", "auto"), ("stdio", "legacy"),
])
def test_deprecation_receipts_conflicts_and_authorization(runtime, catalog_accounts, transport, mode):  # noqa: F811
    async def scenario():
        async with asyncio.timeout(SCENARIO_TIMEOUT):
            async with rest(runtime, catalog_accounts.owner) as http:
                response = await http.post("/api/v1/contexts", json=CTX,
                                           headers={"Idempotency-Key": "lifecycle-context-01"})
                assert response.status_code == 201
                cid = response.json()["context_id"]
                response = await http.post(f"/api/v1/contexts/{cid}/types", json=TYP,
                                           headers={"Idempotency-Key": "lifecycle-type-0001"})
                assert response.status_code == 201
                tid = response.json()["type_id"]
                versions_path = f"/api/v1/document-types/{tid}/versions"
                version_path = versions_path + "/1"
                deprecate_path = version_path + "/deprecate"
                publish_body = {"expected_revision": 1, "expected_context_revision": 1}
                publish_key = "lifecycle-publish-01"
                response = await http.post(versions_path, json=publish_body,
                                           headers={"Idempotency-Key": publish_key})
                assert response.status_code == 201
                publication = response.json()
                assert "lifecycle" not in publication
                response = await http.get(version_path)
                assert response.status_code == 200
                original = response.json()
                original_bytes = canonical(original["schema"])
                assert hashlib.sha256(original_bytes).hexdigest() == publication["schema_sha256"]
                assert original["lifecycle"] == {
                    "state": "published", "revision": 1, "changed_at": publication["created_at"]}

                async def snapshot():
                    paths = ["/api/v1/contexts", f"/api/v1/contexts/{cid}",
                             f"/api/v1/contexts/{cid}/types", f"/api/v1/document-types/{tid}",
                             versions_path, version_path]
                    responses = [await http.get(path) for path in paths]
                    assert all(item.status_code == 200 for item in responses)
                    return [item.json() for item in responses]

                args = {"type_id": tid, "version": 1,
                        "body": {"expected_lifecycle_revision": 1},
                        "idempotency_key": "lifecycle-deprecate-01"}
                before = await snapshot()
                readonly = reduced_token(catalog_accounts.owner, {"contexts:read"}, runtime.env["JWT_SECRET"])
                readwrite = reduced_token(catalog_accounts.owner, {"contexts:read", "contexts:write"},
                                          runtime.env["JWT_SECRET"])
                legacy = legacy_token(catalog_accounts.owner_id, runtime.env["JWT_SECRET"])
                identities = [
                    ("service", runtime.tokens[0], 403, "insufficient_scope"),
                    ("legacy", legacy, 403, "insufficient_scope"),
                    ("readonly", readonly, 403, "insufficient_scope"),
                    ("readwrite", readwrite, 403, "insufficient_scope"),
                    ("other", catalog_accounts.other, 404, "type_version_not_found"),
                    ("admin", catalog_accounts.admin, 404, "type_version_not_found"),
                ]
                for label, token, status, code in identities:
                    async with rest(runtime, token) as denied_http:
                        if label in {"service", "legacy", "readonly", "readwrite"}:
                            me = await denied_http.get("/api/v1/me")
                            assert me.status_code == 200
                            identity = me.json()
                            assert "contexts:publish" not in identity["scopes"]
                            if label == "service":
                                assert identity["kind"] == "service" and len(identity["scopes"]) == 5
                            elif label == "readonly":
                                assert set(identity["scopes"]) == {"contexts:read"}
                        response = await denied_http.post(deprecate_path, json=args["body"],
                                                          headers={"Idempotency-Key": args["idempotency_key"]})
                        assert response.status_code == status
                        assert response.json()["error"]["code"] == code
                    async with connected(runtime, transport, mode, token) as denied_client:
                        denied_tools = Tools(denied_client, (await denied_client.list_tools()).tools)
                        if label == "service":
                            who = await denied_tools("ocr_whoami", {})
                            assert who["kind"] == "service"
                            assert set(who["scopes"]) == set(identity["scopes"])
                            assert who["subject"] == identity["subject"]
                        await denied_tools.denied("ocr_deprecate_document_type_version", args, code)
                    assert await snapshot() == before

                async with connected(runtime, transport, mode, catalog_accounts.owner) as client:
                    tools = Tools(client, (await client.list_tools()).tools)
                    result = await tools.raw("ocr_deprecate_document_type_version", args)
                    receipt = result.structured_content
                    assert set(receipt) == {"type_id", "version", "schema_sha256", "lifecycle"}
                    assert receipt["type_id"] == tid and receipt["version"] == 1
                    assert receipt["schema_sha256"] == publication["schema_sha256"]
                    lifecycle = receipt["lifecycle"]
                    assert set(lifecycle) == {"state", "revision", "changed_at"}
                    assert lifecycle["state"] == "deprecated" and lifecycle["revision"] == 2
                    assert lifecycle["changed_at"].endswith(("Z", "+00:00"))
                    assert all(document == receipt for document in text_documents(result))
                    replay = await tools.raw("ocr_deprecate_document_type_version", args)
                    assert replay.structured_content == receipt
                    assert all(document == receipt for document in text_documents(replay))
                    response = await http.post(deprecate_path, json=args["body"],
                                               headers={"Idempotency-Key": args["idempotency_key"]})
                    assert response.status_code == 200
                    assert response.headers["idempotency-replayed"] == "true"
                    assert response.json() == receipt

                    version = await tools.raw("ocr_get_document_type_version", {"type_id": tid, "version": 1})
                    current = version.structured_content
                    assert current == {**original, "lifecycle": lifecycle}
                    assert all(document == current for document in text_documents(version))
                    assert canonical(current["schema"]) == original_bytes
                    assert hashlib.sha256(canonical(current["schema"])).hexdigest() == current["schema_sha256"]
                    response = await http.get(version_path)
                    assert response.status_code == 200 and response.json() == current
                    listing = await tools("ocr_list_document_type_versions", {"type_id": tid})
                    assert listing == {"items": [{**publication, "lifecycle": lifecycle}], "next_cursor": None}
                    response = await http.get(versions_path)
                    assert response.status_code == 200 and response.json() == listing
                    publish_args = {"type_id": tid, "body": publish_body, "idempotency_key": publish_key}
                    assert await tools("ocr_publish_document_type", publish_args) == publication
                    response = await http.post(versions_path, json=publish_body,
                                               headers={"Idempotency-Key": publish_key})
                    assert response.status_code == 200 and response.json() == publication
                    assert response.headers["idempotency-replayed"] == "true"

                    after = await snapshot()
                    conflicts = [
                        ({**args, "idempotency_key": "lifecycle-stale-0001"}, "type_lifecycle_revision_conflict"),
                        ({**args, "body": {"expected_lifecycle_revision": 2},
                          "idempotency_key": "lifecycle-already-01"}, "type_version_already_deprecated"),
                        ({**args, "body": {"expected_lifecycle_revision": 2}}, "idempotency_conflict"),
                    ]
                    for request, code in conflicts:
                        await tools.denied("ocr_deprecate_document_type_version", request, code)
                        response = await http.post(deprecate_path, json=request["body"],
                                                   headers={"Idempotency-Key": request["idempotency_key"]})
                        assert response.status_code == 409 and response.json()["error"]["code"] == code
                        assert await snapshot() == after
                    assert await tools("ocr_deprecate_document_type_version", args) == receipt
                    assert await tools("ocr_publish_document_type", publish_args) == publication
                    assert await snapshot() == after

    asyncio.run(scenario())
    assert not (runtime.root / "stdio-must-not-create").exists()
