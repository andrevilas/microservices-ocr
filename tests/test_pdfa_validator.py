"""Contract tests for the isolated veraPDF wrapper.

The fake executable only emulates veraPDF's report shape; passing here proves the
wrapper's decision rules, never real PDF/A conformance.
"""
from __future__ import annotations

import hashlib
import os
import signal
import sys
import time
from pathlib import Path

import subprocess
from xml.sax.saxutils import escape

import pytest

from app.services import pdfa_validator
from app.services.cancellation import JobCancelled, ProcessTerminationError, cancellation_scope
from app.services.pdfa_validator import PdfaValidationResult, VeraPdfValidator, _ProcessOutput

PDF_BYTES = b"%PDF-1.7\n% fake content for wrapper tests\n%%EOF\n"

FAKE_TEMPLATE = r'''
import sys, time
from xml.sax.saxutils import escape
args = sys.argv[1:]
if args[:4] != ["--format", "xml", "--flavour", "2b"] or len(args) != 5:
    sys.exit(7)
path = args[4]
if {mutate!r}:
    with open(path, "ab") as handle:
        handle.write(b"tampered")
time.sleep({sleep!r})
out = sys.stdout.buffer
if {flood!r}:
    chunk = b"<x/>" * 4096
    for _ in range({flood!r}):
        out.write(chunk)
out.write({xml!r}.replace("@NAME@", escape(path)).encode("utf-8"))
out.flush()
sys.exit({exit_code!r})
'''


def report(*, compliant: str = "true", profile: str = "PDF/A-2B validation profile", failed: int = 0,
           jobs: str | None = None, namespace: str = "", version: str = "1.26.2",
           batch: str | None = None) -> str:
    xmlns = f' xmlns="{namespace}"' if namespace else ""
    # Shape of a veraPDF 1.30.2 single-file batchSummary; kept consistent with the verdict.
    ok = 1 if compliant == "true" else 0
    default_batch = (
        '<batchSummary totalJobs="1" failedToParse="0" encrypted="0" outOfMemory="0" veraExceptions="0">'
        f'<validationReports compliant="{ok}" nonCompliant="{1 - ok}" failedJobs="0"/></batchSummary>'
    )
    job = (
        '<job><item size="10"><name>@NAME@</name></item>'
        f'<validationReport jobEndStatus="normal" profileName="{profile}" isCompliant="{compliant}">'
        f'<details passedRules="140" failedRules="{failed}" passedChecks="900" failedChecks="{failed * 3}"/>'
        '</validationReport></job>'
    )
    return (
        f'<?xml version="1.0" encoding="utf-8"?><report{xmlns}>'
        f'<buildInformation><releaseDetails id="core" version="1.26.1"/>'
        f'<releaseDetails id="gui" version="{version}"/></buildInformation>'
        f'<jobs>{job if jobs is None else jobs}</jobs>'
        f'{batch if batch is not None else default_batch}</report>'
    )


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="veraPDF execution is POSIX-only")


class ScriptedValidator(VeraPdfValidator):
    """Pure parser tests: the report is produced in-process, so they run on every platform."""

    def __init__(self, xml: str, exit_code: int, mutate: bool, **kwargs) -> None:
        super().__init__(["verapdf-not-executed"], **kwargs)
        self._xml, self._exit_code, self._mutate = xml, exit_code, mutate

    def _run(self, argv: list[str]) -> _ProcessOutput:
        assert argv[1:5] == ["--format", "xml", "--flavour", "2b"] and len(argv) == 6
        if self._mutate:
            with open(argv[5], "ab") as handle:
                handle.write(b"tampered")
        return _ProcessOutput(self._exit_code, self._xml.replace("@NAME@", escape(argv[5])).encode("utf-8"))


def make_validator(tmp_path: Path, xml: str, *, exit_code: int = 0, mutate: bool = False, sleep: float = 0,
                   flood: int = 0, process: bool = False, **kwargs) -> VeraPdfValidator:
    if not process:
        return ScriptedValidator(xml, exit_code, mutate, **kwargs)
    script = tmp_path / "fake_verapdf.py"
    script.write_text(FAKE_TEMPLATE.format(xml=xml, exit_code=exit_code, mutate=mutate, sleep=sleep, flood=flood),
                      encoding="utf-8")
    return VeraPdfValidator([sys.executable, "-I", str(script)], **kwargs)


