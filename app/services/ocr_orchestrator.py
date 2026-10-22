from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader

from app.config import settings
from app.application import ApplicationError
from app.services.capability_service import get_capability_service
from app.models import OCRResult
from app.results import (JobReport, OutputValidation, QualityReport, StageReport,
                         digest_file, package_version, persist_pages, persist_report)
from app.services.easyocr_service import FallbackOcrService
from app.services.ocrmypdf_service import OcrmypdfService
from app.services.pdf_builder import PdfBuilder
from app.services.pdfa_validator import (PROFILE_2B, REASON_NOT_CONFIGURED, VeraPdfValidator, build_validator,
                                         unavailable_result)
from app.services.storage_service import JobRecord, JobStore
from app.services.tesseract_service import PrimaryOcrService
from app.services.cancellation import cancellation_scope, checkpoint, JobCancelled, ProcessTerminationError
from app.utils.quality_evaluator import QualityEvaluation, evaluate_quality

logger = logging.getLogger(__name__)
_FROM_SETTINGS = object()
REASON_HASH_MISMATCH = "validated_hash_mismatch"


def legacy_output_format(final_output_type: str) -> str:
    """Effective format of a job without recorded options, derived from the server setting.

    OCRmyPDF's ``pdfa`` alias is PDF/A-2; other PDF/A parts cannot be proven against the 2b
    validator and are rejected instead of being delivered unvalidated.
    """
    value = (final_output_type or "").strip().lower()
    if value == "pdf":
        return "pdf_searchable"
    if value in ("pdfa", "pdfa-2"):
        return "pdfa"
    raise ApplicationError(422, "unsupported_legacy_output_type",
                           "Legacy output type cannot be validated as PDF/A-2b.")


