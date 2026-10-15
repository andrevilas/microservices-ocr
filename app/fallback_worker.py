"""Internal isolated EasyOCR entrypoint. Never exposed as a client command."""
import json
from pathlib import Path
import sys
from app.services.easyocr_service import FallbackOcrService

def main():
    source, destination = map(Path, sys.argv[1:3])
    pages = FallbackOcrService().process_pages(source)
    destination.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")

if __name__ == "__main__":
    main()
