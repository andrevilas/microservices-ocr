"""Opt-in OAuth resource server. Run uvicorn app.remote:create_remote_app --factory.

The established authorization server owns login, consent, PKCE and token issuance.
This entrypoint never accepts the application's legacy JWT or service credentials.

Subject bindings are explicit: {"user_id": int, "scopes": [...]}. A binding that
contains any document scope (contexts:*, metadata:*) must also carry
"document_scopes_not_before", a finite Unix timestamp (0 < x < 1e12); the
legacy-only format stays valid, and the field is optional there. Operators set the
cutoff after the last second in which old tokens were issued (for example
floor(now)+1) and request new consent/tokens. A token whose verified "iat" is
before the cutoff keeps only its legacy scopes, so an old token that already
carried a previously unsupported name is never promoted. The cutoff is never
generated automatically.
"""
import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import logging
import json
import ipaddress
import math
import os
from pathlib import Path
import re
import ssl
import sqlite3
import time
from urllib.parse import urlsplit

import httpx2
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.application import ApplicationError, Principal
from app.http_contract import error_response
from app.observability import admit_principal
from app.security import authenticate_bearer
from app.scopes import DOCUMENT_SCOPES, LEGACY_SCOPES, OAUTH_SCOPES
from app.services.user_store import get_user_store

MAX_JSON = 65536
METADATA_PATH = "/.well-known/oauth-protected-resource/mcp"
# Explicit paths and methods; account/admin/legacy/docs and future APIs are private.
OPERATIONS = {
    "GET": [r"/api/v1/(me|capabilities|metrics|jobs|batches)",
            r"/api/v1/(jobs|batches|uploads|exports)/[0-9a-f]{32}",
            r"/api/v1/jobs/[0-9a-f]{32}/(text|report|artifact|artifact/content)",
            r"/api/v1/exports/[0-9a-f]{32}/content",
            r"/api/v1/context-templates",
            r"/api/v1/contexts",
            r"/api/v1/contexts/[0-9a-f]{32}",
            r"/api/v1/contexts/[0-9a-f]{32}/types",
            r"/api/v1/document-types/[0-9a-f]{32}",
            r"/api/v1/document-types/[0-9a-f]{32}/versions",
            r"/api/v1/document-types/[0-9a-f]{32}/versions/[1-9][0-9]{0,18}",
            r"/api/v1/jobs/[0-9a-f]{32}/metadata",
            r"/api/v1/jobs/[0-9a-f]{32}/metadata/revisions",
            r"/api/v1/jobs/[0-9a-f]{32}/metadata/revisions/[0-9a-f]{32}",
            r"/api/v1/jobs/[0-9a-f]{32}/metadata/revisions/[0-9a-f]{32}/content",
            r"/api/v1/jobs/[0-9a-f]{32}/metadata/revisions/[0-9a-f]{32}/segments/[0-9a-f]{32}/fields",
            r"/api/v1/jobs/[0-9a-f]{32}/metadata/revisions/[0-9a-f]{32}/segments/[0-9a-f]{32}/fields/[a-z][a-z0-9_]{0,63}/values",
            r"/api/v1/jobs/[0-9a-f]{32}/metadata/revisions/[0-9a-f]{32}/segments/[0-9a-f]{32}/fields/[a-z][a-z0-9_]{0,63}/evidence",
            r"/api/v1/jobs/[0-9a-f]{32}/metadata/revisions/[0-9a-f]{32}/segments/[0-9a-f]{32}/fields/[a-z][a-z0-9_]{0,63}/strings/(raw|normalized)",
            r"/api/v1/jobs/[0-9a-f]{32}/metadata/revisions/[0-9a-f]{32}/schemas/[0-9a-f]{64}",
            r"/api/v1/jobs/[0-9a-f]{32}/canonical/manifest",
            r"/api/v1/jobs/[0-9a-f]{32}/canonical/pages",
            r"/api/v1/jobs/[0-9a-f]{32}/canonical/pages/[1-9][0-9]{0,2}",
            r"/api/v1/jobs/[0-9a-f]{32}/canonical/pages[.]jsonl",
            r"/api/v1/jobs/[0-9a-f]{32}/packages",
            r"/api/v1/jobs/[0-9a-f]{32}/packages/[0-9a-f]{32}",
            r"/api/v1/jobs/[0-9a-f]{32}/packages/[0-9a-f]{32}/content",
            r"/api/v1/jobs/[0-9a-f]{32}/graph",
            r"/api/v1/jobs/[0-9a-f]{32}/extractions",
            r"/api/v1/jobs/[0-9a-f]{32}/extractions/[0-9a-f]{32}"],
    "POST": [r"/api/v1/(upload-sessions|uploads|jobs|batches|exports|jobs/cancel-batch)",
             r"/api/v1/jobs/[0-9a-f]{32}/(cancel|retry)",
             r"/api/v1/context-templates/(?=[a-z0-9-]{1,64}/clone$)[a-z][a-z0-9]*(?:-[a-z0-9]+)*/clone",
             r"/api/v1/contexts",
             r"/api/v1/contexts/[0-9a-f]{32}/types",
             r"/api/v1/document-types/[0-9a-f]{32}/versions",
             r"/api/v1/document-types/[0-9a-f]{32}/versions/[1-9][0-9]{0,18}/deprecate",
             r"/api/v1/jobs/[0-9a-f]{32}/metadata/approve",
             r"/api/v1/jobs/[0-9a-f]{32}/packages",
             r"/api/v1/jobs/[0-9a-f]{32}/extractions",
             r"/api/v1/jobs/[0-9a-f]{32}/extractions/[0-9a-f]{32}/apply"],
    "PUT": [r"/api/v1/uploads/[0-9a-f]{32}/content",
            r"/api/v1/contexts/[0-9a-f]{32}",
            r"/api/v1/document-types/[0-9a-f]{32}",
            r"/api/v1/jobs/[0-9a-f]{32}/metadata"],
    "DELETE": [r"/api/v1/jobs/[0-9a-f]{32}/artifacts"],
}


