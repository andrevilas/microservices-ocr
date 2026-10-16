"""E1: final PDF/A-2b validation in the orchestrator, presets and capability reporting (no real engines)."""
import asyncio
import hashlib
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.application import ApplicationError, JobApplication, Principal
from app.config import settings
from app.options import COMPRESSION_PRESET_VERSION
from app.results import read_report
from app.services import pdfa_validator
from app.services.ocr_orchestrator import REASON_HASH_MISMATCH, OcrOrchestrator
from app.services.ocrmypdf_service import OcrmypdfService, preset_arguments
from app.services.pdfa_validator import PdfaValidationResult
from test_options_capabilities import probes, submit  # noqa: F401
from test_upload_admission import bearer, system, upload  # noqa: F401

READER = Principal("user:1", 1, frozenset({"jobs:read", "artifacts:read"}))
FOREIGN = Principal("user:2", 2, READER.scopes)


def engines():
    engine = Mock()
    engine.is_available.return_value = True
    engine.run_ocr.side_effect = lambda source, target, *args, **kwargs: shutil.copyfile(source, target)
    def optimize(source, target, *, options=None):
        shutil.copyfile(source, target)
        if options is None:
            return None
        return {"arguments": preset_arguments(options.compression_profile, options.output_format), "fallback": None}
    engine.optimize_pdfa.side_effect = optimize
    fallback = Mock()
    fallback.is_available.return_value = False
    return engine, fallback


def run_job(system, options, validator=None, **kwargs):
    from test_results import pdf
    receipt = upload(system, data=pdf(["the original document has a name and a date for the page"]))
    response = submit(system, receipt, options)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    engine, fallback = engines()
    extra = {} if validator is None else {"pdfa_validator": validator}
    result = OcrOrchestrator(system.jobs, ocrmypdf_service=engine, fallback_ocr=fallback, **extra, **kwargs).process_job(job_id)
    return job_id, result


def stub(status, reasons, stats=None, digest=None):
    """Like the real wrapper, the reported hash is that of the bytes received unless ``digest`` forges one."""
    validator = Mock()
    validator.seen = []
    def validate(path, profile):
        data = Path(path).read_bytes()
        validator.seen.append(data)
        return PdfaValidationResult(status, profile, digest or hashlib.sha256(data).hexdigest(),
                                    validator_version="1.30.2", reason_codes=reasons, stats=stats or {})
    validator.validate.side_effect = validate
    return validator


def input_digest(system, job_id):
    return hashlib.sha256(system.jobs.get_snapshot(job_id).input_pdf_path.read_bytes()).hexdigest()


def test_passed_validation_publishes_exactly_the_validated_bytes(system, probes):
    validator = stub("passed", ("compliant",))
    job_id, result = run_job(system, {"languages": ["eng"]}, validator)
    assert result is not None
    snapshot = system.jobs.get_snapshot(job_id)
    assert snapshot.status == "completed"
    report = JobApplication(system.jobs).report(READER, job_id)
    validation = report.output_validation
    assert validation.pdfa_conformance == "passed" and validation.pdfa_profile == "2b"
    assert validation.pdfa_validator == "veraPDF" and validation.pdfa_validator_version == "1.30.2"
    assert validation.compression_preset_version == COMPRESSION_PRESET_VERSION
    assert validation.compression_arguments == ["--output-type", "pdfa-2", "--optimize", "1"]
    published = snapshot.output_pdf_path.read_bytes()
    assert validator.seen == [published]
    assert report.output.sha256 == validation.pdfa_validated_sha256 == hashlib.sha256(published).hexdigest()
    assert report.input.sha256 == input_digest(system, job_id)
    assert not (snapshot.working_dir / "final-candidate.pdf").exists()
    assert [s.name for s in report.stages][-1] == "pdfa_validation"


