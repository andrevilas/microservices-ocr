from __future__ import annotations

from pathlib import Path

from app.utils.image_preprocessing import preprocess_image


class FallbackOcrService:
    @staticmethod
    def is_available() -> bool:
        try:
            import easyocr  # noqa: F401
            from pdf2image import convert_from_path  # noqa: F401
        except ImportError:
            return False
        return True

    def process(self, input_pdf_path: Path) -> str:
        if not self.is_available():
            return ""

        import easyocr
        from pdf2image import convert_from_path

        images = convert_from_path(str(input_pdf_path))
        reader = easyocr.Reader(["pt", "en"], gpu=False)
        chunks: list[str] = []
        preprocessing_dir = input_pdf_path.parent / "preprocessed-pages"
        preprocessing_dir.mkdir(parents=True, exist_ok=True)

        for index, image in enumerate(images, start=1):
            page_path = preprocessing_dir / f"page-{index}.png"
            image.save(page_path)
            processed_path = preprocess_image(page_path)
            results = reader.readtext(image)
            if processed_path.exists():
                results = reader.readtext(str(processed_path))
            chunks.extend(item[1] for item in results if len(item) > 1)
        return "\n".join(chunks).strip()
