from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from pypdf import PdfReader


class PrimaryOcrService:
    def process(self, input_pdf_path: Path, output_pdf_path: Path) -> str:
        if shutil.which("ocrmypdf"):
            try:
                subprocess.run(
                    [
                        "ocrmypdf",
                        "--skip-text",
                        "--optimize",
                        "0",
                        str(input_pdf_path),
                        str(output_pdf_path),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(exc.stderr.strip() or str(exc)) from exc
        else:
            output_pdf_path.write_bytes(input_pdf_path.read_bytes())
        return self.extract_text(output_pdf_path)

    @staticmethod
    def extract_text(pdf_path: Path) -> str:
        reader = PdfReader(str(pdf_path))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(page.strip() for page in pages if page.strip())