def https_url(value, *, origin_only=False):
    if not isinstance(value, str) or not value or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("Expected a canonical HTTPS URL.")
    parsed = urlsplit(value)
    if (not value.startswith("https://") or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.port == 0 or "*" in value
            or parsed.netloc != parsed.netloc.lower() or "%" in value or "\\" in value
            or value.endswith("/") or (origin_only and parsed.path)
            or not re.fullmatch(r"[A-Za-z0-9/_~.:-]*", parsed.path)
            or any(p in (".", "..", "") for p in parsed.path.split("/")[1:])):
        raise ValueError("Expected a canonical HTTPS URL.")
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        if len(parsed.hostname) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                              for label in parsed.hostname.split(".")):
            raise ValueError("Expected a canonical HTTPS URL.") from None
    return value


@dataclass(frozen=True)
class RemoteConfig:
    origin: str
    issuer: str
    introspection_client_id: str
    secret_file: Path
    # subject -> {user_id: int, scopes: list[str][, document_scopes_not_before: number]};
    # exact issuer is globally pinned.
    subjects: dict
    allowed_clients: tuple[str, ...]
    ca_file: Path | None = None
    # Production behind the tunnel/NPM: the proxy proves origin with this shared secret
    # header instead of TLS reaching the process (plain HTTP on the host only).
    gateway_secret_file: Path | None = None

    def __post_init__(self):
        https_url(self.origin, origin_only=True)
        https_url(self.issuer)
        def client_id(value):
            return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", value)
        if (not client_id(self.introspection_client_id)
                or not isinstance(self.secret_file, Path) or not self.secret_file.is_absolute()
                or (self.ca_file is not None and (not isinstance(self.ca_file, Path) or not self.ca_file.is_absolute()))
                or (self.gateway_secret_file is not None and (not isinstance(self.gateway_secret_file, Path)
                                                             or not self.gateway_secret_file.is_absolute()))
                or not isinstance(self.allowed_clients, tuple) or not self.allowed_clients
                or any(not client_id(c) for c in self.allowed_clients)
                or not isinstance(self.subjects, dict)):
            raise ValueError("Invalid OAuth integration configuration.")
        for subject, binding in self.subjects.items():
            if (not isinstance(subject, str) or not 1 <= len(subject) <= 512 or not isinstance(binding, dict)
                    or not {"user_id", "scopes"} <= set(binding)
                    or not set(binding) <= {"user_id", "scopes", "document_scopes_not_before"}
                    or type(binding["user_id"]) is not int
                    or not 0 < binding["user_id"] <= 9223372036854775807
                    or not isinstance(binding["scopes"], list)
                    or not 0 < len(binding["scopes"]) <= len(OAUTH_SCOPES)
                    or any(type(s) is not str or s not in OAUTH_SCOPES for s in binding["scopes"])
                    or len(set(binding["scopes"])) != len(binding["scopes"])):
                raise ValueError("Invalid explicit OAuth subject binding.")
            cutoff = binding.get("document_scopes_not_before")
            documental = not set(binding["scopes"]) <= LEGACY_SCOPES
            if "document_scopes_not_before" in binding:
                if type(cutoff) not in (int, float) or not 0 < cutoff < 1e12:
                    raise ValueError("Invalid explicit OAuth subject binding.")
            elif documental:
                raise ValueError("Invalid explicit OAuth subject binding.")

    @property
    def resource(self):
        return self.origin + "/mcp"

    @property
    def challenge(self):
        return f'Bearer resource_metadata="{self.origin}{METADATA_PATH}", scope="jobs:read"'

    @classmethod
    def from_environment(cls):
        try:
            path = Path(os.environ["OCR_REMOTE_CONFIG_FILE"])
            if not path.is_absolute():
                raise ValueError()
            with path.open(encoding="utf-8") as stream:
                content = stream.read(MAX_JSON + 1)
            if len(content) > MAX_JSON:
                raise ValueError()
            data = json.loads(content)
            data["secret_file"] = Path(data["secret_file"])
            if not isinstance(data["allowed_clients"], list):
                raise ValueError()
            data["allowed_clients"] = tuple(data["allowed_clients"])
            if data.get("ca_file"):
                data["ca_file"] = Path(data["ca_file"])
            if data.get("gateway_secret_file"):
                data["gateway_secret_file"] = Path(data["gateway_secret_file"])
            return cls(**data)
        except (KeyError, TypeError, ValueError, OSError):
            raise ValueError("Set OCR_REMOTE_CONFIG_FILE to a valid absolute private configuration file.") from None