def test_validator_hash_divergence_is_indeterminate_and_nothing_is_delivered(system, probes):
    job_id, result = run_job(system, {"languages": ["eng"]}, stub("passed", ("compliant",), digest="a" * 64))
    assert result is None
    snapshot = system.jobs.get_snapshot(job_id)
    assert snapshot.status == "failed" and snapshot.error_code == "pdfa_validation_indeterminate"
    assert snapshot.output_pdf_path is None
    assert not (snapshot.working_dir / "final-searchable-pdfa.pdf").exists()
    assert not (snapshot.working_dir / "final-candidate.pdf").exists()
    validation = read_report(snapshot.working_dir).output_validation
    assert validation.pdfa_conformance == "indeterminate"
    assert validation.pdfa_reason_codes == [REASON_HASH_MISMATCH]
    with pytest.raises(ApplicationError) as error:
        JobApplication(system.jobs).artifact_file(READER, job_id)
    assert error.value.status == 409


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
    assert snapshot.output_pdf_path is None
    report = read_report(snapshot.working_dir)
    assert report.output_validation.pdfa_conformance == status
    assert report.output_validation.pdfa_reason_codes == list(reasons)
    assert report.output_validation.pdfa_stats == stats
    assert report.input.sha256 == input_digest(system, job_id)
    # The retained report of a failed job is readable by its owner; text stays blocked.
    assert JobApplication(system.jobs).report(READER, job_id) == report
    with pytest.raises(ApplicationError) as error:
        JobApplication(system.jobs).text(READER, job_id)
    assert error.value.code == "result_not_ready"


def test_failed_report_http_and_mcp_parity_and_access(system, probes, monkeypatch):
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from app.mcp_server import build_mcp_server
    job_id, _ = run_job(system, {"languages": ["eng"]}, stub("failed", ("non_compliant",),
                                                             {"passedRules": 1, "failedRules": 1,
                                                              "passedChecks": 1, "failedChecks": 1}))
    headers = bearer(system.tokens[0])
    report = system.client.get(f"/api/v1/jobs/{job_id}/report", headers=headers)
    assert report.status_code == 200 and report.json()["output_validation"]["pdfa_conformance"] == "failed"
    assert system.client.get(f"/api/v1/jobs/{job_id}/text", headers=headers).status_code == 409
    assert system.client.get(f"/api/v1/jobs/{job_id}/artifact/content", headers=headers).status_code == 409
    assert system.client.get(f"/api/v1/jobs/{job_id}/artifact", headers=headers).json()["available"] is False
    service = JobApplication(system.jobs)
    principal = [READER]
    monkeypatch.setattr("app.mcp_server.authenticate_bearer", lambda _: principal[0])
    server, transport = build_mcp_server(lambda: service)
    asgi = Starlette(routes=[Mount("/", app=transport)])
    async def scenario():
        async with server.session_manager.run():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=asgi)) as http:
                async with Client(streamable_http_client("http://localhost/mcp", http_client=http)) as mcp:
                    actual = await mcp.call_tool("ocr_get_report", {"job_id": job_id})
                    assert not actual.is_error and actual.structured_content == report.json()
                    text = await mcp.call_tool("ocr_get_text", {"job_id": job_id})
                    assert text.is_error and "result_not_ready" in text.content[0].text
                    principal[0] = FOREIGN
                    denied = await mcp.call_tool("ocr_get_report", {"job_id": job_id})
                    assert denied.is_error and "job_not_found" in denied.content[0].text
    asyncio.run(scenario())
    with pytest.raises(ApplicationError) as error:
        service.report(FOREIGN, job_id)
    assert error.value.status == 404
    system.jobs.cleanup(job_id)
    assert system.client.get(f"/api/v1/jobs/{job_id}/report", headers=headers).status_code == 410
    with pytest.raises(ApplicationError) as error:
        service.report(READER, job_id)
    assert error.value.status == 410


def test_unconfigured_validator_fails_closed_before_processing(system, probes, monkeypatch):
    receipt = upload(system)
    response = submit(system, receipt, {"languages": ["eng"]})
    assert response.status_code == 202
    monkeypatch.setattr(settings, "verapdf_executable", None)
    engine, fallback = engines()
    assert OcrOrchestrator(system.jobs, ocrmypdf_service=engine, fallback_ocr=fallback).process_job(
        response.json()["job_id"]) is None
    snapshot = system.jobs.get_snapshot(response.json()["job_id"])
    assert snapshot.status == "failed" and snapshot.error_code == "pdfa_validator_unavailable"
    assert snapshot.output_pdf_path is None


