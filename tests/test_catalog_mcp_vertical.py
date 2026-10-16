"""Catalog MCP vertical: real SDK clients (HTTP and child-process stdio) through the persisted OCR runtime.

Real uvicorn runtime, real SQLite stores, real local users with real Bearer JWTs. No registry fakes, no
MockTransport, no replaced dependencies. All data and credentials are synthetic and live in the test tmp dir."""
import asyncio
import copy
import hashlib
import json

import httpx2
from jsonschema import Draft202012Validator
import pytest

from app.catalog_models import ContextInput, TypeInput
from test_mcp_vertical import connected, runtime  # noqa: F401  (runtime is a pytest fixture)

ADMIN_EMAIL = "admin@localhost"
ADMIN_PASSWORD = "synthetic-test-admin-password"
USER_PASSWORD = "synthetic-test-user-password"
DOCUMENT_SCOPES = {"contexts:read", "contexts:write", "contexts:publish"}
SCENARIO_TIMEOUT = 120

LEGACY_TOOLS = frozenset({
    "ocr_delete_job_artifacts", "ocr_cancel_job", "ocr_cancel_jobs", "ocr_retry_job", "ocr_get_capabilities",
    "ocr_whoami", "ocr_list_jobs", "ocr_get_text", "ocr_get_report", "ocr_get_job", "ocr_get_metrics",
    "ocr_get_artifact", "ocr_prepare_upload", "ocr_get_upload", "ocr_submit_job", "ocr_create_batch",
    "ocr_list_batches", "ocr_get_batch", "ocr_create_export", "ocr_get_export",
})
READ_TOOLS = frozenset({"ocr_list_contexts", "ocr_get_context", "ocr_list_document_types",
                        "ocr_get_document_type", "ocr_list_document_type_versions", "ocr_get_document_type_version"})
CREATE_TOOLS = frozenset({"ocr_create_context", "ocr_create_document_type", "ocr_publish_document_type"})
UPDATE_TOOLS = frozenset({"ocr_update_context", "ocr_update_document_type"})
DEPRECATE_TOOLS = frozenset({"ocr_deprecate_document_type_version"})
CATALOG_TOOLS = READ_TOOLS | CREATE_TOOLS | UPDATE_TOOLS | DEPRECATE_TOOLS

CTX = ContextInput.model_config["json_schema_extra"]["examples"][0]
TYP = TypeInput.model_config["json_schema_extra"]["examples"][0]
CTX_OTHER = {"name": "Segundo contexto", "description": "Contexto sintético para paginação.", "common_fields": []}
CTX2 = {**CTX, "name": "Contratos revisados",
        "common_fields": [*CTX["common_fields"], {"name": "data", "type": "date", "description": "Data."}]}
TYP2 = {**TYP, "name": "Contrato revisado"}
CTX3 = {**CTX2, "name": "Contratos editados depois",
        "common_fields": [*CTX2["common_fields"], {"name": "extra_ctx", "type": "string", "description": "Extra."}]}
TYP3 = {**TYP2, "name": "Contrato editado depois",
        "specific_fields": [*TYP["specific_fields"], {"name": "extra_tipo", "type": "string", "description": "Extra."}]}


class CatalogAccounts:
    """Synthetic admin/owner/other identities. Credentials are kept in memory only and never rendered."""
    def __init__(self, admin, owner, other, owner_id, other_id):
        self._admin, self._owner, self._other = admin, owner, other
        self.owner_id, self.other_id = owner_id, other_id

    admin = property(lambda self: self._admin)
    owner = property(lambda self: self._owner)
    other = property(lambda self: self._other)

    def __repr__(self):
        return "<CatalogAccounts redacted>"

    __str__ = __repr__


