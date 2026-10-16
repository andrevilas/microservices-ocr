"""E1: final PDF/A-2b validation in the orchestrator, presets and capability reporting (no real engines)."""
import shutil
from unittest.mock import Mock

import pytest

from app.application import JobApplication, Principal
from app.config import settings
from app.options import COMPRESSION_PRESET_VERSION
from app.services import pdfa_validator
from app.services.ocr_orchestrator import OcrOrchestrator
from app.services.ocrmypdf_service import OcrmypdfService, preset_arguments
from app.services.pdfa_validator import PdfaValidationResult
from test_options_capabilities import probes, submit  # noqa: F401
from test_upload_admission import system, upload  # noqa: F401

READER = Principal("user:1", 1, frozenset({"jobs:read", "artifacts:read"}))


def run_job(system, options, validator=None, **kwargs):
    from test_results import pdf
    receipt = upload(system, data=pdf(["the original document has a name and a date for the page"]))
    response = submit(system, receipt, options)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    engine = Mock()
    engine.is_available.return_value = True
    engine.run_ocr.side_effect = lambda source, target, *, options: shutil.copyfile(source, target)
    def optimize(source, target, *, options):
        shutil.copyfile(source, target)
        return {"arguments": preset_arguments(options.compression_profile, options.output_format), "fallback": None}
    engine.optimize_pdfa.side_effect = optimize
    fallback = Mock()
    fallback.is_available.return_value = False
    extra = {} if validator is None else {"pdfa_validator": validator}
    result = OcrOrchestrator(system.jobs, ocrmypdf_service=engine, fallback_ocr=fallback, **extra, **kwargs).process_job(job_id)
    return job_id, result


def stub(status, reasons, stats=None):
    validator = Mock()
    validator.validate.return_value = PdfaValidationResult(status, "2b", "a" * 64, validator_version="1.30.2",
                                                           reason_codes=reasons, stats=stats or {})
    return validator


def test_passed_validation_completes_with_structured_result(system, probes):
    job_id, result = run_job(system, {"languages": ["eng"]}, stub("passed", ("compliant",)))
    assert result is not None
    assert system.jobs.get_snapshot(job_id).status == "completed"
    validation = JobApplication(system.jobs).report(READER, job_id).output_validation
    assert validation.pdfa_conformance == "passed" and validation.pdfa_profile == "2b"
    assert validation.pdfa_validator == "veraPDF" and validation.pdfa_validator_version == "1.30.2"
    assert validation.compression_preset_version == COMPRESSION_PRESET_VERSION
    assert validation.compression_arguments == ["--output-type", "pdfa-2", "--optimize", "1"]


@pytest.mark.parametrize("status,reasons,code", [
    ("failed", ("non_compliant",), "pdfa_validation_failed"),
    ("indeterminate", ("validator_timeout",), "pdfa_validation_indeterminate"),
    ("indeterminate", ("unsupported_platform",), "pdfa_validation_indeterminate"),
])
def test_failed_or_indeterminate_validation_fails_job_after_report(system, probes, status, reasons, code):
    stats = {"passedRules": 1, "failedRules": 2, "passedChecks": 3, "failedChecks": 4} if status == "failed" else {}
    job_id, result = run_job(system, {"languages": ["eng"]}, stub(status, reasons, stats))
    assert result is None
    snapshot = system.jobs.get_snapshot(job_id)
    assert snapshot.status == "failed" and snapshot.error_code == code
    from app.results import read_report
    report = read_report(snapshot.working_dir)
    assert report.output_validation.pdfa_conformance == status
    assert report.output_validation.pdfa_reason_codes == list(reasons)
    assert report.output_validation.pdfa_stats == stats


def test_unconfigured_validator_fails_closed(system, probes, monkeypatch):
    monkeypatch.setattr(settings, "verapdf_executable", None)
    job_id, result = run_job(system, {"languages": ["eng"]})
    assert result is None
    snapshot = system.jobs.get_snapshot(job_id)
    assert snapshot.status == "failed" and snapshot.error_code == "pdfa_validation_indeterminate"


