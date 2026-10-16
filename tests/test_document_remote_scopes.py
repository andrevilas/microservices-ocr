"""Explicit document scopes for service credentials and OAuth bindings (synthetic; issuer is mocked)."""
import asyncio
import json
import time
from types import SimpleNamespace
from uuid import uuid4

import httpx2
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.application import ApplicationError, JobApplication
from app.config import settings
from app.main import create_app
from app.remote import IntrospectionVerifier, RemoteConfig, create_remote_app
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
    full = bound(config, sorted(OAUTH_SCOPES), time.time() - 1000)
    verifier, _ = verifier_for(full, claims(full))
    jobs = JobStore(tmp_path / "data")
    app = create_remote_app(full, verifier=verifier, service_provider=lambda: JobApplication(jobs))
    async def run():
        await verifier.start()
        try:
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=full.origin) as client:
                for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
                    response = await client.get(path)
                    assert response.status_code == 200
                    assert response.json()["scopes_supported"] == sorted(OAUTH_SCOPES) and len(OAUTH_SCOPES) == 11
                headers = {"authorization": "Bearer ocr_svc_" + "0" * 32 + "." + "A" * 43}
                assert (await client.get("/api/v1/me", headers=headers)).status_code == 401
                for path in ("/api/v1/service-principals", "/api/v1/contexts", "/api/v1/metadata", "/api/v1/future"):
                    assert (await client.get(path, headers={"authorization": "Bearer synthetic-access-token"})).status_code == 404
                    assert (await client.post(path, json={"name": "x"}, headers={"authorization": "Bearer synthetic-access-token"})).status_code == 404
        finally:
            await verifier.close()
            jobs._conn.close()
    asyncio.run(run())