def _login(origin, email, password):
    with httpx2.Client(trust_env=False, timeout=15) as http:  # fresh client: no cookie carries authorization
        response = http.post(origin + "/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200
        token = response.json()["access_token"]
    assert isinstance(token, str) and token
    return token


def _create_user(origin, admin_token, name, email):
    with httpx2.Client(trust_env=False, timeout=15) as http:
        response = http.post(origin + "/api/users", headers={"Authorization": "Bearer " + admin_token},
                             json={"name": name, "email": email, "password": USER_PASSWORD})
        assert response.status_code == 201
        created = response.json()
    assert created["role"] == "user"
    return created["id"]


def _scopes(origin, token):
    with httpx2.Client(trust_env=False, timeout=15) as http:
        response = http.get(origin + "/api/v1/me", headers={"Authorization": "Bearer " + token})
        assert response.status_code == 200
        return set(response.json()["scopes"])


@pytest.fixture
def catalog_accounts(runtime):  # noqa: F811
    """Starts the shared Runtime once and returns real local users with document scopes (V2b2b reuses this)."""
    runtime.env["ADMIN_EMAIL"] = ADMIN_EMAIL
    runtime.start()
    admin = _login(runtime.origin, ADMIN_EMAIL, ADMIN_PASSWORD)
    owner_id = _create_user(runtime.origin, admin, "Catalog Owner", "catalog-owner@example.com")
    other_id = _create_user(runtime.origin, admin, "Catalog Other", "catalog-other@example.com")
    assert owner_id != other_id
    owner = _login(runtime.origin, "catalog-owner@example.com", USER_PASSWORD)
    other = _login(runtime.origin, "catalog-other@example.com", USER_PASSWORD)
    assert DOCUMENT_SCOPES <= _scopes(runtime.origin, owner)
    assert DOCUMENT_SCOPES <= _scopes(runtime.origin, other)
    return CatalogAccounts(admin, owner, other, owner_id, other_id)


def rest(runtime, token):
    return httpx2.AsyncClient(base_url=runtime.origin, headers={"Authorization": "Bearer " + token},
                              trust_env=False, timeout=30)


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


class Tools:
    """Calls real tools, records the names, validates every structured result against its published output_schema."""
    def __init__(self, client, tools):
        self.client, self.called = client, set()
        self.validators = {t.name: Draft202012Validator(t.output_schema) for t in tools}

    async def raw(self, name, args):
        self.called.add(name)
        result = await self.client.call_tool(name, args)
        assert not result.is_error, name
        self.validators[name].validate(result.structured_content)
        return result

    async def __call__(self, name, args):
        return (await self.raw(name, args)).structured_content

    async def denied(self, name, args, code):
        result = await self.client.call_tool(name, args)
        assert result.is_error and code in result.content[0].text
        return result


def check_discovery(listing):
    tools = {t.name: t for t in listing.tools}
    assert len(listing.tools) == len(tools) == 32
    assert set(tools) == LEGACY_TOOLS | CATALOG_TOOLS
    assert all(t.output_schema for t in tools.values())
    assert all(t.input_schema["additionalProperties"] is False for t in tools.values())
    for name in CATALOG_TOOLS:
        hints = tools[name].annotations
        assert hints.open_world_hint is False, name
        if name in READ_TOOLS:
            assert hints.read_only_hint and hints.idempotent_hint and not hints.destructive_hint, name
        elif name in CREATE_TOOLS:
            assert not hints.read_only_hint and hints.idempotent_hint and not hints.destructive_hint, name
        elif name in UPDATE_TOOLS:
            assert not hints.read_only_hint and not hints.idempotent_hint and hints.destructive_hint, name
        else:
            assert name in DEPRECATE_TOOLS
            assert not hints.read_only_hint and hints.idempotent_hint and hints.destructive_hint, name
    return listing.tools


def text_documents(result):
    texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    assert texts
    return [json.loads(text) for text in texts]


def publication_fields(document):
    assert document["lifecycle"] == {
        "state": "published", "revision": 1, "changed_at": document["created_at"]}
    return {k: v for k, v in document.items() if k not in {"schema", "lifecycle"}}


def check_version(result, summary, http_json):
    """structured_content and every text block expose the wire alias 'schema' with the exact envelope and hash."""
    structured = result.structured_content
    documents = [structured, *text_documents(result)]
    for document in documents:
        assert "schema" in document and "schema_document" not in document
        envelope = document["schema"]
        assert isinstance(envelope, dict)
        assert set(envelope) == {"schema_version", "common_fields", "specific_fields", "fields"}
        assert envelope["fields"] == envelope["common_fields"] + envelope["specific_fields"]
        assert len(canonical(envelope)) <= 262144
        assert hashlib.sha256(canonical(envelope)).hexdigest() == document["schema_sha256"] == summary["schema_sha256"]
        assert publication_fields(document) == summary
    assert publication_fields(http_json) == summary
    assert documents[1] == structured
    assert structured == http_json
    return structured


async def get_version(tools, http, type_id, version, summary):
    result = await tools.raw("ocr_get_document_type_version", {"type_id": type_id, "version": version})
    response = await http.get(f"/api/v1/document-types/{type_id}/versions/{version}")
    assert response.status_code == 200
    return check_version(result, summary, response.json())


async def catalog_flow(runtime, accounts, client):
    owner_key = "owner-" + str(runtime.port)
    listing = await client.list_tools()
    tools = Tools(client, check_discovery(listing))
    async with rest(runtime, accounts.owner) as http:
        # -- contexts: create, replay (MCP + REST), get, list summaries, pagination
        receipt = await tools("ocr_create_context", {"body": CTX, "idempotency_key": owner_key + "-ctx1"})
        cid = receipt["context_id"]
        assert receipt["revision"] == 1
        assert await tools("ocr_create_context", {"body": CTX, "idempotency_key": owner_key + "-ctx1"}) == receipt
        replay = await http.post("/api/v1/contexts", json=CTX, headers={"Idempotency-Key": owner_key + "-ctx1"})
        assert replay.status_code == 200 and replay.headers["idempotency-replayed"] == "true"
        assert replay.json() == receipt

        record = await tools("ocr_get_context", {"context_id": cid})
        assert record["revision"] == 1 and record["body"]["name"] == CTX["name"]
        assert [f["name"] for f in record["body"]["common_fields"]] == ["numero"]
        page = await tools("ocr_list_contexts", {})
        assert [item["context_id"] for item in page["items"]] == [cid]
        assert set(page["items"][0]) == {"context_id", "name", "revision", "created_at", "updated_at"}
        assert "common_fields" not in page["items"][0] and "body" not in page["items"][0]

        second = await tools("ocr_create_context", {"body": CTX_OTHER, "idempotency_key": owner_key + "-ctx2"})
        ids = [cid, second["context_id"]]
        seen = []
        after = None
        while True:
            page = await tools("ocr_list_contexts", {"limit": 1, **({"after": after} if after else {})})
            assert len(page["items"]) <= 1
            assert all("common_fields" not in item and "body" not in item for item in page["items"])
            seen += [item["context_id"] for item in page["items"]]
            after = page["next_cursor"]
            if after is None:
                break
            assert after == seen[-1]
        assert len(seen) == len(set(seen)) == 2 and seen == sorted(ids)

        # -- update context with CAS
        updated = await tools("ocr_update_context", {"context_id": cid, "body": {"body": CTX2, "expected_revision": 1}})
        assert updated["revision"] == 2 and updated["body"]["name"] == CTX2["name"]
        record = await tools("ocr_get_context", {"context_id": cid})
        assert record["revision"] == 2 and [f["name"] for f in record["body"]["common_fields"]] == ["numero", "data"]

        # -- types: create, get, list summaries, update
        type_receipt = await tools("ocr_create_document_type",
                                   {"context_id": cid, "body": TYP, "idempotency_key": owner_key + "-typ1"})
        tid = type_receipt["type_id"]
        assert type_receipt["revision"] == 1 and type_receipt["context_id"] == cid
        draft = await tools("ocr_get_document_type", {"type_id": tid})
        assert draft["revision"] == 1 and draft["body"]["name"] == TYP["name"]
        types = await tools("ocr_list_document_types", {"context_id": cid})
        assert [item["type_id"] for item in types["items"]] == [tid]
        assert "specific_fields" not in types["items"][0] and "body" not in types["items"][0]
        draft = await tools("ocr_update_document_type", {"type_id": tid, "body": {"body": TYP2, "expected_revision": 1}})
        assert draft["revision"] == 2 and draft["body"]["name"] == TYP2["name"]

        # -- publish v1 and replay
        publish = {"type_id": tid, "body": {"expected_revision": 2, "expected_context_revision": 2},
                   "idempotency_key": owner_key + "-pub1"}
        summary = await tools("ocr_publish_document_type", publish)
        assert summary["version"] == 1 and "schema" not in summary
        assert summary["type_name"] == TYP2["name"] and summary["context_name"] == CTX2["name"]
        assert await tools("ocr_publish_document_type", publish) == summary
        versions = await tools("ocr_list_document_type_versions", {"type_id": tid})
        assert [publication_fields(item) for item in versions["items"]] == [summary] and versions["next_cursor"] is None
        listed = await http.get(f"/api/v1/document-types/{tid}/versions")
        assert listed.status_code == 200 and listed.json() == versions

        # -- immutable snapshot with wire alias, in structured content and in text blocks
        before = copy.deepcopy(await get_version(tools, http, tid, 1, summary))

        # -- edits after publication never touch v1
        edited_ctx = await tools("ocr_update_context", {"context_id": cid, "body": {"body": CTX3, "expected_revision": 2}})
        edited_typ = await tools("ocr_update_document_type", {"type_id": tid, "body": {"body": TYP3, "expected_revision": 2}})
        assert edited_ctx["revision"] == 3 and edited_typ["revision"] == 3
        assert await get_version(tools, http, tid, 1, summary) == before
        assert await tools("ocr_publish_document_type", publish) == summary

        # -- explicit second publication proves the numeric cursor
        summary2 = await tools("ocr_publish_document_type", {
            "type_id": tid, "body": {"expected_revision": 3, "expected_context_revision": 3},
            "idempotency_key": owner_key + "-pub2"})
        assert summary2["version"] == 2 and summary2["schema_sha256"] != summary["schema_sha256"]
        assert summary2["type_name"] == TYP3["name"] and summary2["context_name"] == CTX3["name"]
        first = await tools("ocr_list_document_type_versions", {"type_id": tid, "limit": 1})
        assert [publication_fields(item) for item in first["items"]] == [summary] and first["next_cursor"] == 1
        last = await tools("ocr_list_document_type_versions", {"type_id": tid, "limit": 1, "after": 1})
        assert [publication_fields(item) for item in last["items"]] == [summary2] and last["next_cursor"] is None
        assert all("schema" not in item for item in first["items"] + last["items"])
        assert (await get_version(tools, http, tid, 1, summary)) == before
        await get_version(tools, http, tid, 2, summary2)

        # Deprecate only after all publication and immutable-snapshot assertions above.
        deprecated = await tools("ocr_deprecate_document_type_version", {
            "type_id": tid, "version": 1, "body": {"expected_lifecycle_revision": 1},
            "idempotency_key": owner_key + "-dep1"})
        assert deprecated["schema_sha256"] == before["schema_sha256"]
        assert deprecated["lifecycle"]["state"] == "deprecated" and deprecated["lifecycle"]["revision"] == 2
        current = await tools("ocr_get_document_type_version", {"type_id": tid, "version": 1})
        assert current == {**before, "lifecycle": deprecated["lifecycle"]}
        assert await tools("ocr_publish_document_type", publish) == summary

        # -- the other user owns nothing here
        async with connected(runtime, "http", "auto", accounts.other) as other_client:
            other_tools = Tools(other_client, (await other_client.list_tools()).tools)
            await other_tools.denied("ocr_get_context", {"context_id": cid}, "not_found")
            assert (await other_tools("ocr_list_contexts", {}))["items"] == []
    return tools.called


@pytest.mark.parametrize("transport,mode", [("http", "auto"), ("http", "legacy"), ("stdio", "auto"), ("stdio", "legacy")])
def test_catalog_twelve_tools_through_real_clients(runtime, catalog_accounts, transport, mode):  # noqa: F811
    async def scenario():
        async with asyncio.timeout(SCENARIO_TIMEOUT):
            async with connected(runtime, transport, mode, catalog_accounts.owner) as client:
                return await catalog_flow(runtime, catalog_accounts, client)

    called = asyncio.run(scenario())
    assert len(called) == 12 and called == CATALOG_TOOLS
    assert not (runtime.root / "stdio-must-not-create").exists()


def test_transports_share_store_and_owner(runtime, catalog_accounts):  # noqa: F811
    key = "parity-context-0001"

    async def scenario():
        async with asyncio.timeout(SCENARIO_TIMEOUT):
            async with connected(runtime, "http", "auto", catalog_accounts.owner) as http_client:
                created = await http_client.call_tool("ocr_create_context", {"body": CTX, "idempotency_key": key})
                assert not created.is_error
                receipt = created.structured_content
            cid = receipt["context_id"]
            assert receipt["revision"] == 1
            async with connected(runtime, "stdio", "auto", catalog_accounts.owner) as stdio_client:
                got = await stdio_client.call_tool("ocr_get_context", {"context_id": cid})
                assert not got.is_error and got.structured_content["revision"] == 1
                updated = await stdio_client.call_tool(
                    "ocr_update_context", {"context_id": cid, "body": {"body": CTX2, "expected_revision": 1}})
                assert not updated.is_error and updated.structured_content["revision"] == 2
            async with connected(runtime, "stdio", "auto", catalog_accounts.other) as other_client:
                denied = await other_client.call_tool("ocr_get_context", {"context_id": cid})
                assert denied.is_error and "not_found" in denied.content[0].text
            async with rest(runtime, catalog_accounts.owner) as http:
                current = await http.get(f"/api/v1/contexts/{cid}")
                assert current.status_code == 200 and current.json()["revision"] == 2
                assert current.json()["body"]["name"] == CTX2["name"]
                # Replay returns the ORIGINAL receipt even though the current state is revision 2.
                replay = await http.post("/api/v1/contexts", json=CTX, headers={"Idempotency-Key": key})
                assert replay.status_code == 200 and replay.headers["idempotency-replayed"] == "true"
                assert replay.json() == receipt and replay.json()["revision"] == 1
        # No credentials at all (fresh client: no headers, no cookies) is rejected.
        async with httpx2.AsyncClient(base_url=runtime.origin, trust_env=False, timeout=15) as anonymous:
            assert (await anonymous.get("/api/v1/contexts")).status_code == 401

    asyncio.run(scenario())
    assert not (runtime.root / "stdio-must-not-create").exists()


def large_context_body():
    fields = [{"name": f"f{i}", "type": "string", "description": chr(ord("a") + i % 26) * 3500} for i in range(20)]
    return {"name": "Contexto grande", "description": "Corpo sintético acima de 64 KiB.", "common_fields": fields}


@pytest.mark.parametrize("transport", ["http", "stdio"])
def test_valid_body_above_64kib_is_not_truncated(runtime, catalog_accounts, transport):  # noqa: F811
    body = large_context_body()
    ContextInput.model_validate(body)  # valid model, proven BEFORE any call
    raw_size = len(json.dumps(body).encode("utf-8"))
    assert 65536 < raw_size and len(canonical(body)) < 262144
    expected = {f["name"]: f["description"] for f in body["common_fields"]}

    def descriptions(fields):
        assert [f["name"] for f in fields] == list(expected)
        assert {f["name"]: f["description"] for f in fields} == expected

    async def scenario():
        async with asyncio.timeout(SCENARIO_TIMEOUT):
            async with connected(runtime, transport, "auto", catalog_accounts.owner) as client:
                tools = Tools(client, (await client.list_tools()).tools)
                async with rest(runtime, catalog_accounts.owner) as http:
                    receipt = await tools("ocr_create_context", {"body": body, "idempotency_key": "large-context-0001"})
                    cid = receipt["context_id"]
                    got = await tools.raw("ocr_get_context", {"context_id": cid})
                    descriptions(got.structured_content["body"]["common_fields"])
                    descriptions(text_documents(got)[0]["body"]["common_fields"])
                    type_receipt = await tools("ocr_create_document_type", {
                        "context_id": cid, "body": TYP, "idempotency_key": "large-type-00001"})
                    summary = await tools("ocr_publish_document_type", {
                        "type_id": type_receipt["type_id"], "body": {"expected_revision": 1, "expected_context_revision": 1},
                        "idempotency_key": "large-publish-001"})
                    version = await get_version(tools, http, type_receipt["type_id"], 1, summary)
                    envelope = version["schema"]
                    size = len(canonical(envelope))
                    assert 65536 < size <= 262144
                    descriptions(envelope["common_fields"])
                    descriptions([f for f in envelope["fields"] if f["name"] in expected])
                    result = await tools.raw("ocr_get_document_type_version",
                                             {"type_id": type_receipt["type_id"], "version": 1})
                    for document in text_documents(result):
                        descriptions(document["schema"]["common_fields"])
                        assert document["schema_sha256"] == hashlib.sha256(canonical(document["schema"])).hexdigest()

    asyncio.run(scenario())
    assert not (runtime.root / "stdio-must-not-create").exists()
