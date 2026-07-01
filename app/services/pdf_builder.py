from __future__ import annotations

import shutil
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas


class PdfBuilder:
    def build(
        self,
        original_pdf_path: Path,
        output_pdf_path: Path,
        text: str,
        base_pdf_path: Path | None = None,
    ) -> Path:
        if base_pdf_path and base_pdf_path.exists():
            shutil.copyfile(base_pdf_path, output_pdf_path)
            return output_pdf_path

        # Fallback path: overlay extracted text onto the original PDF pages
        # so the visual appearance and page count are preserved.
        return self._overlay_text_on_original(original_pdf_path, output_pdf_path, text)

    def _overlay_text_on_original(
        self, original_pdf_path: Path, output_pdf_path: Path, text: str
    ) -> Path:
        reader = PdfReader(str(original_pdf_path))
        writer = PdfWriter()
        num_pages = len(reader.pages)

        # Split text roughly across pages
        lines = text.splitlines() if text.strip() else []
        chunks = self._split_lines_across_pages(lines, num_pages)

        for page_index, page in enumerate(reader.pages):
            page_text = chunks[page_index] if page_index < len(chunks) else []
            if page_text:
                overlay = self._create_text_overlay(page, page_text)
                page.merge_page(overlay)
            writer.add_page(page)

        with open(output_pdf_path, "wb") as out_file:
            writer.write(out_file)
        return output_pdf_path

    @staticmethod
    def _split_lines_across_pages(
        lines: list[str], num_pages: int
    ) -> list[list[str]]:
        if num_pages <= 0 or not lines:
            return [[] for _ in range(max(num_pages, 0))]
        per_page = max(1, len(lines) // num_pages)
        chunks: list[list[str]] = []
        for i in range(num_pages):
            start = i * per_page
            # Last page gets all remaining lines
            end = len(lines) if i == num_pages - 1 else start + per_page
            chunks.append(lines[start:end])
        return chunks

    @staticmethod
    def _create_text_overlay(page, lines: list[str]):
        """Create a transparent text overlay matching the page dimensions.

        The text is rendered with PDF render mode 3 (invisible) so it does
        not obscure the original scan but remains selectable/searchable.
        We use a small but extractable font size (8pt) so that PDF text
        extraction libraries can reliably retrieve the embedded text.
        """
        media = page.mediabox
        page_w = float(media.width)
        page_h = float(media.height)

        buf = BytesIO()
        c = canvas.Canvas(buf, pagesize=(page_w, page_h))
        font_size = 8  # small but extractable by PDF text tools

        text_object = c.beginText(5, page_h - 10)
        text_object.setFont("Helvetica", font_size)
        text_object.setTextRenderMode(3)  # invisible text (searchable but not visible)

        for line in lines:
            if text_object.getY() < 10:
                break
            text_object.textLine(line)

        c.drawText(text_object)
        c.save()
        buf.seek(0)

        from pypdf import PdfReader as _PdfReader
        overlay_reader = _PdfReader(buf)
        return overlay_reader.pages[0]
