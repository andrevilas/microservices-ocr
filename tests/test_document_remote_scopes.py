"""Explicit document scopes for service credentials and OAuth bindings (synthetic; issuer is mocked)."""
import asyncio
import hashlib
import json
import sqlite3
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.application import ApplicationError, JobApplication
from app.catalog_models import ContextInput, TypeInput
from app.config import settings
from app.main import create_app
from app.remote import MAX_JSON, IntrospectionVerifier, RemoteConfig, create_remote_app
from app.scopes import (DOCUMENT_SCOPES, LEGACY_SCOPES, LOCAL_ALLOWED_SCOPES, OAUTH_SCOPES, SERVICE_SCOPES,
                        effective_local_scopes, local_role_scopes)
from app.services import identity_store
from app.services.auth_service import create_token
from app.services.identity_store import IdentityStore, get_identity_store
from app.services.storage_service import JobStore
from app.services.user_store import get_user_store

NEW_SERVICE_SCOPES = ["contexts:read", "contexts:write", "metadata:read", "metadata:write"]
PRIVILEGED = ["contexts:publish", "metadata:approve"]


def bearer(token):
    return {"Authorization": "Bearer " + token}


# ---- 1. exact policy -------------------------------------------------------

def test_exact_scope_policy():
    assert LEGACY_SCOPES == {"jobs:read", "artifacts:read", "jobs:submit", "jobs:cancel", "artifacts:delete"}
    assert DOCUMENT_SCOPES == {"contexts:read", "contexts:write", "contexts:publish",
                               "metadata:read", "metadata:write", "metadata:approve"}
    assert SERVICE_SCOPES == LEGACY_SCOPES | set(NEW_SERVICE_SCOPES) and len(SERVICE_SCOPES) == 9
    assert OAUTH_SCOPES == LEGACY_SCOPES | DOCUMENT_SCOPES and len(OAUTH_SCOPES) == 11
    assert not SERVICE_SCOPES & set(PRIVILEGED)
    assert identity_store.SERVICE_SCOPES is SERVICE_SCOPES
    # A03a local behavior is unchanged.
    assert LOCAL_ALLOWED_SCOPES == OAUTH_SCOPES
    assert local_role_scopes("admin") == local_role_scopes("user") == LOCAL_ALLOWED_SCOPES
    assert local_role_scopes("guest") == LEGACY_SCOPES
    assert effective_local_scopes({}, "user") == LEGACY_SCOPES


# ---- 2-4. service credentials ---------------------------------------------

@pytest.fixture
def system(tmp_path, monkeypatch):
    import app.security as security
    identities = IdentityStore(tmp_path / "identities.sqlite3")
    resources = {"identities": identities}
    jobs = client = None
    try:
        jobs = JobStore(root_dir=tmp_path / "jobs")
        monkeypatch.setattr(security, "get_identity_store", lambda: resources["identities"])
        app = create_app(lambda: JobApplication(jobs))
        app.dependency_overrides[get_identity_store] = lambda: resources["identities"]
        client = TestClient(app)
        users = [get_user_store().create_user(f"Synthetic {role}", f"{uuid4().hex}@test.invalid", "synthetic-password", role=role)
                 for role in ("admin", "user")]
        tokens = [create_token(u["id"], u["email"], u["role"], settings.jwt_secret) for u in users]
        service = client.post("/api/v1/service-principals", json={"name": "Test service"}, headers=bearer(tokens[0])).json()
        resources.update({"client": client, "admin": bearer(tokens[0]), "user": bearer(tokens[1]),
                          "service": service, "path": f"/api/v1/service-principals/{service['id']}/credentials", "tmp_path": tmp_path})
        yield resources
    finally:
        try:
            if client is not None:
                client.close()
        finally:
            try:
                if resources["identities"] is not None:
                    resources["identities"].close()
            finally:
                if jobs is not None:
                    jobs._conn.close()


def issue(system, scopes, **extra):
    return system["client"].post(system["path"], json={"name": "cred", "scopes": scopes, **extra}, headers=system["admin"])


def test_admin_issues_explicit_new_scopes_and_rejects_privileged(system):
    response = issue(system, NEW_SERVICE_SCOPES + ["jobs:read"])
    assert response.status_code == 201 and response.headers["cache-control"] == "no-store"
    assert response.json()["credential"]["scopes"] == sorted(NEW_SERVICE_SCOPES + ["jobs:read"])
    assert issue(system, sorted(SERVICE_SCOPES)).status_code == 201
    for scopes in (PRIVILEGED, ["contexts:read", "contexts:publish"], ["metadata:approve"], ["unknown:scope"],
                   ["contexts:read", "contexts:read"], [True], [{"a": 1}], [["contexts:read"]], [], "contexts:read",
                   sorted(SERVICE_SCOPES) + ["jobs:read"]):
        assert issue(system, scopes).status_code == 422
    assert len(system["identities"].list_credentials(system["service"]["id"])) == 2


def test_store_rejects_invalid_lists_with_sanitized_error(system):
    store, service = system["identities"], system["service"]["id"]
    for scopes in (PRIVILEGED, ["contexts:read", "contexts:read"], [True], [None], [{"x": "secret-value"}], [], None,
                   "contexts:read", ("contexts:read",), sorted(SERVICE_SCOPES) + ["jobs:read"], ["secret-value"]):
        with pytest.raises(ValueError) as error:
            store.issue_credential(service, "cred", scopes, 3600, "user:1")
        assert str(error.value) == "Invalid or duplicate service scopes."
    assert store.list_credentials(service) == []


def test_users_and_services_cannot_provision(system):
    first = issue(system, ["contexts:read"]).json()["token"]
    for headers in (system["user"], bearer(first)):
        assert system["client"].post(system["path"], json={"name": "x", "scopes": ["contexts:read"]}, headers=headers).status_code == 403


