from __future__ import annotations

import shutil
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas


class PdfBuilder:
    def build(self, original_pdf_path: Path, output_pdf_path: Path, text: str, base_pdf_path: Path | None = None) -> Path:
        if base_pdf_path and base_pdf_path.exists():
            shutil.copyfile(base_pdf_path, output_pdf_path)
            return output_pdf_path

        pdf = canvas.Canvas(str(output_pdf_path), pagesize=A4)
        width, height = A4
        margin = 50
        y = height - margin
        pdf.setFont("Helvetica", 10)

        for paragraph in text.splitlines() or [""]:
            words = paragraph.split() or [""]
            line = ""
            for word in words:
                candidate = f"{line} {word}".strip()
                if stringWidth(candidate, "Helvetica", 10) <= width - (2 * margin):
                    line = candidate
                    continue
                pdf.drawString(margin, y, line)
                y -= 14
                line = word
                if y <= margin:
                    pdf.showPage()
                    pdf.setFont("Helvetica", 10)
                    y = height - margin
            if line:
                pdf.drawString(margin, y, line)
                y -= 14
            if y <= margin:
                pdf.showPage()
                pdf.setFont("Helvetica", 10)
                y = height - margin

        pdf.save()
        return output_pdf_path
