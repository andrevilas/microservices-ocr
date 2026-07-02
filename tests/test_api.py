import subprocess
import time
from io import BytesIO
from zipfile import ZipFile

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from app.config import settings
from app.main import app
from app.services.easyocr_service import FallbackOcrService

client = TestClient(app)


def _auth_cookies() -> dict:
    """Login as admin and return cookies for authenticated requests."""
    resp = client.post(
        "/auth/login",
        json={"email": settings.admin_email, "password": settings.admin_password},
    )
    return dict(resp.cookies)


def _fresh_client() -> TestClient:
    """Return module client with cookies cleared for unauthenticated tests."""
    client.cookies.clear()
    return client


def build_pdf_bytes(text: str) -> bytes:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(100, 750, text)
    pdf.save()
    return buffer.getvalue()


def wait_for_job(job_id: str, timeout: float = 30.0) -> dict:
    cookies = _auth_cookies()
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/jobs/{job_id}", cookies=cookies)
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"completed", "failed"}:
            return payload
        time.sleep(0.2)
    raise AssertionError(f"Job {job_id} did not finish within {timeout} seconds.")


# -------------------------------------------------
# Existing tests (preserved)
# -------------------------------------------------


def test_homepage_loads() -> None:
    cookies = _auth_cookies()
    response = client.get("/", cookies=cookies)
    assert response.status_code == 200
    assert "Fluxo OCR" in response.text
    assert "Selecionar diretório" in response.text
    assert "Triagem, fila e entrega PDF/A" in response.text
    assert "Painel de cards e resultados" in response.text
    assert "Busca rápida" in response.text
    assert "Ordenação" in response.text
    assert "Concluídos" in response.text
    assert "Selecionar visíveis" in response.text
    assert "Limpar fila" in response.text
    assert "Resetar workspace" in response.text
    assert "Remover concluídos" in response.text
    assert "Nenhum lote ativo." in response.text
    assert "Detalhes" in response.text
    assert "toast-stack" in response.text


def test_rejects_non_pdf_upload() -> None:
    cookies = _auth_cookies()
    response = client.post(
        "/api/jobs",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        cookies=cookies,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Only PDF uploads are supported."


def test_processes_pdf_and_allows_download() -> None:
    cookies = _auth_cookies()
    pdf_bytes = build_pdf_bytes("Documento OCR de teste")
    create_response = client.post(
        "/api/jobs",
        files={"file": ("sample.pdf", pdf_bytes, "application/pdf")},
        cookies=cookies,
    )

    assert create_response.status_code == 202
    job_id = create_response.json()["job_id"]

    payload = wait_for_job(job_id)
    assert payload["status"] == "completed"
    assert payload["download_url"] == f"/api/jobs/{job_id}/download"

    download_response = client.get(payload["download_url"], cookies=cookies)
    assert download_response.status_code == 200
    assert download_response.headers["content-type"] == "application/pdf"


def test_processes_batch_uploads() -> None:
    cookies = _auth_cookies()
    first_pdf = build_pdf_bytes("Primeiro documento")
    second_pdf = build_pdf_bytes("Segundo documento")

    response = client.post(
        "/api/jobs/batch",
        files=[
            ("files", ("first.pdf", first_pdf, "application/pdf")),
            ("files", ("second.pdf", second_pdf, "application/pdf")),
        ],
        cookies=cookies,
    )

    assert response.status_code == 202
    payload = response.json()
    assert len(payload["jobs"]) == 2
    assert payload["jobs"][0]["filename"] == "first.pdf"
    assert payload["jobs"][1]["filename"] == "second.pdf"


def test_downloads_completed_jobs_as_zip() -> None:
    cookies = _auth_cookies()
    first_pdf = build_pdf_bytes("Primeiro documento")
    second_pdf = build_pdf_bytes("Segundo documento")

    response = client.post(
        "/api/jobs/batch",
        files=[
            ("files", ("first.pdf", first_pdf, "application/pdf")),
            ("files", ("second.pdf", second_pdf, "application/pdf")),
        ],
        cookies=cookies,
    )

    payload = response.json()
    job_ids = [job["job_id"] for job in payload["jobs"]]
    for job_id in job_ids:
        final_payload = wait_for_job(job_id)
        assert final_payload["status"] == "completed"
    query = "&".join(f"job_ids={job_id}" for job_id in job_ids)
    download_response = client.get(f"/api/jobs/download-batch?{query}", cookies=cookies)

    assert download_response.status_code == 200
    assert download_response.headers["content-type"] == "application/zip"
    with ZipFile(BytesIO(download_response.content)) as archive:
        assert sorted(archive.namelist()) == ["first-searchable.pdf", "second-searchable.pdf"]


def test_clears_pending_jobs_from_queue() -> None:
    cookies = _auth_cookies()
    first_pdf = build_pdf_bytes("Primeiro documento")
    second_pdf = build_pdf_bytes("Segundo documento")
    third_pdf = build_pdf_bytes("Terceiro documento")

    response = client.post(
        "/api/jobs/batch",
        files=[
            ("files", ("first.pdf", first_pdf, "application/pdf")),
            ("files", ("second.pdf", second_pdf, "application/pdf")),
            ("files", ("third.pdf", third_pdf, "application/pdf")),
        ],
        cookies=cookies,
    )

    assert response.status_code == 202
    clear_response = client.post("/api/jobs/clear-queue", cookies=cookies)
    assert clear_response.status_code == 200
    payload = clear_response.json()
    assert payload["cleared_count"] >= 0
    assert payload["processing_count"] >= 0


def test_default_upload_limit_is_80_mb() -> None:
    assert settings.max_upload_size_mb == 80
    assert settings.job_worker_concurrency == 2


def test_default_final_output_settings() -> None:
    assert settings.final_output_type == "pdfa"
    assert settings.final_pdf_optimize_level == 3
    assert settings.final_pdfa_image_compression == "jpeg"


def test_fallback_is_optional_in_local_environment() -> None:
    assert isinstance(FallbackOcrService.is_available(), bool)


# -------------------------------------------------
# BLOCK 1 -- New tests
# -------------------------------------------------


# 1. Timeout configuravel para subprocess OCR
def test_ocr_subprocess_timeout_setting() -> None:
    assert settings.ocr_subprocess_timeout_seconds == 300


def test_ocr_subprocess_timeout_raises_runtime_error(monkeypatch) -> None:
    from app.services.ocrmypdf_service import OcrmypdfService

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="ocrmypdf", timeout=5)

    monkeypatch.setattr(subprocess, "run", fake_run)

    service = OcrmypdfService()
    import pytest

    with pytest.raises(RuntimeError, match="timed out"):
        service._run(["--skip-text", "input.pdf", "output.pdf"])