@pytest.fixture
def pdf(tmp_path: Path) -> Path:
    path = tmp_path / "input dir" / "my document.pdf"
    path.parent.mkdir()
    path.write_bytes(PDF_BYTES)
    return path


def expected_sha(data: bytes = PDF_BYTES) -> str:
    return hashlib.sha256(data).hexdigest()


def assert_indeterminate(result: PdfaValidationResult, reason: str) -> None:
    assert result.status == "indeterminate"
    assert result.reason_codes == (reason,)


def test_passed_report_with_spaces_in_path_and_exact_sha(tmp_path: Path, pdf: Path) -> None:
    result = make_validator(tmp_path, report()).validate(pdf)
    assert result.status == "passed"
    assert result.profile == "2b"
    assert result.sha256 == expected_sha()
    assert result.validator == "veraPDF"
    assert result.validator_version == "1.26.2"
    assert result.reason_codes == ("compliant",)
    assert result.stats == {"passedRules": 140, "failedRules": 0, "passedChecks": 900, "failedChecks": 0}
    # The external result carries no local path or raw XML.
    assert str(pdf) not in repr(result) and "<report" not in repr(result)


def test_namespaced_report_is_accepted(tmp_path: Path, pdf: Path) -> None:
    xml = report(namespace="http://www.verapdf.org/MachineReadableReport")
    assert make_validator(tmp_path, xml).validate(pdf).status == "passed"


def test_explicit_failure(tmp_path: Path, pdf: Path) -> None:
    result = make_validator(tmp_path, report(compliant="false", failed=2), exit_code=1).validate(pdf)
    assert result.status == "failed"
    assert result.reason_codes == ("non_compliant",)
    assert result.stats["failedRules"] == 2
    assert result.sha256 == expected_sha()


@posix_only
def test_missing_executable(tmp_path: Path, pdf: Path) -> None:
    validator = VeraPdfValidator([str(tmp_path / "does-not-exist" / "verapdf")])
    result = validator.validate(pdf)
    assert_indeterminate(result, "validator_unavailable")
    assert result.sha256 == expected_sha()


@posix_only
def test_real_process_passes_and_fails(tmp_path: Path, pdf: Path) -> None:
    assert make_validator(tmp_path, report(), process=True).validate(pdf).status == "passed"
    failed = make_validator(tmp_path, report(compliant="false", failed=1), exit_code=1, process=True).validate(pdf)
    assert failed.status == "failed"


@posix_only
def test_timeout(tmp_path: Path, pdf: Path) -> None:
    result = make_validator(tmp_path, report(), sleep=10, timeout_seconds=0.5, process=True).validate(pdf)
    assert_indeterminate(result, "validator_timeout")


def test_windows_is_unsupported_and_never_starts_a_process(monkeypatch, tmp_path: Path, pdf: Path) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("no process may be started on Windows")
    monkeypatch.setattr(pdfa_validator, "_PLATFORM", "win32")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    result = VeraPdfValidator([sys.executable]).validate(pdf)
    assert_indeterminate(result, "unsupported_platform")
    assert result.sha256 == expected_sha()
    assert not pdfa_validator.platform_supported()


def test_invalid_xml(tmp_path: Path, pdf: Path) -> None:
    result = make_validator(tmp_path, report()[:-20]).validate(pdf)
    assert_indeterminate(result, "invalid_report_xml")


def test_doctype_and_entities_are_rejected(tmp_path: Path, pdf: Path) -> None:
    xml = report().replace('<?xml version="1.0" encoding="utf-8"?>',
                           '<?xml version="1.0"?><!DOCTYPE report [<!ENTITY x "y">]>')
    assert_indeterminate(make_validator(tmp_path, xml).validate(pdf), "forbidden_xml_construct")


def test_wrong_profile(tmp_path: Path, pdf: Path) -> None:
    result = make_validator(tmp_path, report(profile="PDF/A-1B validation profile")).validate(pdf)
    assert_indeterminate(result, "profile_mismatch")


