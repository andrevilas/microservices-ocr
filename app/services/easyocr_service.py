from __future__ import annotations

import threading
import importlib.util
import json
import sys
from pathlib import Path

from app.utils.image_preprocessing import preprocess_image


class FallbackOcrService:
    def __init__(self) -> None:
        self._reader = None
        self._reader_lock = threading.Lock()

    @staticmethod
    def is_available() -> bool:
        try:
            return all(importlib.util.find_spec(name) is not None for name in ("easyocr", "pdf2image"))
        except (ImportError, ValueError):
            return False

    def _get_reader(self):
        """Lazy, thread-safe singleton Reader per instance."""
        if self._reader is None:
            with self._reader_lock:
                if self._reader is None:
                    import easyocr
                    self._reader = easyocr.Reader(["pt", "en"], gpu=False, download_enabled=False)
        return self._reader

    def process(self, input_pdf_path: Path) -> str:
        return "\n".join(self.process_pages(input_pdf_path)).strip()

    def process_pages(self, input_pdf_path: Path) -> list[str]:
        from app.services.cancellation import active, run_process, checkpoint
        if active():
            from app.config import settings
            destination = input_pdf_path.parent / "fallback-pages.json"
            try:
                run_process([sys.executable, "-m", "app.fallback_worker", str(input_pdf_path), str(destination)],
                            timeout=settings.ocr_subprocess_timeout_seconds)
                checkpoint()
                return json.loads(destination.read_text(encoding="utf-8"))
            finally:
                destination.unlink(missing_ok=True)
        if not self.is_available():
            raise RuntimeError("Fallback OCR unavailable.")

        from pdf2image import convert_from_path

        images = convert_from_path(str(input_pdf_path))
        reader = self._get_reader()
        chunks: list[str] = []
        preprocessing_dir = input_pdf_path.parent / "preprocessed-pages"
        preprocessing_dir.mkdir(parents=True, exist_ok=True)

        for index, image in enumerate(images, start=1):
            page_path = preprocessing_dir / f"page-{index}.png"
            image.save(page_path)
            processed_path = preprocess_image(page_path)
            # Single readtext call per page: prefer preprocessed image
            ocr_input = str(processed_path) if processed_path.exists() else image
            results = reader.readtext(ocr_input)
            chunks.append("\n".join(item[1] for item in results if len(item) > 1).strip())
        return chunks
