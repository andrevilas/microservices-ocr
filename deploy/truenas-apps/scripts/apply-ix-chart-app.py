#!/usr/bin/env python3
"""Create or update the microservices-ocr TrueNAS ix-chart release."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path


CATALOG = "TRUENAS"
TRAIN = "charts"
ITEM = "ix-chart"
VERSION = "2403.0.0"

HOST = os.environ.get("OCR_HOST", "192.168.3.140")
PUBLIC_URL = os.environ.get("OCR_PUBLIC_URL", f"http://{HOST}:31800")
WEB_PORT = int(os.environ.get("OCR_WEB_PORT", "31800"))
IMAGE_REPOSITORY = os.environ.get("OCR_IMAGE_REPOSITORY", "ghcr.io/andrevilas/microservices-ocr")
IMAGE_TAG = os.environ.get("OCR_IMAGE_TAG", "latest")
RELEASE_NAME = os.environ.get("OCR_TRUENAS_RELEASE", "ocr-recognizer")
DATASET_ROOT = os.environ.get("OCR_DATASET_ROOT", "NVME/ocr-apps")
DATA_DIR = os.environ.get("OCR_DATA_DIR", "/mnt/NVME/ocr-apps/data")
ENABLE_RESOURCE_LIMITS = os.environ.get("OCR_ENABLE_RESOURCE_LIMITS", "false").lower() == "true"


def run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, capture_output=True, check=check)


def output(args: list[str], *, check: bool = True) -> str:
    return run(args, check=check).stdout.strip()


def midclt(method: str, *args):
    cmd = ["midclt", "call", method]
    cmd.extend(json.dumps(arg) if isinstance(arg, (dict, list)) else str(arg) for arg in args)
    data = output(cmd)
    return json.loads(data) if data else None


def wait_job(job_id: int) -> dict:
    deadline = time.time() + 600
    while time.time() < deadline:
        jobs = midclt("core.get_jobs", [["id", "=", job_id]])
        job = jobs[0] if jobs else None
        if job and job.get("state") in {"SUCCESS", "FAILED", "ABORTED"}:
            if job["state"] != "SUCCESS":
                print(json.dumps(job, indent=2), file=sys.stderr)
                raise SystemExit(f"TrueNAS job {job_id} failed: {job['state']}")
            return job
        time.sleep(2)
    raise SystemExit(f"TrueNAS job {job_id} did not finish in time")


def release_configs() -> dict[str, dict]:
    return {release["name"]: release.get("config") or {} for release in midclt("chart.release.query")}


def env_from_release(config: dict, name: str) -> str | None:
    for item in config.get("containerEnvironmentVariables", []):
        if item.get("name") == name:
            return item.get("value")
    return None


def secret_value(config: dict, env_name: str, default: str | None = None) -> str:
    value = os.environ.get(env_name)
    if value:
        return value
    value = env_from_release(config, env_name)
    if value:
        return value
    if default is not None:
        return default
    return secrets.token_urlsafe(48)


def ensure_dataset(dataset: str, mode: str | None = None) -> None:
    if run(["zfs", "list", dataset], check=False).returncode != 0:
        output(["zfs", "create", dataset])
    mountpoint = output(["zfs", "get", "-H", "-o", "value", "mountpoint", dataset])
    if mode:
        output(["chmod", mode, mountpoint])


def env(items: list[tuple[str, str]]) -> list[dict[str, str]]:
    return [{"name": key, "value": str(value)} for key, value in items if value != ""]


def hp(host_path: str, mount_path: str, read_only: bool = False) -> dict:
    return {"hostPath": host_path, "mountPath": mount_path, "readOnly": read_only}


def pf(container_port: int, node_port: int, protocol: str = "TCP") -> dict:
    return {"containerPort": container_port, "nodePort": node_port, "protocol": protocol}


BASE_VALUES = {
    "enableUIPortal": True,
    "portalDetails": {"portalName": "Fluxo OCR", "protocol": "http", "useNodeIP": True, "port": WEB_PORT},
    "workloadType": "Deployment",
    "updateStrategy": "Recreate",
    "containerCommand": [],
    "containerArgs": [],
    "containerEnvironmentVariables": [],
    "externalInterfaces": [],
    "dnsPolicy": "ClusterFirst",
    "dnsConfig": {"nameservers": [], "searches": [], "options": []},
    "hostNetwork": False,
    "hostPortsList": [],
    "portForwardingList": [],
    "hostPathVolumes": [],
    "emptyDirVolumes": [],
    "volumes": [],
    "livenessProbe": None,
    "gpuConfiguration": {},
    "tty": False,
    "stdin": False,
    "securityContext": {"privileged": False, "capabilities": [], "enableRunAsUser": False},
    "enableResourceLimits": ENABLE_RESOURCE_LIMITS,
}


def build_values(existing_config: dict) -> dict:
    admin_password = secret_value(existing_config, "ADMIN_PASSWORD")
    jwt_secret = secret_value(existing_config, "JWT_SECRET")
    api_key = secret_value(existing_config, "API_KEY", os.environ.get("OCR_API_KEY", ""))
    values = dict(BASE_VALUES)
    values.update(
        {
            "release_name": RELEASE_NAME,
            "image": {
                "repository": IMAGE_REPOSITORY,
                "tag": IMAGE_TAG,
                "pullPolicy": os.environ.get("OCR_IMAGE_PULL_POLICY", "IfNotPresent"),
            },
            "containerEnvironmentVariables": env(
                [
                    ("APP_ENV", "production"),
                    ("OCR_TMP_DIR", "/tmp/ocr-recognizer"),
                    ("MAX_UPLOAD_SIZE_MB", os.environ.get("MAX_UPLOAD_SIZE_MB", "80")),
                    ("OCR_SUBPROCESS_TIMEOUT_SECONDS", os.environ.get("OCR_SUBPROCESS_TIMEOUT_SECONDS", "300")),
                    ("MAX_BATCH_FILES", os.environ.get("MAX_BATCH_FILES", "25")),
                    ("UPLOAD_RATE_LIMIT_PER_MINUTE", os.environ.get("UPLOAD_RATE_LIMIT_PER_MINUTE", "20")),
                    ("JOB_RETENTION_SECONDS", os.environ.get("JOB_RETENTION_SECONDS", "86400")),
                    ("JWT_SECRET", jwt_secret),
                    ("JWT_EXP_MINUTES", os.environ.get("JWT_EXP_MINUTES", "480")),
                    ("ADMIN_NAME", os.environ.get("ADMIN_NAME", "Admin")),
                    ("ADMIN_EMAIL", os.environ.get("ADMIN_EMAIL", "admin@ocr.andre.goiania.br")),
                    ("ADMIN_PASSWORD", admin_password),
                    ("API_KEY", api_key),
                ]
            ),
            "hostPathVolumes": [hp(DATA_DIR, "/tmp/ocr-recognizer")],
            "portForwardingList": [pf(8000, WEB_PORT)],
        }
    )
    if existing_config.get("imagePullSecrets"):
        values["imagePullSecrets"] = existing_config["imagePullSecrets"]
    return values


def main() -> None:
    for dataset, mode in [
        (DATASET_ROOT, None),
        (f"{DATASET_ROOT}/data", "700"),
        (f"{DATASET_ROOT}/releases", None),
        (f"{DATASET_ROOT}/backups", None),
    ]:
        ensure_dataset(dataset, mode=mode)
    Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

    configs = release_configs()
    existing = configs.get(RELEASE_NAME, {})
    values = build_values(existing)
    if RELEASE_NAME in configs:
        print(f"Updating {RELEASE_NAME}...")
        job_id = midclt("chart.release.update", RELEASE_NAME, {"values": values})
    else:
        print(f"Creating {RELEASE_NAME}...")
        job_id = midclt(
            "chart.release.create",
            {
                "catalog": CATALOG,
                "train": TRAIN,
                "item": ITEM,
                "version": VERSION,
                "release_name": RELEASE_NAME,
                "values": values,
            },
        )
    wait_job(job_id)
    print(f"{RELEASE_NAME}: ok")

    print(f"Ensuring {RELEASE_NAME} is running...")
    job_id = midclt("chart.release.scale", RELEASE_NAME, {"replica_count": 1})
    wait_job(job_id)
    print(f"{RELEASE_NAME}: running")
    print(f"Portal: {PUBLIC_URL}")


if __name__ == "__main__":
    main()
