"""Contract tests for the isolated veraPDF wrapper.

The fake executable only emulates veraPDF's report shape; passing here proves the
wrapper's decision rules, never real PDF/A conformance.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from app.services.pdfa_validator import PdfaValidationResult, VeraPdfValidator

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
           jobs: str | None = None, namespace: str = "", version: str = "1.26.2") -> str:
    xmlns = f' xmlns="{namespace}"' if namespace else ""
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
        '<batchSummary totalJobs="1"><validationReports compliant="1" nonCompliant="0" failedJobs="0"/>'
        '</batchSummary></report>'
    )


def make_validator(tmp_path: Path, xml: str, *, exit_code: int = 0, mutate: bool = False, sleep: float = 0,
                   flood: int = 0, **kwargs) -> VeraPdfValidator:
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


def test_missing_executable(tmp_path: Path, pdf: Path) -> None:
    validator = VeraPdfValidator([str(tmp_path / "does-not-exist" / "verapdf")])
    result = validator.validate(pdf)
    assert_indeterminate(result, "validator_unavailable")
    assert result.sha256 == expected_sha()


def test_timeout(tmp_path: Path, pdf: Path) -> None:
    result = make_validator(tmp_path, report(), sleep=10, timeout_seconds=0.5).validate(pdf)
    assert_indeterminate(result, "validator_timeout")


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


def test_report_size_limit(tmp_path: Path, pdf: Path) -> None:
    result = make_validator(tmp_path, report(), flood=64, max_report_bytes=64 * 1024).validate(pdf)
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
