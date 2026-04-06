import time
from io import BytesIO
from zipfile import ZipFile

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from app.main import app
from app.config import settings
from app.services.easyocr_service import FallbackOcrService


client = TestClient(app)


def build_pdf_bytes(text: str) -> bytes:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(100, 750, text)
    pdf.save()
    return buffer.getvalue()


def wait_for_job(job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"completed", "failed"}:
            return payload
        time.sleep(0.2)
    raise AssertionError(f"Job {job_id} did not finish within {timeout} seconds.")


def test_homepage_loads() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Fluxo OCR" in response.text
    assert "Selecionar diretorio" in response.text
    assert "Triagem, fila e entrega PDF/A" in response.text
    assert "Painel de cards e resultados" in response.text
    assert "Busca rapida" in response.text
    assert "Ordenacao" in response.text
    assert "Concluidos" in response.text
    assert "Selecionar visiveis" in response.text
    assert "Limpar fila" in response.text
    assert "Resetar workspace" in response.text
    assert "Remover concluidos" in response.text
    assert "Nenhum lote ativo." in response.text
    assert "Detalhes" in response.text
    assert "toast-stack" in response.text


def test_rejects_non_pdf_upload() -> None:
    response = client.post(
        "/api/jobs",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Only PDF uploads are supported."


def test_processes_pdf_and_allows_download() -> None:
    pdf_bytes = build_pdf_bytes("Documento OCR de teste")
    create_response = client.post(
        "/api/jobs",
        files={"file": ("sample.pdf", pdf_bytes, "application/pdf")},
    )

    assert create_response.status_code == 202
    job_id = create_response.json()["job_id"]

    payload = wait_for_job(job_id)
    assert payload["status"] == "completed"
    assert payload["download_url"] == f"/api/jobs/{job_id}/download"

    download_response = client.get(payload["download_url"])
    assert download_response.status_code == 200
    assert download_response.headers["content-type"] == "application/pdf"


def test_processes_batch_uploads() -> None:
    first_pdf = build_pdf_bytes("Primeiro documento")
    second_pdf = build_pdf_bytes("Segundo documento")

    response = client.post(
        "/api/jobs/batch",
        files=[
            ("files", ("first.pdf", first_pdf, "application/pdf")),
            ("files", ("second.pdf", second_pdf, "application/pdf")),
        ],
    )

    assert response.status_code == 202
    payload = response.json()
    assert len(payload["jobs"]) == 2
    assert payload["jobs"][0]["filename"] == "first.pdf"
    assert payload["jobs"][1]["filename"] == "second.pdf"


def test_downloads_completed_jobs_as_zip() -> None:
    first_pdf = build_pdf_bytes("Primeiro documento")
    second_pdf = build_pdf_bytes("Segundo documento")

    response = client.post(
        "/api/jobs/batch",
        files=[
            ("files", ("first.pdf", first_pdf, "application/pdf")),
            ("files", ("second.pdf", second_pdf, "application/pdf")),
        ],
    )

    payload = response.json()
    job_ids = [job["job_id"] for job in payload["jobs"]]
    for job_id in job_ids:
        final_payload = wait_for_job(job_id)
        assert final_payload["status"] == "completed"
    query = "&".join(f"job_ids={job_id}" for job_id in job_ids)
    download_response = client.get(f"/api/jobs/download-batch?{query}")

    assert download_response.status_code == 200
    assert download_response.headers["content-type"] == "application/zip"
    with ZipFile(BytesIO(download_response.content)) as archive:
        assert sorted(archive.namelist()) == ["first-searchable.pdf", "second-searchable.pdf"]


def test_clears_pending_jobs_from_queue() -> None:
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
    )

    assert response.status_code == 202
    clear_response = client.post("/api/jobs/clear-queue")
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
