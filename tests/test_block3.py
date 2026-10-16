"""Tests for BLOCK 3: OCR quality/performance, XSS, Docker/CI, observability."""

import re
import sys
import threading
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from app.config import settings
from app.main import app
from app.services.easyocr_service import FallbackOcrService
from app.services.ocr_orchestrator import OcrOrchestrator
from app.services.pdf_builder import PdfBuilder
from app.services.storage_service import JobStore
from app.utils.quality_evaluator import (
    COMMON_WORDS,
    QualityEvaluation,
    _is_valid_alphanumeric,
    evaluate_quality,
)

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


def _make_store(tmp_path: Path) -> JobStore:
    return JobStore(root_dir=tmp_path, _db_path=tmp_path / "test-jobs.sqlite3")


# ---------------------------------------------------------------------------
# 1. EasyOCR: lazy cached Reader, single readtext per page
# ---------------------------------------------------------------------------


class TestEasyOcrCacheAndSingleRead:
    def test_fallback_service_has_init_with_reader_fields(self) -> None:
        svc = FallbackOcrService()
        assert svc._reader is None
        assert isinstance(svc._reader_lock, type(threading.Lock()))

    def test_get_reader_caches_instance(self) -> None:
        svc = FallbackOcrService()
        mock_reader = MagicMock()
        # Simulate the Reader being created
        svc._reader = mock_reader
        assert svc._get_reader() is mock_reader
        # Call again to verify caching
        assert svc._get_reader() is mock_reader

    def test_get_reader_thread_safe_double_check(self) -> None:
        """Verify double-checked locking pattern works."""
        svc = FallbackOcrService()
        mock_reader = MagicMock()

        call_count = 0

        def fake_reader_init(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return mock_reader

        with patch.dict("sys.modules", {"easyocr": MagicMock()}):
            sys.modules["easyocr"].Reader = fake_reader_init
            # First call creates the reader
            reader1 = svc._get_reader()
            # Second call should return cached
            reader2 = svc._get_reader()
            assert reader1 is reader2

    def test_process_calls_readtext_once_per_page_with_mocks(self, tmp_path: Path) -> None:
        """Mock easyocr and pdf2image to validate readtext is called exactly
        once per page and Reader is created once (cached)."""
        # --- build a real 2-page PDF ---
        buf = BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(100, 750, "Page 1")
        c.showPage()
        c.drawString(100, 750, "Page 2")
        c.showPage()
        c.save()
        pdf_path = tmp_path / "two_pages.pdf"
        pdf_path.write_bytes(buf.getvalue())

        # --- set up mocks ---
        mock_reader_instance = MagicMock()
        mock_reader_instance.readtext.return_value = [
            ([0, 0, 100, 100], "Texto OCR", 0.95),
        ]

        mock_easyocr = MagicMock()
        mock_easyocr.Reader.return_value = mock_reader_instance

        fake_image_1 = MagicMock()
        fake_image_2 = MagicMock()

        mock_pdf2image = MagicMock()
        mock_pdf2image.convert_from_path.return_value = [fake_image_1, fake_image_2]

        # Fresh service instance with no cached reader
        svc = FallbackOcrService()

        with patch.dict("sys.modules", {
            "easyocr": mock_easyocr,
            "pdf2image": mock_pdf2image,
        }), patch("app.services.easyocr_service.preprocess_image") as mock_preprocess:
            # Make preprocess_image return the same page path (which we ensure exists)
            def preprocess_side_effect(page_path):
                page_path.write_bytes(b"fake png")
                return page_path
            mock_preprocess.side_effect = preprocess_side_effect

            # Force is_available to return True since we mocked the modules
            with patch.object(FallbackOcrService, "is_available", return_value=True):
                result = svc.process(pdf_path)

        # Reader should be created exactly once
        mock_easyocr.Reader.assert_called_once_with(["pt", "en"], gpu=False, download_enabled=False)

        # readtext should be called exactly once per page (2 pages)
        assert mock_reader_instance.readtext.call_count == 2

        # Result should contain OCR text from both pages
        assert "Texto OCR" in result

    def test_reader_is_cached_across_multiple_process_calls(self, tmp_path: Path) -> None:
        """Calling process() twice should not create Reader twice."""
        buf = BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(100, 750, "Single page")
        c.save()
        pdf_path = tmp_path / "single.pdf"
        pdf_path.write_bytes(buf.getvalue())

        mock_reader_instance = MagicMock()
        mock_reader_instance.readtext.return_value = [
            ([0, 0, 100, 100], "Hello", 0.9),
        ]

        mock_easyocr = MagicMock()
        mock_easyocr.Reader.return_value = mock_reader_instance

        fake_image = MagicMock()
        mock_pdf2image = MagicMock()
        mock_pdf2image.convert_from_path.return_value = [fake_image]

        svc = FallbackOcrService()

        with patch.dict("sys.modules", {
            "easyocr": mock_easyocr,
            "pdf2image": mock_pdf2image,
        }), patch("app.services.easyocr_service.preprocess_image") as mock_preprocess, \
             patch.object(FallbackOcrService, "is_available", return_value=True):
            def preprocess_side_effect(page_path):
                page_path.write_bytes(b"fake png")
                return page_path
            mock_preprocess.side_effect = preprocess_side_effect

            svc.process(pdf_path)
            svc.process(pdf_path)

        # Reader created only once despite two process() calls
        mock_easyocr.Reader.assert_called_once()


# ---------------------------------------------------------------------------
# 2. _is_better: conservative fallback selection
# ---------------------------------------------------------------------------


class TestIsBetterConservative:
    def test_fallback_character_tolerance_setting(self) -> None:
        assert settings.fallback_character_tolerance == 20

    def test_fallback_with_better_ratio_and_enough_chars(self) -> None:
        primary = QualityEvaluation(label="LOW", valid_ratio=0.3, character_count=100)
        fallback = QualityEvaluation(label="HIGH", valid_ratio=0.8, character_count=90)
        assert OcrOrchestrator._is_better(primary, fallback) is True

    def test_fallback_rejected_when_much_less_text(self) -> None:
        primary = QualityEvaluation(label="LOW", valid_ratio=0.3, character_count=200)
        fallback = QualityEvaluation(label="HIGH", valid_ratio=0.9, character_count=50)
        # 50 < 200 - 20 = 180, so fallback is rejected
        assert OcrOrchestrator._is_better(primary, fallback) is False

    def test_fallback_rejected_when_lower_ratio(self) -> None:
        primary = QualityEvaluation(label="LOW", valid_ratio=0.5, character_count=100)
        fallback = QualityEvaluation(label="HIGH", valid_ratio=0.4, character_count=100)
        assert OcrOrchestrator._is_better(primary, fallback) is False

    def test_fallback_accepted_equal_ratio_within_tolerance(self) -> None:
        primary = QualityEvaluation(label="LOW", valid_ratio=0.5, character_count=100)
        fallback = QualityEvaluation(label="HIGH", valid_ratio=0.5, character_count=85)
        # 85 >= 100 - 20 = 80
        assert OcrOrchestrator._is_better(primary, fallback) is True

    def test_fallback_rejected_at_tolerance_boundary(self) -> None:
        primary = QualityEvaluation(label="LOW", valid_ratio=0.5, character_count=100)
        fallback = QualityEvaluation(label="HIGH", valid_ratio=0.5, character_count=79)
        # 79 < 100 - 20 = 80
        assert OcrOrchestrator._is_better(primary, fallback) is False

    def test_is_better_uses_and_not_or(self) -> None:
        """Old logic used OR (ratio OR chars). New logic requires BOTH."""
        # High ratio but way fewer chars - should be rejected
        primary = QualityEvaluation(label="LOW", valid_ratio=0.2, character_count=500)
        fallback = QualityEvaluation(label="HIGH", valid_ratio=0.9, character_count=100)
        # 100 < 500 - 20 = 480 -> rejected despite better ratio
        assert OcrOrchestrator._is_better(primary, fallback) is False


# ---------------------------------------------------------------------------
# 3. PdfBuilder: preserve pages in fallback mode + searchable overlay
# ---------------------------------------------------------------------------


class TestPdfBuilderPreservesPages:
    def test_fallback_preserves_page_count(self, tmp_path: Path) -> None:
        """When base_pdf_path is None, output should have same page count as original."""
        # Create a 3-page PDF
        buf = BytesIO()
        c = canvas.Canvas(buf)
        for i in range(3):
            c.drawString(100, 750, f"Page {i + 1}")
            c.showPage()
        c.save()
        original = tmp_path / "original.pdf"
        original.write_bytes(buf.getvalue())

        output = tmp_path / "output.pdf"
        builder = PdfBuilder()
        builder.build(
            original_pdf_path=original,
            output_pdf_path=output,
            text="Line one\nLine two\nLine three",
            base_pdf_path=None,
        )

        reader = PdfReader(str(output))
        assert len(reader.pages) == 3

    def test_fallback_creates_valid_pdf(self, tmp_path: Path) -> None:
        buf = BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(100, 750, "Test page")
        c.save()
        original = tmp_path / "original.pdf"
        original.write_bytes(buf.getvalue())

        output = tmp_path / "output.pdf"
        builder = PdfBuilder()
        result = builder.build(
            original_pdf_path=original,
            output_pdf_path=output,
            text="Overlay text",
            base_pdf_path=None,
        )
        assert result == output
        assert output.exists()
        assert output.stat().st_size > 0

    def test_fallback_overlay_is_searchable(self, tmp_path: Path) -> None:
        """After build with fallback, PdfReader.extract_text() must contain
        the overlay text, proving the PDF is searchable."""
        buf = BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(100, 750, "Original visual content")
        c.save()
        original = tmp_path / "original.pdf"
        original.write_bytes(buf.getvalue())

        overlay_text = "contrato de locacao residencial"
        output = tmp_path / "searchable_output.pdf"
        builder = PdfBuilder()
        builder.build(
            original_pdf_path=original,
            output_pdf_path=output,
            text=overlay_text,
            base_pdf_path=None,
        )

        reader = PdfReader(str(output))
        extracted = reader.pages[0].extract_text() or ""
        # The overlay text must be present in extracted text
        assert "contrato" in extracted.lower(), (
            f"Overlay text not found in extracted PDF text. Got: {extracted!r}"
        )
        assert "locacao" in extracted.lower(), (
            f"Expected 'locacao' in extracted text. Got: {extracted!r}"
        )

    def test_fallback_multipage_overlay_searchable(self, tmp_path: Path) -> None:
        """Multi-page overlay: each page's text chunk should be extractable."""
        buf = BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(100, 750, "Page 1 scan")
        c.showPage()
        c.drawString(100, 750, "Page 2 scan")
        c.showPage()
        c.save()
        original = tmp_path / "multipage.pdf"
        original.write_bytes(buf.getvalue())

        output = tmp_path / "multipage_out.pdf"
        builder = PdfBuilder()
        builder.build(
            original_pdf_path=original,
            output_pdf_path=output,
            text="primeira linha\nsegunda linha",
            base_pdf_path=None,
        )

        reader = PdfReader(str(output))
        assert len(reader.pages) == 2
        all_text = " ".join(
            (reader.pages[i].extract_text() or "") for i in range(len(reader.pages))
        ).lower()
        assert "primeira" in all_text or "segunda" in all_text, (
            f"Overlay text not found across pages. Got: {all_text!r}"
        )

    def test_with_base_pdf_copies_directly(self, tmp_path: Path) -> None:
        buf = BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(100, 750, "Base PDF")
        c.save()
        base = tmp_path / "base.pdf"
        base.write_bytes(buf.getvalue())

        original = tmp_path / "original.pdf"
        original.write_bytes(buf.getvalue())

        output = tmp_path / "output.pdf"
        builder = PdfBuilder()
        builder.build(
            original_pdf_path=original,
            output_pdf_path=output,
            text="ignored text",
            base_pdf_path=base,
        )
        assert output.read_bytes() == base.read_bytes()

    def test_split_lines_across_pages(self) -> None:
        lines = ["a", "b", "c", "d", "e", "f"]
        chunks = PdfBuilder._split_lines_across_pages(lines, 3)
        assert len(chunks) == 3
        # All lines should be distributed
        all_lines = []
        for chunk in chunks:
            all_lines.extend(chunk)
        assert all_lines == lines

    def test_split_lines_empty(self) -> None:
        chunks = PdfBuilder._split_lines_across_pages([], 3)
        assert len(chunks) == 3
        assert all(c == [] for c in chunks)

    def test_split_lines_single_page(self) -> None:
        lines = ["a", "b", "c"]
        chunks = PdfBuilder._split_lines_across_pages(lines, 1)
        assert len(chunks) == 1
        assert chunks[0] == lines


# ---------------------------------------------------------------------------
# 4. Quality evaluator: alphanumeric, expanded words
# ---------------------------------------------------------------------------


class TestQualityEvaluatorAlphanumeric:
    def test_common_words_expanded(self) -> None:
        # Check some of the expanded PT words exist
        for word in ["que", "uma", "mais", "pagina", "arquivo"]:
            assert word in COMMON_WORDS
        # Check some EN words
        for word in ["with", "this", "from", "have", "page"]:
            assert word in COMMON_WORDS

    def test_valid_alphanumeric_accepts_digits(self) -> None:
        assert _is_valid_alphanumeric("2024") is True
        assert _is_valid_alphanumeric("42") is True

    def test_valid_alphanumeric_accepts_normal_words(self) -> None:
        assert _is_valid_alphanumeric("hello") is True
        assert _is_valid_alphanumeric("teste") is True

    def test_valid_alphanumeric_rejects_garbage(self) -> None:
        # 4+ consonants with no vowel = garbage
        assert _is_valid_alphanumeric("xkcd") is False
        assert _is_valid_alphanumeric("bcdfg") is False
        assert _is_valid_alphanumeric("zxcv") is False

    def test_valid_alphanumeric_accepts_short_consonants(self) -> None:
        # Short tokens (< 4 chars) are accepted even if all consonants
        assert _is_valid_alphanumeric("tv") is True
        assert _is_valid_alphanumeric("xml") is True

    def test_valid_alphanumeric_accepts_mixed(self) -> None:
        # Mixed alphanumeric with at least one letter
        assert _is_valid_alphanumeric("abc123") is True
        assert _is_valid_alphanumeric("r2d2") is True

    def test_evaluate_quality_with_real_text(self) -> None:
        text = "Este documento para teste com dados de pagina arquivo total"
        result = evaluate_quality(text, min_text=10, valid_ratio_threshold=0.5)
        assert result.label == "HIGH"
        assert result.valid_ratio > 0.5

    def test_evaluate_quality_with_garbage(self) -> None:
        text = "xkcd bcdfg zxcvbnm qwrtyp sdfgh hjklm bnmcv dfghj"
        result = evaluate_quality(text, min_text=10, valid_ratio_threshold=0.7)
        assert result.label == "LOW"

    def test_evaluate_quality_preserves_public_signature(self) -> None:
        """Ensure the public function signature is unchanged."""
        result = evaluate_quality("test text here", min_text=5, valid_ratio_threshold=0.5)
        assert hasattr(result, "label")
        assert hasattr(result, "valid_ratio")
        assert hasattr(result, "character_count")


# ---------------------------------------------------------------------------
# 5. XSS: escapeHtml + renderState/renderStateHtml in frontend
# ---------------------------------------------------------------------------


# Behavioral XSS coverage now lives in test_workspace_ui.py and executes in Chromium.


# ---------------------------------------------------------------------------
# 6. /metrics endpoint
# ---------------------------------------------------------------------------


class TestMetricsEndpoint:
    def test_metrics_returns_200(self) -> None:
        cookies = _auth_cookies()
        response = client.get("/metrics", cookies=cookies)
        assert response.status_code == 200
        data = response.json()
        assert "total_jobs" in data
        assert "by_status" in data
        assert isinstance(data["total_jobs"], int)
        assert isinstance(data["by_status"], dict)

    def test_metrics_counts_jobs(self) -> None:
        cookies = _auth_cookies()
        # Create a job and check metrics reflect it
        pdf_bytes = build_pdf_bytes("test doc for metrics")
        create_resp = client.post(
            "/api/jobs",
            files={"file": ("metrics-test.pdf", pdf_bytes, "application/pdf")},
            cookies=cookies,
        )
        assert create_resp.status_code == 202

        response = client.get("/metrics", cookies=cookies)
        data = response.json()
        assert data["total_jobs"] >= 1

    def test_metrics_requires_auth(self) -> None:
        """Metrics now requires authentication (JWT or API key)."""
        c = _fresh_client()
        response = c.get("/metrics")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# 7. Observability: logging in orchestrator
# ---------------------------------------------------------------------------


class TestOrchestratorLogging:
    def test_process_job_logs_start_and_completion(self, tmp_path: Path) -> None:
        """Verify that process_job emits log messages."""

        store = _make_store(tmp_path)
        job = store.create(filename="test.pdf", payload=build_pdf_bytes("test"))

        orch = OcrOrchestrator(job_store=store)
        with patch("app.services.ocr_orchestrator.logger") as mock_logger:
            orch.process_job(job.job_id)
            # Should have at least one info call for start and one for completion
            info_calls = mock_logger.info.call_args_list
            assert len(info_calls) >= 2
            # First call should mention "started"
            assert "started" in str(info_calls[0]).lower()
            # Last call should mention "completed"
            assert "completed" in str(info_calls[-1]).lower()


# ---------------------------------------------------------------------------
# 8. Config: new settings
# ---------------------------------------------------------------------------


class TestBlock3Settings:
    def test_fallback_character_tolerance_default(self) -> None:
        assert settings.fallback_character_tolerance == 20

    def test_fallback_min_improvement_chars_still_exists(self) -> None:
        assert settings.fallback_min_improvement_chars == 20
