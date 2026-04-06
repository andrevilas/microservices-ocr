from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

from app.config import settings


class OcrmypdfService:
    @staticmethod
    def is_available() -> bool:
        return importlib.util.find_spec("ocrmypdf") is not None

    def run_ocr(self, input_pdf_path: Path, output_pdf_path: Path) -> None:
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

    def optimize_pdfa(self, input_pdf_path: Path, output_pdf_path: Path) -> None:
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

    def _run(self, args: list[str]) -> None:
        try:
            subprocess.run(
                [sys.executable, "-m", "ocrmypdf", *args],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(exc.stderr.strip() or str(exc)) from exc
