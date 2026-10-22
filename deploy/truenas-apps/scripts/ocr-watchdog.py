#!/usr/bin/env python3
"""Host watchdog for the OCR/RAG stack on TrueNAS (OCR-AM-15). Standard library only.

Runs from cron, reads state read-only and records content-free findings:
  - status.json   current checks (overwritten every run)
  - alerts.jsonl  one line per state change (raised/cleared)
Exit status is 1 while any alert is active, so a cron runner with mail configured reports it.
--notify-command receives each transition as JSON on stdin; delivery channel is the owner's choice.
Never reads document content, extraction results, tokens or payloads.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

FAILED_OUTCOMES = ("provider_error", "provider_quota", "provider_policy", "run_timeout", "invalid_output")


def check_pods(pods: dict, previous: dict) -> list[dict]:
    """pods: {name: {"ready": bool, "restarts": int}} for the watched namespaces."""
    findings = []
    for name, pod in sorted(pods.items()):
        if pod is None:
            findings.append({"check": f"pod:{name}", "alert": "pod_missing"})
            continue
        before = previous.get(f"pod:{name}", {}).get("restarts")
        if not pod["ready"]:
            findings.append({"check": f"pod:{name}", "alert": "pod_not_ready", "restarts": pod["restarts"]})
        elif before is not None and pod["restarts"] > before:
            findings.append({"check": f"pod:{name}", "alert": "pod_restarted", "restarts": pod["restarts"]})
        else:
            findings.append({"check": f"pod:{name}", "alert": None, "restarts": pod["restarts"]})
    return findings


def check_sync(reports: list[tuple[float, dict]], now: float, max_age_hours: float) -> dict:
    """reports: (mtime, report) newest last. Fresh and imported/unchanged is healthy."""
    if not reports:
        return {"check": "rag_sync", "alert": "sync_never_ran"}
    mtime, report = reports[-1]
    age_hours = round((now - mtime) / 3600, 1)
    status = report.get("status")
    if status not in ("imported", "unchanged"):
        return {"check": "rag_sync", "alert": "sync_" + str(status or "unknown"), "age_hours": age_hours}
    if age_hours > max_age_hours:
        return {"check": "rag_sync", "alert": "sync_stale", "age_hours": age_hours}
    return {"check": "rag_sync", "alert": None, "status": status, "age_hours": age_hours}


def check_llm(probe) -> dict:
    status = probe()
    return {"check": "extraction_llm", "alert": None if status == 200 else "llm_unreachable", "http": status}


def check_database(conn: sqlite3.Connection, now: float) -> list[dict]:
    day_ago = datetime.fromtimestamp(now - 86400, timezone.utc).isoformat()
    findings = []
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "extraction_runs" in tables:
        total, failed = conn.execute(
            "SELECT count(*), coalesce(sum(state='failed' AND outcome IN (%s)),0) FROM extraction_runs WHERE created_at>=?"
            % ",".join("?" * len(FAILED_OUTCOMES)), (*FAILED_OUTCOMES, day_ago)).fetchone()
        alert = "extraction_failing" if failed >= 3 and failed * 2 >= total else None
        findings.append({"check": "extraction_runs_24h", "alert": alert, "runs": total, "failed": failed})
    if "rag_outbox" in tables:
        resolution = conn.execute("SELECT count(*) FROM rag_outbox WHERE state='resolution'").fetchone()[0]
        pending = conn.execute("SELECT count(*) FROM rag_outbox WHERE state NOT IN ('indexed','canceled','resolution')"
                               " AND created<?", (now - 3600,)).fetchone()[0]
        alert = "rag_delivery_resolution" if resolution else "rag_delivery_stuck" if pending else None
        findings.append({"check": "rag_outbox", "alert": alert, "resolution": resolution, "pending_over_1h": pending})
    stale = datetime.fromtimestamp(now - 1800, timezone.utc).isoformat()
    stuck = conn.execute("SELECT count(*) FROM jobs WHERE status IN ('processing','cancel_requested') AND updated_at<?",
                         (stale,)).fetchone()[0]
    findings.append({"check": "jobs_processing", "alert": "jobs_stuck" if stuck else None, "stuck_over_30min": stuck})
    return findings


def check_capacity(free_disk_gb: float, total_disk_gb: float, mem_available_mb: float, min_disk_gb: float,
                   min_disk_ratio: float, min_mem_mb: float) -> list[dict]:
    """Disk is measured on the data dataset, whose total is its ZFS quota: use an absolute and a relative floor."""
    low = free_disk_gb < min_disk_gb or (total_disk_gb > 0 and free_disk_gb / total_disk_gb < min_disk_ratio)
    return [{"check": "disk", "alert": "disk_low" if low else None, "free_gb": round(free_disk_gb, 1),
             "total_gb": round(total_disk_gb, 1)},
            {"check": "memory", "alert": "memory_low" if mem_available_mb < min_mem_mb else None,
             "available_mb": round(mem_available_mb)}]


def transitions(previous: dict, findings: list[dict], at: str) -> list[dict]:
    events = []
    for finding in findings:
        before = previous.get(finding["check"], {}).get("alert")
        if finding["alert"] != before:
            kind = "raised" if finding["alert"] else "cleared"
            events.append({"at": at, "event": kind, "check": finding["check"], "alert": finding["alert"] or before})
    return events


# ── host collection ─────────────────────────────────────────────────────────────────────

def _pods(namespaces):
    out = subprocess.run(["k3s", "kubectl", "get", "pods", "-A", "-o", "json"], capture_output=True, text=True, timeout=60)
    items = json.loads(out.stdout)["items"] if out.returncode == 0 else []
    pods = {ns: None for ns in namespaces}
    for item in items:
        ns = item["metadata"]["namespace"]
        if ns not in pods or item.get("status", {}).get("phase") != "Running" or item["metadata"].get("ownerReferences", [{}])[0].get("kind") == "Job":
            continue
        statuses = item["status"].get("containerStatuses", [])
        pods[ns] = {"ready": bool(statuses) and all(s.get("ready") for s in statuses),
                    "restarts": sum(s.get("restartCount", 0) for s in statuses)}
    return pods


def _reports(directory: Path):
    found = []
    for path in sorted(directory.glob("report-*.json")):
        try:
            found.append((path.stat().st_mtime, {"status": json.loads(path.read_text()).get("status")}))
        except (OSError, ValueError):
            found.append((path.stat().st_mtime, {"status": "unreadable"}))
    return found


def _http_status(url):
    def probe():
        try:
            with urllib.request.urlopen(url, timeout=8) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code
        except OSError:
            return 0
    return probe


def _mem_available_mb():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default="/mnt/NVME/ocr-apps/monitor")
    parser.add_argument("--ocr-db", default="/mnt/NVME/ocr-apps/data/jobs.sqlite3")
    parser.add_argument("--sync-dir", default="/mnt/NVME/document-rag/backups/db/sync")
    parser.add_argument("--llm-url", default="http://100.120.78.1:11434/v1/models")
    parser.add_argument("--namespaces", default="ix-ocr-recognizer,document-rag")
    parser.add_argument("--sync-max-age-hours", type=float, default=26)
    parser.add_argument("--min-disk-gb", type=float, default=1)
    parser.add_argument("--min-disk-ratio", type=float, default=0.15)
    parser.add_argument("--min-mem-mb", type=float, default=300)
    parser.add_argument("--notify-command", default=os.environ.get("OCR_WATCHDOG_NOTIFY"))
    args = parser.parse_args(argv)

    state_dir = Path(args.state_dir)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    status_file = state_dir / "status.json"
    try:
        previous = {f["check"]: f for f in json.loads(status_file.read_text())["checks"]}
    except (OSError, ValueError, KeyError):
        previous = {}
    now = time.time()
    at = datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds")

    findings = check_pods(_pods(args.namespaces.split(",")), previous)
    findings.append(check_sync(_reports(Path(args.sync_dir)), now, args.sync_max_age_hours))
    findings.append(check_llm(_http_status(args.llm_url)))
    try:
        conn = sqlite3.connect(f"file:{args.ocr_db}?mode=ro", uri=True, timeout=10)
        try:
            findings.extend(check_database(conn, now))
        finally:
            conn.close()
    except sqlite3.Error:
        findings.append({"check": "ocr_database", "alert": "database_unreadable"})
    disk = shutil.disk_usage(Path(args.ocr_db).parent)
    findings.extend(check_capacity(disk.free / 2**30, disk.total / 2**30, _mem_available_mb(),
                                   args.min_disk_gb, args.min_disk_ratio, args.min_mem_mb))

    events = transitions(previous, findings, at)
    active = [f["alert"] for f in findings if f["alert"]]
    temporary = status_file.with_suffix(".tmp")
    temporary.write_text(json.dumps({"at": at, "active": active, "checks": findings}, indent=1))
    os.replace(temporary, status_file)
    if events:
        with (state_dir / "alerts.jsonl").open("a") as log:
            for event in events:
                log.write(json.dumps(event) + "\n")
        if args.notify_command:
            subprocess.run(args.notify_command, shell=True, input=json.dumps(events), text=True, timeout=60)
    print(json.dumps({"at": at, "active": active, "events": len(events)}))
    return 1 if active else 0


if __name__ == "__main__":
    sys.exit(main())