def test_old_credentials_keep_scopes_and_state_controls_still_apply(system):
    store, service, path = system["identities"], system["service"]["id"], system["tmp_path"] / "identities.sqlite3"
    old, old_token = store.issue_credential(service, "old", sorted(LEGACY_SCOPES), 3600, "user:1")
    new, new_token = store.issue_credential(service, "new", ["metadata:write", "contexts:read"], 3600, "user:1")
    assert new["scopes"] == ["contexts:read", "metadata:write"]
    store.close()
    system["identities"] = None
    reopened = IdentityStore(path)
    system["identities"] = reopened  # Fixture owns and closes the current store, including on failure.
    rows = {row["id"]: row["scopes"] for row in reopened._conn.execute("SELECT id,scopes FROM service_credentials")}
    assert json.loads(rows[old["id"]]) == sorted(LEGACY_SCOPES)
    assert reopened.authenticate(old_token)["scopes"] == sorted(LEGACY_SCOPES)
    assert reopened.authenticate(new_token)["scopes"] == ["contexts:read", "metadata:write"]
    reopened.revoke_credential(service, new["id"], "user:1")
    assert reopened.authenticate(new_token) is None
    with reopened._conn:
        reopened._conn.execute("UPDATE service_credentials SET expires_at=? WHERE id=?", (time.time() - 1, old["id"]))
    assert reopened.authenticate(old_token) is None


@pytest.mark.parametrize("last_used_at", [None, 1234.5])
@pytest.mark.parametrize("stored", ["not json", "{}", '"contexts:read"', "[]", '["unknown"]', '["contexts:publish"]',
                                    '["jobs:read","metadata:approve"]', '["jobs:read","jobs:read"]', "[true]", "[1]", "null",
                                    "[" * 1100 + "0" + "]" * 1100])
def test_invalid_persisted_scopes_never_authenticate(system, stored, last_used_at):
    store, service = system["identities"], system["service"]["id"]
    credential, token = store.issue_credential(service, "cred", ["jobs:read"], 3600, "user:1")
    with store._conn:
        store._conn.execute("UPDATE service_credentials SET scopes=?,last_used_at=? WHERE id=?", (stored, last_used_at, credential["id"]))
    assert store.authenticate(token) is None
    row = store._conn.execute("SELECT last_used_at FROM service_credentials WHERE id=?", (credential["id"],)).fetchone()
    assert row["last_used_at"] == last_used_at


# ---- 5-9. OAuth bindings ---------------------------------------------------

@pytest.fixture
def config(tmp_path):
    secret = tmp_path / "introspection.txt"
    secret.write_text("synthetic-test-secret", encoding="utf-8")
    return RemoteConfig("https://ocr.test", "https://identity.test/realms/ocr", "ocr-resource", secret,
                        {"subject-one": {"user_id": 1, "scopes": ["jobs:read"]}}, ("ocr-test",))


def bound(config, scopes, cutoff=None):
    binding = {"user_id": 1, "scopes": scopes}
    if cutoff is not None:
        binding["document_scopes_not_before"] = cutoff
    return RemoteConfig(config.origin, config.issuer, config.introspection_client_id, config.secret_file,
                        {"subject-one": binding}, config.allowed_clients)


def discovery(config):
    return {"issuer": config.issuer, "code_challenge_methods_supported": ["S256"],
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code"],
            "introspection_endpoint_auth_methods_supported": ["client_secret_basic"],
            **{name + "_endpoint": config.issuer + "/" + name
               for name in ("authorization", "token", "introspection", "revocation")}}


def claims(config, scope=" ".join(sorted(OAUTH_SCOPES)), **extra):
    now = time.time()
    return {"active": True, "iss": config.issuer, "iat": now - 100, "exp": now + 300, "sub": "subject-one",
            "aud": config.resource, "client_id": "ocr-test", "scope": scope, "token_type": "Bearer",
            "roles": ["admin"], **extra}


def request():
    return Request({"type": "http", "headers": [(b"authorization", b"Bearer synthetic-access-token")]})


def verifier_for(config, token_claims):
    def respond(req):
        if req.method == "GET":
            return httpx2.Response(200, json=discovery(config))
        return httpx2.Response(200, content=json.dumps(token_claims))
    users = {1: {"id": 1, "role": "admin"}}
    verifier = IntrospectionVerifier(config, transport=httpx2.MockTransport(respond),
                                    users=lambda: SimpleNamespace(get_user_by_id=users.get))
    return verifier, users


def authenticate(config, token_claims):
    verifier, _ = verifier_for(config, token_claims)
    async def run():
        await verifier.start()
        try:
            return await verifier.authenticate(request())
        finally:
            await verifier.close()
    return asyncio.run(run())


def denied(config, token_claims):
    with pytest.raises(ApplicationError) as error:
        authenticate(config, token_claims)
    assert error.value.status == 401 and error.value.code == "authentication_required"


def test_legacy_binding_limits_token_with_all_scopes(config):
    principal = authenticate(config, claims(config))
    assert principal.scopes == {"jobs:read"}
    legacy = bound(config, sorted(LEGACY_SCOPES))
    assert authenticate(legacy, claims(legacy)).scopes == LEGACY_SCOPES


@pytest.mark.parametrize("cutoff", [None, True, False, float("nan"), float("inf"), float("-inf"), 0, -1, 1e12, "1", [1],
                                    10**400, -(10**400)])
def test_document_binding_requires_valid_cutoff(config, cutoff):
    with pytest.raises(ValueError, match="Invalid explicit OAuth subject binding.") as error:
        bound(config, ["contexts:read"], cutoff)
    assert str(error.value) == "Invalid explicit OAuth subject binding."
    if cutoff is not None:  # An optional cutoff on a legacy binding is validated too.
        with pytest.raises(ValueError) as error:
            bound(config, ["jobs:read"], cutoff)
        assert str(error.value) == "Invalid explicit OAuth subject binding."


@pytest.mark.parametrize("scopes", [[], ["unknown"], ["jobs:read", "jobs:read"], ["contexts:read", "contexts:read"],
                                    [True], [None], "jobs:read", sorted(OAUTH_SCOPES) + ["jobs:read"]])
def test_binding_scope_list_validation(config, scopes):
    with pytest.raises(ValueError):
        bound(config, scopes, 1000)