# 2. Upload oversized
def test_upload_oversized_returns_413(monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_upload_size_mb", 0)
    cookies = _auth_cookies()
    pdf_bytes = build_pdf_bytes("small doc")
    response = client.post(
        "/api/jobs",
        files={"file": ("doc.pdf", pdf_bytes, "application/pdf")},
        cookies=cookies,
    )
    assert response.status_code == 413
    assert "exceeds" in response.json()["detail"].lower()
    # Restore default
    monkeypatch.setattr(settings, "max_upload_size_mb", 80)


# 2b. Invalid magic bytes
def test_upload_invalid_magic_bytes_returns_400() -> None:
    cookies = _auth_cookies()
    fake_pdf = b"NOT-A-PDF-CONTENT-AT-ALL"
    response = client.post(
        "/api/jobs",
        files={"file": ("fake.pdf", fake_pdf, "application/pdf")},
        cookies=cookies,
    )
    assert response.status_code == 400
    assert "magic bytes" in response.json()["detail"].lower()


# 2c. Corrupted PDF (valid magic bytes but invalid structure)
def test_upload_corrupted_pdf_returns_400() -> None:
    cookies = _auth_cookies()
    corrupted = b"%PDF-1.4 corrupted garbage data that is not a real PDF"
    response = client.post(
        "/api/jobs",
        files={"file": ("corrupted.pdf", corrupted, "application/pdf")},
        cookies=cookies,
    )
    assert response.status_code == 400
    assert "parsing failed" in response.json()["detail"].lower()


# 3. Batch max limit
def test_batch_max_files_limit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_batch_files", 2)
    cookies = _auth_cookies()
    pdf_bytes = build_pdf_bytes("doc")
    response = client.post(
        "/api/jobs/batch",
        files=[
            ("files", ("a.pdf", pdf_bytes, "application/pdf")),
            ("files", ("b.pdf", pdf_bytes, "application/pdf")),
            ("files", ("c.pdf", pdf_bytes, "application/pdf")),
        ],
        cookies=cookies,
    )
    assert response.status_code == 400
    assert "limited to" in response.json()["detail"].lower()
    monkeypatch.setattr(settings, "max_batch_files", 25)


def test_batch_max_files_default_setting() -> None:
    assert settings.max_batch_files == 25


# 4. API key authentication (now works alongside JWT via _require_auth_or_api_key)
def test_api_key_rejects_unauthorized(monkeypatch) -> None:
    c = _fresh_client()
    monkeypatch.setattr(settings, "api_key", "test-secret-key-123")
    pdf_bytes = build_pdf_bytes("doc")
    # No header and no JWT
    response = c.post(
        "/api/jobs",
        files={"file": ("doc.pdf", pdf_bytes, "application/pdf")},
    )
    assert response.status_code == 401
    assert "api key" in response.json()["detail"].lower()

    # Wrong header and no JWT
    response = c.post(
        "/api/jobs",
        files={"file": ("doc.pdf", pdf_bytes, "application/pdf")},
        headers={"X-API-Key": "wrong-key"},
    )
    assert response.status_code == 401
    monkeypatch.setattr(settings, "api_key", None)


def test_api_key_accepts_valid_key(monkeypatch) -> None:
    c = _fresh_client()
    monkeypatch.setattr(settings, "api_key", "test-secret-key-123")
    pdf_bytes = build_pdf_bytes("doc")
    response = c.post(
        "/api/jobs",
        files={"file": ("doc.pdf", pdf_bytes, "application/pdf")},
        headers={"X-API-Key": "test-secret-key-123"},
    )
    assert response.status_code == 202
    monkeypatch.setattr(settings, "api_key", None)


def test_api_key_home_requires_jwt_not_api_key(monkeypatch) -> None:
    c = _fresh_client()
    monkeypatch.setattr(settings, "api_key", "test-secret-key-123")
    # Home now requires JWT cookie, not API key
    response = c.get("/", follow_redirects=False)
    assert response.status_code == 302  # Redirects to /login without JWT
    # With JWT cookie, home should work even with api_key configured
    cookies = _auth_cookies()
    response = c.get("/", cookies=cookies)
    assert response.status_code == 200
    monkeypatch.setattr(settings, "api_key", None)


def test_api_key_protects_status_endpoint(monkeypatch) -> None:
    c = _fresh_client()
    monkeypatch.setattr(settings, "api_key", "test-secret-key-123")
    response = c.get("/api/jobs/nonexistent")
    assert response.status_code == 401
    monkeypatch.setattr(settings, "api_key", None)


def test_api_key_protects_clear_queue(monkeypatch) -> None:
    c = _fresh_client()
    monkeypatch.setattr(settings, "api_key", "test-secret-key-123")
    response = c.post("/api/jobs/clear-queue")
    assert response.status_code == 401
    monkeypatch.setattr(settings, "api_key", None)


# 5. Rate limit
def test_rate_limit_returns_429(monkeypatch) -> None:
    monkeypatch.setattr(settings, "upload_rate_limit_per_minute", 1)
    # Clear any existing rate buckets
    from app.routes.api import _rate_buckets
    _rate_buckets.clear()

    cookies = _auth_cookies()
    pdf_bytes = build_pdf_bytes("doc")
    # First request should pass
    response1 = client.post(
        "/api/jobs",
        files={"file": ("doc1.pdf", pdf_bytes, "application/pdf")},
        cookies=cookies,
    )
    assert response1.status_code == 202

    # Second request within same minute should fail
    response2 = client.post(
        "/api/jobs",
        files={"file": ("doc2.pdf", pdf_bytes, "application/pdf")},
        cookies=cookies,
    )
    assert response2.status_code == 429
    assert "rate limit" in response2.json()["detail"].lower()

    monkeypatch.setattr(settings, "upload_rate_limit_per_minute", 0)
    _rate_buckets.clear()


def test_rate_limit_disabled_by_default() -> None:
    assert settings.upload_rate_limit_per_minute == 0


# 6. Health and readiness
def test_health_endpoint() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


def test_readiness_endpoint() -> None:
    response = client.get("/readiness")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"


def test_readiness_returns_503_when_tmp_unavailable(monkeypatch) -> None:
    from pathlib import Path
    monkeypatch.setattr(settings, "ocr_tmp_dir", Path("/nonexistent/impossible/path/xyz"))
    response = client.get("/readiness")
    assert response.status_code == 503
    assert "not available" in response.json()["detail"].lower()
    monkeypatch.setattr(settings, "ocr_tmp_dir", Path("/tmp/ocr-recognizer"))