class IntrospectionVerifier:
    def __init__(self, config, *, transport=None, users=get_user_store):
        self.config, self.transport, self.users = config, transport, users
        self.http = None
        self.endpoint = None
        self.secret = None

    async def _json(self, method, url, **kwargs):
        try:
            async with asyncio.timeout(5):
                async with self.http.stream(method, url, **kwargs) as response:
                    if response.status_code != 200:
                        raise ValueError()
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_JSON:
                            raise ValueError()
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise ValueError()
                    return value
        except (httpx2.HTTPError, ValueError, TimeoutError, UnicodeError):
            raise ApplicationError(503, "identity_unavailable", "Identity verification is unavailable.") from None

    async def start(self):
        try:
            with self.config.secret_file.open(encoding="utf-8") as stream:
                self.secret = stream.read(4097).rstrip("\r\n")
            if not self.secret or len(self.secret) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in self.secret):
                raise ValueError()
            context = ssl.create_default_context()
            if self.config.ca_file:
                context.load_verify_locations(str(self.config.ca_file))
            self.http = httpx2.AsyncClient(verify=context, transport=self.transport, trust_env=False,
                                           follow_redirects=False, timeout=5, limits=httpx2.Limits(max_connections=16))
            metadata = await self._json("GET", self.config.issuer + "/.well-known/openid-configuration")
            if (metadata.get("issuer") != self.config.issuer
                    or "S256" not in metadata.get("code_challenge_methods_supported", [])
                    or "code" not in metadata.get("response_types_supported", [])
                    or "authorization_code" not in metadata.get("grant_types_supported", [])
                    or "client_secret_basic" not in metadata.get("introspection_endpoint_auth_methods_supported", [])):
                raise ValueError()
            for key in ("authorization_endpoint", "token_endpoint", "introspection_endpoint", "revocation_endpoint"):
                endpoint = https_url(metadata.get(key))
                if urlsplit(endpoint).netloc != urlsplit(self.config.issuer).netloc:
                    raise ValueError()
            self.endpoint = metadata["introspection_endpoint"]
        except Exception:
            await self.close()
            raise ValueError("OAuth provider discovery/configuration failed validation.") from None

    async def close(self):
        if self.http:
            await self.http.aclose()
        self.http = self.endpoint = self.secret = None

    async def authenticate(self, request):
        denied = ApplicationError(401, "authentication_required", "A valid OAuth access token is required.")
        values = request.headers.getlist("authorization")
        if len(values) != 1:
            raise denied
        scheme, _, token = values[0].partition(" ")
        if (scheme.lower() != "bearer" or not 1 <= len(token) <= 8192
                or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token)
                or token.startswith("ocr_svc_")):
            raise denied
        if not self.endpoint:
            raise ApplicationError(503, "identity_unavailable", "Identity verification is unavailable.")
        claims = await self._json("POST", self.endpoint, data={"token": token, "token_type_hint": "access_token"},
                                  auth=httpx2.BasicAuth(self.config.introspection_client_id, self.secret))
        now = time.time()
        expiry, subject, scopes = claims.get("exp"), claims.get("sub"), claims.get("scope")
        audience = claims.get("aud")
        audience = [audience] if isinstance(audience, str) else audience
        not_before = claims.get("nbf", 0)
        token_type = claims.get("token_type")
        if (claims.get("active") is not True or claims.get("iss") != self.config.issuer
                or type(expiry) not in (int, float) or not (now < expiry < 1e12)
                or type(not_before) not in (int, float) or not (-1e12 < not_before <= now)
                or not isinstance(audience, list) or any(not isinstance(a, str) for a in audience)
                or self.config.resource not in audience or claims.get("client_id") not in self.config.allowed_clients
                or not isinstance(token_type, str) or token_type.lower() != "bearer"
                or not isinstance(subject, str) or subject not in self.config.subjects
                or not isinstance(scopes, str) or len(scopes) > 4096):
            raise denied
        binding = self.config.subjects[subject]
        try:
            user = await run_in_threadpool(lambda: self.users().get_user_by_id(binding["user_id"]))
        except (OSError, sqlite3.Error):
            raise ApplicationError(503, "identity_unavailable", "Identity verification is unavailable.") from None
        if user is None:
            raise denied
        allowed = frozenset(scopes.split()) & frozenset(binding["scopes"]) & OAUTH_SCOPES
        if allowed & DOCUMENT_SCOPES:
            issued_at = claims.get("iat")
            if (type(issued_at) not in (int, float)
                    or not 0 < issued_at <= now or not issued_at < expiry):
                raise denied
            if issued_at < binding.get("document_scopes_not_before", math.inf):
                allowed -= DOCUMENT_SCOPES  # Older tokens are never promoted to document scopes.
        # OAuth never gains local administrative powers from roles/email/claims.
        return Principal(f"user:{user['id']}", user["id"], allowed, kind="user", is_admin=False, auth_source="oauth")


