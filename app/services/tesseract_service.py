from __future__ import annotations

import shutil
from pathlib import Path

from pypdf import PdfReader

from app.services.ocrmypdf_service import OcrmypdfService


class PrimaryOcrService:
    def __init__(self, ocrmypdf_service: OcrmypdfService | None = None) -> None:
        self.ocrmypdf_service = ocrmypdf_service or OcrmypdfService()

    def process(self, input_pdf_path: Path, output_pdf_path: Path) -> str:
        if self.ocrmypdf_service.is_available():
            self.ocrmypdf_service.run_ocr(input_pdf_path, output_pdf_path)
        else:
            output_pdf_path.write_bytes(input_pdf_path.read_bytes())
        return self.extract_text(output_pdf_path)

    @staticmethod
    def extract_text(pdf_path: Path) -> str:
        reader = PdfReader(str(pdf_path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(page.strip() for page in pages if page.strip())
