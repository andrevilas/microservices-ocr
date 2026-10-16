"""Isolated PDF/A validation through an external veraPDF executable.

Candidate OCR-EV-01A: not wired to the orchestrator, API or settings yet.
The command is fixed by the server; requests may only choose the file.
"""
from __future__ import annotations

import hashlib
import math
import os
import subprocess
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Sequence

PdfaStatus = Literal["passed", "failed", "indeterminate"]

PROFILE_2B = "2b"
_PROFILE_NAMES = {PROFILE_2B: "PDF/A-2B"}
_HASH_CHUNK = 1024 * 1024
_READ_CHUNK = 64 * 1024
_STAT_KEYS = ("passedRules", "failedRules", "passedChecks", "failedChecks")

# Reason codes are stable identifiers; they never contain paths or raw XML.
REASON_COMPLIANT = "compliant"
REASON_NON_COMPLIANT = "non_compliant"
REASON_INPUT_UNREADABLE = "input_unreadable"
REASON_INPUT_CHANGED = "input_changed_during_validation"
REASON_VALIDATOR_UNAVAILABLE = "validator_unavailable"
REASON_TIMEOUT = "validator_timeout"
REASON_OUTPUT_TOO_LARGE = "report_too_large"
REASON_UNEXPECTED_EXIT = "unexpected_exit_code"
REASON_EXIT_MISMATCH = "exit_code_report_mismatch"
REASON_INVALID_XML = "invalid_report_xml"
REASON_FORBIDDEN_XML = "forbidden_xml_construct"
REASON_UNEXPECTED_STRUCTURE = "unexpected_report_structure"
REASON_JOB_COUNT = "job_count_mismatch"
REASON_JOB_MISMATCH = "job_item_mismatch"
REASON_JOB_FAILED = "job_not_completed"
REASON_REPORT_COUNT = "validation_report_count_mismatch"
REASON_PROFILE_MISMATCH = "profile_mismatch"
REASON_INVALID_VERDICT = "invalid_compliance_verdict"
REASON_INVALID_DETAILS = "invalid_report_details"


@dataclass(frozen=True)
class PdfaValidationResult:
    status: PdfaStatus
    profile: str
    sha256: str | None
    validator: str = "veraPDF"
    validator_version: str | None = None
    reason_codes: tuple[str, ...] = ()
    stats: dict[str, int] = field(default_factory=dict)


class _ReportError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _ProcessOutput:
    returncode: int
    stdout: bytes


