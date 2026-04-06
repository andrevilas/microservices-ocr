from __future__ import annotations

from queue import Empty, Queue
from threading import Event, Lock, Thread

from app.config import settings
from app.services.ocr_orchestrator import OcrOrchestrator
from app.services.storage_service import JobStore, get_job_store


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
            self._threads = [
                Thread(target=self._worker_loop, name=f"ocr-worker-{index + 1}", daemon=True)
                for index in range(self.worker_count)
            ]
            for thread in self._threads:
                thread.start()
            self._started = True

    def stop(self) -> None:
        with self._lock:
            if not self._started:
                return
            self._stop_event.set()
        for thread in self._threads:
            thread.join(timeout=1)
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

    def clear_pending_jobs(self) -> tuple[int, int]:
        cleared_count = 0
        retained: list[str] = []
        while True:
            try:
                job_id = self._queue.get_nowait()
            except Empty:
                break
            job = self.job_store.get(job_id)
            if job and job.status == "queued":
                self.job_store.update(job_id, status="canceled", error="Job removido da fila pelo operador.")
                with self._lock:
                    self._queued_ids.discard(job_id)
                cleared_count += 1
            else:
                retained.append(job_id)
            self._queue.task_done()

        for job_id in retained:
            self._queue.put(job_id)

        processing_count = sum(1 for job in self.job_store.list_all() if job.status == "processing")
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