@pytest.mark.parametrize("jobs", ["", None])
def test_empty_or_multiple_jobs(tmp_path: Path, pdf: Path, jobs: str | None) -> None:
    if jobs is None:
        single = report().split("<jobs>")[1].split("</jobs>")[0]
        jobs = single + single
    assert_indeterminate(make_validator(tmp_path, report(jobs=jobs)).validate(pdf), "job_count_mismatch")


def test_job_for_other_file_is_rejected(tmp_path: Path, pdf: Path) -> None:
    xml = report().replace("@NAME@", "/elsewhere/other.pdf")
    assert_indeterminate(make_validator(tmp_path, xml).validate(pdf), "job_item_mismatch")


def test_summary_without_validation_report_is_not_proof(tmp_path: Path, pdf: Path) -> None:
    jobs = '<job><item><name>@NAME@</name></item><featuresReport/></job>'
    assert_indeterminate(make_validator(tmp_path, report(jobs=jobs)).validate(pdf),
                         "validation_report_count_mismatch")


def test_compliant_without_details_is_indeterminate(tmp_path: Path, pdf: Path) -> None:
    xml = report().replace('<details passedRules="140" failedRules="0" passedChecks="900" failedChecks="0"/>', "")
    assert_indeterminate(make_validator(tmp_path, xml).validate(pdf), "invalid_report_details")


def test_compliant_report_with_nonzero_exit_is_indeterminate(tmp_path: Path, pdf: Path) -> None:
    assert_indeterminate(make_validator(tmp_path, report(), exit_code=1).validate(pdf), "exit_code_report_mismatch")
    assert_indeterminate(make_validator(tmp_path, report(), exit_code=2).validate(pdf), "unexpected_exit_code")


def test_pdf_altered_during_validation(tmp_path: Path, pdf: Path) -> None:
    result = make_validator(tmp_path, report(), mutate=True).validate(pdf)
    assert_indeterminate(result, "input_changed_during_validation")
    assert result.sha256 == expected_sha(PDF_BYTES + b"tampered")


@posix_only
def test_report_size_limit(tmp_path: Path, pdf: Path) -> None:
    result = make_validator(tmp_path, report(), flood=64, max_report_bytes=64 * 1024, process=True).validate(pdf)
    assert_indeterminate(result, "report_too_large")


def test_unreadable_input(tmp_path: Path) -> None:
    result = make_validator(tmp_path, report()).validate(tmp_path / "missing.pdf")
    assert_indeterminate(result, "input_unreadable")
    assert result.sha256 is None


def test_rejects_unsafe_configuration_and_profiles(tmp_path: Path, pdf: Path) -> None:
    with pytest.raises(ValueError):
        VeraPdfValidator([])
    with pytest.raises(ValueError):
        VeraPdfValidator(["verapdf"], timeout_seconds=float("inf"))
    with pytest.raises(ValueError):
        make_validator(tmp_path, report()).validate(pdf, profile="1a")


SPAWNER = r'''
import subprocess, sys
pid_file = sys.argv[1]
child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(3)"])
with open(pid_file, "w") as handle:
    handle.write(str(child.pid))
sys.stdout.flush()
child.wait()
'''


def _alive(pid: int) -> bool:
    import os
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) != 0
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    with open(f"/proc/{pid}/stat", "rb") as handle:
        return handle.read().split(b") ", 1)[1][:1] != b"Z"


@posix_only
def test_timeout_kills_descendant_holding_stdout(tmp_path: Path, pdf: Path) -> None:
    import time
    script = tmp_path / "spawner.py"
    script.write_text(SPAWNER, encoding="utf-8")
    pid_file = tmp_path / "child.pid"
    validator = VeraPdfValidator([sys.executable, "-I", str(script), str(pid_file)], timeout_seconds=0.15)
    started = time.monotonic()
    result = validator.validate(pdf)
    assert time.monotonic() - started < 2.5
    assert_indeterminate(result, "validator_timeout")
    if pid_file.exists():
        child_pid = int(pid_file.read_text())
        deadline = time.monotonic() + 2
        while _alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _alive(child_pid)


@pytest.mark.parametrize("attrs", [
    'failedToParse="1"', 'encrypted="1"', 'outOfMemory="1"', 'veraExceptions="1"',
])
def test_batch_summary_failures_are_rejected(tmp_path: Path, pdf: Path, attrs: str) -> None:
    key = attrs.split("=")[0]
    xml = report().replace(f'{key}="0"', attrs)
    assert make_validator(tmp_path, xml).validate(pdf).status == "indeterminate"


