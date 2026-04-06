from io import BytesIO

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from app.main import app
from app.config import settings


client = TestClient(app)


def build_pdf_bytes(text: str) -> bytes:
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(100, 750, text)
    pdf.save()
    return buffer.getvalue()


def test_homepage_loads() -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "OCR Recognizer" in response.text


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

    status_response = client.get(f"/api/jobs/{job_id}")
    assert status_response.status_code == 200
    payload = status_response.json()
    assert payload["status"] == "completed"
    assert payload["download_url"] == f"/api/jobs/{job_id}/download"

    download_response = client.get(payload["download_url"])
    assert download_response.status_code == 200
    assert download_response.headers["content-type"] == "application/pdf"


def test_default_upload_limit_is_80_mb() -> None:
    assert settings.max_upload_size_mb == 80