class OcrOrchestrator:
    def __init__(
        self,
        job_store: JobStore,
        primary_ocr: PrimaryOcrService | None = None,
        fallback_ocr: FallbackOcrService | None = None,
        pdf_builder: PdfBuilder | None = None,
        ocrmypdf_service: OcrmypdfService | None = None,
        pdfa_validator: VeraPdfValidator | None | object = _FROM_SETTINGS,
    ) -> None:
        self.job_store = job_store
        self.pdfa_validator = pdfa_validator
        self.ocrmypdf_service = ocrmypdf_service or OcrmypdfService()
        self.primary_ocr = primary_ocr or PrimaryOcrService(ocrmypdf_service=self.ocrmypdf_service)
        self.fallback_ocr = fallback_ocr or FallbackOcrService()
        self.pdf_builder = pdf_builder or PdfBuilder()

    def create_job(self, filename: str, payload: bytes, owner_user_id: int | None = None,
                   *, owner_principal_id: str | None = None) -> JobRecord:
        return self.job_store.create(filename=filename, payload=payload, owner_user_id=owner_user_id,
                                     owner_principal_id=owner_principal_id)

    def process_job(self, job_id: str) -> OCRResult | None:
        job = self.job_store.claim(job_id)
        if job is None:
            return None
        try:
            with cancellation_scope(lambda: self.job_store.cancellation_requested(job_id)):
                return self._process_claimed(job)
        except JobCancelled:
            self.job_store.finish_cancellation(job_id)
            return None
        except ProcessTerminationError:
            # Never claim cancellation/completion while a process may still be alive.
            self.job_store.update(job_id, status="cancel_requested", error_code="process_termination_failed")
            raise

    def _process_claimed(self, job) -> OCRResult | None:
        job_id = job.job_id
        _start_ms = time.monotonic()
        primary_output = job.working_dir / "primary-searchable.pdf"
        draft_output = job.working_dir / "final-draft.pdf"
        candidate_output = job.working_dir / "final-candidate.pdf"
        final_output = job.working_dir / "final-searchable-pdfa.pdf"
        try:
            logger.info("Job %s started", job_id)
            options = job.options_effective
            output_format = options.output_format if options else legacy_output_format(settings.final_output_type)
            capabilities = get_capability_service().validate(options, refresh=True) if options is not None else None
            quality_min_text = options.quality_min_text if options else settings.quality_min_text
            quality_ratio = options.quality_valid_ratio_threshold if options else settings.quality_valid_ratio_threshold
            fallback_enabled = options.fallback_policy == "if_low_quality" if options else True
            checkpoint()
            stages = []
            warnings = ["quality_is_heuristic_not_accuracy"]
            primary_available = options.processing_mode != "extract_text" if options else self.ocrmypdf_service.is_available()
            source = "ocrmypdf" if primary_available else "pdf_text"
            engines = {"pypdf": package_version("pypdf")}
            if primary_available:
                engines["ocrmypdf"] = package_version("ocrmypdf")
                # New jobs record the fresh binary probe; legacy jobs have no such snapshot.
                engines["tesseract"] = capabilities.engines["tesseract"].version if capabilities else None
                if capabilities:
                    engines["ghostscript"] = capabilities.engines["ghostscript"].version
                else:
                    warnings.append("tesseract_version_not_captured")
            else:
                warnings.append("text_extraction_only" if options else "primary_ocr_unavailable")
            stage_start = time.monotonic()
            pages = (self.primary_ocr.process_pages(job.input_pdf_path, primary_output, options=options)
                     if options else self.primary_ocr.process_pages(job.input_pdf_path, primary_output))
            stages.append(StageReport(name="primary", duration_ms=int((time.monotonic() - stage_start) * 1000)))
            primary_text = "\n".join(pages)
            self.job_store.worker_update(job_id, progress_percent=35)
            evaluation = evaluate_quality(
                primary_text,
                min_text=quality_min_text,
                valid_ratio_threshold=quality_ratio,
            )
            text = primary_text
            used_fallback = False
            fallback_attempted = False
            base_pdf_path: Path | None = primary_output

            if evaluation.label == "LOW" and fallback_enabled and self.fallback_ocr.is_available():
                fallback_attempted = True
                engines["easyocr"] = package_version("easyocr")
                engines["pdf2image"] = package_version("pdf2image")
                engines["poppler"] = capabilities.engines["pdftoppm"].version if capabilities else None
                if not capabilities:
                    warnings.append("poppler_version_not_captured")
                logger.info("Job %s: primary quality LOW, trying fallback", job_id)
                self.job_store.worker_update(job_id, progress_percent=45)
                stage_start = time.monotonic()
                fallback_pages = self.fallback_ocr.process_pages(job.input_pdf_path)
                if len(fallback_pages) != len(pages):
                    raise ValueError("Fallback page count mismatch.")
                fallback_text = "\n".join(fallback_pages)
                stages.append(StageReport(name="fallback", duration_ms=int((time.monotonic() - stage_start) * 1000)))
                self.job_store.worker_update(job_id, progress_percent=60)
                fallback_eval = evaluate_quality(
                    fallback_text,
                    min_text=quality_min_text,
                    valid_ratio_threshold=quality_ratio,
                )
                if self._is_better(primary=evaluation, fallback=fallback_eval,
                                   tolerance=options.fallback_character_tolerance if options else None):
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
                if options and fallback_enabled:
                    raise ApplicationError(503, "fallback_unavailable", "Fallback dependencies are unavailable.")
                warnings.append("fallback_disabled" if not fallback_enabled else "fallback_ocr_unavailable")
            if not text.strip():
                warnings.append("no_text_extracted_blankness_not_determined")
            elif evaluation.character_count < quality_min_text:
                warnings.append("insufficient_text")
            if used_fallback:
                warnings.append("overlay_geometry_approximate_text_may_be_clipped")
                engines["reportlab"] = package_version("reportlab")
            checkpoint()
            page_info = persist_pages(job.working_dir, pages, source)

            self.job_store.worker_update(job_id, progress_percent=72)
            stage_start = time.monotonic()
            self.pdf_builder.build(
                original_pdf_path=job.input_pdf_path,
                output_pdf_path=draft_output,
                text=text,
                base_pdf_path=base_pdf_path,
                page_texts=pages,
            )
            stages.append(StageReport(name="build", duration_ms=int((time.monotonic() - stage_start) * 1000)))
            self.job_store.worker_update(job_id, progress_percent=88)
            stage_start = time.monotonic()
            compression = self._finalize_output(draft_output, candidate_output, options=options) or {}
            stages.append(StageReport(name="finalize", duration_ms=int((time.monotonic() - stage_start) * 1000)))
            input_count = len(PdfReader(str(job.input_pdf_path)).pages)
            output_count = len(PdfReader(str(candidate_output)).pages)
            if output_count != input_count or input_count != len(pages):
                raise ValueError("Output page count mismatch.")
            validation = OutputValidation(
                requested_format=output_format,
                pdf_readable=True, page_count_matches=True,
                compression_preset_version=options.compression_preset_version if options else None,
                compression_arguments=compression.get("arguments") or [],
                compression_fallback=compression.get("fallback"))
            if compression.get("fallback"):
                warnings.append(compression["fallback"])
            pdfa = None
            if output_format == "pdfa":
                checkpoint()
                stage_start = time.monotonic()
                pdfa = self._validate_pdfa(candidate_output, options)
                stages.append(StageReport(name="pdfa_validation",
                                          duration_ms=int((time.monotonic() - stage_start) * 1000)))
            # Hashed exactly once, after validation; these are the bytes that get published.
            output_digest = digest_file(candidate_output)
            if pdfa is not None:
                if pdfa.status == "passed" and pdfa.sha256 != output_digest.sha256:
                    # The verdict does not cover the bytes we would deliver: fail closed.
                    pdfa = replace(pdfa, status="indeterminate", reason_codes=(REASON_HASH_MISMATCH,))
                validation = validation.model_copy(update={
                    "pdfa_conformance": pdfa.status, "pdfa_profile": pdfa.profile, "pdfa_validator": pdfa.validator,
                    "pdfa_validator_version": pdfa.validator_version, "pdfa_validated_sha256": pdfa.sha256,
                    "pdfa_reason_codes": list(pdfa.reason_codes), "pdfa_stats": dict(pdfa.stats)})
            else:
                warnings.append("pdfa_conformance_not_validated")
            report = JobReport(
                job_id=job_id, generated_at=datetime.now(timezone.utc),
                duration_ms=int((time.monotonic() - _start_ms) * 1000),
                input=digest_file(job.input_pdf_path), output=output_digest,
                page_count=len(pages), pages=page_info,
                quality=QualityReport(label=evaluation.label, character_count=evaluation.character_count,
                                      valid_ratio=evaluation.valid_ratio, min_text=quality_min_text,
                                      valid_ratio_threshold=quality_ratio),
                stages=stages, engines=engines, fallback_attempted=fallback_attempted,
                used_fallback=used_fallback, warnings=warnings,
                options_requested=job.options_requested, options_effective=options,
                output_validation=validation,
            )
            checkpoint()
            persist_report(job.working_dir, report)
            if pdfa is not None and pdfa.status != "passed":
                # Fail closed: the report above explains why; no PDF/A is delivered as completed.
                code = "pdfa_validation_failed" if pdfa.status == "failed" else "pdfa_validation_indeterminate"
                logger.warning("Job %s: PDF/A-%s validation %s (%s)", job_id, pdfa.profile, pdfa.status,
                               ",".join(pdfa.reason_codes))
                self.job_store.worker_update(job_id, status="failed", progress_percent=100,
                                             error=f"PDF/A validation {pdfa.status}: {', '.join(pdfa.reason_codes)}",
                                             error_code=code)
                candidate_output.unlink(missing_ok=True)
                return None
            self.job_store.worker_update(job_id, progress_percent=96)
            checkpoint()
            # Atomic publish inside the job's exclusive working dir; the file is never mutated afterwards.
            os.replace(candidate_output, final_output)
            _elapsed_ms = int((time.monotonic() - _start_ms) * 1000)
            logger.info(
                "Job %s completed in %d ms (fallback=%s, quality=%s)",
                job_id, _elapsed_ms, used_fallback, evaluation.label,
            )
            self.job_store.worker_update(
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
        except (JobCancelled, ProcessTerminationError):
            raise
        except Exception as exc:  # pragma: no cover - final safety net for background work
            _elapsed_ms = int((time.monotonic() - _start_ms) * 1000)
            logger.error("Job %s failed after %d ms (%s)", job_id, _elapsed_ms, type(exc).__name__)
            code = exc.code if isinstance(exc, ApplicationError) else "processing_failed"
            self.job_store.worker_update(job_id, status="failed", progress_percent=100, error=str(exc), error_code=code)
            return None

    def _validate_pdfa(self, final_output: Path, options):
        validator = self.pdfa_validator
        if validator is _FROM_SETTINGS:
            timeout = (options.pdfa_validation_timeout_seconds if options else None) or settings.verapdf_timeout_seconds
            validator = build_validator(str(settings.verapdf_executable) if settings.verapdf_executable else None,
                                        timeout)
        if validator is None:
            return unavailable_result(REASON_NOT_CONFIGURED, digest_file(final_output).sha256)
        return validator.validate(final_output, PROFILE_2B)

    def _finalize_output(self, draft_output: Path, final_output: Path, options=None) -> dict | None:
        if options is not None:
            if options.output_format == "pdfa" and options.processing_mode == "extract_text":
                # A copied text-layer PDF must never be labelled PDF/A.
                raise ApplicationError(422, "unsupported_option_combination", "Text extraction cannot produce PDF/A.")
            if options.processing_mode == "extract_text" or (options.output_format == "pdf_searchable" and options.compression_profile == "none"):
                shutil.copyfile(draft_output, final_output)
                return None
            return self.ocrmypdf_service.optimize_pdfa(draft_output, final_output, options=options)
        if self.ocrmypdf_service.is_available():
            self.ocrmypdf_service.optimize_pdfa(draft_output, final_output)
            return
        shutil.copyfile(draft_output, final_output)

    @staticmethod
    def _is_better(primary: QualityEvaluation, fallback: QualityEvaluation, tolerance: int | None = None) -> bool:
        # Conservative: fallback must not lose significant text and must have
        # equal or better valid_ratio to be preferred over primary.
        tolerance = settings.fallback_character_tolerance if tolerance is None else tolerance
        return (
            fallback.valid_ratio >= primary.valid_ratio
            and fallback.character_count >= primary.character_count - tolerance
        )