async def remote_principal(request: Request):
    principal = request.scope.get("state", {}).get("ocr_remote_principal")
    if principal is None:
        raise ApplicationError(401, "authentication_required", "OAuth authentication is required.")
    return principal


GATEWAY_HEADER = b"x-ocr-gateway"


def read_gateway_secret(path):
    try:
        with path.open(encoding="utf-8") as stream:
            secret = stream.read(257).rstrip("\r\n")
    except OSError:
        raise ValueError("OAuth gateway secret is unavailable.") from None
    if not 32 <= len(secret) <= 256 or any(ord(c) < 33 or ord(c) > 126 for c in secret):
        raise ValueError("OAuth gateway secret is invalid.")
    return secret.encode("ascii")


class RemoteSurface:
    def __init__(self, app, verifier):
        self.app, self.verifier, self.config = app, verifier, verifier.config
        self.gateway = (read_gateway_secret(self.config.gateway_secret_file)
                        if self.config.gateway_secret_file else None)

    def _secure(self, scope):
        """TLS to this process, or the configured gateway's secret (header removed before the app)."""
        values = [v for k, v in scope.get("headers", []) if k == GATEWAY_HEADER]
        scope["headers"] = [(k, v) for k, v in scope.get("headers", []) if k != GATEWAY_HEADER]
        if self.gateway is None:
            return scope.get("scheme") == "https" and not values
        # Fixed-length digests: the comparison reveals neither content nor the secret's length.
        provided = hashlib.sha256(values[0] if len(values) == 1 else b"").digest()
        return len(values) == 1 and hmac.compare_digest(provided, hashlib.sha256(self.gateway).digest())

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            async def lifecycle_receive():
                message = await receive()
                if message["type"] == "lifespan.startup":
                    try:
                        await self.verifier.start()
                    except Exception:
                        await send({"type": "lifespan.startup.failed", "message": "OAuth provider validation failed."})
                        raise ValueError("OAuth provider validation failed.") from None
                return message
            try:
                return await self.app(scope, lifecycle_receive, send)
            finally:
                await self.verifier.close()
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        request = Request(scope)
        host = urlsplit(self.config.origin).netloc
        headers = {"Cache-Control": "no-store"}
        if (not self._secure(scope) or request.headers.getlist("host") != [host]
                or ("origin" in request.headers and request.headers.getlist("origin") != [self.config.origin])):
            return await error_response(request, 403, "origin_rejected", "Origin is not allowed.")(scope, receive, send)
        path, method = scope["path"], scope["method"]
        # Credentials must never enter URL histories or access logs.
        if "access_token" in request.query_params:
            return await error_response(request, 400, "invalid_request", "Use the Authorization header.")(scope, receive, send)
        if path in (METADATA_PATH, "/.well-known/oauth-protected-resource") and method == "GET":
            return await JSONResponse({"resource": self.config.resource, "authorization_servers": [self.config.issuer],
                "scopes_supported": sorted(OAUTH_SCOPES), "bearer_methods_supported": ["header"]}, headers=headers)(scope, receive, send)
        allowed = (path == "/mcp" and method in {"POST", "GET", "DELETE"}) or any(
            re.fullmatch(pattern, path) for pattern in OPERATIONS.get(method, []))
        if not allowed:
            return await error_response(request, 404, "route_not_found", "Route not found.")(scope, receive, send)
        try:
            principal = await self.verifier.authenticate(request)
        except ApplicationError as error:
            extra = {"WWW-Authenticate": self.config.challenge} if error.status == 401 else {"Retry-After": "5"}
            return await error_response(request, error.status, error.code, error.message, headers=extra)(scope, receive, send)
        scope.setdefault("state", {})["ocr_remote_principal"] = principal
        await self.app(scope, receive, send)