@pytest.mark.parametrize("batch", [
    "",  # missing
    '<batchSummary totalJobs="1"><validationReports compliant="1" nonCompliant="0" failedJobs="0"/></batchSummary>',
    '<batchSummary totalJobs="1" failedToParse="0" encrypted="0" outOfMemory="0" veraExceptions="0">'
    '<validationReports compliant="1" nonCompliant="0" failedJobs="1"/></batchSummary>',
    '<batchSummary totalJobs="1" failedToParse="0" encrypted="0" outOfMemory="0" veraExceptions="0">'
    '<validationReports compliant="0" nonCompliant="1" failedJobs="0"/></batchSummary>',
    '<batchSummary totalJobs="1" failedToParse="0" encrypted="0" outOfMemory="0" veraExceptions="0">'
    '<validationReports compliant="1" nonCompliant="1" failedJobs="0"/></batchSummary>',
])
def test_contradictory_or_incomplete_batch_summary(tmp_path: Path, pdf: Path, batch: str) -> None:
    result = make_validator(tmp_path, report(batch=batch)).validate(pdf)
    assert result.status == "indeterminate"


def test_verapdf_1_30_2_shape_passes(tmp_path: Path, pdf: Path) -> None:
    result = make_validator(tmp_path, report(profile="PDF/A-2b validation profile", version="1.30.2")).validate(pdf)
    assert result.status == "passed"
    assert result.validator_version == "1.30.2"
    assert result.stats == {"passedRules": 140, "failedRules": 0, "passedChecks": 900, "failedChecks": 0}


@pytest.mark.parametrize("value", ["9" * 5000, "١٢", "²", "-1", "", " 1"])
def test_counters_must_be_bounded_ascii_digits(tmp_path: Path, pdf: Path, value: str) -> None:
    xml = report().replace('passedChecks="900"', f'passedChecks="{value}"')
    assert_indeterminate(make_validator(tmp_path, xml).validate(pdf), "invalid_report_details")


def test_noncompliant_without_failures_is_indeterminate(tmp_path: Path, pdf: Path) -> None:
    result = make_validator(tmp_path, report(compliant="false", failed=0), exit_code=1).validate(pdf)
    assert_indeterminate(result, "invalid_report_details")


def test_source_is_untouched_and_hash_covers_validated_bytes(tmp_path: Path, pdf: Path) -> None:
    result = make_validator(tmp_path, report(), mutate=True).validate(pdf)
    assert result.status == "indeterminate"
    assert pdf.read_bytes() == PDF_BYTES


# Cancellation and process-group termination. Containment is implemented only for Linux/POSIX
# (killpg + /proc); on Windows the validator never starts a process, so there is nothing to prove.
linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="validator process-group termination is implemented only on Linux/POSIX; "
           "Windows returns unsupported_platform without starting a process",
)

# Leader spawns a long-lived descendant in its session. "open": the descendant inherits stdout, so
# the reader never sees EOF (first wait loop). "closed": nobody holds stdout, so the reader ends
# and the leader keeps running (second wait loop). PIDs are published atomically.
CANCEL_SPAWNER = r'''
import os, subprocess, sys
pid_file, mode = sys.argv[1], sys.argv[2]
out = subprocess.DEVNULL if mode == "closed" else None
child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(30)"], stdout=out)
if mode == "closed":
    os.close(1)
with open(pid_file + ".tmp", "w") as handle:
    handle.write(f"{os.getpid()} {child.pid}")
os.replace(pid_file + ".tmp", pid_file)
child.wait()
'''


def _live_members(pgid: int) -> list[int]:
    """Non-zombie processes of ``pgid`` read directly from /proc (independent of the wrapper)."""
    members = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            with open(f"/proc/{entry.name}/stat", "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        fields = data[data.rfind(b")") + 2:].split()
        if len(fields) >= 3 and fields[2] == str(pgid).encode() and fields[0] not in (b"Z", b"X"):
            members.append(int(entry.name))
    return members