def test_binding_unknown_keys_rejected_and_valid_forms_accepted(config):
    with pytest.raises(ValueError):
        RemoteConfig(config.origin, config.issuer, config.introspection_client_id, config.secret_file,
                     {"s": {"user_id": 1, "scopes": ["jobs:read"], "role": "admin"}}, config.allowed_clients)
    bound(config, ["jobs:read"], 1000)
    bound(config, ["jobs:read"], 1000.5)
    bound(config, sorted(OAUTH_SCOPES), 1000)


def test_recent_document_token_gets_exact_intersection(config):
    cutoff = time.time() - 1000
    full = bound(config, sorted(OAUTH_SCOPES), cutoff)
    principal = authenticate(full, claims(full))
    assert principal.scopes == OAUTH_SCOPES
    assert principal.auth_source == "oauth" and principal.is_admin is False and principal.kind == "user"
    assert principal.subject == "user:1" and principal.user_id == 1
    partial = authenticate(full, claims(full, scope="contexts:publish metadata:approve jobs:read unknown:x"))
    assert partial.scopes == {"contexts:publish", "metadata:approve", "jobs:read"}
    narrow = bound(config, ["contexts:read", "jobs:read"], cutoff)
    assert authenticate(narrow, claims(narrow)).scopes == {"contexts:read", "jobs:read"}


def test_old_token_keeps_only_legacy_and_bad_iat_is_rejected(config):
    now = time.time()
    full = bound(config, sorted(OAUTH_SCOPES), now - 50)
    assert authenticate(full, claims(full, iat=now - 60)).scopes == LEGACY_SCOPES
    assert authenticate(full, claims(full, iat=now - 50)).scopes == OAUTH_SCOPES
    only_docs = bound(config, ["contexts:read"], now - 50)
    assert authenticate(only_docs, claims(only_docs, iat=now - 60)).scopes == frozenset()
    for iat in (None, "now", True, False, float("nan"), float("inf"), float("-inf"), 0, -5, now + 600, [], {},
                10**400, -(10**400)):
        bad = claims(full, iat=iat)
        denied(full, bad)
    missing = claims(full)
    del missing["iat"]
    denied(full, missing)
    denied(full, claims(full, iat=now + 400))  # iat >= exp
    # Legacy-only grants never need iat.
    legacy = bound(config, sorted(LEGACY_SCOPES))
    no_iat = claims(legacy)
    del no_iat["iat"]
    assert authenticate(legacy, no_iat).scopes == LEGACY_SCOPES
    # A token whose scope has no document scope in the intersection needs no iat either.
    narrow = claims(full, scope="jobs:read")
    del narrow["iat"]
    assert authenticate(full, narrow).scopes == {"jobs:read"}


def test_reductions_apply_immediately_and_roles_grant_nothing(config):
    cutoff = time.time() - 1000
    full = bound(config, sorted(OAUTH_SCOPES), cutoff)
    token_claims = claims(full)
    verifier, users = verifier_for(full, token_claims)
    async def run():
        await verifier.start()
        try:
            assert (await verifier.authenticate(request())).scopes == OAUTH_SCOPES
            token_claims["scope"] = "jobs:read contexts:read"
            assert (await verifier.authenticate(request())).scopes == {"jobs:read", "contexts:read"}
            token_claims["roles"], token_claims["scope"] = ["admin"], "contexts:read"
            principal = await verifier.authenticate(request())
            assert principal.scopes == {"contexts:read"} and not principal.is_admin
            verifier.config = bound(config, ["jobs:read"], cutoff)
            assert (await verifier.authenticate(request())).scopes == frozenset()
            for change in ({"iss": "https://evil.test"}, {"aud": "https://other.test/mcp"}, {"client_id": "other"}):
                verifier.config = full
                token_claims.update(change)
                with pytest.raises(ApplicationError) as error:
                    await verifier.authenticate(request())
                assert error.value.status == 401
                token_claims.update(claims(full, scope="contexts:read"))
        finally:
            await verifier.close()
    asyncio.run(run())


# ---- 10. discovery and route boundaries ------------------------------------