def create_remote_app(config=None, *, verifier=None, service_provider=None):
    from app.main import create_app
    config = config or RemoteConfig.from_environment()
    verifier = verifier or IntrospectionVerifier(config)
    security = TransportSecuritySettings(allowed_hosts=[urlsplit(config.origin).netloc], allowed_origins=[config.origin])
    kwargs = {"request_authenticator": remote_principal, "transport_security": security}
    if service_provider:
        kwargs["service_provider"] = service_provider
    return RemoteSurface(create_app(**kwargs), verifier)


async def combined_principal(request: Request):
    """One process, two surfaces: OAuth principals set by RemoteSurface, else the local Bearer."""
    principal = request.scope.get("state", {}).get("ocr_remote_principal")
    if principal is not None:
        admit_principal(principal)
        return principal
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    return await run_in_threadpool(authenticate_bearer, token if scheme.lower() == "bearer" else None)


LOOPBACK_HOSTS = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"]
LOOPBACK_ORIGINS = ["http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*",
                    "http://[::1]", "http://[::1]:*"]


class HostSplit:
    """Route the remote hostname through RemoteSurface; every other host reaches the app unchanged.

    The identity provider is discovered in the background and retried: while it is
    unavailable the remote surface answers 503 and the local application keeps serving.
    """
    RETRY_SECONDS = 30

    def __init__(self, app, surface, host):
        self.app, self.surface, self.host = app, surface, host.encode("ascii")
        self._task = None

    async def _discover(self):
        while True:
            try:
                await self.surface.verifier.start()
                logging.getLogger(__name__).info("OAuth provider discovery succeeded")
                return
            except Exception:
                logging.getLogger(__name__).warning("OAuth provider discovery failed; retrying")
                await asyncio.sleep(self.RETRY_SECONDS)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            async def lifecycle_receive():
                message = await receive()
                if message["type"] == "lifespan.startup" and self._task is None:
                    self._task = asyncio.create_task(self._discover())
                return message
            try:
                return await self.app(scope, lifecycle_receive, send)
            finally:
                if self._task is not None:
                    self._task.cancel()
                    self._task = None
                await self.surface.verifier.close()
        if scope["type"] in ("http", "websocket"):
            hosts = [v for k, v in scope.get("headers", []) if k == b"host"]
            if hosts and hosts[0].lower() == self.host:
                return await self.surface(scope, receive, send)
        return await self.app(scope, receive, send)


def create_served_app(config=None, *, verifier=None, service_provider=None):
    """Production entrypoint with OCR_REMOTE_CONFIG_FILE: local surface plus the OAuth host."""
    from app.main import create_app
    config = config or RemoteConfig.from_environment()
    verifier = verifier or IntrospectionVerifier(config)
    host = urlsplit(config.origin).netloc
    security = TransportSecuritySettings(allowed_hosts=[*LOOPBACK_HOSTS, host],
                                         allowed_origins=[*LOOPBACK_ORIGINS, config.origin])
    kwargs = {"request_authenticator": combined_principal, "transport_security": security}
    if service_provider:
        kwargs["service_provider"] = service_provider
    app = create_app(**kwargs)
    return HostSplit(app, RemoteSurface(app, verifier), host)
