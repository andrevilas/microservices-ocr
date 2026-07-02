#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${OCR_K8S_NAMESPACE:-ix-ocr-recognizer}"
RELEASE_NAME="${OCR_TRUENAS_RELEASE:-ocr-recognizer}"
SECRET_NAME="${OCR_GHCR_SECRET_NAME:-ghcr-pull-secret}"
SERVER="${GHCR_SERVER:-ghcr.io}"
PATCH_RELEASE="${OCR_PATCH_RELEASE:-true}"
VALIDATE_PULL="${OCR_VALIDATE_PULL:-true}"
TEST_IMAGE="${OCR_GHCR_TEST_IMAGE:-}"
TEST_POD="ocr-ghcr-pull-test"
DRY_RUN="false"

usage() {
  cat <<'EOF'
Configure a dedicated GHCR pull secret for the microservices-ocr TrueNAS App.

Required environment:
  GHCR_USERNAME   GitHub/GHCR username or machine account
  GHCR_TOKEN      Token with read:packages for ghcr.io/andrevilas/microservices-ocr
  GHCR_EMAIL      Contact email for the docker-registry secret

Optional environment:
  OCR_K8S_NAMESPACE       default: ix-ocr-recognizer
  OCR_TRUENAS_RELEASE     default: ocr-recognizer
  OCR_GHCR_SECRET_NAME    default: ghcr-pull-secret
  OCR_PATCH_RELEASE       default: true
  OCR_VALIDATE_PULL       default: true
  OCR_GHCR_TEST_IMAGE     default: image from current TrueNAS App config

Options:
  --dry-run               Render intended actions without applying changes
  -h, --help              Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "$name is required" >&2
    exit 1
  fi
}

require_env GHCR_USERNAME
require_env GHCR_TOKEN
require_env GHCR_EMAIL

if [[ "$DRY_RUN" == "true" ]]; then
  echo "DRY-RUN: would configure docker-registry secret '$SECRET_NAME' in namespace '$NAMESPACE'."
  echo "DRY-RUN: would patch TrueNAS App '$RELEASE_NAME' imagePullSecrets when OCR_PATCH_RELEASE=true."
  echo "DRY-RUN: would validate image pull when OCR_VALIDATE_PULL=true."
  exit 0
fi

k3s kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || k3s kubectl create namespace "$NAMESPACE"

k3s kubectl -n "$NAMESPACE" create secret docker-registry "$SECRET_NAME" \
  --docker-server="$SERVER" \
  --docker-username="$GHCR_USERNAME" \
  --docker-password="$GHCR_TOKEN" \
  --docker-email="$GHCR_EMAIL" \
  --dry-run=client \
  -o yaml | k3s kubectl apply -f -

echo "Secret $SECRET_NAME configured in namespace $NAMESPACE."

if [[ "$PATCH_RELEASE" == "true" ]]; then
  SECRET_NAME="$SECRET_NAME" RELEASE_NAME="$RELEASE_NAME" python3 - <<'PY'
import json
import os
import subprocess
import time

release_name = os.environ["RELEASE_NAME"]
secret_name = os.environ["SECRET_NAME"]


def midclt(method, *args):
    cmd = ["midclt", "call", method]
    cmd.extend(json.dumps(arg) if isinstance(arg, (dict, list)) else str(arg) for arg in args)
    output = subprocess.check_output(cmd, text=True).strip()
    return json.loads(output) if output else None


releases = [release for release in midclt("chart.release.query") if release.get("name") == release_name]
if not releases:
    raise SystemExit(f"TrueNAS App release not found: {release_name}")
config = releases[0].get("config") or {}
secrets = [item for item in config.get("imagePullSecrets") or [] if item.get("name") != secret_name]
secrets.append({"name": secret_name})
config["imagePullSecrets"] = secrets
job_id = midclt("chart.release.update", release_name, {"values": config})
deadline = time.time() + 600
while time.time() < deadline:
    jobs = midclt("core.get_jobs", [["id", "=", job_id]])
    job = jobs[0] if jobs else None
    if job and job.get("state") in {"SUCCESS", "FAILED", "ABORTED"}:
        if job["state"] != "SUCCESS":
            raise SystemExit(f"chart.release.update failed: {job['state']}")
        print(f"TrueNAS App {release_name} patched with imagePullSecrets={secrets}.")
        break
    time.sleep(2)
else:
    raise SystemExit(f"Timed out waiting for TrueNAS job {job_id}")
PY
fi

if [[ "$VALIDATE_PULL" == "true" ]]; then
  if [[ -z "$TEST_IMAGE" ]]; then
    TEST_IMAGE="$(RELEASE_NAME="$RELEASE_NAME" python3 - <<'PY'
import json
import os
import subprocess

release_name = os.environ["RELEASE_NAME"]
releases = json.loads(subprocess.check_output(["midclt", "call", "chart.release.query"], text=True))
for release in releases:
    if release.get("name") != release_name:
        continue
    config = release.get("config") or {}
    image = {}
    for key in ("ocr", "ocr-recognizer"):
        if key in config and "image" in (config.get(key) or {}):
            image = config[key]["image"]
            break
    if not image and "image" in config:
        image = config["image"]
    print(f"{image.get('repository')}:{image.get('tag')}")
    break
PY
)"
  fi
  k3s kubectl -n "$NAMESPACE" delete pod "$TEST_POD" --ignore-not-found=true >/dev/null
  cat <<EOF | k3s kubectl -n "$NAMESPACE" apply -f -
apiVersion: v1
kind: Pod
metadata:
  name: $TEST_POD
  labels:
    app.kubernetes.io/name: ocr-ghcr-pull-test
spec:
  restartPolicy: Never
  imagePullSecrets:
    - name: $SECRET_NAME
  containers:
    - name: pull-test
      image: $TEST_IMAGE
      imagePullPolicy: Always
      command: ["sh", "-c", "sleep 20"]
EOF
  k3s kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/$TEST_POD" --timeout=180s
  k3s kubectl -n "$NAMESPACE" delete pod "$TEST_POD" --wait=false >/dev/null
  echo "Validated GHCR pull using $TEST_IMAGE and imagePullSecret $SECRET_NAME."
fi