def test_discovery_lists_eleven_and_routes_stay_private(config, tmp_path):
    assert MAX_JSON == 65536
    full = bound(config, sorted(OAUTH_SCOPES), time.time() - 1000)
    verifier, _ = verifier_for(full, claims(full))
    jobs = JobStore(tmp_path / "data")
    app = create_remote_app(full, verifier=verifier, service_provider=lambda: JobApplication(jobs))
    async def run():
        try:
            await verifier.start()
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=full.origin) as client:
                for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
                    response = await client.get(path)
                    assert response.status_code == 200
                    assert response.json()["scopes_supported"] == sorted(OAUTH_SCOPES) and len(OAUTH_SCOPES) == 11
                headers = {"authorization": "Bearer ocr_svc_" + "0" * 32 + "." + "A" * 43}
                assert (await client.get("/api/v1/me", headers=headers)).status_code == 401
                for path in ("/api/v1/service-principals", "/api/v1/metadata", "/api/v1/future",
                             "/api/v1/admin", "/api/v1/account", "/api/v1/accounts", "/api/v1/legacy",
                             "/admin", "/account", "/accounts", "/docs", "/redoc", "/openapi.json",
                             "/api/v1/document-types"):
                    assert (await client.get(path, headers={"authorization": "Bearer synthetic-access-token"})).status_code == 404
                    assert (await client.post(path, json={"name": "x"}, headers={"authorization": "Bearer synthetic-access-token"})).status_code == 404

                context_path = "/api/v1/contexts/" + "a" * 32
                type_path = "/api/v1/document-types/" + "b" * 32
                allowed = (
                    ("GET", "/api/v1/contexts"),
                    ("POST", "/api/v1/contexts"),
                    ("GET", context_path),
                    ("PUT", context_path),
                    ("GET", context_path + "/types"),
                    ("POST", context_path + "/types"),
                    ("GET", type_path),
                    ("PUT", type_path),
                    ("GET", type_path + "/versions"),
                    ("POST", type_path + "/versions"),
                    ("GET", type_path + "/versions/1"),
                    ("POST", type_path + "/versions/1/deprecate"),
                )
                for method, path in allowed:
                    response = await client.request(method, path)
                    assert response.status_code == 401, (method, path, response.text)
                    assert response.headers["www-authenticate"] == full.challenge
                response = await client.get(type_path + "/versions/9223372036854775807")
                assert response.status_code == 401
                assert response.headers["www-authenticate"] == full.challenge
                response = await client.post(type_path + "/versions/9223372036854775807/deprecate")
                assert response.status_code == 401
                assert response.headers["www-authenticate"] == full.challenge

                auth = bearer("synthetic-access-token")
                catalog_paths = {path for _, path in allowed}
                for path in catalog_paths:
                    for method in ("DELETE", "PATCH", "HEAD"):
                        response = await client.request(method, path, headers=auth)
                        assert response.status_code == 404, (method, path)
                    for suffix in ("/", "/extra"):
                        response = await client.get(path + suffix, headers=auth)
                        assert response.status_code == 404, path + suffix
                for method, path in (
                    ("POST", context_path), ("POST", type_path), ("POST", type_path + "/versions/1"),
                    ("PUT", "/api/v1/contexts"), ("PUT", context_path + "/types"),
                    ("PUT", type_path + "/versions"), ("PUT", type_path + "/versions/1"),
                ):
                    response = await client.request(method, path, headers=auth)
                    assert response.status_code == 404, (method, path)

                for method in ("GET", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
                    response = await client.request(method, type_path + "/versions/1/deprecate", headers=auth)
                    assert response.status_code == 404, method
                for path in (type_path + "/versions/deprecate", type_path + "/versions/1/deprecate/",
                             type_path + "/versions/1/deprecate/extra", type_path + "/1/deprecate"):
                    assert (await client.post(path, headers=auth)).status_code == 404, path

                for bad_id in ("A" * 32, "a" * 31, "a" * 33, "g" * 32):
                    for path in (
                        "/api/v1/contexts/" + bad_id,
                        "/api/v1/contexts/" + bad_id + "/types",
                        "/api/v1/document-types/" + bad_id,
                        "/api/v1/document-types/" + bad_id + "/versions",
                        "/api/v1/document-types/" + bad_id + "/versions/1",
                        "/api/v1/document-types/" + bad_id + "/versions/1/deprecate",
                    ):
                        for method in ("GET", "POST", "PUT"):
                            response = await client.request(method, path, headers=auth)
                            assert response.status_code == 404, (method, path)
                for version in ("0", "-1", "+1", "01", "1x", "1" + "0" * 19, "١"):
                    path = type_path + "/versions/" + version
                    response = await client.get(path, headers=auth)
                    assert response.status_code == 404, path
                    response = await client.post(path + "/deprecate", headers=auth)
                    assert response.status_code == 404, path + "/deprecate"
        finally:
            try:
                await verifier.close()
            finally:
                jobs._conn.close()
    asyncio.run(run())


# ---- 11. real catalog services through the OAuth surface -------------------

def catalog_headers():
    return {**bearer("synthetic-access-token"), "Idempotency-Key": uuid4().hex}


def oauth_catalog_snapshot(root):
    """Logical SQLite rows, including lifecycle and persisted idempotency ledger rows."""
    snapshot = {}
    for path in sorted(root.rglob("*")):
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
                snapshot[(str(path.relative_to(root)), table)] = tuple(sorted(cursor.fetchall(), key=repr))
        finally:
            connection.close()
    assert snapshot
    return snapshot


def test_oauth_deprecation_lifecycle_replays_denials_and_owner_isolation(config, tmp_path):
    cutoff = time.time() - 1000
    full = RemoteConfig(
        config.origin, config.issuer, config.introspection_client_id, config.secret_file,
        {subject: {"user_id": user_id, "scopes": sorted(OAUTH_SCOPES),
                   "document_scopes_not_before": cutoff}
         for subject, user_id in (("subject-one", 1), ("subject-two", 2))},
        config.allowed_clients,
    )
    token_claims = claims(full, iat=cutoff)
    verifier, users = verifier_for(full, token_claims)
    users[2] = {"id": 2, "role": "admin"}
    root = tmp_path / "data"
    jobs = JobStore(root)
    app = create_remote_app(full, verifier=verifier, service_provider=lambda: JobApplication(jobs))
    ctx = ContextInput.model_config['json_schema_extra']['examples'][0]
    typ = TypeInput.model_config['json_schema_extra']['examples'][0]
    auth = bearer("synthetic-access-token")
    body = {"expected_lifecycle_revision": 1}

    async def run():
        try:
            await verifier.start()
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=full.origin) as client:
                response = await client.post("/api/v1/contexts", json=ctx, headers=catalog_headers())
                assert response.status_code == 201, response.text
                context_path = "/api/v1/contexts/" + response.json()["context_id"]
                response = await client.post(context_path + "/types", json=typ, headers=catalog_headers())
                assert response.status_code == 201, response.text
                type_id = response.json()["type_id"]
                versions_path = "/api/v1/document-types/" + type_id + "/versions"
                publish_body = {"expected_revision": 1, "expected_context_revision": 1}
                publish_headers = catalog_headers()
                response = await client.post(versions_path, json=publish_body, headers=publish_headers)
                assert response.status_code == 201, response.text
                published = response.json()
                assert "lifecycle" not in published
                version_path = versions_path + "/1"
                path = version_path + "/deprecate"
                response = await client.get(version_path, headers=auth)
                assert response.status_code == 200, response.text
                original = response.json()
                canonical = json.dumps(original["schema"], sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False).encode()
                assert hashlib.sha256(canonical).hexdigest() == original["schema_sha256"] == published["schema_sha256"]
                before = oauth_catalog_snapshot(root)
                immutable = {(table, row) for table, rows in before.items() for row in rows
                             if canonical in row or canonical.decode("utf-8") in row}
                assert immutable
                row_counts = {table: len(rows) for table, rows in before.items()}

                response = await client.post(path, json=body)
                assert response.status_code == 401
                assert response.headers["www-authenticate"] == full.challenge
                # Both the binding and token must grant read + publish; old tokens retain only legacy scopes.
                for binding_scopes, token_scopes, issued_at in (
                    (["contexts:read", "contexts:write"], sorted(OAUTH_SCOPES), cutoff),
                    (sorted(OAUTH_SCOPES), ["contexts:read", "contexts:write"], cutoff),
                    (["contexts:publish"], sorted(OAUTH_SCOPES), cutoff),
                    (sorted(OAUTH_SCOPES), ["contexts:publish"], cutoff),
                    (sorted(LEGACY_SCOPES), sorted(OAUTH_SCOPES), cutoff),
                    (sorted(OAUTH_SCOPES), sorted(OAUTH_SCOPES), cutoff - 1),
                ):
                    verifier.config = bound(config, binding_scopes, cutoff)
                    token_claims.update(scope=" ".join(token_scopes), iat=issued_at)
                    for payload in (json.dumps(body), '{"PRIVATE-SENTINEL":'):
                        response = await client.post(
                            path, content=payload,
                            headers={**catalog_headers(), "Content-Type": "application/json"})
                        assert response.status_code == 403, response.text
                        assert response.json()["error"]["code"] == "insufficient_scope"
                        assert "PRIVATE" not in response.text and "synthetic-access-token" not in response.text
                    assert oauth_catalog_snapshot(root) == before

                verifier.config = full
                token_claims.update(scope=" ".join(sorted(OAUTH_SCOPES)), iat=cutoff, sub="subject-two")
                response = await client.post(path, json=body, headers=catalog_headers())
                assert response.status_code == 404, response.text
                assert response.json()["error"]["code"] == "type_version_not_found"
                token_claims["sub"] = "subject-one"
                for missing in (versions_path + "/2/deprecate",
                                "/api/v1/document-types/" + "0" * 32 + "/versions/1/deprecate"):
                    response = await client.post(missing, json=body, headers=catalog_headers())
                    assert response.status_code == 404, response.text
                    assert response.json()["error"]["code"] == "type_version_not_found"
                for version in ("9223372036854775808", "9999999999999999999"):
                    response = await client.post(versions_path + "/" + version + "/deprecate",
                                                 json=body, headers=catalog_headers())
                    assert response.status_code == 422, response.text
                for revision in (True, "1", 0, -1, 2**31, None):
                    response = await client.post(path, json={"expected_lifecycle_revision": revision},
                                                 headers=catalog_headers())
                    assert response.status_code == 422, response.text
                assert oauth_catalog_snapshot(root) == before
                assert {table: len(rows) for table, rows in oauth_catalog_snapshot(root).items()} == row_counts

                deprecate_headers = catalog_headers()
                response = await client.post(path, json=body, headers=deprecate_headers)
                assert response.status_code == 200, response.text
                assert response.headers["idempotency-replayed"] == "false"
                assert response.headers["cache-control"] == "no-store"
                receipt = response.json()
                assert set(receipt) == {"type_id", "version", "schema_sha256", "lifecycle"}
                assert (receipt["type_id"], receipt["version"], receipt["schema_sha256"]) == (type_id, 1, published["schema_sha256"])
                lifecycle = receipt["lifecycle"]
                assert set(lifecycle) == {"state", "revision", "changed_at"}
                assert lifecycle["state"] == "deprecated" and lifecycle["revision"] == 2
                assert lifecycle["changed_at"].endswith(("Z", "+00:00"))
                response = await client.get(version_path, headers=auth)
                assert response.status_code == 200, response.text
                assert response.json() == {**original, "lifecycle": lifecycle}
                assert json.dumps(response.json()["schema"], sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=False).encode() == canonical
                response = await client.get(versions_path, headers=auth)
                assert response.status_code == 200, response.text
                assert response.json() == {"items": [{**published, "lifecycle": lifecycle}], "next_cursor": None}
                persisted = oauth_catalog_snapshot(root)
                assert {(table, row) for table, rows in persisted.items() for row in rows
                        if canonical in row or canonical.decode("utf-8") in row} == immutable

                response = await client.post(path, json=body, headers=deprecate_headers)
                assert response.status_code == 200 and response.json() == receipt
                assert response.headers["idempotency-replayed"] == "true"
                assert oauth_catalog_snapshot(root) == persisted  # No extra ledger row on replay.
                for headers, revision, code in (
                    (catalog_headers(), 1, "type_lifecycle_revision_conflict"),
                    (catalog_headers(), 2, "type_version_already_deprecated"),
                    (deprecate_headers, 2, "idempotency_conflict"),
                ):
                    response = await client.post(path, json={"expected_lifecycle_revision": revision}, headers=headers)
                    assert response.status_code == 409, response.text
                    assert response.json()["error"]["code"] == code
                    assert "synthetic-access-token" not in response.text
                    assert oauth_catalog_snapshot(root) == persisted
                response = await client.post(versions_path, json=publish_body, headers=publish_headers)
                assert response.status_code == 200 and response.json() == published
                assert response.headers["idempotency-replayed"] == "true"
                token_claims["sub"] = "subject-two"
                response = await client.post(path, json=body, headers=deprecate_headers)
                assert response.status_code == 404, response.text
                assert response.json()["error"]["code"] == "type_version_not_found"
                response = await client.get(version_path, headers=auth)
                assert response.status_code == 404, response.text
                assert oauth_catalog_snapshot(root) == persisted
        finally:
            try:
                await verifier.close()
            finally:
                jobs._conn.close()
    asyncio.run(run())


