"""Synthetic PDFs: page fidelity, durable results, bounded reads and HTTP/MCP parity."""
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import Mock

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pypdf import PdfReader
from reportlab.pdfgen import canvas
from starlette.applications import Starlette
from starlette.routing import Mount

from app.application import ApplicationError, JobApplication, Principal
from app.main import create_app
from app.mcp_server import build_mcp_server
from app.results import MAX_PAGE_CHARS, page_path, read_report
from app.security import require_principal
from app.config import settings
from app.services.ocr_orchestrator import OcrOrchestrator, legacy_output_format
from app.services.storage_service import JobStore
from app.services.tesseract_service import PrimaryOcrService
from test_http_contract import validate_response

OWNER = Principal("user:1", 1, frozenset({"jobs:read", "artifacts:read"}))


def pdf(pages):
    buffer = BytesIO()
    doc = canvas.Canvas(buffer)
    for text in pages:
        if text:
            doc.drawString(20, 700, text)
        doc.showPage()
    doc.save()
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def searchable_output(monkeypatch):
    # Jobs here carry no recorded options, so the format comes from the legacy setting:
    # "pdf" -> "pdf_searchable". These tests cover text/results, not PDF/A conformance.
    monkeypatch.setattr(settings, "final_output_type", "pdf")
    assert legacy_output_format(settings.final_output_type) == "pdf_searchable"


def orchestrator(store, fallback=None):
    engine = Mock()
    engine.is_available.return_value = False
    fallback = fallback or Mock()
    if fallback.is_available.return_value is not True:
        fallback.is_available.return_value = False
    return OcrOrchestrator(store, primary_ocr=PrimaryOcrService(engine),
                           fallback_ocr=fallback, ocrmypdf_service=engine)


@pytest.fixture
def results(tmp_path):
    store = JobStore(root_dir=tmp_path)
    job = store.create("Páginas.pdf", pdf(["primeira pagina com texto", "", "terceira pagina final"]), owner_user_id=1)
    assert orchestrator(store).process_job(job.job_id) is not None
    service = JobApplication(store)
    app = create_app(lambda: service)
    app.dependency_overrides[require_principal] = lambda: OWNER
    client = TestClient(app)
    yield store, job, service, app, client
    client.close()
    store._conn.close()


def test_pages_blank_provenance_hashes_and_reopen(results):
    store, job, service, app, client = results
    report = service.report(OWNER, job.job_id)
    assert report.page_count == 3
    assert [p.character_count for p in report.pages] == [25, 0, 21]
    assert {p.source for p in report.pages} == {"pdf_text"}
    assert report.input.sha256 == hashlib.sha256(job.input_pdf_path.read_bytes()).hexdigest()
    assert report.output.sha256 == hashlib.sha256(job.output_pdf_path.read_bytes()).hexdigest()
    assert not report.quality.is_accuracy_measure
    assert report.output_validation.pdfa_conformance == "not_validated"
    assert report.engines["pypdf"]
    assert "primary_ocr_unavailable" in report.warnings
    assert [s.name for s in report.stages] == ["primary", "build", "finalize"]
    other = JobStore(root_dir=store.root_dir)
    try:
        assert JobApplication(other).report(OWNER, job.job_id) == report
        assert JobApplication(other).text(OWNER, job.job_id, page=3).text == "terceira pagina final"
    finally:
        other._conn.close()
    for suffix in ("text", "report"):
        response = client.get(f"/api/v1/jobs/{job.job_id}/{suffix}")
        assert response.status_code == 200
        validate_response(app, response, "/api/v1/jobs/{job_id}/" + suffix)
        assert str(store.root_dir) not in response.text


def test_chunk_walk_keeps_empty_pages_and_unicode_offsets(results):
    _, job, service, _, _ = results
    chunks, page, offset = [], 1, 0
    while page is not None:
        result = service.text(OWNER, job.job_id, page, offset, 5)
        assert len(result.text) <= 5
        chunks.append((page, result.text))
        page, offset = result.next_page, result.next_offset
    assert (2, "") in chunks
    assert "".join(text for page, text in chunks if page == 1) == "primeira pagina com texto"
    assert "".join(text for page, text in chunks if page == 3) == "terceira pagina final"


