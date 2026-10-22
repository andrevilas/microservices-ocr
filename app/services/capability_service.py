"""Bounded local probes with single-flight caching; responses contain no paths/stderr."""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from threading import Lock

from app.config import settings
from app.options import COMPRESSION_PRESET_VERSION, HOMOLOGATED_COMPRESSION, Capabilities, EffectiveOptions, EngineCapability, ProcessingOptions, ResolvedOptions
from app.results import MAX_PAGE_CHARS, MAX_TOTAL_CHARS, package_version


def command_output(args, timeout=5):
    try:
        from app.services.cancellation import active, run_process
        if active():
            result = run_process(args, timeout=timeout, check=False)
        else:
            result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if result.returncode != 0:
            return None
        return (result.stdout + result.stderr)[:65536]
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None


def version_from(text):
    match = re.search(r"\b(\d+\.\d+(?:\.\d+)?)\b", text or "")
    return match[1] if match else None


def installed(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


class CapabilityService:
    def __init__(self):
        self._lock = Lock()
        self._cached = None
        self._expires = 0

    def snapshot(self, *, refresh=False) -> Capabilities:
        with self._lock:
            if refresh or self._cached is None or time.monotonic() >= self._expires:
                self._cached = self._probe()
                self._expires = time.monotonic() + 60
            result = self._cached.model_copy(deep=True)
        # Limits are inexpensive and always reflect the current operator configuration.
        result.limits = {
            "max_upload_bytes": settings.max_upload_size_mb * 1024 * 1024,
            "max_pages": min(settings.upload_max_pages, 300),
            "max_page_pixels": settings.upload_max_page_pixels,
            "upload_concurrency": settings.upload_concurrency,
            "upload_owner_limit_bytes": settings.upload_owner_limit_mb * 1024 * 1024,
            "upload_storage_limit_bytes": settings.upload_storage_limit_mb * 1024 * 1024,
            "upload_min_free_bytes": settings.upload_min_free_mb * 1024 * 1024,
            "upload_ttl_seconds": settings.upload_ttl_seconds,
            "pending_jobs_per_owner": settings.job_owner_max_pending,
            "batch_max_items": min(settings.max_batch_files, 25),
            "cancel_max_items": 25,
            "job_retention_seconds": settings.job_retention_seconds,
            "retention_interval_seconds": settings.retention_interval_seconds,
            "batch_max_total_bytes": settings.batch_max_total_mb * 1024 * 1024,
            "export_max_items": 25,
            "export_max_total_bytes": settings.export_max_total_mb * 1024 * 1024,
            "export_ttl_seconds": settings.export_ttl_seconds,
            "export_pending_per_owner": settings.export_owner_max_pending,
            "export_pending_total": settings.export_max_pending,
            "worker_concurrency": settings.job_worker_concurrency,
            "engine_jobs_per_job": settings.ocr_engine_jobs,
            "subprocess_timeout_seconds": settings.ocr_subprocess_timeout_seconds,
            "text_response_max_chars": 20000, "page_max_chars": MAX_PAGE_CHARS,
            "document_max_chars": MAX_TOTAL_CHARS,
        }
        return result

    def _probe(self) -> Capabilities:
        engines = {}
        binary_names = {"tesseract": ("tesseract",), "ghostscript": ("gs", "gswin64c", "gswin32c"),
                        "pdftoppm": ("pdftoppm",), "pdfinfo": ("pdfinfo",)}
        binaries = {}
        for name, candidates in binary_names.items():
            path = next((found for candidate in candidates if (found := shutil.which(candidate))), None)
            binaries[name] = path
            output = command_output([path, "-v" if name in ("pdftoppm", "pdfinfo") else "--version"]) if path else None
            version = version_from(output)
            engines[name] = EngineCapability(available=version is not None, version=version,
                                            reasons=[] if version else ["missing_or_failed_probe"])
        ocr_version = version_from(command_output([sys.executable, "-m", "ocrmypdf", "--version"])) if installed("ocrmypdf") else None
        def numeric(value):
            parts = tuple(map(int, (value or "0").split(".")))
            return (parts + (0, 0))[:3]
        for name, invalid in (
            ("tesseract", numeric(engines["tesseract"].version) < (4, 1, 1) or numeric(engines["tesseract"].version) == (5, 4, 0)),
            ("ghostscript", numeric(engines["ghostscript"].version) < (9, 54, 0) or (10, 0, 0) <= numeric(engines["ghostscript"].version) < (10, 2, 1)),
        ):
            if engines[name].available and invalid:
                engines[name].available = False
                engines[name].reasons = ["unsupported_engine_version"]
        supported = bool(ocr_version and (16, 4) <= tuple(map(int, ocr_version.split(".")[:2])) < (17, 0))
        languages = []
        if engines["tesseract"].available:
            output = command_output([binaries["tesseract"], "--list-langs"])
            if output is not None:
                languages = sorted(set(output.splitlines()) & {"eng", "por"})
        reasons = []
        if not supported:
            reasons.append("ocrmypdf_16_4_to_16_x_required")
        if not engines["tesseract"].available:
            reasons.append("tesseract_unavailable")
        if not engines["ghostscript"].available:
            reasons.append("ghostscript_unavailable")
        if not languages:
            reasons.append("no_supported_language_installed")
        ready = not reasons
        engines["ocrmypdf"] = EngineCapability(available=ready, version=ocr_version, reasons=reasons)
        fallback_ready = False
        if (all(installed(x) for x in ("easyocr", "pdf2image", "cv2"))
                and engines["pdftoppm"].available and engines["pdfinfo"].available):
            output = command_output([sys.executable, "-m", "app.capability_probe"], timeout=20)
            try:
                fallback_ready = json.loads(output or "{}").get("ready") is True
            except (ValueError, AttributeError):
                pass
        engines["easyocr"] = EngineCapability(available=fallback_ready, version=package_version("easyocr"),
                                               reasons=[] if fallback_ready else ["dependencies_or_local_pt_en_models_unavailable"])
        engines["verapdf"] = self._probe_verapdf()
        warnings = ["probe_is_not_ocr_homologation", "defaults_may_be_unavailable",
                    "extract_text_does_not_recognize_scans"]
        pdfa_ready = ready and engines["verapdf"].available
        if not engines["verapdf"].available:
            # PDF/A is not offered (and is rejected at admission) until a validator is available.
            warnings.append("pdfa_validator_unavailable_pdfa_not_offered")
        return Capabilities(
            engines=engines, languages=languages if ready else [],
            processing_modes=["extract_text", "preserve_existing_text"] if ready else ["extract_text"],
            output_formats=["pdf_searchable", "pdfa"] if pdfa_ready else ["pdf_searchable"],
            compression_profiles=list(HOMOLOGATED_COMPRESSION) if ready else ["none"],
            fallback_policies=["disabled", "if_low_quality"] if ready and fallback_ready and languages == ["eng", "por"] else ["disabled"],
            fallback_language_sets=[["eng", "por"]] if fallback_ready else [],
            defaults=ProcessingOptions().resolved(), limits={}, warnings=warnings,
        )

    @staticmethod
    def _probe_verapdf() -> EngineCapability:
        from app.services.pdfa_validator import (REASON_NOT_CONFIGURED, REASON_UNSUPPORTED_PLATFORM,
                                                 platform_supported)
        if not platform_supported():
            return EngineCapability(available=False, reasons=[REASON_UNSUPPORTED_PLATFORM])
        if settings.verapdf_executable is None:
            return EngineCapability(available=False, reasons=[REASON_NOT_CONFIGURED])
        version = version_from(command_output([str(settings.verapdf_executable), "--version"], timeout=30))
        return EngineCapability(available=version is not None, version=version,
                                reasons=[] if version else ["missing_or_failed_probe"])

    def validate(self, options: ResolvedOptions, *, refresh=False):
        from app.application import ApplicationError
        if options.processing_mode == "extract_text":
            if (options.languages or options.output_format != "pdf_searchable"
                    or options.compression_profile != "none" or options.fallback_policy != "disabled"):
                raise ApplicationError(422, "unsupported_option_combination", "Text extraction requires no languages, searchable PDF, no compression and no fallback.")
            return
        capabilities = self.snapshot(refresh=refresh)
        if not capabilities.engines["ocrmypdf"].available:
            raise ApplicationError(503, "ocr_unavailable", "OCR dependencies are unavailable.")
        if not options.languages or not set(options.languages) <= set(capabilities.languages):
            raise ApplicationError(422, "language_unavailable", "Requested OCR language is not installed or supported.")
        if options.compression_profile not in capabilities.compression_profiles:
            # Also refuses, at the worker's revalidation, jobs admitted before a preset was withdrawn.
            raise ApplicationError(422, "compression_profile_unavailable",
                                   "Requested compression preset is not homologated or not available.")
        if options.output_format == "pdfa" and "pdfa" not in capabilities.output_formats:
            # Checked before admission and again by the worker (refresh=True) before processing.
            raise ApplicationError(503, "pdfa_validator_unavailable", "PDF/A output requires an available veraPDF validator.")
        if options.fallback_policy == "if_low_quality":
            if options.languages != ("eng", "por"):
                raise ApplicationError(422, "unsupported_option_combination", "Fallback requires the eng and por language pair.")
            if not capabilities.engines["easyocr"].available:
                raise ApplicationError(503, "fallback_unavailable", "Fallback dependencies or local models are unavailable.")
        return capabilities

    def resolve(self, requested: ProcessingOptions) -> EffectiveOptions:
        resolved = requested.resolved()
        self.validate(resolved)
        return EffectiveOptions(**resolved.model_dump(), engine_jobs=settings.ocr_engine_jobs,
                                subprocess_timeout_seconds=settings.ocr_subprocess_timeout_seconds,
                                quality_min_text=settings.quality_min_text,
                                quality_valid_ratio_threshold=settings.quality_valid_ratio_threshold,
                                fallback_character_tolerance=settings.fallback_character_tolerance,
                                compression_preset_version=COMPRESSION_PRESET_VERSION,
                                pdfa_validation_timeout_seconds=(settings.verapdf_timeout_seconds
                                                                 if resolved.output_format == "pdfa" else None))


_service = CapabilityService()


def get_capability_service():
    return _service