def test_oauth_catalog_eleven_operations_and_owner_isolation(config, tmp_path):
    cutoff = time.time() - 1000
    full = RemoteConfig(
        config.origin, config.issuer, config.introspection_client_id, config.secret_file,
        {
            "subject-one": {"user_id": 1, "scopes": sorted(OAUTH_SCOPES),
                            "document_scopes_not_before": cutoff},
            "subject-two": {"user_id": 2, "scopes": sorted(OAUTH_SCOPES),
                            "document_scopes_not_before": cutoff},
        },
        config.allowed_clients,
    )
    token_claims = claims(full, iat=cutoff)
    verifier, users = verifier_for(full, token_claims)
    users[2] = {"id": 2, "role": "admin"}
    jobs = JobStore(tmp_path / "data")
    app = create_remote_app(full, verifier=verifier, service_provider=lambda: JobApplication(jobs))
    ctx = ContextInput.model_config['json_schema_extra']['examples'][0]
    typ = TypeInput.model_config['json_schema_extra']['examples'][0]
    auth = bearer("synthetic-access-token")

    async def run():
        try:
            await verifier.start()
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=full.origin) as client:
                response = await client.get("/api/v1/contexts", headers=auth)
                assert response.status_code == 200, response.text
                assert response.json()["items"] == []

                response = await client.post("/api/v1/contexts", json=ctx, headers=catalog_headers())
                assert response.status_code == 201, response.text
                context_id = response.json()["context_id"]
                context_path = "/api/v1/contexts/" + context_id

                response = await client.get("/api/v1/contexts", headers=auth)
                assert response.status_code == 200, response.text
                assert [item["context_id"] for item in response.json()["items"]] == [context_id]
                response = await client.get(context_path, headers=auth)
                assert response.status_code == 200, response.text
                assert response.json()["context_id"] == context_id
                response = await client.put(context_path, json={"body": ctx, "expected_revision": 1}, headers=auth)
                assert response.status_code == 200, response.text
                assert response.json()["revision"] == 2

                response = await client.post(context_path + "/types", json=typ, headers=catalog_headers())
                assert response.status_code == 201, response.text
                type_id = response.json()["type_id"]
                type_path = "/api/v1/document-types/" + type_id

                response = await client.get(context_path + "/types", headers=auth)
                assert response.status_code == 200, response.text
                assert [item["type_id"] for item in response.json()["items"]] == [type_id]
                response = await client.get(type_path, headers=auth)
                assert response.status_code == 200, response.text
                assert response.json()["type_id"] == type_id
                response = await client.put(type_path, json={"body": typ, "expected_revision": 1}, headers=auth)
                assert response.status_code == 200, response.text
                assert response.json()["revision"] == 2

                publish_body = {"expected_revision": 2, "expected_context_revision": 2}
                response = await client.post(type_path + "/versions", json=publish_body, headers=catalog_headers())
                assert response.status_code == 201, response.text
                receipt = response.json()
                assert receipt["version"] == 1
                assert len(receipt["schema_sha256"]) == 64
                response = await client.get(type_path + "/versions", headers=auth)
                assert response.status_code == 200, response.text
                assert [item["version"] for item in response.json()["items"]] == [1]
                version_path = type_path + "/versions/1"
                response = await client.get(version_path, headers=auth)
                assert response.status_code == 200, response.text
                assert "schema" in response.json() and "schema_document" not in response.json()

                # Nineteen ASCII digits pass the gateway; the route enforces int64.
                for version in ("9223372036854775808", "9999999999999999999"):
                    response = await client.get(type_path + "/versions/" + version, headers=auth)
                    assert response.status_code == 422, response.text

                # Introspection is repeated: the same token now identifies another bound admin user.
                token_claims["sub"] = "subject-two"
                response = await client.get("/api/v1/contexts", headers=auth)
                assert response.status_code == 200, response.text
                assert response.json()["items"] == []
                for path in (context_path, context_path + "/types", type_path,
                             type_path + "/versions", version_path):
                    response = await client.get(path, headers=auth)
                    assert response.status_code == 404, (path, response.text)
                for path, body in ((context_path, ctx), (type_path, typ)):
                    response = await client.put(path, json={"body": body, "expected_revision": 2}, headers=auth)
                    assert response.status_code == 404, (path, response.text)
                response = await client.post(type_path + "/versions", json=publish_body, headers=catalog_headers())
                assert response.status_code == 404, response.text
                response = await client.post(context_path + "/types", json=typ, headers=catalog_headers())
                assert response.status_code == 404, response.text

                token_claims["sub"] = "subject-one"
                response = await client.get("/api/v1/contexts", headers=auth)
                assert response.status_code == 200, response.text
                assert [item["context_id"] for item in response.json()["items"]] == [context_id]
                response = await client.get(context_path, headers=auth)
                assert response.status_code == 200 and response.json()["revision"] == 2
                response = await client.get(context_path + "/types", headers=auth)
                assert response.status_code == 200, response.text
                assert [item["type_id"] for item in response.json()["items"]] == [type_id]
                response = await client.get(type_path + "/versions", headers=auth)
                assert response.status_code == 200, response.text
                assert [item["version"] for item in response.json()["items"]] == [1]
        finally:
            try:
                await verifier.close()
            finally:
                jobs._conn.close()
    asyncio.run(run())


