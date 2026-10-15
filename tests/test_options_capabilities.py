"""Options admission/execution and real dependency discovery with synthetic probes."""
import hashlib
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.application import ApplicationError, JobApplication, Principal
from app.config import settings
from app.options import ProcessingOptions
from app.services import capability_service as module
from app.services.capability_service import CapabilityService
from app.services.ocr_orchestrator import OcrOrchestrator
from app.services.ocrmypdf_service import OcrmypdfService
from app.services.storage_service import JobStore
from app.services.upload_service import UploadService
from test_upload_admission import system, upload, bearer
from test_http_contract import validate_response


@pytest.fixture
def probes(monkeypatch):
    outputs = {"tesseract": "tesseract 5.3.4", "langs": "List of available languages in PRIVATE-PATH:\neng\npor\nosd\n",
               "gs": "10.03.1", "pdftoppm": "pdftoppm version 24.02.0", "pdfinfo": "pdfinfo version 24.02.0",
               "ocrmypdf": "16.10.4", "fallback": '{"ready":true}'}
    calls = []
    monkeypatch.setattr(module.shutil, "which", lambda name: name if name in outputs else None)
    monkeypatch.setattr(module, "installed", lambda _: True)
    def output(args, timeout=5):
        calls.append((list(args), timeout))
        if args[0] == sys.executable:
            return outputs["fallback" if "app.capability_probe" in args else "ocrmypdf"]
        return outputs["langs" if "--list-langs" in args else args[0]]
    monkeypatch.setattr(module, "command_output", output)
    service = CapabilityService()
    monkeypatch.setattr(module, "get_capability_service", lambda: service)
    return outputs, calls, service


def submit(system, receipt, options=None, key="options-key-0001"):
    body = {"upload_id": receipt["upload_id"]}
    if options is not None:
        body["options"] = options
    return system.client.post("/api/v1/jobs", json=body, headers={**bearer(system.tokens[0]), "Idempotency-Key": key})


def test_catalog_auth_and_real_http_schema(system, probes):
    _, calls, service = probes
    response = system.client.get("/api/v1/capabilities")
    assert response.status_code == 401 and calls == []
    response = system.client.get("/api/v1/capabilities", headers=bearer(system.tokens[0]))
    assert response.status_code == 200
    data = validate_response(system.app, response, "/api/v1/capabilities")
    assert data["languages"] == ["eng", "por"]
    assert data["fallback_policies"] == ["disabled", "if_low_quality"]
    assert data["limits"]["max_pages"] == min(settings.upload_max_pages, 300)
    assert data["limits"]["engine_jobs_per_job"] == settings.ocr_engine_jobs
    assert "PRIVATE" not in response.text
    assert data == service.snapshot().model_dump(mode="json")


@pytest.mark.parametrize("dependency,value", [("ocrmypdf", None), ("ocrmypdf", "17.0.0"), ("ocrmypdf", "16.3.0"),
                                              ("gs", None), ("gs", "9.53.0"), ("gs", "10.02.0"),
                                              ("tesseract", None), ("tesseract", "4.0.0"), ("tesseract", "5.4.0"),
                                              ("langs", None), ("langs", "osd\nfra")])
def test_missing_broken_or_unsupported_engine_rejected(system, probes, dependency, value):
    outputs, _, service = probes
    outputs[dependency] = value
    capabilities = service.snapshot(refresh=True)
    assert not capabilities.engines["ocrmypdf"].available
    assert capabilities.processing_modes == ["extract_text"]
    receipt = upload(system)
    response = submit(system, receipt)
    assert response.status_code == 503 and response.json()["error"]["code"] == "ocr_unavailable"
    assert system.jobs.snapshot() == [] and system.queue.enqueued == []
    assert system.jobs._conn.execute("SELECT COUNT(*) FROM job_admissions").fetchone()[0] == 0


def test_language_probe_and_supported_profiles(system, probes):
    outputs, _, service = probes
    outputs["langs"] = "eng\nosd\nfra"
    receipt = upload(system)
    response = submit(system, receipt, {"languages": ["por"]})
    assert response.status_code == 422 and response.json()["error"]["code"] == "language_unavailable"
    assert service.snapshot().languages == ["eng"]
    assert submit(system, receipt, {"languages": ["eng"]}).status_code == 202


@pytest.mark.parametrize("options", [
    {"languages": ["--config=PRIVATE"]}, {"languages": ["eng", "eng"]}, {"languages": []},
    {"processing_mode": "force_ocr"}, {"output_format": "docx"}, {"compression_profile": "compact"},
    {"fallback_policy": "always"}, {"command": "PRIVATE"}, {"engine_jobs": 99},
    {"processing_mode": "extract_text", "languages": ["eng"]},
    {"processing_mode": "extract_text", "output_format": "pdfa"},
    {"processing_mode": "extract_text", "compression_profile": "balanced"},
    {"fallback_policy": "if_low_quality", "languages": ["eng"]},
])
def test_rejects_unsupported_options_before_admission(system, probes, options):
    receipt = upload(system)
    response = submit(system, receipt, options)
    assert response.status_code == 422, response.text
    assert "PRIVATE" not in response.text
    validate_response(system.app, response, "/api/v1/jobs", "post")
    assert not system.jobs.snapshot()


