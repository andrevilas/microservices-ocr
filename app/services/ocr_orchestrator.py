from __future__ import annotations

import re
from pathlib import Path

from app.config import settings
from app.models import OCRResult
from app.services.easyocr_service import FallbackOcrService
from app.services.ocrmypdf_service import OcrmypdfService
from app.services.pdf_builder import PdfBuilder
from app.services.storage_service import JobRecord, JobStore
from app.services.tesseract_service import PrimaryOcrService
from app.utils.quality_evaluator import QualityEvaluation, evaluate_quality


class OcrOrchestrator:
    def __init__(
        self,
        job_store: JobStore,
        primary_ocr: PrimaryOcrService | None = None,
        fallback_ocr: FallbackOcrService | None = None,
        pdf_builder: PdfBuilder | None = None,
        ocrmypdf_service: OcrmypdfService | None = None,
    ) -> None:
        self.job_store = job_store
        self.ocrmypdf_service = ocrmypdf_service or OcrmypdfService()
        self.primary_ocr = primary_ocr or PrimaryOcrService(ocrmypdf_service=self.ocrmypdf_service)
        self.fallback_ocr = fallback_ocr or FallbackOcrService()
        self.pdf_builder = pdf_builder or PdfBuilder()

    def create_job(self, filename: str, payload: bytes) -> JobRecord:
        return self.job_store.create(filename=filename, payload=payload)

    def process_job(self, job_id: str) -> OCRResult | None:
        job = self.job_store.update(job_id, status="processing", error=None)
        primary_output = job.working_dir / "primary-searchable.pdf"
        draft_output = job.working_dir / "final-draft.pdf"
        final_output = job.working_dir / "final-searchable-pdfa.pdf"
        try:
            primary_text = self.primary_ocr.process(job.input_pdf_path, primary_output)
            evaluation = evaluate_quality(
                primary_text,
                min_text=settings.quality_min_text,
                valid_ratio_threshold=settings.quality_valid_ratio_threshold,
            )
            text = primary_text
            used_fallback = False
            base_pdf_path: Path | None = primary_output

            if evaluation.label == "LOW" and self.fallback_ocr.is_available():
                fallback_text = self.fallback_ocr.process(job.input_pdf_path)
                fallback_eval = evaluate_quality(
                    fallback_text,
                    min_text=settings.quality_min_text,
                    valid_ratio_threshold=settings.quality_valid_ratio_threshold,
                )
                if self._is_better(primary=evaluation, fallback=fallback_eval):
                    text = fallback_text
                    evaluation = fallback_eval
                    used_fallback = True
                    base_pdf_path = None

            self.pdf_builder.build(
                original_pdf_path=job.input_pdf_path,
                output_pdf_path=draft_output,
                text=text or primary_text,
                base_pdf_path=base_pdf_path,
            )
            self._finalize_output(draft_output, final_output)
            self.job_store.update(job_id, status="completed", output_pdf_path=final_output, quality=evaluation.label)
            return OCRResult(
                text=text,
                quality=evaluation.label,
                valid_ratio=evaluation.valid_ratio,
                character_count=evaluation.character_count,
                output_pdf_path=str(final_output),
                used_fallback=used_fallback,
            )
        except Exception as exc:  # pragma: no cover - final safety net for background work
            self.job_store.update(job_id, status="failed", error=str(exc))
            return None

    def _finalize_output(self, draft_output: Path, final_output: Path) -> None:
        if self.ocrmypdf_service.is_available():
            self.ocrmypdf_service.optimize_pdfa(draft_output, final_output)
            return
        final_output.write_bytes(draft_output.read_bytes())

    @staticmethod
    def _is_better(primary: QualityEvaluation, fallback: QualityEvaluation) -> bool:
        return (
            fallback.character_count >= primary.character_count + settings.fallback_min_improvement_chars
            or fallback.valid_ratio > primary.valid_ratio
        )