@pytest.mark.parametrize("params", [{"page": 0}, {"page": 4}, {"offset": -1}, {"offset": 999},
                                    {"max_chars": 0}, {"max_chars": 20001}])
def test_text_range_errors_match_schema(results, params):
    _, job, _, app, client = results
    response = client.get(f"/api/v1/jobs/{job.job_id}/text", params=params)
    assert response.status_code == 422
    validate_response(app, response, "/api/v1/jobs/{job_id}/text")


@pytest.mark.parametrize("kind,status", [("other", 404), ("admin", 404), ("no-artifacts", 403), ("no-jobs", 403)])
def test_result_access_controls(results, kind, status):
    _, job, service, app, client = results
    principal = Principal("user:2", 2, OWNER.scopes, is_admin=kind == "admin")
    if kind.startswith("no-"):
        principal = Principal("user:1", 1, frozenset({"jobs:read"} if kind == "no-artifacts" else {"artifacts:read"}))
    app.dependency_overrides[require_principal] = lambda: principal
    for suffix in ("text", "report"):
        response = client.get(f"/api/v1/jobs/{job.job_id}/{suffix}")
        assert response.status_code == status
        validate_response(app, response, "/api/v1/jobs/{job_id}/" + suffix)


@pytest.mark.parametrize("state", ["queued", "processing", "failed", "canceled", "historical", "expired"])
def test_nonexistent_results_are_not_fabricated(results, state):
    store, job, _, app, client = results
    if state == "expired":
        store.cleanup(job.job_id)
    else:
        job = store.create("historical.pdf", pdf([""]), owner_user_id=1)
        store.update(job.job_id, status="completed" if state == "historical" else state)
    for suffix in ("text", "report"):
        response = client.get(f"/api/v1/jobs/{job.job_id}/{suffix}")
        assert response.status_code == (410 if state == "expired" else 409)
        validate_response(app, response, "/api/v1/jobs/{job_id}/" + suffix)


@pytest.mark.parametrize("kind", ["text", "manifest", "oversized", "wrong-job"])
def test_corrupt_results_sanitized(results, kind):
    _, job, _, app, client = results
    path = job.working_dir / "results" / "report.json"
    if kind == "text":
        page_path(job.working_dir, 1).write_bytes(b"PRIVATE-SENTINEL")
    elif kind == "oversized":
        path.write_bytes(b"x" * 1_048_577)
    elif kind == "manifest":
        path.write_bytes(b"PRIVATE-SENTINEL")
    else:
        data = json.loads(path.read_text())
        data["job_id"] = "wrong"
        path.write_text(json.dumps(data))
    response = client.get(f"/api/v1/jobs/{job.job_id}/text")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert "PRIVATE-SENTINEL" not in response.text
    validate_response(app, response, "/api/v1/jobs/{job_id}/text")


def test_fallback_page_assignment_unicode_and_overlay(results):
    store, _, service, _, _ = results
    job = store.create("scan.pdf", pdf(["", "", ""]), owner_user_id=1)
    fallback = Mock()
    fallback.is_available.return_value = True
    fallback.process_pages.return_value = ["pagina um ação 😀", "", "pagina tres final"]
    assert orchestrator(store, fallback).process_job(job.job_id)
    report = service.report(OWNER, job.job_id)
    assert report.used_fallback and report.fallback_attempted
    assert all(p.source == "easyocr" for p in report.pages)
    assert service.text(OWNER, job.job_id, max_chars=15).text == "pagina um ação "
    assert service.text(OWNER, job.job_id, offset=15).text == "😀"
    assert service.text(OWNER, job.job_id, page=2).text == ""
    output = PdfReader(str(job.output_pdf_path))
    assert "pagina um" in output.pages[0].extract_text()
    assert not output.pages[1].extract_text()
    assert "pagina tres" in output.pages[2].extract_text()
    assert "pagina tres" not in output.pages[0].extract_text()


