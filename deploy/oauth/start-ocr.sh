#!/bin/sh
set -eu
export JWT_SECRET="$(cat /run/secrets/ocr_jwt_secret)"
export ADMIN_PASSWORD="$(cat /run/secrets/ocr_admin_password)"
exec uvicorn app.remote:create_remote_app --factory --host 0.0.0.0 --port 8000 \
  --workers 1 --no-proxy-headers --no-access-log \
  --ssl-certfile /run/secrets/ocr_certificate --ssl-keyfile /run/secrets/ocr_private_key
