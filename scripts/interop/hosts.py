"""OCR-AM-16 host sessions: real MCP hosts drive the stdio adapter; the server verifies what they did.

  python scripts/interop/hosts.py --api http://127.0.0.1:18080 --admin-env <server.env> --out <dir> \
      --hosts claude-code,codex,agy [--restart "docker restart ocr-am16"]

Per host: its own service principal and credential file, the configuration produced by
app.mcp_client_config, and two sessions. Session 1 runs the job flow with a denial, cancel, retry
and delete; the server then restarts; session 2 (a new host process) reads the job back. Only
synthetic PDFs exist on the server, so model providers see synthetic text only. Host transcripts
are kept privately; results.json carries only checks and identifiers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time

import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.mcp_client_config import files_for  # noqa: E402
from matrix import FULL, Recorder, admin_token, issue, synthetic_pdf  # noqa: E402

CLAUDE = Path.home() / ".local/bin/claude.exe"
CODEX = Path.home() / "AppData/Local/Programs/OpenAI/Codex/bin/codex.exe"
AGY = Path.home() / "AppData/Local/agy/bin/agy.exe"
TIMEOUT = 900

SESSION_1 = """You are testing an MCP server named ocr-recognizer. Use only its tools, exactly in this order, then reply
with ONE line of JSON and nothing else (no prose, no code fence).
1. Call ocr_whoami, then ocr_get_capabilities.
2. Call ocr_submit_job with upload_id "{u1}", idempotency_key "{k1}" and options {{"languages": ["eng"]}}.
3. Call ocr_get_job on that job_id repeatedly (wait a few seconds between calls, at most 60 calls) until status is
   completed, failed or canceled.
4. Call ocr_get_text and ocr_get_report for that job.
5. Call ocr_get_job with job_id "{foreign}". It belongs to another owner, so an error is the expected outcome.
6. Call ocr_submit_job with upload_id "{u2}", idempotency_key "{k2}" and options {{"languages": ["eng"]}}, then
   immediately ocr_cancel_job on the new job_id. Poll ocr_get_job on it until its status is canceled or completed.
7. Call ocr_retry_job on that canceled job with idempotency_key "{k3}", then poll ocr_get_job on the job_id it returns
   until it is terminal.