def test_fallback_requires_local_models(system, probes):
    outputs, _, service = probes
    outputs["fallback"] = '{"ready":false}'
    receipt = upload(system)
    response = submit(system, receipt, {"languages": ["eng", "por"], "fallback_policy": "if_low_quality"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "fallback_unavailable"
    assert service.snapshot().fallback_policies == ["disabled"]


def test_options_survive_reopen_and_replay_even_if_engines_disappear(system, probes, monkeypatch):
    outputs, _, service = probes
    receipt = upload(system)
    options = {"languages": ["por", "eng"], "output_format": "pdf_searchable", "compression_profile": "none"}
    first = submit(system, receipt, options)
    assert first.status_code == 202
    job = system.jobs.get_snapshot(first.json()["job_id"])
    assert job.options_requested.languages == ("eng", "por")
    assert job.options_effective.fallback_policy == "disabled"
    assert job.options_effective.engine_jobs == 1
    with pytest.raises(ValidationError):
        job.options_effective.engine_jobs = 2
    with pytest.raises(ValueError, match="immutable"):
        system.jobs.update(job.job_id, options_effective=None)
    outputs["ocrmypdf"] = None
    service.snapshot(refresh=True)
    monkeypatch.setattr(settings, "ocr_engine_jobs", 4)
    second = submit(system, receipt, {**options, "languages": ["eng", "por"]})
    assert second.json() == first.json() and second.headers["idempotency-replayed"] == "true"
    changed = submit(system, receipt, {**options, "compression_profile": "balanced"})
    assert changed.status_code == 409
    other = JobStore(root_dir=system.jobs.root_dir)
    try:
        reloaded = other.get_snapshot(job.job_id)
        assert reloaded.options_requested == job.options_requested
        assert reloaded.options_effective == job.options_effective
        response = system.client.get(f"/api/v1/jobs/{job.job_id}", headers=bearer(system.tokens[0]))
        assert response.json()["options_effective"]["engine_jobs"] == 1
    finally:
        other._conn.close()


def test_default_and_explicit_default_are_same_request(system, probes):
    receipt = upload(system)
    first = submit(system, receipt)
    second = submit(system, receipt, ProcessingOptions().resolved().model_dump(mode="json"))
    assert first.status_code == second.status_code == 202 and first.json() == second.json()
    assert second.headers["idempotency-replayed"] == "true"


def test_pre_options_receipt_still_replays_without_probing(system, probes):
    receipt = upload(system)
    first = submit(system, receipt)
    conn = system.jobs._conn
    legacy_hash = hashlib.sha256(json.dumps({"upload_id": receipt["upload_id"]}, sort_keys=True).encode()).hexdigest()
    conn.execute("UPDATE job_admissions SET request_hash=?", (legacy_hash,))
    conn.commit()
    probes[0]["ocrmypdf"] = None
    probes[2].snapshot(refresh=True)
    assert submit(system, receipt).json() == first.json()
    assert submit(system, receipt, {"processing_mode": "extract_text"}).status_code == 409


def test_explicit_extraction_runs_without_ocr_and_records_options(system, probes, monkeypatch):
    probes[0]["ocrmypdf"] = None
    receipt = upload(system)
    response = submit(system, receipt, {"processing_mode": "extract_text"})
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    monkeypatch.setattr(OcrmypdfService, "run_ocr", Mock(side_effect=AssertionError("Must not run OCR")))
    monkeypatch.setattr(OcrmypdfService, "optimize_pdfa", Mock(side_effect=AssertionError("Must not run OCR")))
    assert OcrOrchestrator(system.jobs).process_job(job_id)
    report = JobApplication(system.jobs).report(Principal("user:1", 1, frozenset({"jobs:read", "artifacts:read"})), job_id)
    assert report.options_effective.processing_mode == "extract_text"
    assert report.options_effective.languages == ()
    assert report.input.sha256 == report.output.sha256
    assert report.pages[0].source == "pdf_text"
    assert "text_extraction_only" in report.warnings


def test_worker_revalidates_and_fails_when_engine_disappears(system, probes):
    receipt = upload(system)
    response = submit(system, receipt)
    assert response.status_code == 202
    probes[0]["tesseract"] = None
    job_id = response.json()["job_id"]
    assert OcrOrchestrator(system.jobs).process_job(job_id) is None
    job = system.jobs.get_snapshot(job_id)
    assert job.status == "failed" and job.error_code == "ocr_unavailable"
    assert job.output_pdf_path is None
    assert system.client.get(f"/api/v1/jobs/{job_id}", headers=bearer(system.tokens[0])).json()["error_code"] == "ocr_unavailable"


def test_concurrent_commands_use_immutable_options_without_global_mutation(probes, monkeypatch, tmp_path):
    _, _, service = probes
    first = service.resolve(ProcessingOptions(languages=("eng",), output_format="pdf_searchable", compression_profile="none"))
    second = service.resolve(ProcessingOptions(languages=("por",), output_format="pdfa", compression_profile="balanced"))
    watched = ("ocr_engine_jobs", "ocr_subprocess_timeout_seconds", "final_output_type", "final_pdf_optimize_level",
               "quality_min_text", "quality_valid_ratio_threshold", "fallback_character_tolerance")
    before = {key: getattr(settings, key) for key in watched}
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr("app.services.ocrmypdf_service.subprocess.run", run)
    engine = OcrmypdfService()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(engine.optimize_pdfa, tmp_path/f"input-{i}.pdf", tmp_path/f"out-{i}.pdf", options)
                   for i, options in enumerate((first, second))]
        for future in futures:
            future.result()
    assert {key: getattr(settings, key) for key in watched} == before
    for command, kwargs in calls:
        lang = command[command.index("--language") + 1]
        assert command[command.index("--output-type") + 1] == ("pdf" if lang == "eng" else "pdfa")
        assert command[command.index("--optimize") + 1] == ("0" if lang == "eng" else "1")
        assert "--skip-text" in command and "--force-ocr" not in command
        assert command[command.index("--jobs") + 1] == "1"
        assert kwargs["timeout"] == first.subprocess_timeout_seconds
        assert not kwargs.get("shell")
    engine.run_ocr(tmp_path/"in.pdf", tmp_path/"out.pdf", first)
    assert "--language" in calls[-1][0]


def test_probe_single_flight_cache_and_copy_isolation(probes):
    _, calls, service = probes
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: service.snapshot(), range(16)))
    assert len(calls) == 7
    values[0].languages.clear()
    assert service.snapshot().languages == ["eng", "por"]
    service.snapshot(refresh=True)
    assert len(calls) == 14


