"""Generate a reviewable Keycloak realm, with no embedded credentials or users."""
import argparse
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from app.remote import https_url
from app.services.identity_store import SERVICE_SCOPES


def build_realm(resource_origin, clients):
    """clients maps pre-registered client IDs to exact, observed callback URLs."""
    https_url(resource_origin, origin_only=True)
    if not isinstance(clients, dict) or not clients:
        raise ValueError("Register at least one client with exact callback URLs.")
    registrations = []
    for client_id, redirects in clients.items():
        if (not isinstance(client_id, str) or not re.fullmatch(r"ocr-[a-z0-9-]{1,80}", client_id)
                or client_id == "ocr-resource" or not isinstance(redirects, list) or not redirects):
            raise ValueError("Invalid client registration.")
        for value in redirects:
            if not isinstance(value, str) or any(ord(c) < 33 or ord(c) > 126 for c in value):
                raise ValueError("An exact HTTPS or loopback HTTP callback is required.")
            parsed = urlsplit(value)
            if (parsed.fragment or parsed.query or parsed.username or parsed.password or parsed.port == 0
                    or any(c in value for c in ('*', '%', '\\', '"', '<', '>'))
                    or parsed.scheme not in ("https", "http")):
                raise ValueError("An exact HTTPS or loopback HTTP callback is required.")
            if parsed.scheme == "https":
                https_url(value.rstrip("/"))
            elif parsed.hostname not in ("127.0.0.1", "[::1]", "::1", "localhost"):
                raise ValueError("HTTP callbacks must be loopback addresses.")
        registrations.append({
            "clientId": client_id, "name": client_id, "protocol": "openid-connect", "enabled": True,
            "publicClient": True, "standardFlowEnabled": True, "implicitFlowEnabled": False,
            "directAccessGrantsEnabled": False, "serviceAccountsEnabled": False,
            "consentRequired": True, "redirectUris": redirects, "webOrigins": [],
            "attributes": {"pkce.code.challenge.method": "S256"},
            "defaultClientScopes": ["ocr-audience"], "optionalClientScopes": sorted(SERVICE_SCOPES),
        })
    # The environment substitution is performed by Keycloak's realm importer.
    # Only this confidential client can authenticate our introspection requests.
    registrations.append({
        "clientId": "ocr-resource", "protocol": "openid-connect", "enabled": True,
        "publicClient": False, "clientAuthenticatorType": "client-secret",
        "secret": "${OCR_INTROSPECTION_SECRET}", "standardFlowEnabled": False,
        "implicitFlowEnabled": False, "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False, "redirectUris": [], "webOrigins": [],
        "defaultClientScopes": [], "optionalClientScopes": [],
    })
    audience = {"name": "ocr-audience", "protocol": "openid-connect",
        "attributes": {"include.in.token.scope": "false", "display.on.consent.screen": "false"},
        "protocolMappers": [{"name": "ocr-resource", "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper", "consentRequired": False,
            "config": {"included.custom.audience": resource_origin + "/mcp",
                       "access.token.claim": "true", "id.token.claim": "false",
                       "introspection.token.claim": "true"}},
            {"name": "introspection-client", "protocol": "openid-connect",
             "protocolMapper": "oidc-audience-mapper", "consentRequired": False,
             "config": {"included.client.audience": "ocr-resource", "access.token.claim": "true",
                        "id.token.claim": "false", "introspection.token.claim": "true"}},
            {"name": "subject", "protocol": "openid-connect", "protocolMapper": "oidc-sub-mapper",
             "consentRequired": False, "config": {"access.token.claim": "true", "introspection.token.claim": "true"}}]}
    scopes = [{"name": name, "protocol": "openid-connect",
               "attributes": {"include.in.token.scope": "true", "display.on.consent.screen": "true",
                              "consent.screen.text": name}, "protocolMappers": []}
              for name in sorted(SERVICE_SCOPES)]
    return {
        "realm": "ocr", "enabled": True, "sslRequired": "all",
        "registrationAllowed": False, "resetPasswordAllowed": False,
        "rememberMe": False, "bruteForceProtected": True,
        "accessTokenLifespan": 300, "ssoSessionIdleTimeout": 1800,
        "ssoSessionMaxLifespan": 28800, "revokeRefreshToken": True, "refreshTokenMaxReuse": 0,
        "clientScopes": [audience, *scopes], "clients": registrations,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-origin", required=True)
    parser.add_argument("--clients", type=Path, required=True, help="JSON: client ID -> exact callback URLs")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    realm = build_realm(args.resource_origin, json.loads(args.clients.read_text(encoding="utf-8")))
    # Refuse to overwrite a hand-edited realm or existing secret-bearing export.
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(realm, stream, ensure_ascii=False, indent=2)
    print("Realm configuration generated; no users or credentials were exported.")


if __name__ == "__main__":
    main()
