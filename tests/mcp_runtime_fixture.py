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

from app.main import app  # noqa: E402
