from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

from app.config import settings
from app.options import EffectiveOptions
from app.services.cancellation import active, run_process


def preset_arguments(profile: str, output_format: str) -> list[str]:
    """Fixed OCRmyPDF 16.x arguments per preset. No JBIG2 lossy, no rasterization, no resampling."""
    pdfa = output_format == "pdfa"
    args = ["--output-type", "pdfa-2" if pdfa else "pdf"]
    if profile == "economic":  # withdrawn (not homologated): kept to document what earlier jobs ran
        args += ["--optimize", "2", "--jpeg-quality", "75"]
    elif profile == "balanced":  # lossless optimizer; Ghostscript PDF/A conversion may still re-encode JPEG
        args += ["--optimize", "1"]
    else:  # none / preservation
        args += ["--optimize", "0"]
    if pdfa and profile == "preservation":
        args += ["--pdfa-image-compression", "lossless"]
    return args


class OcrmypdfService:
    @staticmethod
    def is_available() -> bool:
        return importlib.util.find_spec("ocrmypdf") is not None

    def run_ocr(self, input_pdf_path: Path, output_pdf_path: Path, options: EffectiveOptions | None = None) -> None:
        if options is not None:
            self._run(["--skip-text", "--output-type", "pdf", "--optimize", "0",
                       "--language", "+".join(options.languages), "--jobs", str(options.engine_jobs),
                       str(input_pdf_path), str(output_pdf_path)], timeout=options.subprocess_timeout_seconds)
            return
        self._run(
            [
                "--skip-text",
                "--output-type",
                "pdf",
                "--optimize",
                "0",
                str(input_pdf_path),
                str(output_pdf_path),
            ]
        )

    def optimize_pdfa(self, input_pdf_path: Path, output_pdf_path: Path,
                      options: EffectiveOptions | None = None) -> dict | None:
        """Final conversion/compression. With options, returns the effective preset arguments."""
        if options is not None:
            preset = preset_arguments(options.compression_profile, options.output_format)
            common = ["--skip-text", "--language", "+".join(options.languages), "--jobs", str(options.engine_jobs)]
            self._run([*common, *preset, str(input_pdf_path), str(output_pdf_path)],
                      timeout=options.subprocess_timeout_seconds)
            # No homologated preset needs pngquant, so there is no fallback to record any more.
            return {"arguments": preset, "fallback": None}
        try:
            self._run(self._optimize_args(input_pdf_path, output_pdf_path, settings.final_pdf_optimize_level))
        except RuntimeError as exc:
            if settings.final_pdf_optimize_level > 1 and "pngquant" in str(exc).lower():
                self._run(self._optimize_args(input_pdf_path, output_pdf_path, 1))
                return
            raise

    @staticmethod
    def _optimize_args(input_pdf_path: Path, output_pdf_path: Path, optimize_level: int) -> list[str]:
        return [
            "--skip-text",
            "--output-type",
            settings.final_output_type,
            "--optimize",
            str(optimize_level),
            "--pdfa-image-compression",
            settings.final_pdfa_image_compression,
            "--jpeg-quality",
            str(settings.final_pdf_jpeg_quality),
            str(input_pdf_path),
            str(output_pdf_path),
        ]

    def _run(self, args: list[str], *, timeout: int | None = None) -> None:
        try:
            if active():
                run_process([sys.executable, "-m", "ocrmypdf", *args],
                            timeout=timeout if timeout is not None else settings.ocr_subprocess_timeout_seconds)
                return
            subprocess.run(
                [sys.executable, "-m", "ocrmypdf", *args],
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout if timeout is not None else settings.ocr_subprocess_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "OCR subprocess timed out."
            ) from exc
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(exc.stderr.strip() or str(exc)) from exc
