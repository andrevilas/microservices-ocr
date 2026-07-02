from __future__ import annotations

import logging
from queue import Empty, Queue
from threading import Event, Lock, Thread

from app.config import settings
from app.services.ocr_orchestrator import OcrOrchestrator
from app.services.storage_service import JobStore, get_job_store

logger = logging.getLogger(__name__)


class JobQueueProcessor:
    def __init__(self, job_store: JobStore, worker_count: int | None = None) -> None:
        self.job_store = job_store
        self.worker_count = max(1, worker_count or settings.job_worker_concurrency)
        self.orchestrator = OcrOrchestrator(job_store=job_store)
        self._queue: Queue[str] = Queue()
        self._queued_ids: set[str] = set()
        self._threads: list[Thread] = []
        self._lock = Lock()
        self._stop_event = Event()
        self._started = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._stop_event.clear()

            # --- Cleanup expired jobs before recovery ----------------------
            try:
                cleaned = self.job_store.cleanup_expired()
                if cleaned:
                    logger.info("Startup cleanup: removed files for %d expired jobs", len(cleaned))
            except Exception:
                logger.exception("Startup cleanup failed, continuing")

            # --- Recovery: reset processing -> queued ----------------------
            for job in self.job_store.get_processing_jobs():
                logger.info("Recovery: job %s was processing, resetting to queued", job.job_id)
                self.job_store.update(
                    job.job_id,
                    status="queued",
                    progress_percent=0,
                    error="Recovered after restart: was processing when server stopped.",
                )

            # --- Recovery: re-enqueue persisted queued jobs ----------------
            for job in self.job_store.get_queued_jobs():
                if job.job_id not in self._queued_ids:
                    logger.info("Recovery: re-enqueuing persisted queued job %s", job.job_id)
                    self._queued_ids.add(job.job_id)
                    self._queue.put(job.job_id)

            self._threads = [
                Thread(target=self._worker_loop, name=f"ocr-worker-{index + 1}", daemon=True)
                for index in range(self.worker_count)
            ]
            for thread in self._threads:
                thread.start()
            self._started = True

    def stop(self) -> None:
        timeout = settings.worker_shutdown_timeout_seconds
        with self._lock:
            if not self._started:
                return
            self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        # Mark any jobs still processing as failed (shutdown)
        for job in self.job_store.get_processing_jobs():
            logger.warning("Shutdown: job %s still processing, marking as failed", job.job_id)
            try:
                self.job_store.update(
                    job.job_id,
                    status="failed",
                    progress_percent=100,
                    error="Server shutdown while job was processing.",
                )
            except Exception:
                logger.exception("Failed to mark job %s during shutdown", job.job_id)
        with self._lock:
            self._threads = []
            self._started = False

    def enqueue(self, job_id: str) -> None:
        self.start()
        with self._lock:
            if job_id in self._queued_ids:
                return
            self._queued_ids.add(job_id)
        self._queue.put(job_id)

    def clear_pending_jobs(self, owner_user_id: int | None = None) -> tuple[int, int]:
        cleared_count = 0
        retained: list[str] = []
        while True:
            try:
                job_id = self._queue.get_nowait()
            except Empty:
                break
            job = self.job_store.get(job_id)
            if job and job.status == "queued" and (owner_user_id is None or job.owner_user_id == owner_user_id):
                self.job_store.update(
                    job_id,
                    status="canceled",
                    progress_percent=0,
                    error="Job removido da fila pelo operador.",
                )
                with self._lock:
                    self._queued_ids.discard(job_id)
                cleared_count += 1
            else:
                retained.append(job_id)
            self._queue.task_done()

        for job_id in retained:
            self._queue.put(job_id)

        processing_count = sum(
            1
            for job in self.job_store.list_for_owner(owner_user_id)
            if job.status == "processing"
        )
        return cleared_count, processing_count

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except Empty:
                continue
            try:
                job = self.job_store.get(job_id)
                if job and job.status == "canceled":
                    continue
                self.orchestrator.process_job(job_id)
            finally:
                with self._lock:
                    self._queued_ids.discard(job_id)
                self._queue.task_done()


_job_queue_processor = JobQueueProcessor(job_store=get_job_store())


def get_job_queue_processor() -> JobQueueProcessor:
    return _job_queue_processor