@pytest.mark.parametrize("binding_scopes,token_scopes,read_status", [
    (["contexts:read"], ["contexts:read"], 200),
    (["metadata:read"], ["metadata:read"], 403),
    (sorted(LEGACY_SCOPES), sorted(LEGACY_SCOPES), 403),
    (sorted(LEGACY_SCOPES), sorted(OAUTH_SCOPES), 403),
    (sorted(OAUTH_SCOPES), ["jobs:read"], 403),
])
def test_oauth_catalog_requires_explicit_grants_before_body_parsing(
        config, tmp_path, binding_scopes, token_scopes, read_status):
    cutoff = time.time() - 1000
    restricted = bound(config, binding_scopes, cutoff)
    token_claims = claims(restricted, scope=" ".join(token_scopes), iat=cutoff)
    verifier, _ = verifier_for(restricted, token_claims)
    jobs = JobStore(tmp_path / "data")
    app = create_remote_app(restricted, verifier=verifier, service_provider=lambda: JobApplication(jobs))
    ctx = ContextInput.model_config['json_schema_extra']['examples'][0]
    auth = bearer("synthetic-access-token")

    async def run():
        try:
            await verifier.start()
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=restricted.origin) as client:
                response = await client.get("/api/v1/contexts", headers=auth)
                assert response.status_code == read_status, response.text
                if read_status == 200:
                    assert response.json()["items"] == []
                response = await client.post("/api/v1/contexts", json=ctx, headers=catalog_headers())
                assert response.status_code == 403, response.text
                response = await client.post(
                    "/api/v1/contexts", content="{",
                    headers={**catalog_headers(), "Content-Type": "application/json"},
                )
                assert response.status_code == 403, response.text

                # Verify denied requests left the injected catalog empty using a fresh explicit grant.
                verifier.config = bound(config, sorted(OAUTH_SCOPES), cutoff)
                token_claims["scope"] = " ".join(sorted(OAUTH_SCOPES))
                response = await client.get("/api/v1/contexts", headers=auth)
                assert response.status_code == 200, response.text
                assert response.json()["items"] == []
        finally:
            try:
                await verifier.close()
            finally:
                jobs._conn.close()
    asyncio.run(run())