class VeraPdfValidator:
    """Synchronous validator; ``command`` is the server-configured veraPDF prefix."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        timeout_seconds: float = 120.0,
        max_report_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError("validator command must be a non-empty sequence of strings")
        if not (isinstance(timeout_seconds, (int, float)) and math.isfinite(timeout_seconds) and timeout_seconds > 0):
            raise ValueError("timeout_seconds must be finite and positive")
        if max_report_bytes <= 0:
            raise ValueError("max_report_bytes must be positive")
        self._command = tuple(command)
        self._timeout = float(timeout_seconds)
        self._max_report_bytes = max_report_bytes

    def validate(self, pdf_path: Path, profile: str = PROFILE_2B) -> PdfaValidationResult:
        if profile not in _PROFILE_NAMES:
            raise ValueError(f"unsupported PDF/A profile: {profile!r}")
        try:
            target = Path(pdf_path).resolve(strict=True)
            if not target.is_file():
                raise OSError("not a regular file")
            digest_before = sha256_file(target)
        except OSError:
            return self._indeterminate(profile, None, REASON_INPUT_UNREADABLE)

        argv = [*self._command, "--format", "xml", "--flavour", profile, str(target)]
        try:
            output = self._run(argv)
        except FileNotFoundError:
            return self._indeterminate(profile, digest_before, REASON_VALIDATOR_UNAVAILABLE)
        except subprocess.TimeoutExpired:
            return self._indeterminate(profile, digest_before, REASON_TIMEOUT)
        except _ReportError as exc:
            return self._indeterminate(profile, digest_before, exc.reason)
        except OSError:
            return self._indeterminate(profile, digest_before, REASON_VALIDATOR_UNAVAILABLE)

        try:
            digest_after = sha256_file(target)
        except OSError:
            return self._indeterminate(profile, digest_before, REASON_INPUT_CHANGED)
        if digest_after != digest_before:
            return self._indeterminate(profile, digest_after, REASON_INPUT_CHANGED)

        if output.returncode not in (0, 1):
            return self._indeterminate(profile, digest_before, REASON_UNEXPECTED_EXIT)
        try:
            compliant, stats, version = _interpret_report(output.stdout, target, str(target), profile)
        except _ReportError as exc:
            return self._indeterminate(profile, digest_before, exc.reason)
        if compliant and output.returncode != 0:
            return PdfaValidationResult("indeterminate", profile, digest_before, validator_version=version,
                                        reason_codes=(REASON_EXIT_MISMATCH,), stats=stats)
        if compliant:
            return PdfaValidationResult("passed", profile, digest_before, validator_version=version,
                                        reason_codes=(REASON_COMPLIANT,), stats=stats)
        return PdfaValidationResult("failed", profile, digest_before, validator_version=version,
                                    reason_codes=(REASON_NON_COMPLIANT,), stats=stats)

    @staticmethod
    def _indeterminate(profile: str, digest: str | None, reason: str) -> PdfaValidationResult:
        return PdfaValidationResult("indeterminate", profile, digest, reason_codes=(reason,))

    def _run(self, argv: list[str]) -> _ProcessOutput:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, shell=False)
        chunks: list[bytes] = []
        state = {"size": 0, "overflow": False}

        def reader() -> None:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(_READ_CHUNK)
                if not chunk:
                    return
                state["size"] += len(chunk)
                if state["size"] > self._max_report_bytes:
                    state["overflow"] = True
                    proc.kill()
                    return
                chunks.append(chunk)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            try:
                returncode = proc.wait(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise
            thread.join(timeout=self._timeout)
            if thread.is_alive():
                raise subprocess.TimeoutExpired(argv, self._timeout)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
        if state["overflow"]:
            raise _ReportError(REASON_OUTPUT_TOO_LARGE)
        return _ProcessOutput(returncode, b"".join(chunks))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local(child.tag) == name]


def _parse_xml(raw: bytes) -> ET.Element:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _ReportError(REASON_INVALID_XML) from exc
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise _ReportError(REASON_FORBIDDEN_XML)
    try:
        return ET.fromstring(text)
    except (ET.ParseError, ValueError) as exc:
        raise _ReportError(REASON_INVALID_XML) from exc


def _same_path(name: str, target: Path, argument: str) -> bool:
    if name == argument:
        return True
    try:
        candidate = Path(name)
        if not candidate.is_absolute():
            return False
        return os.path.normcase(str(candidate.resolve())) == os.path.normcase(str(target))
    except (OSError, ValueError):
        return False


def _interpret_report(
    raw: bytes, target: Path, argument: str, profile: str
) -> tuple[bool, dict[str, int], str | None]:
    root = _parse_xml(raw)
    if _local(root.tag) != "report":
        raise _ReportError(REASON_UNEXPECTED_STRUCTURE)

    jobs_containers = _children(root, "jobs")
    if len(jobs_containers) != 1:
        raise _ReportError(REASON_UNEXPECTED_STRUCTURE)
    jobs = _children(jobs_containers[0], "job")
    if len(jobs) != 1:
        raise _ReportError(REASON_JOB_COUNT)
    job = jobs[0]

    for summary in _children(root, "batchSummary"):
        if summary.get("totalJobs") not in (None, "1"):
            raise _ReportError(REASON_JOB_COUNT)

    items = _children(job, "item")
    names = [name for item in items for name in _children(item, "name")]
    if len(items) != 1 or len(names) != 1 or not _same_path((names[0].text or "").strip(), target, argument):
        raise _ReportError(REASON_JOB_MISMATCH)
    if _children(job, "taskException"):
        raise _ReportError(REASON_JOB_FAILED)

    # Exactly one validationReport in the whole document, and it must belong to the job.
    all_reports = [element for element in root.iter() if _local(element.tag) == "validationReport"]
    job_reports = _children(job, "validationReport")
    if len(all_reports) != 1 or len(job_reports) != 1:
        raise _ReportError(REASON_REPORT_COUNT)
    report = job_reports[0]

    end_status = report.get("jobEndStatus")
    if end_status is not None and end_status != "normal":
        raise _ReportError(REASON_JOB_FAILED)

    expected = _PROFILE_NAMES[profile]
    profile_name = (report.get("profileName") or "").strip().upper()
    if not (profile_name == expected or profile_name.startswith(expected + " ")):
        raise _ReportError(REASON_PROFILE_MISMATCH)

    verdict = report.get("isCompliant")
    if verdict not in ("true", "false"):
        raise _ReportError(REASON_INVALID_VERDICT)
    compliant = verdict == "true"

    stats: dict[str, int] = {}
    details = _children(report, "details")
    if len(details) > 1:
        raise _ReportError(REASON_INVALID_DETAILS)
    if details:
        for key in _STAT_KEYS:
            value = details[0].get(key)
            if value is None:
                continue
            if not value.isdigit():
                raise _ReportError(REASON_INVALID_DETAILS)
            stats[key] = int(value)
    if compliant and (not details or stats.get("failedRules") != 0 or stats.get("failedChecks") != 0):
        raise _ReportError(REASON_INVALID_DETAILS)

    return compliant, stats, _validator_version(root)


def _validator_version(root: ET.Element) -> str | None:
    releases: dict[str, str] = {}
    for build in _children(root, "buildInformation"):
        for release in _children(build, "releaseDetails"):
            release_id, version = release.get("id"), release.get("version")
            if release_id and version and release_id not in releases:
                releases[release_id] = version.strip()[:64]
    return releases.get("gui") or releases.get("core")