def test_rejected_fallback_preserves_primary_pages(results):
    store, job, service, _, _ = results
    job = store.create("short.pdf", pdf(["texto original com letras", "", "texto tres"]), owner_user_id=1)
    fallback = Mock()
    fallback.is_available.return_value = True
    fallback.process_pages.return_value = ["", "", "?"]
    assert orchestrator(store, fallback).process_job(job.job_id)
    report = service.report(OWNER, job.job_id)
    assert report.fallback_attempted and not report.used_fallback
    assert service.text(OWNER, job.job_id, page=3).text == "texto tres"


@pytest.mark.parametrize("failure", ["write", "too-much-text", "page-count", "extraction"])
def test_failure_does_not_publish_partial_results(results, monkeypatch, failure):
    store, _, service, _, _ = results
    job = store.create("failure.pdf", pdf([""]), owner_user_id=1)
    fallback = Mock()
    fallback.is_available.return_value = True
    fallback.process_pages.return_value = ["normal text"]
    if failure == "write":
        monkeypatch.setattr("app.services.ocr_orchestrator.persist_report", Mock(side_effect=OSError("synthetic disk full")))
    elif failure == "too-much-text":
        fallback.process_pages.return_value = ["a" * (MAX_PAGE_CHARS + 1)]
    elif failure == "page-count":
        fallback.process_pages.return_value = ["one", "two"]
    else:
        fallback.process_pages.side_effect = RuntimeError("synthetic extraction failure")
    assert orchestrator(store, fallback).process_job(job.job_id) is None
    assert store.get_snapshot(job.job_id).status == "failed"
    # Failed jobs may expose a retained diagnostic report; none was recorded here.
    with pytest.raises(ApplicationError) as error:
        service.report(OWNER, job.job_id)
    assert (error.value.status, error.value.code) == (409, "result_unavailable")
    # Text and artifact are never published for a failed job.
    with pytest.raises(ApplicationError) as error:
        service.text(OWNER, job.job_id)
    assert (error.value.status, error.value.code) == (409, "result_not_ready")
    assert service.artifact(OWNER, job.job_id).available is False
    with pytest.raises(ApplicationError) as error:
        service.artifact_file(OWNER, job.job_id)
    assert error.value.code == "artifact_unavailable"