def test_oauth_context_templates_contract_and_isolation(config, tmp_path):
    cutoff = time.time() - 1000
    grants = ["contexts:read", "contexts:write"]
    full = RemoteConfig(config.origin, config.issuer, config.introspection_client_id, config.secret_file,
                        {subject: {"user_id": uid, "scopes": grants, "document_scopes_not_before": cutoff}
                         for subject, uid in (("subject-one", 1), ("subject-two", 2))}, config.allowed_clients)
    token_claims = claims(full, iat=cutoff)
    verifier, users = verifier_for(full, token_claims)
    users[2] = {"id": 2, "role": "admin"}
    root = tmp_path / "data"
    jobs = JobStore(root)
    app = create_remote_app(full, verifier=verifier, service_provider=lambda: JobApplication(jobs))
    listing_path = "/api/v1/context-templates"
    clone_path = listing_path + "/generic/clone"
    auth = bearer("synthetic-access-token")
    headers = {**auth, "Idempotency-Key": "oauth-template-clone-0001"}

    async def run():
        try:
            await verifier.start()
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=full.origin) as client:
                for method, path in (("GET", listing_path), ("POST", clone_path)):
                    response = await client.request(method, path)
                    assert response.status_code == 401, response.text
                    assert response.headers["www-authenticate"] == full.challenge
                for method, path in (
                    ("POST", listing_path), ("GET", clone_path), ("PUT", listing_path),
                    ("PUT", clone_path), ("DELETE", clone_path), ("PATCH", clone_path),
                    ("HEAD", listing_path), ("OPTIONS", clone_path),
                    ("GET", listing_path + "/"), ("GET", listing_path + "/extra"),
                    ("POST", clone_path + "/"), ("POST", clone_path + "/extra"),
                    ("POST", listing_path + "/Generic/clone"),
                    ("POST", listing_path + "/" + "a" * 65 + "/clone"),
                ):
                    response = await client.request(method, path)
                    assert response.status_code == 404, (method, path, response.text)
                response = await client.get(listing_path, headers=auth)
                assert response.status_code == 200, response.text
                listing = response.json()
                assert set(listing) == {"items"}
                assert [item["template_id"] for item in listing["items"]] == [
                    "generic", "real-estate-deed", "procurement-notice"]
                for item in listing["items"]:
                    assert set(item) == {"template_id", "template_version", "template_sha256", "context", "types"}
                    assert type(item["template_version"]) is int and item["template_version"] >= 1
                    assert len(item["template_sha256"]) == 64
                    assert set(item["template_sha256"]) <= set("0123456789abcdef")
                    assert isinstance(item["context"], dict) and item["context"]
                    assert item["types"]
                    for definition in item["types"]:
                        assert set(definition) == {"type_key", "body"}
                        assert isinstance(definition["type_key"], str) and definition["type_key"]
                        assert isinstance(definition["body"], dict) and definition["body"]
                before = oauth_catalog_snapshot(root)
                assert all(not rows for rows in before.values())  # Listing initializes tables without seeding rows.
                response = await client.get(listing_path, headers=auth)
                assert response.status_code == 200 and response.json() == listing
                assert oauth_catalog_snapshot(root) == before
                template = listing["items"][0]
                body = {"expected_version": template["template_version"],
                        "expected_sha256": template["template_sha256"]}
                response = await client.post(listing_path + "/" + "a" * 64 + "/clone", json=body, headers=headers)
                assert response.status_code == 404, response.text
                assert response.json()["error"]["code"] == "context_template_not_found"
                assert oauth_catalog_snapshot(root) == before
                for binding_scopes, token_scopes, issued_at in (
                    (["contexts:write"], grants, cutoff), (["contexts:read"], grants, cutoff),
                    (grants, ["contexts:write"], cutoff), (grants, ["contexts:read"], cutoff),
                    (grants, grants, cutoff - 1), (sorted(LEGACY_SCOPES), grants, cutoff),
                    (grants, sorted(LEGACY_SCOPES), cutoff),
                ):
                    verifier.config = bound(config, binding_scopes, cutoff)
                    token_claims.update(scope=" ".join(token_scopes), iat=issued_at)
                    if binding_scopes == ["contexts:read"] or token_scopes == ["contexts:read"]:
                        response = await client.get(listing_path, headers=auth)
                        assert response.status_code == 200 and response.json() == listing
                    for payload in (json.dumps(body), '{"PRIVATE":', "x" * (2 * 1024 * 1024 + 1)):
                        response = await client.post(clone_path, content=payload,
                                                     headers={**headers, "Content-Type": "application/json"})
                        assert response.status_code == 403, response.text
                        assert response.json()["error"]["code"] == "insufficient_scope"
                        assert "PRIVATE" not in response.text and "synthetic-access-token" not in response.text
                        assert oauth_catalog_snapshot(root) == before
                verifier.config = full
                token_claims.update(scope=" ".join(grants), iat=cutoff)
                response = await client.post(clone_path, json=body, headers=headers)
                assert response.status_code == 201, response.text
                assert response.headers["idempotency-replayed"] == "false"
                receipt_bytes, receipt = response.content, response.json()
                assert set(receipt) == {"template_id", "template_version", "template_sha256", "context_id",
                                        "context_revision", "types", "created_at"}
                for field in ("template_id", "template_version", "template_sha256"):
                    assert receipt[field] == template[field]
                assert receipt["context_revision"] == 1
                assert receipt["created_at"].endswith(("Z", "+00:00"))
                assert [item["type_key"] for item in receipt["types"]] == [item["type_key"] for item in template["types"]]
                context_path = "/api/v1/contexts/" + receipt["context_id"]
                response = await client.get(context_path, headers=auth)
                assert response.status_code == 200 and response.json()["body"] == template["context"]
                for cloned, definition in zip(receipt["types"], template["types"], strict=True):
                    assert set(cloned) == {"type_key", "type_id", "revision"}
                    assert cloned["revision"] == 1
                    type_path = "/api/v1/document-types/" + cloned["type_id"]
                    response = await client.get(type_path, headers=auth)
                    assert response.status_code == 200 and response.json()["body"] == definition["body"]
                    assert "schema" not in response.json() and "schema_document" not in response.json()
                    response = await client.get(type_path + "/versions", headers=auth)
                    assert response.status_code == 200 and response.json()["items"] == []
                saved = oauth_catalog_snapshot(root)
                response = await client.post(clone_path, json=body, headers=headers)
                assert response.status_code == 200 and response.content == receipt_bytes
                assert response.headers["idempotency-replayed"] == "true"
                assert oauth_catalog_snapshot(root) == saved
                response = await client.post(type_path + "/versions",
                                             json={"expected_revision": 1, "expected_context_revision": 1},
                                             headers={**auth, "Idempotency-Key": "oauth-template-publish-0001"})
                assert response.status_code == 403, response.text
                assert response.json()["error"]["code"] == "insufficient_scope"
                assert oauth_catalog_snapshot(root) == saved
                token_claims["sub"] = "subject-two"
                response = await client.get(listing_path, headers=auth)
                assert response.status_code == 200 and response.json() == listing
                for path in (context_path, *("/api/v1/document-types/" + item["type_id"] for item in receipt["types"])):
                    response = await client.get(path, headers=auth)
                    assert response.status_code == 404, response.text
                assert oauth_catalog_snapshot(root) == saved
                response = await client.post(clone_path, json=body, headers=headers)
                assert response.status_code == 201, response.text
                assert response.headers["idempotency-replayed"] == "false"
                other = response.json()
                assert {receipt["context_id"], *(item["type_id"] for item in receipt["types"])}.isdisjoint(
                    {other["context_id"], *(item["type_id"] for item in other["types"])})
                response = await client.get("/api/v1/contexts/" + other["context_id"], headers=auth)
                assert response.status_code == 200 and response.json()["body"] == template["context"]
                saved = oauth_catalog_snapshot(root)
                token_claims["sub"] = "subject-one"
                response = await client.post(clone_path, json=body, headers=headers)
                assert response.status_code == 200 and response.content == receipt_bytes
                assert response.headers["idempotency-replayed"] == "true"
                assert oauth_catalog_snapshot(root) == saved
        finally:
            try:
                await verifier.close()
            finally:
                jobs._conn.close()
    asyncio.run(run())


