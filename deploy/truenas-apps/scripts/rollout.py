#!/usr/bin/env python3
"""Manual/semi-automatic rollout helper for the microservices-ocr TrueNAS Apps release."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_HOST = os.environ.get("OCR_HOST", "192.168.3.140")
DEFAULT_WEB_PORT = int(os.environ.get("OCR_WEB_PORT", "31800"))
DEFAULT_PUBLIC_URL = os.environ.get("OCR_PUBLIC_URL", f"http://{DEFAULT_HOST}:{DEFAULT_WEB_PORT}")
DEFAULT_IMAGE_REPOSITORY = os.environ.get("OCR_IMAGE_REPOSITORY", "ghcr.io/andrevilas/microservices-ocr")
DEFAULT_RELEASE_ROOT = Path(os.environ.get("OCR_RELEASE_ROOT", "/mnt/NVME/ocr-apps/releases"))
DEFAULT_BACKUP_ROOT = Path(os.environ.get("OCR_BACKUP_ROOT", "/mnt/NVME/ocr-apps/backups"))
DEFAULT_CURL_IP_VERSION = os.environ.get("OCR_CURL_IP_VERSION", "4")

RELEASE_NAME = os.environ.get("OCR_TRUENAS_RELEASE", "ocr-recognizer")
NAMESPACE = os.environ.get("OCR_TRUENAS_NAMESPACE", "ix-ocr-recognizer")
ALL_DEPLOYMENTS = tuple(os.environ.get("OCR_TRUENAS_DEPLOYMENTS", "ocr-recognizer").split(","))

DEFAULT_DATASET = os.environ.get("OCR_TRUENAS_DATASET", "NVME/ocr-apps/data")
DEFAULT_DATA_DIR = os.environ.get("OCR_DATA_DIR", "/mnt/NVME/ocr-apps/data")

DATASETS = (DEFAULT_DATASET,)
PERSISTENT_PATHS = {
    "ocr_data.tar.gz": DEFAULT_DATA_DIR,
}


class RolloutError(RuntimeError):
    pass


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(args: list[str], *, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, input=input_text, capture_output=True, check=check)


def output(args: list[str], *, check: bool = True) -> str:
    return run(args, check=check).stdout.strip()


def log_command(report: dict, label: str, args: list[str], *, check: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    started = utc_now()
    result = run(args, check=False, input_text=input_text)
    entry = {
        "label": label,
        "command": args,
        "started_at": started,
        "finished_at": utc_now(),
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }
    report.setdefault("commands", []).append(entry)
    if check and result.returncode != 0:
        raise RolloutError(f"{label} failed with exit code {result.returncode}")
    return result


def midclt(method: str, *args):
    cmd = ["midclt", "call", method]
    cmd.extend(json.dumps(arg) if isinstance(arg, (dict, list)) else str(arg) for arg in args)
    data = output(cmd)
    return json.loads(data) if data else None


def wait_job(job_id: int, report: dict | None = None) -> dict:
    deadline = time.time() + 600
    while time.time() < deadline:
        jobs = midclt("core.get_jobs", [["id", "=", job_id]])
        job = jobs[0] if jobs else None
        if job and job.get("state") in {"SUCCESS", "FAILED", "ABORTED"}:
            if report is not None:
                report.setdefault("jobs", []).append(job)
            if job["state"] != "SUCCESS":
                raise RolloutError(f"TrueNAS job {job_id} failed: {job['state']}")
            return job
        time.sleep(2)
    raise RolloutError(f"TrueNAS job {job_id} did not finish in time")


def kubectl(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["k3s", "kubectl", *args], check=check)


def kubectl_output(args: list[str], *, check: bool = True) -> str:
    return kubectl(args, check=check).stdout.strip()


def release_query() -> list[dict]:
    return midclt("chart.release.query")


def release_map() -> dict[str, dict]:
    return {release["name"]: release for release in release_query() if release["name"] == RELEASE_NAME}


def release_config() -> dict:
    releases = release_map()
    release = releases.get(RELEASE_NAME)
    if not release:
        raise RolloutError(f"TrueNAS App release `{RELEASE_NAME}` not found")
    config = release.get("config") or {}

    # Check if there is an image config
    has_image = False
    for key in ("ocr", "ocr-recognizer"):
        if key in config and "image" in (config.get(key) or {}):
            has_image = True
            break
    if not has_image and "image" in config:
        has_image = True

    if not has_image:
        raise RolloutError(f"Release `{RELEASE_NAME}` does not expose image config under key `ocr`, `ocr-recognizer`, or root `image` in its config")
    return config


def release_image(release: dict) -> dict[str, str]:
    config = release.get("config") or {}
    image = {}
    for key in ("ocr", "ocr-recognizer"):
        if key in config and "image" in (config.get(key) or {}):
            image = config[key]["image"]
            break
    if not image and "image" in config:
        image = config["image"]
    return {
        "repository": str(image.get("repository") or ""),
        "tag": str(image.get("tag") or ""),
    }


def current_state() -> dict:
    releases = release_map()
    return {
        "captured_at": utc_now(),
        "releases": {
            name: {
                "status": release.get("status"),
                "pod_status": release.get("pod_status"),
                "image": release_image(release),
            }
            for name, release in releases.items()
        },
    }


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_report(release_dir: Path, report: dict) -> None:
    write_json(release_dir / "report.json", report)
    lines = [
        f"# Rollout OCR {release_dir.name}",
        "",
        f"- Início: {report.get('started_at')}",
        f"- Fim: {report.get('finished_at', '')}",
        f"- Status: {report.get('status', 'unknown')}",
        f"- Imagem: `{report.get('image', '')}`",
        f"- Escopo: `{report.get('scope', '')}`",
        f"- Backup: `{report.get('backup_dir', '')}`",
        "",
        "## Validações",
    ]
    for check in report.get("checks", []):
        lines.append(f"- {check.get('name')}: {check.get('status')} {check.get('detail', '')}".rstrip())
    lines.extend(["", "## Comandos"])
    for command in report.get("commands", []):
        lines.append(f"- {command.get('label')}: exit `{command.get('returncode')}`")
    (release_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def check_ok(report: dict, name: str, detail: str = "") -> None:
    report.setdefault("checks", []).append({"name": name, "status": "ok", "detail": detail})


def check_fail(report: dict, name: str, detail: str) -> None:
    report.setdefault("checks", []).append({"name": name, "status": "fail", "detail": detail})
    raise RolloutError(f"{name}: {detail}")


def validate_releases(report: dict) -> None:
    last_detail = ""
    for _attempt in range(30):
        releases = release_map()
        release = releases.get(RELEASE_NAME)
        if not release:
            last_detail = f"missing release: {RELEASE_NAME}"
        else:
            pod_status = release.get("pod_status") or {}
            available = pod_status.get("available") or 0
            desired = pod_status.get("desired") or 0
            if release.get("status") == "ACTIVE" and desired >= len(ALL_DEPLOYMENTS) and available == desired:
                check_ok(report, "release-active", f"{RELEASE_NAME} ACTIVE with {available}/{desired} pods available")
                return
            last_detail = f"{RELEASE_NAME}: status={release.get('status')} pod_status={pod_status}"
        time.sleep(2)
    check_fail(report, "release-active", last_detail)


def validate_deployments(report: dict) -> None:
    for deployment in ALL_DEPLOYMENTS:
        log_command(
            report,
            f"rollout-status-{deployment}",
            ["k3s", "kubectl", "-n", NAMESPACE, "rollout", "status", f"deployment/{deployment}", "--timeout=240s"],
        )
    check_ok(report, "deployments-ready", f"{len(ALL_DEPLOYMENTS)} deployments ready in {NAMESPACE}")


def http_status(url: str, host_header: str | None = None, *, curl_ip_version: str = DEFAULT_CURL_IP_VERSION) -> str:
    args = ["curl", "-k", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "20"]
    if curl_ip_version in {"4", "6"}:
        args.insert(1, f"-{curl_ip_version}")
    if host_header:
        args.extend(["-H", f"Host: {host_header}"])
    args.append(url)
    result = run(args, check=False)
    if result.returncode != 0:
        return f"curl-error-{result.returncode}"
    return result.stdout.strip()


def wait_http_status(
    report: dict,
    name: str,
    url: str,
    *,
    expected: set[str],
    host_header: str | None = None,
    attempts: int = 12,
    delay: float = 5.0,
    curl_ip_version: str = DEFAULT_CURL_IP_VERSION,
) -> str:
    history = []
    for attempt in range(1, attempts + 1):
        status = http_status(url, host_header, curl_ip_version=curl_ip_version)
        history.append(status)
        if status in expected:
            detail = f"{url} returned {status}"
            if attempt > 1:
                detail += f" after {attempt} attempts; history={history}"
            check_ok(report, name, detail)
            return status
        if attempt < attempts:
            time.sleep(delay)
    check_fail(report, name, f"expected {sorted(expected)}, got {history[-1]}; history={history}")
    return history[-1]


def certificate_summary(public_url: str) -> dict:
    parsed = urlparse(public_url)
    if parsed.scheme != "https" or not parsed.hostname:
        return {}
    cmd = f"echo | openssl s_client -servername {parsed.hostname} -connect {parsed.hostname}:443 2>/dev/null | openssl x509 -noout -issuer -subject -dates"
    result = run(["sh", "-c", cmd], check=False)
    return {"returncode": result.returncode, "summary": result.stdout.strip()}


def image_pull_secret_names(config: dict) -> list[str]:
    names = []
    raw_items = []
    if isinstance(config.get("imagePullSecrets"), list):
        raw_items.extend(config["imagePullSecrets"])
    for key in ("ocr", "ocr-recognizer"):
        ocr_config = config.get(key) or {}
        if isinstance(ocr_config.get("imagePullSecrets"), list):
            raw_items.extend(ocr_config["imagePullSecrets"])
    for item in raw_items:
        if isinstance(item, dict):
            name = item.get("name")
        else:
            name = str(item or "")
        if name and name not in names:
            names.append(name)
    return names


def validate_image_pull(report: dict, image: str) -> None:
    secrets = image_pull_secret_names(release_config())
    if not secrets:
        log_command(report, "image-pull-check", ["k3s", "ctr", "-n", "k8s.io", "images", "pull", image])
        check_ok(report, "image-pull-check", "validated by containerd anonymous pull")
        return

    pod = f"ocr-image-pull-check-{datetime.now().strftime('%H%M%S')}"
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod,
            "namespace": NAMESPACE,
            "labels": {"app.kubernetes.io/name": "ocr-image-pull-check"},
        },
        "spec": {
            "restartPolicy": "Never",
            "imagePullSecrets": [{"name": name} for name in secrets],
            "containers": [
                {
                    "name": "pull-check",
                    "image": image,
                    "imagePullPolicy": "Always",
                    "command": ["sh", "-c", "sleep 20"],
                }
            ],
        },
    }
    try:
        log_command(
            report,
            "image-pull-check-apply",
            ["k3s", "kubectl", "-n", NAMESPACE, "apply", "-f", "-"],
            input_text=json.dumps(manifest),
        )
        wait = log_command(
            report,
            "image-pull-check-ready",
            ["k3s", "kubectl", "-n", NAMESPACE, "wait", "--for=condition=Ready", f"pod/{pod}", "--timeout=180s"],
            check=False,
        )
        if wait.returncode != 0:
            log_command(report, "image-pull-check-describe", ["k3s", "kubectl", "-n", NAMESPACE, "describe", f"pod/{pod}"], check=False)
            raise RolloutError("image-pull-check-ready failed with exit code " f"{wait.returncode}")
        check_ok(report, "image-pull-check", f"validated by Kubernetes pod using imagePullSecrets={','.join(secrets)}")
    finally:
        log_command(report, "image-pull-check-cleanup", ["k3s", "kubectl", "-n", NAMESPACE, "delete", f"pod/{pod}", "--ignore-not-found=true", "--wait=false"], check=False)


def validate_stack(args: argparse.Namespace, report: dict) -> None:
    validate_releases(report)
    validate_deployments(report)

    public_host = urlparse(args.public_url).hostname or args.host

    # Try validation using /health or /login
    wait_http_status(
        report,
        "http-nodeport",
        f"http://{args.host}:{args.web_port}/health",
        expected={"200"},
        host_header=public_host,
        attempts=args.http_attempts,
        delay=args.http_retry_delay,
        curl_ip_version=args.curl_ip_version,
    )

    wait_http_status(
        report,
        "https-public",
        f"{args.public_url.rstrip('/')}/health",
        expected={"200"},
        attempts=args.http_attempts,
        delay=args.http_retry_delay,
        curl_ip_version=args.curl_ip_version,
    )

    cert = certificate_summary(args.public_url)
    if cert.get("summary"):
        check_ok(report, "https-certificate", cert.get("summary", ""))


def preflight(args: argparse.Namespace, report: dict) -> None:
    log_command(report, "truenas-version", ["midclt", "call", "system.version"])
    log_command(report, "k3s-nodes", ["k3s", "kubectl", "get", "nodes"])
    for dataset in DATASETS:
        log_command(report, f"dataset-{dataset}", ["zfs", "list", dataset])
    validate_releases(report)
    if args.image_tag:
        if args.image_tag == "latest" and not args.allow_latest:
            check_fail(report, "image-tag", "`latest` is not allowed for rollout without --allow-latest")
        image = f"{args.image_repository}:{args.image_tag}"
        if not args.skip_image_pull_check:
            validate_image_pull(report, image)
        check_ok(report, "image-tag", image)
    check_ok(report, "preflight", "ready")


def create_backup(args: argparse.Namespace, report: dict) -> Path:
    stamp = now_stamp()
    backup_dir = Path(args.backup_root) / stamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    report["backup_dir"] = str(backup_dir)

    for archive, source in PERSISTENT_PATHS.items():
        log_command(report, f"backup-{archive}", ["tar", "-C", source, "-czf", str(backup_dir / archive), "."])

    write_json(backup_dir / "release-state.json", current_state())
    manifest = {
        "created_at": utc_now(),
        "image_repository": args.image_repository,
        "image_tag": getattr(args, "image_tag", ""),
        "persistent_paths": PERSISTENT_PATHS,
    }
    write_json(backup_dir / "manifest.json", manifest)
    log_command(report, "backup-checksum", ["sh", "-c", f"cd {backup_dir} && sha256sum *.tar.gz manifest.json release-state.json > SHA256SUMS"])
    log_command(report, "backup-verify", ["sh", "-c", f"cd {backup_dir} && sha256sum -c SHA256SUMS"])
    check_ok(report, "backup", str(backup_dir))
    return backup_dir


def apply_stack(args: argparse.Namespace, report: dict, *, image_tag: str, scope: str) -> None:
    if scope == "infra":
        raise RolloutError("The consolidated TrueNAS App rollout cannot update only infra. Use `app` for image promotion or `all` for a full chart values update.")
    config = json.loads(json.dumps(release_config()))

    # Update repository, tag, and pullPolicy under nested or root 'image' config
    updated = False
    for key in ("ocr", "ocr-recognizer"):
        if key in config and "image" in (config.get(key) or {}):
            config[key]["image"]["repository"] = args.image_repository
            config[key]["image"]["tag"] = image_tag
            config[key]["image"]["pullPolicy"] = getattr(args, "image_pull_policy", "IfNotPresent")
            updated = True
            break
    if not updated and "image" in config:
        config["image"]["repository"] = args.image_repository
        config["image"]["tag"] = image_tag
        config["image"]["pullPolicy"] = getattr(args, "image_pull_policy", "IfNotPresent")
        updated = True

    if not updated:
        raise RolloutError("Could not find image configuration in release values to update")

    started = utc_now()
    job_id = midclt("chart.release.update", RELEASE_NAME, {"values": config})
    report.setdefault("commands", []).append(
        {
            "label": f"chart-release-update-{scope}",
            "command": ["midclt", "call", "chart.release.update", RELEASE_NAME, "{values: <preserved current config with updated ocr image>}"],
            "returncode": 0,
            "stdout": f"job_id={job_id}",
            "stderr": "",
            "started_at": started,
            "finished_at": utc_now(),
        }
    )
    if not isinstance(job_id, int):
        raise RolloutError(f"Unexpected chart.release.update response: {job_id!r}")
    wait_job(job_id, report)


def deploy(args: argparse.Namespace) -> dict:
    if args.image_tag == "latest" and not args.allow_latest:
        raise RolloutError("Refusing to deploy `latest`. Use sha-<commit> or pass --allow-latest explicitly.")
    release_dir = Path(args.release_root) / now_stamp()
    release_dir.mkdir(parents=True, exist_ok=False)
    report = {
        "started_at": utc_now(),
        "status": "running",
        "image": f"{args.image_repository}:{args.image_tag}",
        "image_tag": args.image_tag,
        "scope": args.only,
        "release_dir": str(release_dir),
        "previous_state": current_state(),
    }
    write_json(release_dir / "previous-state.json", report["previous_state"])
    try:
        preflight(args, report)
        if args.skip_backup:
            if not args.confirm_skip_backup:
                raise RolloutError("--skip-backup requires --confirm-skip-backup")
            check_ok(report, "backup", "skipped by explicit operator confirmation")
        else:
            create_backup(args, report)
        apply_stack(args, report, image_tag=args.image_tag, scope=args.only)
        validate_stack(args, report)
        write_json(Path(args.release_root) / "current.json", {"updated_at": utc_now(), "release_dir": str(release_dir), "image_tag": args.image_tag})
        report["status"] = "ok"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        raise
    finally:
        report["finished_at"] = utc_now()
        write_report(release_dir, report)
    return report


def rollback(args: argparse.Namespace) -> dict:
    if not args.image_tag:
        state_path = Path(args.release_root) / "current.json"
        if not state_path.exists():
            raise RolloutError("Missing --image-tag and no current.json state was found")
        raise RolloutError("Provide --image-tag for rollback. Automatic previous-state rollback is intentionally explicit.")
    release_dir = Path(args.release_root) / f"rollback-{now_stamp()}"
    release_dir.mkdir(parents=True, exist_ok=False)
    report = {
        "started_at": utc_now(),
        "status": "running",
        "image": f"{args.image_repository}:{args.image_tag}",
        "image_tag": args.image_tag,
        "scope": "app",
        "release_dir": str(release_dir),
        "previous_state": current_state(),
    }
    try:
        apply_stack(args, report, image_tag=args.image_tag, scope="app")
        validate_stack(args, report)
        report["status"] = "ok"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        raise
    finally:
        report["finished_at"] = utc_now()
        write_report(release_dir, report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--public-url", default=DEFAULT_PUBLIC_URL)
    parser.add_argument("--web-port", type=int, default=DEFAULT_WEB_PORT)
    parser.add_argument("--image-repository", default=DEFAULT_IMAGE_REPOSITORY)
    parser.add_argument("--image-pull-policy", default=os.environ.get("OCR_IMAGE_PULL_POLICY", "IfNotPresent"))
    parser.add_argument("--release-root", default=str(DEFAULT_RELEASE_ROOT))
    parser.add_argument("--backup-root", default=str(DEFAULT_BACKUP_ROOT))
    parser.add_argument("--http-attempts", type=int, default=int(os.environ.get("OCR_ROLLOUT_HTTP_ATTEMPTS", "12")))
    parser.add_argument("--http-retry-delay", type=float, default=float(os.environ.get("OCR_ROLLOUT_HTTP_RETRY_DELAY", "5")))
    parser.add_argument("--curl-ip-version", choices=["4", "6", "any"], default=DEFAULT_CURL_IP_VERSION)
    parser.add_argument("--allow-latest", action="store_true")
    parser.add_argument("--skip-image-pull-check", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight_parser = subparsers.add_parser("preflight")
    preflight_parser.add_argument("--image-tag", default="")

    subparsers.add_parser("backup")

    deploy_parser = subparsers.add_parser("deploy")
    deploy_parser.add_argument("--image-tag", required=True)
    deploy_parser.add_argument("--only", choices=["app", "infra", "all"], default="app")
    deploy_parser.add_argument("--skip-backup", action="store_true")
    deploy_parser.add_argument("--confirm-skip-backup", action="store_true")

    subparsers.add_parser("validate")

    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("--image-tag", required=True)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    report = {
        "started_at": utc_now(),
        "status": "running",
        "image": f"{args.image_repository}:{getattr(args, 'image_tag', '')}",
        "image_tag": getattr(args, "image_tag", ""),
        "scope": getattr(args, "only", ""),
    }
    try:
        if args.command == "preflight":
            preflight(args, report)
            report["status"] = "ok"
            print(json.dumps(report, indent=2, ensure_ascii=False))
        elif args.command == "backup":
            create_backup(args, report)
            report["status"] = "ok"
            print(json.dumps(report, indent=2, ensure_ascii=False))
        elif args.command == "deploy":
            deploy_report = deploy(args)
            print(json.dumps({"status": deploy_report["status"], "report": deploy_report["release_dir"]}, indent=2))
        elif args.command == "validate":
            validate_stack(args, report)
            report["status"] = "ok"
            print(json.dumps(report, indent=2, ensure_ascii=False))
        elif args.command == "rollback":
            rollback_report = rollback(args)
            print(json.dumps({"status": rollback_report["status"], "report": rollback_report["release_dir"]}, indent=2))
        else:
            parser.error("unknown command")
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        print(json.dumps(report, indent=2, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
