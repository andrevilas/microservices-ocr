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

from app.main import app  # noqa: E402
