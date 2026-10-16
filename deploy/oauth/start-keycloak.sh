#!/bin/bash
set -eu
# Never enable shell tracing. Values remain outside Compose environment dumps.
export KC_DB_PASSWORD="$(cat /run/secrets/database_password)"
export KC_BOOTSTRAP_ADMIN_PASSWORD="$(cat /run/secrets/bootstrap_password)"
export OCR_INTROSPECTION_SECRET="$(cat /run/secrets/introspection_secret)"
exec /opt/keycloak/bin/kc.sh start --import-realm