def test_oauth_catalog_read_only_cannot_update_create_types_or_publish(config, tmp_path):
    cutoff = time.time() - 1000
    full = bound(config, sorted(OAUTH_SCOPES), cutoff)
    token_claims = claims(full, iat=cutoff)
    verifier, _ = verifier_for(full, token_claims)
    jobs = JobStore(tmp_path / "data")
    app = create_remote_app(full, verifier=verifier, service_provider=lambda: JobApplication(jobs))
    ctx = ContextInput.model_config['json_schema_extra']['examples'][0]
    typ = TypeInput.model_config['json_schema_extra']['examples'][0]
    auth = bearer("synthetic-access-token")

    async def run():
        try:
            await verifier.start()
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=full.origin) as client:
                response = await client.post("/api/v1/contexts", json=ctx, headers=catalog_headers())
                assert response.status_code == 201, response.text
                context_path = "/api/v1/contexts/" + response.json()["context_id"]
                response = await client.post(context_path + "/types", json=typ, headers=catalog_headers())
                assert response.status_code == 201, response.text
                type_path = "/api/v1/document-types/" + response.json()["type_id"]
                type_id = response.json()["type_id"]

                verifier.config = bound(config, ["contexts:read"], cutoff)
                token_claims["scope"] = "contexts:read"
                for path in (context_path, context_path + "/types", type_path, type_path + "/versions"):
                    response = await client.get(path, headers=auth)
                    assert response.status_code == 200, (path, response.text)

                writes = (
                    ("PUT", context_path, {"body": ctx, "expected_revision": 1}),
                    ("POST", context_path + "/types", typ),
                    ("PUT", type_path, {"body": typ, "expected_revision": 1}),
                    ("POST", type_path + "/versions", {"expected_revision": 1, "expected_context_revision": 1}),
                )
                for method, path, body in writes:
                    response = await client.request(method, path, json=body, headers=catalog_headers())
                    assert response.status_code == 403, (method, path, response.text)
                    response = await client.request(
                        method, path, content="{",
                        headers={**catalog_headers(), "Content-Type": "application/json"},
                    )
                    assert response.status_code == 403, (method, path, response.text)

                response = await client.get(context_path, headers=auth)
                assert response.status_code == 200 and response.json()["revision"] == 1
                response = await client.get(type_path, headers=auth)
                assert response.status_code == 200 and response.json()["revision"] == 1
                response = await client.get(context_path + "/types", headers=auth)
                assert response.status_code == 200, response.text
                assert [item["type_id"] for item in response.json()["items"]] == [type_id]
                response = await client.get(type_path + "/versions", headers=auth)
                assert response.status_code == 200 and response.json()["items"] == []
        finally:
            try:
                await verifier.close()
            finally:
                jobs._conn.close()
    asyncio.run(run())
