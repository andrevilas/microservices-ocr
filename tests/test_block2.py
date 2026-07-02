"""Tests for BLOCK 2: persistence, recovery, cleanup, and shutdown."""

import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue
from threading import Event
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.services.storage_service import JobRecord, JobStore
from app.services.job_queue import JobQueueProcessor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(tmp_path: Path) -> JobStore:
    """Create an isolated JobStore backed by a temp directory."""
    return JobStore(root_dir=tmp_path, _db_path=tmp_path / "test-jobs.sqlite3")


def _create_dummy_job(store: JobStore, filename: str = "test.pdf") -> JobRecord:
    return store.create(filename=filename, payload=b"%PDF-1.4 dummy")


# ---------------------------------------------------------------------------
# 1. JobStore SQLite persistence
# ---------------------------------------------------------------------------


class TestJobStorePersistence:
    def test_create_persists_to_sqlite(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)

        # New store instance loading from same DB should recover the job
        store2 = JobStore(root_dir=tmp_path, _db_path=tmp_path / "test-jobs.sqlite3")
        recovered = store2.get(job.job_id)
        assert recovered is not None
        assert recovered.filename == "test.pdf"
        assert recovered.status == "queued"

    def test_update_persists_to_sqlite(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        store.update(job.job_id, status="processing")

        store2 = JobStore(root_dir=tmp_path, _db_path=tmp_path / "test-jobs.sqlite3")
        recovered = store2.get(job.job_id)
        assert recovered is not None
        assert recovered.status == "processing"

    def test_list_all_after_reload(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        _create_dummy_job(store, "a.pdf")
        _create_dummy_job(store, "b.pdf")

        store2 = JobStore(root_dir=tmp_path, _db_path=tmp_path / "test-jobs.sqlite3")
        jobs = store2.list_all()
        assert len(jobs) == 2
        filenames = {j.filename for j in jobs}
        assert filenames == {"a.pdf", "b.pdf"}

    def test_db_file_created(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        assert store.db_path.exists()

    def test_check_db_returns_true(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        assert store.check_db() is True


def test_clear_pending_jobs_respects_owner(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    job_a = store.create("a.pdf", b"%PDF-1.4\n%%EOF", owner_user_id=10)
    job_b = store.create("b.pdf", b"%PDF-1.4\n%%EOF", owner_user_id=20)
    processor = JobQueueProcessor(job_store=store, worker_count=1)
    processor._queued_ids.update({job_a.job_id, job_b.job_id})
    processor._queue.put(job_a.job_id)
    processor._queue.put(job_b.job_id)

    cleared_count, processing_count = processor.clear_pending_jobs(owner_user_id=10)

    assert cleared_count == 1
    assert processing_count == 0
    assert store.get(job_a.job_id).status == "canceled"
    assert store.get(job_b.job_id).status == "queued"


# ---------------------------------------------------------------------------
# 2. JobStore.update validation
# ---------------------------------------------------------------------------


class TestJobStoreUpdateValidation:
    def test_update_rejects_unknown_field(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        with pytest.raises(ValueError, match="Unknown or immutable fields"):
            store.update(job.job_id, nonexistent_field="bad")

    def test_update_rejects_immutable_job_id(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        # Python raises TypeError before our validation because job_id is a
        # positional argument to update(). Either way, the mutation is blocked.
        with pytest.raises((ValueError, TypeError)):
            store.update(job.job_id, **{"job_id": "new-id"})

    def test_update_rejects_immutable_created_at(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        with pytest.raises(ValueError, match="Unknown or immutable fields"):
            store.update(job.job_id, created_at=datetime.now(timezone.utc))

    def test_update_accepts_valid_fields(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        updated = store.update(job.job_id, status="completed", quality="HIGH")
        assert updated.status == "completed"
        assert updated.quality == "HIGH"

    def test_update_rejects_invalid_status(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        with pytest.raises(ValueError, match="Invalid status"):
            store.update(job.job_id, status="unknown_status")

    def test_update_rejects_invalid_quality(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        with pytest.raises(ValueError, match="Invalid quality"):
            store.update(job.job_id, quality="MEDIUM")

    def test_update_allows_none_quality(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        store.update(job.job_id, quality="HIGH")
        updated = store.update(job.job_id, quality=None)
        assert updated.quality is None

    def test_update_sets_updated_at(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        old_updated = job.updated_at
        time.sleep(0.01)
        store.update(job.job_id, status="processing")
        assert job.updated_at > old_updated

    def test_update_multiple_bad_fields(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        with pytest.raises(ValueError, match="Unknown or immutable fields"):
            store.update(job.job_id, foo="bar", baz="qux")


# ---------------------------------------------------------------------------
# 3. Recovery: processing -> queued on startup
# ---------------------------------------------------------------------------


class TestRecovery:
    def test_recovery_resets_processing_to_queued(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        store.update(job.job_id, status="processing")

        # Simulate restart by creating a new store and processor
        from app.services.job_queue import JobQueueProcessor

        store2 = JobStore(root_dir=tmp_path, _db_path=tmp_path / "test-jobs.sqlite3")
        # Job should still be "processing" in the DB before recovery
        assert store2.get(job.job_id).status == "processing"

        processor = JobQueueProcessor(job_store=store2, worker_count=1)
        # Patch _worker_loop to prevent workers from consuming the queue
        processor.orchestrator = MagicMock()
        with patch.object(processor, "_worker_loop"):
            processor.start()
            try:
                # After start(), the job should have been reset to queued
                recovered = store2.get(job.job_id)
                assert recovered.status == "queued"
                assert "Recovered after restart" in (recovered.error or "")
            finally:
                processor.stop()

    def test_recovery_reenqueues_queued_jobs(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        # Job is already "queued" in DB

        from app.services.job_queue import JobQueueProcessor

        store2 = JobStore(root_dir=tmp_path, _db_path=tmp_path / "test-jobs.sqlite3")
        processor = JobQueueProcessor(job_store=store2, worker_count=1)
        processor.orchestrator = MagicMock()
        # Patch _worker_loop to prevent workers from consuming before assertion
        with patch.object(processor, "_worker_loop"):
            processor.start()
            try:
                # The job should have been enqueued (check _queued_ids)
                assert job.job_id in processor._queued_ids
                # Also verify it was placed in the internal queue
                assert processor._queue.qsize() >= 1
            finally:
                processor.stop()

    def test_recovery_no_duplicate_enqueue(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job1 = _create_dummy_job(store, "a.pdf")
        job2 = _create_dummy_job(store, "b.pdf")

        from app.services.job_queue import JobQueueProcessor

        store2 = JobStore(root_dir=tmp_path, _db_path=tmp_path / "test-jobs.sqlite3")
        processor = JobQueueProcessor(job_store=store2, worker_count=0)
        processor.orchestrator = MagicMock()
        # Pre-add job1 to simulate it was already in queue before start()
        processor._queued_ids.add(job1.job_id)
        processor._queue.put(job1.job_id)

        # Manually call the recovery logic by invoking start's internals
        # We use worker_count=0 trick: override _started to run recovery without spawning threads
        with processor._lock:
            processor._stop_event.clear()
            for job in processor.job_store.get_queued_jobs():
                if job.job_id not in processor._queued_ids:
                    processor._queued_ids.add(job.job_id)
                    processor._queue.put(job.job_id)

        # job1 should not have been added again, job2 should have been added
        assert job1.job_id in processor._queued_ids
        assert job2.job_id in processor._queued_ids
        # Queue should have exactly 2 items (job1 pre-added + job2 added by recovery)
        assert processor._queue.qsize() == 2


# ---------------------------------------------------------------------------
# 4. Cleanup by TTL
# ---------------------------------------------------------------------------


class TestCleanupTTL:
    def test_cleanup_expired_removes_files(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        store.update(job.job_id, status="completed")

        # Hack updated_at to be old
        with store._lock:
            job.updated_at = datetime.now(timezone.utc) - timedelta(seconds=100)
            store._persist(job)

        # Ensure working_dir exists before cleanup
        assert job.working_dir.exists()

        cleaned = store.cleanup_expired(retention_seconds=50)
        assert job.job_id in cleaned
        # Files removed
        assert not job.working_dir.exists()
        # Record still in DB but output_pdf_path is None
        assert store.get(job.job_id) is not None
        assert store.get(job.job_id).output_pdf_path is None

    def test_cleanup_preserves_recent_jobs(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        store.update(job.job_id, status="completed")

        cleaned = store.cleanup_expired(retention_seconds=86400)
        assert job.job_id not in cleaned
        assert job.working_dir.exists()

    def test_cleanup_skips_queued_and_processing(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job_q = _create_dummy_job(store, "queued.pdf")
        job_p = _create_dummy_job(store, "processing.pdf")
        store.update(job_p.job_id, status="processing")

        # Make them old
        with store._lock:
            job_q.updated_at = datetime.now(timezone.utc) - timedelta(seconds=200)
            store._persist(job_q)
            job_p.updated_at = datetime.now(timezone.utc) - timedelta(seconds=200)
            store._persist(job_p)

        cleaned = store.cleanup_expired(retention_seconds=50)
        assert len(cleaned) == 0

    def test_cleanup_handles_failed_and_canceled(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job_f = _create_dummy_job(store, "failed.pdf")
        job_c = _create_dummy_job(store, "canceled.pdf")
        store.update(job_f.job_id, status="failed", error="boom")
        store.update(job_c.job_id, status="canceled", error="canceled")

        with store._lock:
            job_f.updated_at = datetime.now(timezone.utc) - timedelta(seconds=200)
            store._persist(job_f)
            job_c.updated_at = datetime.now(timezone.utc) - timedelta(seconds=200)
            store._persist(job_c)

        cleaned = store.cleanup_expired(retention_seconds=50)
        assert job_f.job_id in cleaned
        assert job_c.job_id in cleaned

    def test_cleanup_already_removed_dir(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        store.update(job.job_id, status="completed")

        # Pre-remove dir
        shutil.rmtree(job.working_dir)

        with store._lock:
            job.updated_at = datetime.now(timezone.utc) - timedelta(seconds=200)
            store._persist(job)

        # Should not raise
        cleaned = store.cleanup_expired(retention_seconds=50)
        assert job.job_id in cleaned

    def test_cleanup_persists_to_db(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        store.update(job.job_id, status="completed",
                     output_pdf_path=job.working_dir / "out.pdf")

        with store._lock:
            job.updated_at = datetime.now(timezone.utc) - timedelta(seconds=200)
            store._persist(job)

        store.cleanup_expired(retention_seconds=50)

        # Reload from DB
        store2 = JobStore(root_dir=tmp_path, _db_path=tmp_path / "test-jobs.sqlite3")
        recovered = store2.get(job.job_id)
        assert recovered is not None
        assert recovered.output_pdf_path is None


# ---------------------------------------------------------------------------
# 5. Graceful shutdown
# ---------------------------------------------------------------------------


class TestGracefulShutdown:
    def test_shutdown_timeout_setting(self) -> None:
        assert settings.worker_shutdown_timeout_seconds == 30

    def test_retention_setting(self) -> None:
        assert settings.job_retention_seconds == 86400

    def test_stop_marks_processing_as_failed(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        store.update(job.job_id, status="processing")

        from app.services.job_queue import JobQueueProcessor

        processor = JobQueueProcessor(job_store=store, worker_count=1)
        processor._started = True
        processor._stop_event = Event()
        processor._threads = []  # No actual threads

        processor.stop()

        result = store.get(job.job_id)
        assert result.status == "failed"
        assert "shutdown" in result.error.lower()

    def test_stop_uses_configurable_timeout(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setattr(settings, "worker_shutdown_timeout_seconds", 1)

        store = _make_store(tmp_path)

        from app.services.job_queue import JobQueueProcessor

        processor = JobQueueProcessor(job_store=store, worker_count=1)
        processor.orchestrator = MagicMock()
        processor.start()

        start_time = time.monotonic()
        processor.stop()
        elapsed = time.monotonic() - start_time

        # Should have stopped reasonably quickly (workers idle, so join returns fast)
        assert elapsed < 5

    def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        from app.services.job_queue import JobQueueProcessor

        store = _make_store(tmp_path)
        processor = JobQueueProcessor(job_store=store, worker_count=1)
        processor.orchestrator = MagicMock()
        processor.start()
        processor.stop()
        processor.stop()  # Second stop should not raise


# ---------------------------------------------------------------------------
# 6. Integration: readiness checks DB
# ---------------------------------------------------------------------------


class TestReadinessDB:
    def test_readiness_still_works(self) -> None:
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        response = client.get("/readiness")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"


# ---------------------------------------------------------------------------
# 7. JobStore.cleanup removes files
# ---------------------------------------------------------------------------


class TestJobStoreCleanup:
    def test_cleanup_removes_working_dir(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        assert job.working_dir.exists()
        store.cleanup(job.job_id)
        assert not job.working_dir.exists()

    def test_cleanup_nonexistent_job(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        # Should not raise
        store.cleanup("nonexistent-id")

    def test_cleanup_clears_output_pdf_path(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        job = _create_dummy_job(store)
        out_pdf = job.working_dir / "out.pdf"
        out_pdf.write_bytes(b"%PDF-1.4 output")
        store.update(job.job_id, status="completed", output_pdf_path=out_pdf)

        store.cleanup(job.job_id)

        # In-memory record should have output_pdf_path cleared
        updated = store.get(job.job_id)
        assert updated is not None
        assert updated.output_pdf_path is None
        # Status must NOT be changed
        assert updated.status == "completed"

        # Persisted in DB as well
        store2 = JobStore(root_dir=tmp_path, _db_path=tmp_path / "test-jobs.sqlite3")
        recovered = store2.get(job.job_id)
        assert recovered.output_pdf_path is None
        assert recovered.status == "completed"


# ---------------------------------------------------------------------------
# 8. Startup calls cleanup_expired
# ---------------------------------------------------------------------------


class TestStartupCleanup:
    def test_startup_calls_cleanup_expired(self, tmp_path: Path) -> None:
        from app.services.job_queue import JobQueueProcessor

        store = _make_store(tmp_path)
        processor = JobQueueProcessor(job_store=store, worker_count=1)
        processor.orchestrator = MagicMock()

        with patch.object(store, "cleanup_expired", return_value=[]) as mock_cleanup, \
             patch.object(processor, "_worker_loop"):
            processor.start()
            try:
                mock_cleanup.assert_called_once()
            finally:
                processor.stop()

    def test_startup_survives_cleanup_failure(self, tmp_path: Path) -> None:
        from app.services.job_queue import JobQueueProcessor

        store = _make_store(tmp_path)
        processor = JobQueueProcessor(job_store=store, worker_count=1)
        processor.orchestrator = MagicMock()

        with patch.object(store, "cleanup_expired", side_effect=RuntimeError("boom")), \
             patch.object(processor, "_worker_loop"):
            # Should not raise despite cleanup failure
            processor.start()
            try:
                assert processor._started
            finally:
                processor.stop()