def _spawner_validator(tmp_path: Path, mode: str, pid_file: Path) -> VeraPdfValidator:
    script = tmp_path / "cancel_spawner.py"
    script.write_text(CANCEL_SPAWNER, encoding="utf-8")
    return VeraPdfValidator([sys.executable, "-I", str(script), str(pid_file), mode], timeout_seconds=60)


def _record_terminations(monkeypatch) -> list[int]:
    calls: list[int] = []
    original = pdfa_validator._terminate_group

    def spy(proc):
        calls.append(proc.pid)
        original(proc)
    monkeypatch.setattr(pdfa_validator, "_terminate_group", spy)
    return calls


@linux_only
@pytest.mark.parametrize("mode", ["open", "closed"])
def test_cancel_during_wait_kills_whole_group(monkeypatch, tmp_path: Path, pdf: Path, mode: str) -> None:
    pid_file = tmp_path / "pids"
    validator = _spawner_validator(tmp_path, mode, pid_file)
    terminations = _record_terminations(monkeypatch)
    started = time.monotonic()
    with pytest.raises(JobCancelled):
        with cancellation_scope(pid_file.exists):
            validator.validate(pdf)
    # Cancelled well before the 30 s sleep or the 60 s timeout.
    assert time.monotonic() - started < 15
    leader, child = (int(part) for part in pid_file.read_text().split())
    assert terminations == [leader]
    assert _live_members(leader) == []
    assert child not in _live_members(leader)


@linux_only
def test_cancel_at_checkpoint_before_start_never_spawns(monkeypatch, pdf: Path) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("no process may be started after cancellation")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    checks = {"count": 0}

    def checker() -> bool:
        # The scope's own entry checkpoint passes; the validator's pre-spawn checkpoint cancels.
        checks["count"] += 1
        return checks["count"] > 1
    with pytest.raises(JobCancelled):
        with cancellation_scope(checker):
            VeraPdfValidator([sys.executable]).validate(pdf)
    assert checks["count"] == 2


@linux_only
def test_unconfirmed_group_death_raises_process_termination_error(monkeypatch, tmp_path: Path, pdf: Path) -> None:
    pid_file = tmp_path / "pids"
    validator = _spawner_validator(tmp_path, "open", pid_file)
    real_group_alive = pdfa_validator._group_alive
    monkeypatch.setattr(pdfa_validator, "_CLEANUP_GRACE", 0.2)
    monkeypatch.setattr(pdfa_validator, "_group_alive", lambda pgid: True)
    # The termination failure replaces the cancellation: the job must not be reported as cancelled.
    with pytest.raises(ProcessTerminationError):
        with cancellation_scope(pid_file.exists):
            validator.validate(pdf)
    leader, _ = (int(part) for part in pid_file.read_text().split())
    # SIGKILL was still sent; only its confirmation was forced to fail.
    deadline = time.monotonic() + 2
    while real_group_alive(leader) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert _live_members(leader) == []


class _UnkillableProcess:
    pid = 424242

    def poll(self):
        return None

    def kill(self):
        pass

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("verapdf", timeout)


@linux_only
def test_leader_that_does_not_exit_raises_process_termination_error(monkeypatch) -> None:
    signals: list[tuple[int, int]] = []
    # Never signal a real group from a unit test: record the call instead.
    monkeypatch.setattr(pdfa_validator.os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
    monkeypatch.setattr(pdfa_validator, "_CLEANUP_GRACE", 0.05)
    with pytest.raises(ProcessTerminationError, match="did not exit"):
        pdfa_validator._terminate_group(_UnkillableProcess())
    assert signals == [(424242, signal.SIGKILL)]


@linux_only
def test_surviving_group_member_raises_process_termination_error(monkeypatch) -> None:
    class Exited(_UnkillableProcess):
        def wait(self, timeout=None):
            return -9
    monkeypatch.setattr(pdfa_validator.os, "killpg", lambda pgid, sig: None)
    monkeypatch.setattr(pdfa_validator, "_CLEANUP_GRACE", 0.05)
    monkeypatch.setattr(pdfa_validator, "_group_alive", lambda pgid: True)
    with pytest.raises(ProcessTerminationError, match="could not be confirmed"):
        pdfa_validator._terminate_group(Exited())