def test_filters_ties_cursor_insert_and_filter_binding(results):
    store, _, service, app, client = results
    instant = datetime(2026, 10, 15, tzinfo=timezone.utc)
    jobs = [store.create(f"Relatório {i}.pdf", pdf([""]), owner_user_id=1) for i in range(4)]
    for job in jobs:
        # Test fixture assigns identical immutable creation timestamps before persisting.
        job.created_at = instant
        store.update(job.job_id, status="completed", quality="LOW")
    expected = sorted(j.job_id for j in jobs)
    filters = dict(filename="RELATÓRIO", quality="LOW", status="completed", created_from=instant, created_to=instant)
    first = service.list_jobs(OWNER, limit=2, **filters)
    assert [j.job_id for j in first.items] == expected[:2]
    newcomer = store.create("Relatório novo.pdf", pdf([""]), owner_user_id=1)
    store.update(newcomer.job_id, status="completed", quality="LOW")
    second = service.list_jobs(OWNER, limit=2, cursor=first.next_cursor, **filters)
    assert [j.job_id for j in second.items] == expected[2:]
    assert second.next_cursor is None
    # Newly inserted job outside the explicit period cannot displace this page.
    assert newcomer.job_id not in [j.job_id for j in second.items]
    with pytest.raises(ApplicationError, match="Invalid pagination cursor"):
        service.list_jobs(OWNER, cursor=first.next_cursor)
    response = client.get("/api/v1/jobs", params={"filename": "relatório", "quality": "LOW",
                                                 "created_from": instant.isoformat(), "created_to": instant.isoformat()})
    assert len(validate_response(app, response, "/api/v1/jobs")["items"]) == 4
    for params in ({"created_from": "2026-10-15"}, {"created_from": "2026-10-16T00:00:00Z", "created_to": "2026-10-15T00:00:00Z"}, {"filename": ""}):
        assert client.get("/api/v1/jobs", params=params).status_code == 422


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_real_mcp_result_parity_and_access(results, monkeypatch, mode):
    _, job, service, app, client = results
    principal = [OWNER]
    monkeypatch.setattr("app.mcp_server.authenticate_bearer", lambda _: principal[0])
    server, transport = build_mcp_server(lambda: service)
    asgi = Starlette(routes=[Mount("/", app=transport)])
    async def scenario():
        async with server.session_manager.run():
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=asgi)) as http:
                async with Client(streamable_http_client("http://localhost/mcp", http_client=http), mode=mode) as mcp:
                    for suffix in ("text", "report"):
                        actual = await mcp.call_tool("ocr_get_" + suffix, {"job_id": job.job_id})
                        assert not actual.is_error
                        assert actual.structured_content == client.get(f"/api/v1/jobs/{job.job_id}/{suffix}").json()
                    bad = await mcp.call_tool("ocr_get_text", {"job_id": job.job_id, "max_chars": 20001})
                    assert bad.is_error and "invalid_text_range" in bad.content[0].text
                    principal[0] = Principal("user:2", 2, OWNER.scopes)
                    for suffix in ("text", "report"):
                        denied = await mcp.call_tool("ocr_get_" + suffix, {"job_id": job.job_id})
                        assert denied.is_error and "job_not_found" in denied.content[0].text
    asyncio.run(scenario())


def test_storage_failure_sanitized_in_shared_service(results, monkeypatch):
    _, job, service, _, _ = results
    monkeypatch.setattr("app.application.read_report", Mock(side_effect=PermissionError("PRIVATE-PATH")))
    with pytest.raises(ApplicationError) as error:
        service.report(OWNER, job.job_id)
    assert error.value.code == "storage_unavailable"
    assert "PRIVATE" not in str(error.value)


def test_fallback_service_keeps_raster_page_boundaries(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from app.services.easyocr_service import FallbackOcrService
    images = [Mock(), Mock(), Mock()]
    monkeypatch.setitem(sys.modules, "pdf2image", SimpleNamespace(convert_from_path=lambda _: images))
    monkeypatch.setattr("app.services.easyocr_service.preprocess_image", lambda path: path)
    reader = Mock()
    reader.readtext.side_effect = [[([], "first", .9), ([], "line", .9)], [], [([], "third", .8)]]
    fallback = FallbackOcrService()
    monkeypatch.setattr(fallback, "is_available", lambda: True)
    monkeypatch.setattr(fallback, "_get_reader", lambda: reader)
    assert fallback.process_pages(tmp_path / "synthetic.pdf") == ["first\nline", "", "third"]
    assert reader.readtext.call_count == 3


def test_ocrmypdf_provenance_records_execution_not_accuracy(results):
    import shutil
    store, _, service, _, _ = results
    job = store.create("digital.pdf", pdf(["the document has text for each page and this is the original text"]), owner_user_id=1)
    engine, fallback = Mock(), Mock()
    engine.is_available.return_value = True
    engine.run_ocr.side_effect = shutil.copyfile
    engine.optimize_pdfa.side_effect = shutil.copyfile
    fallback.is_available.return_value = False
    assert OcrOrchestrator(store, ocrmypdf_service=engine, fallback_ocr=fallback).process_job(job.job_id)
    report = service.report(OWNER, job.job_id)
    assert report.pages[0].source == "ocrmypdf"
    assert "ocrmypdf" in report.engines and report.engines["tesseract"] is None
    assert report.output_validation.pdfa_conformance == "not_validated"
    assert report.quality.label == "HIGH" and not report.quality.is_accuracy_measure