8. Call ocr_delete_job_artifacts on the canceled job from step 6.
Reply JSON keys: subject (from whoami), job_id (step 2), status (step 3 final), code (the 6-digit number that follows
"Codigo" in the text of step 4), pdfa_validated_sha256 (from the report's output_validation), foreign_denied (true if
step 5 returned an error), cancel_job_id, cancel_status, retry_job_id, retry_status, deleted (true if step 8
succeeded)."""

SESSION_2 = """You are testing an MCP server named ocr-recognizer. Use only its tools, then reply with ONE line of JSON
and nothing else. Call ocr_get_job with job_id "{job}", ocr_get_job with job_id "{deleted}" and ocr_list_jobs.
Reply JSON keys: status (of the first job), updated_at (of the first job, verbatim), retention_state (the second
job's retention.state), listed (how many jobs ocr_list_jobs returned)."""


def last_json(text: str) -> dict | None:
    for match in reversed(list(re.finditer(r"\{.*\}", text or "", re.S))):
        candidate = match.group(0)
        for start in [m.start() for m in re.finditer(r"\{", candidate)]:
            try:
                value = json.loads(candidate[start:])
                if isinstance(value, dict):
                    return value
            except ValueError:
                continue
    return None


def run_host(host: str, prompt: str, workdir: Path, credential: Path, api: str) -> tuple[str, dict]:
    """Run one non-interactive host session; returns (final text, metadata). Nothing global is edited."""
    files = files_for({"claude-code": "claude-code", "codex": "codex", "agy": "agy"}[host], sys.executable, api, credential)
    started = time.monotonic()
    if host == "claude-code":
        config = workdir / "claude-code.json"
        config.write_text(files["claude-code.json"], encoding="utf-8")
        cmd = [str(CLAUDE), "-p", prompt, "--mcp-config", str(config), "--strict-mcp-config",
               "--allowedTools", "mcp__ocr-recognizer", "--model", "haiku", "--output-format", "json"]
        version = subprocess.run([str(CLAUDE), "--version"], capture_output=True, text=True).stdout.strip()
    elif host == "codex":
        import tomllib
        server = tomllib.loads(files["ocr-config.toml"])["mcp_servers"]["ocr-recognizer"]
        inline = "{" + ",".join(k + "=" + ("{" + ",".join(a + "=" + json.dumps(b) for a, b in v.items()) + "}"
                                           if isinstance(v, dict) else json.dumps(v)) for k, v in server.items()) + "}"
        last = workdir / "codex-last.txt"
        cmd = [str(CODEX), "exec", "--skip-git-repo-check", "--sandbox", "read-only",
               "-c", "mcp_servers.ocr-recognizer=" + inline,
               "-c", 'mcp_servers.ocr-recognizer.default_tools_approval_mode="approve"',
               "-o", str(last), prompt]
        version = subprocess.run([str(CODEX), "--version"], capture_output=True, text=True).stdout.strip()
    else:
        (workdir / ".agents").mkdir(exist_ok=True)
        (workdir / ".agents" / "mcp_config.json").write_text(files["agy.json"], encoding="utf-8")
        cmd = [str(AGY), "-p", prompt, "--output-format", "json", "--dangerously-skip-permissions",
               "--print-timeout", "14m"]
        version = subprocess.run([str(AGY), "--version"], capture_output=True, text=True).stdout.strip()
    result = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, encoding="utf-8", errors="replace",
                            timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    (workdir / f"{host}-{int(started)}.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (workdir / f"{host}-{int(started)}.stderr.txt").write_text(result.stderr, encoding="utf-8")
    text = result.stdout
    if host == "claude-code":
        try:
            text = json.loads(result.stdout).get("result", "")
        except ValueError:
            pass
    elif host == "codex" and (workdir / "codex-last.txt").is_file():
        text = (workdir / "codex-last.txt").read_text(encoding="utf-8")
    elif host == "agy":
        try:
            parsed = json.loads(result.stdout)
            text = parsed.get("result") or parsed.get("response") or result.stdout
        except ValueError:
            pass
    return text, {"version": version, "exit": result.returncode, "seconds": round(time.monotonic() - started, 1)}


def api_client(api, token):
    return httpx2.Client(base_url=api, trust_env=False, timeout=60, headers={"Authorization": "Bearer " + token})


def upload(api, token, name, data) -> str:
    with api_client(api, token) as http:
        receipt = http.post("/api/v1/upload-sessions", json={"filename": name, "size_bytes": len(data),
                                                              "sha256": hashlib.sha256(data).hexdigest()}).json()
        assert http.put(receipt["content_url"], content=data, headers={"Content-Type": "application/pdf"}).status_code == 200
        return receipt["upload_id"]


def wait_health(api):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            if httpx2.get(api + "/health", trust_env=False, timeout=5).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--admin-env", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--hosts", default="claude-code,codex,agy")
    parser.add_argument("--restart")
    args = parser.parse_args()
    out = Path(args.out)
    private = out / "private"
    private.mkdir(parents=True, exist_ok=True)
    rec, sessions = Recorder(), {}
    with httpx2.Client(base_url=args.api, trust_env=False, timeout=30) as http:
        admin = admin_token(http, Path(args.admin_env))
        other = issue(http, admin, "hosts-other-owner", FULL)
        hosts = {h: issue(http, admin, "host-" + h, FULL) for h in args.hosts.split(",")}
    with api_client(args.api, other[1]) as http:
        data = synthetic_pdf(1, "Documento de outro dono")
        foreign_upload = upload(args.api, other[1], "foreign.pdf", data)
        foreign = http.post("/api/v1/jobs", json={"upload_id": foreign_upload, "options": {"languages": ["eng"]}},
                            headers={"Idempotency-Key": "foreign-" + foreign_upload}).json()["job_id"]
    for host, (pid, token) in hosts.items():
        workdir = Path(tempfile.mkdtemp(prefix=f"am16-{host}-", dir=private))
        credential = workdir / "credential.txt"
        credential.write_text(token, encoding="utf-8")
        # Printed only in the PDF, never in the prompt: the host can report it only by reading the text.
        code = f"{int.from_bytes(os.urandom(4), 'big') % 900000 + 100000}"
        u1 = upload(args.api, token, f"{host}-1.pdf", synthetic_pdf(1, f"Codigo {code}"))
        # A long job of the same owner keeps the worker busy, so the cancel in step 6 meets a queued job.
        u_busy = upload(args.api, token, f"{host}-busy.pdf", synthetic_pdf(25))
        u2 = upload(args.api, token, f"{host}-2.pdf", synthetic_pdf(1))
        k1, k2, k3 = (f"{host}-{n}-{u1[:8]}" for n in ("submit", "cancel", "retry"))
        prompt = SESSION_1.format(u1=u1, u2=u2, k1=k1, k2=k2, k3=k3, foreign=foreign)
        try:
            with api_client(args.api, token) as http:
                http.post("/api/v1/jobs", json={"upload_id": u_busy, "options": {"languages": ["eng"]}},
                          headers={"Idempotency-Key": "busy-" + u_busy})
            text, meta = run_host(host, prompt, workdir, credential, args.api)
            answer = last_json(text) or {}
            rec.check(host, "session1_answer_json", bool(answer), **meta)
            with api_client(args.api, token) as http:
                me = http.get("/api/v1/me").json()
                jobs = {j["job_id"]: j for j in http.get("/api/v1/jobs", params={"limit": 50}).json()["items"]}
                first = next((j for j in jobs.values() if j.get("filename") == f"{host}-1.pdf"), None)
                rec.check(host, "whoami_matches_own_principal", answer.get("subject") == me["subject"])
                rec.check(host, "job_submitted_by_host", first is not None and answer.get("job_id") == first["job_id"])
                job_id = first["job_id"] if first else answer.get("job_id")
                report = http.get(f"/api/v1/jobs/{job_id}/report").json() if job_id else {}
                validated = (report.get("output_validation") or {}).get("pdfa_validated_sha256")
                rec.check(host, "job_completed", (first or {}).get("status") == "completed" and answer.get("status") == "completed")
                rec.check(host, "text_read_by_host", str(answer.get("code")) == code)
                rec.check(host, "report_read_by_host", bool(validated) and answer.get("pdfa_validated_sha256") == validated)
                rec.check(host, "other_owner_denied", answer.get("foreign_denied") is True)
                canceled = jobs.get(answer.get("cancel_job_id") or "", {})
                rec.check(host, "cancel_job", canceled.get("status") == "canceled", status=canceled.get("status"))
                retried = http.get(f"/api/v1/jobs/{answer.get('retry_job_id')}").json() if answer.get("retry_job_id") else {}
                rec.check(host, "retry_job", retried.get("status") == "completed"
                          and retried.get("parent_job_id") == answer.get("cancel_job_id"), status=retried.get("status"))
                after = http.get(f"/api/v1/jobs/{answer.get('cancel_job_id')}").json() if answer.get("cancel_job_id") else {}
                state = (after.get("retention") or {}).get("state")
                rec.check(host, "delete_artifacts", state == "deleted" and answer.get("deleted") is True, state=state)
            with api_client(args.api, other[1]) as http:
                rec.check(host, "foreign_job_untouched", http.get(f"/api/v1/jobs/{foreign}").json().get("status")
                          in ("completed", "queued", "processing"))
            sessions[host] = (workdir, credential, job_id, answer.get("cancel_job_id"), token)
        except Exception as exc:
            rec.check(host, "session1_aborted", False, error=type(exc).__name__ + ": " + str(exc)[:300])
    if args.restart:
        subprocess.run(shlex.split(args.restart), check=True, capture_output=True, timeout=120)
        wait_health(args.api)
    for host, (workdir, credential, job_id, deleted, token) in sessions.items():
        try:
            text, meta = run_host(host, SESSION_2.format(job=job_id, deleted=deleted), workdir, credential, args.api)
            answer = last_json(text) or {}
            with api_client(args.api, token) as http:  # Values only the server knows, read independently.
                job = http.get(f"/api/v1/jobs/{job_id}").json()
                listed = len(http.get("/api/v1/jobs", params={"limit": 50}).json()["items"])
            rec.check(host, "session2_after_restart", answer.get("status") == job.get("status") == "completed"
                      and str(answer.get("updated_at")) == job.get("updated_at")
                      and answer.get("retention_state") == "deleted" and answer.get("listed") == listed, **meta)
        except Exception as exc:
            rec.check(host, "session2_aborted", False, error=type(exc).__name__ + ": " + str(exc)[:300])
    secrets = [admin] + [t for _, t in [*hosts.values(), other]]
    leaked = [str(p.relative_to(private)) for p in private.rglob("*") if p.is_file() and p.name != "credential.txt"
              and any(t in p.read_text(errors="replace") for t in secrets)]
    rec.check("all", "transcripts_have_no_token", not leaked, files=leaked)
    for path in private.rglob("credential.txt"):
        path.unlink()
    summary = {}
    for row in rec.rows:
        entry = summary.setdefault(row["client"], {"passed": 0, "failed": []})
        entry["passed" if row["ok"] else "failed"] = entry["passed"] + 1 if row["ok"] else [*entry["failed"], row["check"]]
    (out / "results.json").write_text(json.dumps({"api": args.api, "summary": summary, "checks": rec.rows},
                                                 indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
