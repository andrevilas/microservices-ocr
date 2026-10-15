from __future__ import annotations

import shutil
from pathlib import Path

from pypdf import PdfReader

from app.services.ocrmypdf_service import OcrmypdfService


class PrimaryOcrService:
    def __init__(self, ocrmypdf_service: OcrmypdfService | None = None) -> None:
        self.ocrmypdf_service = ocrmypdf_service or OcrmypdfService()

    def process(self, input_pdf_path: Path, output_pdf_path: Path) -> str:
        return "\n".join(self.process_pages(input_pdf_path, output_pdf_path))

    def process_pages(self, input_pdf_path: Path, output_pdf_path: Path) -> list[str]:
        if self.ocrmypdf_service.is_available():
            self.ocrmypdf_service.run_ocr(input_pdf_path, output_pdf_path)
        else:
            shutil.copyfile(input_pdf_path, output_pdf_path)
        return self.extract_pages(output_pdf_path)

    @staticmethod
    def extract_text(pdf_path: Path) -> str:
        return "\n".join(PrimaryOcrService.extract_pages(pdf_path))

    @staticmethod
    def extract_pages(pdf_path: Path) -> list[str]:
        reader = PdfReader(str(pdf_path))
        return [(page.extract_text() or "").strip() for page in reader.pages]