def test_searchable_pdf_is_not_validated_and_completes(system, probes):
    validator = stub("failed", ("non_compliant",))
    job_id, result = run_job(system, {"languages": ["eng"], "output_format": "pdf_searchable",
                                      "compression_profile": "preservation"}, validator)
    assert result is not None and validator.validate.call_count == 0
    report = JobApplication(system.jobs).report(READER, job_id)
    assert report.output_validation.pdfa_conformance == "not_validated"
    assert "pdfa_conformance_not_validated" in report.warnings


def test_extract_text_never_produces_pdfa(system, probes):
    receipt = upload(system)
    response = submit(system, receipt, {"processing_mode": "extract_text", "output_format": "pdfa"})
    assert response.status_code == 422


@pytest.mark.parametrize("profile,fmt,expected", [
    ("none", "pdfa", ["--output-type", "pdfa-2", "--optimize", "0"]),
    ("balanced", "pdfa", ["--output-type", "pdfa-2", "--optimize", "1"]),
    ("economic", "pdfa", ["--output-type", "pdfa-2", "--optimize", "2", "--jpeg-quality", "75"]),
    ("preservation", "pdfa", ["--output-type", "pdfa-2", "--optimize", "0", "--pdfa-image-compression", "lossless"]),
    ("preservation", "pdf_searchable", ["--output-type", "pdf", "--optimize", "0"]),
])
def test_preset_arguments_are_fixed_and_conservative(profile, fmt, expected):
    args = preset_arguments(profile, fmt)
    assert args == expected
    for forbidden in ("--jbig2-lossy", "--force-ocr", "--oversample", "--redo-ocr"):
        assert forbidden not in args


def test_economic_falls_back_without_pngquant_and_records_it(system, probes, monkeypatch, tmp_path):
    _, _, service = probes
    from app.options import ProcessingOptions
    options = service.resolve(ProcessingOptions(languages=("eng",), compression_profile="economic"))
    calls = []
    def run(self, args, *, timeout=None):
        calls.append(args)
        if "2" == args[args.index("--optimize") + 1]:
            raise RuntimeError("pngquant not found")
    monkeypatch.setattr(OcrmypdfService, "_run", run)
    effective = OcrmypdfService().optimize_pdfa(tmp_path / "in.pdf", tmp_path / "out.pdf", options)
    assert effective == {"arguments": ["--output-type", "pdfa-2", "--optimize", "1"],
                         "fallback": "pngquant_unavailable_used_optimize_1"}
    assert len(calls) == 2


def test_capabilities_report_validator_reason_without_paths(system, probes, monkeypatch):
    _, _, service = probes
    monkeypatch.setattr(pdfa_validator, "_PLATFORM", "win32")
    capabilities = service.snapshot(refresh=True)
    assert capabilities.engines["verapdf"].reasons == ["unsupported_platform"]
    assert "pdfa_validator_unavailable_pdfa_jobs_will_fail" in capabilities.warnings
    assert capabilities.compression_profiles == ["none", "economic", "balanced", "preservation"]
    assert capabilities.experimental_compression_profiles == ["economic"]
    monkeypatch.setattr(pdfa_validator, "_PLATFORM", "linux")
    monkeypatch.setattr(settings, "verapdf_executable", None)
    assert service.snapshot(refresh=True).engines["verapdf"].reasons == ["validator_not_configured"]


def test_effective_options_record_preset_version_and_timeout(probes):
    _, _, service = probes
    from app.options import ProcessingOptions
    effective = service.resolve(ProcessingOptions(languages=("eng",)))
    assert effective.output_format == "pdfa" and effective.compression_profile == "balanced"
    assert effective.compression_preset_version == COMPRESSION_PRESET_VERSION
    assert effective.pdfa_validation_timeout_seconds == settings.verapdf_timeout_seconds
