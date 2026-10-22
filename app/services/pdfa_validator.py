"""Isolated PDF/A validation through an external veraPDF executable.

The command is fixed by the server; requests may only choose the file. Execution is POSIX-only:
on Windows the result is ``indeterminate`` (``unsupported_platform``) and no process is started.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import signal
import tempfile
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Sequence

from app.services.cancellation import ProcessTerminationError, checkpoint

PdfaStatus = Literal["passed", "failed", "indeterminate"]

PROFILE_2B = "2b"
_PROFILE_NAMES = {PROFILE_2B: "PDF/A-2B"}
_HASH_CHUNK = 1024 * 1024
_READ_CHUNK = 64 * 1024
_STAT_KEYS = ("passedRules", "failedRules", "passedChecks", "failedChecks")
_CLEANUP_GRACE = 2.0
# ASCII digits only, bounded length: str.isdigit() accepts Unicode digits that int() may reject.
_COUNTER = re.compile(r"[0-9]{1,12}\Z")
_BATCH_ZERO_KEYS = ("failedToParse", "encrypted", "outOfMemory", "veraExceptions")
REASON_BATCH_SUMMARY = "invalid_batch_summary"


def _counter(value: str | None, reason: str) -> int:
    if value is None or not _COUNTER.match(value):
        raise _ReportError(reason)
    return int(value)

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
REASON_UNSUPPORTED_PLATFORM = "unsupported_platform"
REASON_NOT_CONFIGURED = "validator_not_configured"

_PLATFORM = sys.platform


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
            source = Path(pdf_path).resolve(strict=True)
            if not source.is_file():
                raise OSError("not a regular file")
        except OSError:
            return self._indeterminate(profile, None, REASON_INPUT_UNREADABLE)
        # Validate a private frozen copy; the reported hash covers exactly those bytes, and the
        # source must still match them afterwards so the delivered file is the validated one.
        with tempfile.TemporaryDirectory(prefix="pdfa-validate-", ignore_cleanup_errors=True) as private_dir:
            target = Path(private_dir) / "candidate.pdf"
            try:
                shutil.copyfile(source, target)
                digest_before = sha256_file(target)
            except OSError:
                return self._indeterminate(profile, None, REASON_INPUT_UNREADABLE)
            result = self._validate_frozen(target, profile, digest_before)
        try:
            source_digest = sha256_file(source)
        except OSError:
            return self._indeterminate(profile, None, REASON_INPUT_CHANGED)
        if REASON_INPUT_CHANGED not in result.reason_codes and source_digest != digest_before:
            return self._indeterminate(profile, source_digest, REASON_INPUT_CHANGED)
        return result

    def _validate_frozen(self, target: Path, profile: str, digest_before: str) -> PdfaValidationResult:
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
        # Process-tree containment is only guaranteed on POSIX (production is Linux/Docker):
        # refuse before any process is started instead of risking an orphaned descendant.
        if not platform_supported():
            raise _ReportError(REASON_UNSUPPORTED_PLATFORM)
        checkpoint()
        # Own session so descendants holding the pipe are reachable through killpg.
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, shell=False, start_new_session=True)
        deadline = time.monotonic() + self._timeout
        chunks: list[bytes] = []
        state = {"size": 0}
        overflow = threading.Event()

        def reader() -> None:
            assert proc.stdout is not None
            try:
                while True:
                    chunk = proc.stdout.read1(_READ_CHUNK)
                    if not chunk:
                        return
                    state["size"] += len(chunk)
                    if state["size"] > self._max_report_bytes:
                        overflow.set()
                        return
                    chunks.append(chunk)
            except (OSError, ValueError):
                return

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            # Single deadline covers both EOF on stdout (descendants included) and process exit;
            # every wait slice is a cancellation checkpoint.
            while thread.is_alive() and not overflow.is_set():
                checkpoint()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, self._timeout)
                thread.join(timeout=min(remaining, 0.05))
            if overflow.is_set():
                raise _ReportError(REASON_OUTPUT_TOO_LARGE)
            while proc.poll() is None:
                checkpoint()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, self._timeout)
                try:
                    proc.wait(timeout=min(remaining, 0.05))
                except subprocess.TimeoutExpired:
                    pass
            return _ProcessOutput(proc.returncode, b"".join(chunks))
        finally:
            # Every exit (normal, timeout, overflow, JobCancelled, interrupts) sweeps the group; an
            # unconfirmed termination raises ProcessTerminationError instead of the original error.
            _terminate_group(proc)
            # Bounded: never block on close while a reader may still be attached.
            thread.join(timeout=_CLEANUP_GRACE)
            if not thread.is_alive() and proc.stdout is not None:
                proc.stdout.close()


def platform_supported() -> bool:
    return _PLATFORM != "win32"


def build_validator(command: str | None, timeout_seconds: float) -> VeraPdfValidator | None:
    """Server-configured validator (single executable path, no shell parsing); ``None`` if unset."""
    return VeraPdfValidator([command], timeout_seconds=timeout_seconds) if command else None


def unavailable_result(reason: str, digest: str | None = None) -> PdfaValidationResult:
    return PdfaValidationResult("indeterminate", PROFILE_2B, digest, reason_codes=(reason,))


def _terminate_group(proc: subprocess.Popen) -> None:
    """Kill the validator session and confirm it is gone (POSIX only); never swallow a survivor."""
    pgid = proc.pid
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        # e.g. EPERM on a zombie leader: fall through to the explicit confirmation below.
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=_CLEANUP_GRACE)
    except subprocess.TimeoutExpired:
        raise ProcessTerminationError("Validator process did not exit.") from None
    deadline = time.monotonic() + _CLEANUP_GRACE
    while _group_alive(pgid):
        if time.monotonic() >= deadline:
            raise ProcessTerminationError("Validator process group termination could not be confirmed.")
        time.sleep(0.02)


def _group_alive(pgid: int) -> bool:
    """True while a non-zombie member of ``pgid`` exists; zombies left for init are already dead."""
    if os.path.isfile("/proc/self/stat"):
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit():
                continue
            try:
                with open(f"/proc/{entry.name}/stat", "rb") as handle:
                    data = handle.read()
            except OSError:
                continue
            # Fields after the command's closing parenthesis: state ppid pgrp ...
            fields = data[data.rfind(b")") + 2:].split()
            if len(fields) >= 3 and fields[2] == str(pgid).encode() and fields[0] not in (b"Z", b"X"):
                return True
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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

    summaries = _children(root, "batchSummary")
    if len(summaries) != 1:
        raise _ReportError(REASON_BATCH_SUMMARY)
    summary = summaries[0]
    if _counter(summary.get("totalJobs"), REASON_BATCH_SUMMARY) != 1:
        raise _ReportError(REASON_JOB_COUNT)
    for key in _BATCH_ZERO_KEYS:
        if _counter(summary.get(key), REASON_BATCH_SUMMARY) != 0:
            raise _ReportError(REASON_JOB_FAILED)
    summary_reports = _children(summary, "validationReports")
    if len(summary_reports) != 1:
        raise _ReportError(REASON_BATCH_SUMMARY)
    summary_compliant = _counter(summary_reports[0].get("compliant"), REASON_BATCH_SUMMARY)
    summary_non_compliant = _counter(summary_reports[0].get("nonCompliant"), REASON_BATCH_SUMMARY)
    if _counter(summary_reports[0].get("failedJobs"), REASON_BATCH_SUMMARY) != 0:
        raise _ReportError(REASON_JOB_FAILED)
    if summary_compliant + summary_non_compliant != 1:
        raise _ReportError(REASON_BATCH_SUMMARY)

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
    if summary_compliant != (1 if compliant else 0):
        raise _ReportError(REASON_BATCH_SUMMARY)

    # Fail closed: all four counters are mandatory and must agree with the verdict.
    details = _children(report, "details")
    if len(details) != 1:
        raise _ReportError(REASON_INVALID_DETAILS)
    stats = {key: _counter(details[0].get(key), REASON_INVALID_DETAILS) for key in _STAT_KEYS}
    if compliant and (stats["failedRules"] != 0 or stats["failedChecks"] != 0):
        raise _ReportError(REASON_INVALID_DETAILS)
    if not compliant and stats["failedRules"] == 0 and stats["failedChecks"] == 0:
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
