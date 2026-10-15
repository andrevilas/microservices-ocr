from __future__ import annotations

import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader

from app.config import settings
from app.models import OCRResult
from app.results import (JobReport, OutputValidation, QualityReport, StageReport,
                         digest_file, package_version, persist_pages, persist_report)
from app.services.easyocr_service import FallbackOcrService
from app.services.ocrmypdf_service import OcrmypdfService
from app.services.pdf_builder import PdfBuilder
from app.services.storage_service import JobRecord, JobStore
from app.services.tesseract_service import PrimaryOcrService
from app.utils.quality_evaluator import QualityEvaluation, evaluate_quality

logger = logging.getLogger(__name__)


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

    def create_job(self, filename: str, payload: bytes, owner_user_id: int | None = None,
                   *, owner_principal_id: str | None = None) -> JobRecord:
        return self.job_store.create(filename=filename, payload=payload, owner_user_id=owner_user_id,
                                     owner_principal_id=owner_principal_id)

    def process_job(self, job_id: str) -> OCRResult | None:
        job = self.job_store.update(job_id, status="processing", progress_percent=5, error=None)
        _start_ms = time.monotonic()
        primary_output = job.working_dir / "primary-searchable.pdf"
        draft_output = job.working_dir / "final-draft.pdf"
        final_output = job.working_dir / "final-searchable-pdfa.pdf"
        try:
            logger.info("Job %s started", job_id)
            stages = []
            warnings = ["quality_is_heuristic_not_accuracy", "pdfa_conformance_not_validated"]
            primary_available = self.ocrmypdf_service.is_available()
            source = "ocrmypdf" if primary_available else "pdf_text"
            engines = {"pypdf": package_version("pypdf")}
            if primary_available:
                engines["ocrmypdf"] = package_version("ocrmypdf")
                # OCRmyPDF owns invocation of Tesseract; its binary version is not captured here.
                engines["tesseract"] = None
                warnings.append("tesseract_version_not_captured")
            else:
                warnings.append("primary_ocr_unavailable")
            stage_start = time.monotonic()
            pages = self.primary_ocr.process_pages(job.input_pdf_path, primary_output)
            stages.append(StageReport(name="primary", duration_ms=int((time.monotonic() - stage_start) * 1000)))
            primary_text = "\n".join(pages)
            self.job_store.update(job_id, progress_percent=35)
            evaluation = evaluate_quality(
                primary_text,
                min_text=settings.quality_min_text,
                valid_ratio_threshold=settings.quality_valid_ratio_threshold,
            )
            text = primary_text
            used_fallback = False
            fallback_attempted = False
            base_pdf_path: Path | None = primary_output

            if evaluation.label == "LOW" and self.fallback_ocr.is_available():
                fallback_attempted = True
                engines["easyocr"] = package_version("easyocr")
                engines["pdf2image"] = package_version("pdf2image")
                engines["poppler"] = None
                warnings.append("poppler_version_not_captured")
                logger.info("Job %s: primary quality LOW, trying fallback", job_id)
                self.job_store.update(job_id, progress_percent=45)
                stage_start = time.monotonic()
                fallback_pages = self.fallback_ocr.process_pages(job.input_pdf_path)
                if len(fallback_pages) != len(pages):
                    raise ValueError("Fallback page count mismatch.")
                fallback_text = "\n".join(fallback_pages)
                stages.append(StageReport(name="fallback", duration_ms=int((time.monotonic() - stage_start) * 1000)))
                self.job_store.update(job_id, progress_percent=60)
                fallback_eval = evaluate_quality(
                    fallback_text,
                    min_text=settings.quality_min_text,
                    valid_ratio_threshold=settings.quality_valid_ratio_threshold,
                )
                if self._is_better(primary=evaluation, fallback=fallback_eval):
                    logger.info(
                        "Job %s: fallback chosen (ratio=%.2f, chars=%d)",
                        job_id, fallback_eval.valid_ratio, fallback_eval.character_count,
                    )
                    text = fallback_text
                    evaluation = fallback_eval
                    used_fallback = True
                    pages = fallback_pages
                    source = "easyocr"
                    base_pdf_path = None

            if evaluation.label == "LOW" and not fallback_attempted:
                warnings.append("fallback_ocr_unavailable")
            if not text.strip():
                warnings.append("no_text_extracted_blankness_not_determined")
            elif evaluation.character_count < settings.quality_min_text:
                warnings.append("insufficient_text")
            if used_fallback:
                warnings.append("overlay_geometry_approximate_text_may_be_clipped")
                engines["reportlab"] = package_version("reportlab")
            page_info = persist_pages(job.working_dir, pages, source)

            self.job_store.update(job_id, progress_percent=72)
            stage_start = time.monotonic()
            self.pdf_builder.build(
                original_pdf_path=job.input_pdf_path,
                output_pdf_path=draft_output,
                text=text,
                base_pdf_path=base_pdf_path,
                page_texts=pages,
            )
            stages.append(StageReport(name="build", duration_ms=int((time.monotonic() - stage_start) * 1000)))
            self.job_store.update(job_id, progress_percent=88)
            stage_start = time.monotonic()
            self._finalize_output(draft_output, final_output)
            stages.append(StageReport(name="finalize", duration_ms=int((time.monotonic() - stage_start) * 1000)))
            input_count = len(PdfReader(str(job.input_pdf_path)).pages)
            output_count = len(PdfReader(str(final_output)).pages)
            if output_count != input_count or input_count != len(pages):
                raise ValueError("Output page count mismatch.")
            report = JobReport(
                job_id=job_id, generated_at=datetime.now(timezone.utc),
                duration_ms=int((time.monotonic() - _start_ms) * 1000),
                input=digest_file(job.input_pdf_path), output=digest_file(final_output),
                page_count=len(pages), pages=page_info,
                quality=QualityReport(label=evaluation.label, character_count=evaluation.character_count,
                                      valid_ratio=evaluation.valid_ratio, min_text=settings.quality_min_text,
                                      valid_ratio_threshold=settings.quality_valid_ratio_threshold),
                stages=stages, engines=engines, fallback_attempted=fallback_attempted,
                used_fallback=used_fallback, warnings=warnings,
                output_validation=OutputValidation(requested_format=settings.final_output_type,
                                                   pdf_readable=True, page_count_matches=True),
            )
            persist_report(job.working_dir, report)
            self.job_store.update(job_id, progress_percent=96)
            _elapsed_ms = int((time.monotonic() - _start_ms) * 1000)
            logger.info(
                "Job %s completed in %d ms (fallback=%s, quality=%s)",
                job_id, _elapsed_ms, used_fallback, evaluation.label,
            )
            self.job_store.update(
                job_id,
                status="completed",
                progress_percent=100,
                output_pdf_path=final_output,
                quality=evaluation.label,
            )
            return OCRResult(
                text=text,
                quality=evaluation.label,
                valid_ratio=evaluation.valid_ratio,
                character_count=evaluation.character_count,
                output_pdf_path=str(final_output),
                used_fallback=used_fallback,
            )
        except Exception as exc:  # pragma: no cover - final safety net for background work
            _elapsed_ms = int((time.monotonic() - _start_ms) * 1000)
            logger.error("Job %s failed after %d ms (%s)", job_id, _elapsed_ms, type(exc).__name__)
            self.job_store.update(job_id, status="failed", progress_percent=100, error=str(exc))
            return None

    def _finalize_output(self, draft_output: Path, final_output: Path) -> None:
        if self.ocrmypdf_service.is_available():
            self.ocrmypdf_service.optimize_pdfa(draft_output, final_output)
            return
        shutil.copyfile(draft_output, final_output)

    @staticmethod
    def _is_better(primary: QualityEvaluation, fallback: QualityEvaluation) -> bool:
        # Conservative: fallback must not lose significant text and must have
        # equal or better valid_ratio to be preferred over primary.
        tolerance = settings.fallback_character_tolerance
        return (
            fallback.valid_ratio >= primary.valid_ratio
            and fallback.character_count >= primary.character_count - tolerance
        )