def test_searchable_pdf_is_not_validated_and_completes(system, probes):
    validator = stub("failed", ("non_compliant",))
    job_id, result = run_job(system, {"languages": ["eng"], "output_format": "pdf_searchable",
                                      "compression_profile": "preservation"}, validator)
    assert result is not None and validator.validate.call_count == 0
    report = JobApplication(system.jobs).report(READER, job_id)
    assert report.output_validation.pdfa_conformance == "not_validated"
    assert "pdfa_conformance_not_validated" in report.warnings
    published = system.jobs.get_snapshot(job_id).output_pdf_path.read_bytes()
    assert report.output.sha256 == hashlib.sha256(published).hexdigest()


def legacy_job(system):
    from test_results import pdf
    return system.jobs.create(filename="legacy.pdf", payload=pdf(["the original document has a name and a date"]),
                              owner_user_id=1)


def test_legacy_pdfa_job_is_validated(system, monkeypatch):
    monkeypatch.setattr(settings, "final_output_type", "pdfa")
    job = legacy_job(system)
    assert job.options_effective is None
    validator = stub("passed", ("compliant",))
    engine, fallback = engines()
    assert OcrOrchestrator(system.jobs, ocrmypdf_service=engine, fallback_ocr=fallback,
                           pdfa_validator=validator).process_job(job.job_id)
    assert validator.validate.call_count == 1
    report = JobApplication(system.jobs).report(READER, job.job_id)
    assert report.output_validation.requested_format == "pdfa"
    assert report.output_validation.pdfa_conformance == "passed"
    assert report.output.sha256 == report.output_validation.pdfa_validated_sha256


def test_legacy_pdfa_job_without_validator_fails_closed(system, monkeypatch):
    monkeypatch.setattr(settings, "final_output_type", "pdfa")
    monkeypatch.setattr(settings, "verapdf_executable", None)
    job = legacy_job(system)
    engine, fallback = engines()
    assert OcrOrchestrator(system.jobs, ocrmypdf_service=engine, fallback_ocr=fallback).process_job(job.job_id) is None
    snapshot = system.jobs.get_snapshot(job.job_id)
    assert snapshot.status == "failed" and snapshot.error_code == "pdfa_validation_indeterminate"
    assert snapshot.output_pdf_path is None


def test_legacy_unprovable_pdfa_part_fails_closed(system, monkeypatch):
    monkeypatch.setattr(settings, "final_output_type", "pdfa-3")
    job = legacy_job(system)
    validator = stub("passed", ("compliant",))
    engine, fallback = engines()
    assert OcrOrchestrator(system.jobs, ocrmypdf_service=engine, fallback_ocr=fallback,
                           pdfa_validator=validator).process_job(job.job_id) is None
    snapshot = system.jobs.get_snapshot(job.job_id)
    assert snapshot.status == "failed" and snapshot.error_code == "unsupported_legacy_output_type"
    assert validator.validate.call_count == 0


def test_legacy_explicit_searchable_pdf_is_not_validated(system, monkeypatch):
    monkeypatch.setattr(settings, "final_output_type", "pdf")
    job = legacy_job(system)
    validator = stub("failed", ("non_compliant",))
    engine, fallback = engines()
    assert OcrOrchestrator(system.jobs, ocrmypdf_service=engine, fallback_ocr=fallback,
                           pdfa_validator=validator).process_job(job.job_id)
    assert validator.validate.call_count == 0
    report = JobApplication(system.jobs).report(READER, job.job_id)
    assert report.output_validation.requested_format == "pdf_searchable"
    assert report.output_validation.pdfa_conformance == "not_validated"


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
    ready = service.snapshot(refresh=True)
    assert ready.engines["verapdf"].available and ready.output_formats == ["pdf_searchable", "pdfa"]
    monkeypatch.setattr(pdfa_validator, "_PLATFORM", "win32")
    capabilities = service.snapshot(refresh=True)
    assert capabilities.engines["verapdf"].reasons == ["unsupported_platform"]
    assert capabilities.output_formats == ["pdf_searchable"]
    assert "pdfa_validator_unavailable_pdfa_not_offered" in capabilities.warnings
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
