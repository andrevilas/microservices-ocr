"""Synthetic crash point for the subprocess integration test; never a deployment entrypoint."""
import os
from pathlib import Path
from threading import Event

if os.environ.get("OCR_TEST_PAUSE") == "1":
    from app.services.ocr_orchestrator import OcrOrchestrator

    def paused(self, job_id):
        self.job_store.update(job_id, status="processing")
        Path(os.environ["OCR_TEST_MARKER"]).write_text(job_id)
        Event().wait(120)

    OcrOrchestrator.process_job = paused

if os.environ.get("OCR_TEST_EXPORT_PAUSE") == "1":
    from app.services.export_service import ExportService

    def paused_export(self, *args):
        Path(os.environ["OCR_TEST_MARKER"] + "-export").write_text("processing")
        Event().wait(120)

    ExportService._build = paused_export

if os.environ.get("OCR_TEST_CONTROL_PAUSE") == "1":
    import sys
    from app.services.tesseract_service import PrimaryOcrService
    from app.services.cancellation import run_process
    original_pages = PrimaryOcrService.process_pages

    def pause_once(self, *args, **kwargs):
        marker = Path(os.environ["OCR_TEST_MARKER"] + "-control")
        if not marker.exists():
            run_process([sys.executable, "-c",
                         "import os,time;from pathlib import Path;Path(" + repr(str(marker)) + ").write_text(str(os.getpid()));time.sleep(60)"],
                        timeout=90)
        return original_pages(self, *args, **kwargs)

    PrimaryOcrService.process_pages = pause_once

from app.main import app  # noqa: E402