@pytest.mark.parametrize("failure", [OSError("PRIVATE-PATH"), subprocess.TimeoutExpired("PRIVATE", 5)])
def test_probe_errors_do_not_escape(monkeypatch, failure):
    monkeypatch.setattr(module.subprocess, "run", Mock(side_effect=failure))
    assert module.command_output(["synthetic-command"]) is None


def test_fallback_probe_disables_downloads(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from app.capability_probe import main
    reader = Mock()
    for name in ("easyocr", "pdf2image", "cv2"):
        monkeypatch.setitem(sys.modules, name, SimpleNamespace(Reader=reader))
    main()
    assert json.loads(capsys.readouterr().out) == {"ready": True}
    reader.assert_called_once_with(["pt", "en"], gpu=False, download_enabled=False)


@pytest.mark.parametrize("policy", ["disabled", "if_low_quality"])
def test_orchestrator_passes_options_and_honors_fallback(system, probes, policy):
    from test_results import pdf
    receipt = upload(system, data=pdf(["short text"]))
    response = submit(system, receipt, {"languages": ["eng", "por"], "fallback_policy": policy})
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    effective = system.jobs.get_snapshot(job_id).options_effective
    calls = []
    engine, fallback = Mock(), Mock()
    engine.is_available.return_value = True
    def copy(source, target, *, options):
        calls.append(options)
        shutil.copyfile(source, target)
    engine.run_ocr.side_effect = copy
    engine.optimize_pdfa.side_effect = copy
    fallback.is_available.return_value = True
    fallback.process_pages.return_value = ["the original document has a name and a date for the page"]
    assert OcrOrchestrator(system.jobs, ocrmypdf_service=engine, fallback_ocr=fallback).process_job(job_id)
    assert calls == [effective, effective]
    assert fallback.process_pages.call_count == (1 if policy == "if_low_quality" else 0)
    report = JobApplication(system.jobs).report(Principal("user:1", 1, frozenset({"jobs:read", "artifacts:read"})), job_id)
    assert report.options_effective == effective
    assert report.options_requested.fallback_policy == policy
    assert report.engines["tesseract"] == "5.3.4"
    assert report.engines["ghostscript"] == "10.03.1"
    assert report.used_fallback == (policy == "if_low_quality")


def test_actual_unavailable_probe_is_safe_and_explicit(tmp_path):
    # Independent interpreter prevents synthetic probes from masking this environment.
    import os
    code = "from app.services.capability_service import CapabilityService; print(CapabilityService().snapshot().model_dump_json())"
    result = subprocess.run([sys.executable, "-c", code], env={**os.environ, "OCR_TMP_DIR": str(tmp_path)},
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "extract_text" in data["processing_modes"]
    assert data["engines"]["ocrmypdf"]["available"] == ("preserve_existing_text" in data["processing_modes"])
    assert str(tmp_path) not in result.stdout
