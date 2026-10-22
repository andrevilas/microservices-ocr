"""Delivered pending-only recovery with real SQLite, Linux files and leases.

These tests do not inventory storage or establish namespace startup readiness.
"""
import os
import sys
import threading
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import metadata_files as files
from app.services.metadata_recovery import MetadataRecovery, RecoveryCounters
from app.services.metadata_recovery_files import MetadataRecoveryFiles
from app.services.metadata_store import MetadataStore
from app.services.runtime_lease import RuntimeLease
from app.services.storage_service import JobStore
from test_metadata_files import case, snapshot
from test_metadata_storage import (
    assert_error, coordination, make_write, principal, sql_state,
)


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux physical recovery")
MARKER = "SENSITIVE_METADATA_MARKER"


@contextmanager
def runtime(root):
    lease = RuntimeLease(root)
    lease.acquire()
    try:
        yield lease
    finally:
        lease.release()


def collector(c):
    return MetadataRecovery(c.store, MetadataRecoveryFiles(c.jobs.root_dir))


def counts(result, scanned, recovered, skipped=0, failed=0):
    assert type(result) is RecoveryCounters
    assert result == RecoveryCounters(scanned, recovered, skipped, failed)


def memory(c):
    with c.jobs._lock:
        return set(c.store._active_writes), dict(c.jobs._artifact_leases)


def pending(c, w):
    with c.jobs._lock:
        return c.store._pending(c.jobs._get_conn(), w.pending.pending_id)


def charged(c, w):
    actual = pending(c, w)
    assert actual == w.pending
    assert actual.charged_slots == 2
    assert actual.charged_bytes == actual.size_bytes + actual.approval_bytes
    assert actual.approval_slots == 1 and actual.approval_bytes == 1024 * 1024


def new_attempt(c, *, state="cleanup", payload="final", pending_id=None):
    # Never use case's original job to establish maintenance-lease behavior.
    job = c.jobs.create("recovery.pdf", b"%PDF-1.4\nrecovery\n", owner_user_id=1)
    c.jobs.update(job.job_id, status="completed", progress_percent=100)
    with c.jobs.artifact_lease(job.job_id, "user:1") as leased:
        w = make_write(c, job=leased, model=snapshot(leased))
        if pending_id is not None:
            # Deterministic ordering only; authority remains the real pending row.
            with c.store._transaction() as conn:
                conn.execute("UPDATE metadata_pending SET pending_id=? WHERE pending_id=?",
                             (pending_id, w.pending.pending_id))
                w.pending = c.store._pending(conn, pending_id)
                assert w.pending is not None
        w.directory = leased.working_dir / "results" / "metadata"
        w.final = w.directory / (w.pending.revision_id + ".json")
        w.temp = w.directory / ("." + w.pending.pending_id + ".tmp")
        attempt = c.service.prepare(
            leased, pending_id=w.pending.pending_id,
            revision_id=w.pending.revision_id, sha256=w.pending.sha256,
            size_bytes=w.pending.size_bytes,
        )
        if payload in ("final", "both"):
            attempt.publish(w.data)
            assert w.final.read_bytes() == w.data
        if payload in ("temp", "both"):
            w.directory.mkdir(parents=True, exist_ok=True)
            w.temp.write_bytes(w.data[:23])
        # Deliberately leave this interrupted attempt's files for recovery.
    if state == "cleanup":
        with c.store._transaction() as conn:
            c.store._mark_cleanup_locked(conn, w.pending.pending_id)
        w.pending = replace(w.pending, state="cleanup")
    else:
        assert state == "active"
    assert w.job.job_id not in c.jobs._artifact_leases
    charged(c, w)
    return w


def absent(c, w):
    assert pending(c, w) is None
    assert not w.final.exists() and not w.temp.exists()


def public_gone(c, w, reason):
    def access():
        with c.jobs.artifact_lease(w.job.job_id, "user:1"):
            pytest.fail("removed or expired artifacts were admitted")
    assert_error(access, 410, "artifacts_" + reason)


def markers(c, job_id):
    with c.jobs._lock:
        job = c.jobs.jobs[job_id]
        cached = (job.artifacts_expires_at, job.artifacts_removed_at,
                  job.artifacts_purged_at)
        row = c.jobs._get_conn().execute(
            "SELECT artifacts_expires_at,artifacts_removed_at,artifacts_purged_at "
            "FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        assert tuple(row) == tuple(value.isoformat() if value is not None else None
                                  for value in cached)
        return cached


def test_constructor_is_pure_and_limits_validate_before_work(coordination, monkeypatch, tmp_path):
    c = coordination
    physical = MetadataRecoveryFiles(c.jobs.root_dir)
    before = sql_state(c)
    baseline = memory(c)

    def forbidden(*args, **kwargs):
        raise AssertionError(MARKER)

    with monkeypatch.context() as patch:
        patch.setattr(c.store, "_transaction", forbidden)
        patch.setattr(c.jobs, "_get_conn", forbidden)
        for name in ("open", "stat", "lstat", "fstat", "mkdir", "scandir",
                     "listdir", "read", "write", "fsync", "unlink", "close",
                     "statvfs", "fstatvfs"):
            patch.setattr(os, name, forbidden)
        recovery = MetadataRecovery(c.store, physical)
    assert sql_state(c) == before and memory(c) == baseline
    assert_error(lambda: MetadataRecovery(
        c.store, MetadataRecoveryFiles(tmp_path.absolute() / "different-root")))
    assert sql_state(c) == before and memory(c) == baseline

    for value in (True, False, 0, 1025, "1"):
        with monkeypatch.context() as patch:
            patch.setattr(c.store, "_transaction", forbidden)
            patch.setattr(physical, "open", forbidden)
            assert_error(lambda: recovery.collect_cleanup(value),
                         422, "metadata_invalid_input")
        assert sql_state(c) == before and memory(c) == baseline

    attempts = [new_attempt(c, payload="temp") for _ in range(2)]
    result = recovery.collect_cleanup(limit=1)
    counts(result, 1, 1)
    with pytest.raises(FrozenInstanceError):
        result.scanned = 99
    assert sum(pending(c, w) is not None for w in attempts) == 1
    assert memory(c) == baseline
    counts(recovery.collect_cleanup(limit=1024), 1, 1)
    for w in attempts:
        absent(c, w)
    assert memory(c) == baseline


def test_periodic_only_collects_unclaimed_cleanup_and_preserves_foreign_claim(coordination):
    c = coordination
    baseline = memory(c)
    active = new_attempt(c, state="active", payload="both", pending_id="1" * 32)
    claimed = new_attempt(c, payload="both", pending_id="2" * 32)
    eligible = new_attempt(c, payload="both", pending_id="3" * 32)
    recovery = collector(c)
    with c.jobs.artifact_lease(claimed.job.job_id, "user:1"):
        with c.jobs._lock:
            c.store._active_writes.add(claimed.pending.pending_id)
        foreign_baseline = memory(c)
        try:
            counts(recovery.collect_cleanup(), 2, 1, skipped=1)
            absent(c, eligible)
            charged(c, active)
            charged(c, claimed)
            for w in (active, claimed):
                assert w.final.read_bytes() == w.data
                assert w.temp.read_bytes() == w.data[:23]
            assert memory(c) == foreign_baseline
        finally:
            with c.jobs._lock:
                c.store._active_writes.remove(claimed.pending.pending_id)
    assert memory(c) == baseline
    counts(collector(c).collect_cleanup(), 1, 1)
    absent(c, claimed)
    charged(c, active)
    assert active.final.read_bytes() == active.data
    assert active.temp.read_bytes() == active.data[:23]
    assert memory(c) == baseline


def test_startup_reopens_real_store_and_requires_held_matching_runtime_lease(tmp_path, monkeypatch):
    root = tmp_path.absolute() / "restart-jobs"
    external = tmp_path.absolute() / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_bytes(MARKER.encode())
    first = JobStore(root)
    try:
        c = SimpleNamespace(jobs=first, store=MetadataStore(first),
                            service=files.MetadataFiles(root), principal=principal())
        with runtime(root):
            w = new_attempt(c, state="active", payload="both")
        assert not first._artifact_leases and not c.store._active_writes
        keep = root / "unrelated.keep"
        keep.write_bytes(b"keep")
    finally:
        if first._conn is not None:
            first._conn.close()

    # This store belongs to the test, not to case's fixture finalizer.
    reopened = JobStore(root)
    try:
        c = SimpleNamespace(jobs=reopened, store=MetadataStore(reopened))
        recovery = collector(c)
        before = sql_state(c)
        baseline = memory(c)
        released = RuntimeLease(root)
        released.acquire()
        released.release()
        opens = []
        transactions = []

        def forbidden_open(*args, **kwargs):
            opens.append(args)
            raise AssertionError(MARKER)

        def forbidden_transaction(*args, **kwargs):
            transactions.append(args)
            raise AssertionError(MARKER)

        with runtime(external / "wrong-root") as wrong:
            for authority in (None, released, wrong):
                with monkeypatch.context() as patch:
                    patch.setattr(recovery.physical, "open", forbidden_open)
                    patch.setattr(c.store, "_transaction", forbidden_transaction)
                    assert_error(lambda: recovery.recover_pending_startup(authority))
                assert opens == [] and transactions == []
                assert sql_state(c) == before and memory(c) == baseline
                assert w.final.read_bytes() == w.data
                assert w.temp.read_bytes() == w.data[:23]
                assert keep.read_bytes() == b"keep"
                assert sentinel.read_bytes() == MARKER.encode()
            with runtime(root) as held:
                counts(recovery.recover_pending_startup(held), 1, 1)
        absent(c, w)
        assert memory(c) == baseline
        assert keep.read_bytes() == b"keep"
        assert sentinel.read_bytes() == MARKER.encode()
        with reopened.artifact_lease(w.job.job_id, "user:1") as retained:
            assert retained.status == "completed"
    finally:
        if reopened._conn is not None:
            reopened._conn.close()


def test_periodic_budget_crosses_sql_page_boundary_and_reaches_last_pending(coordination):
    c = coordination
    baseline = memory(c)
    attempts = [new_attempt(c, payload="temp") for _ in range(35)]
    ordered = sorted(attempts, key=lambda w: w.pending.pending_id)
    recovery = collector(c)
    counts(recovery.collect_cleanup(limit=33), 33, 33)
    for w in ordered[:33]:
        absent(c, w)
    for w in ordered[33:]:
        charged(c, w)
        assert w.temp.read_bytes() == w.data[:23]
    assert memory(c) == baseline
    counts(recovery.collect_cleanup(limit=33), 2, 2)
    for w in ordered:
        absent(c, w)
    assert pending(c, ordered[-1]) is None
    assert memory(c) == baseline


def test_unlink_failure_is_local_and_fresh_session_retries_real_files(coordination, monkeypatch):
    c = coordination
    baseline = memory(c)
    broken = new_attempt(c, pending_id="1" * 32)
    healthy = new_attempt(c, pending_id="2" * 32)
    original_unlink = os.unlink
    observed = []

    def unlink(path, *args, **kwargs):
        name = Path(os.fsdecode(path)).name
        if name in (broken.final.name, healthy.final.name):
            observed.append(name)
        if name == broken.final.name:
            # Raise before the real syscall: the charged file must survive.
            raise OSError(MARKER)
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", unlink)
        counts(collector(c).collect_cleanup(), 2, 1, failed=1)
    assert observed.index(broken.final.name) < observed.index(healthy.final.name)
    assert broken.final.read_bytes() == broken.data
    charged(c, broken)
    absent(c, healthy)
    assert memory(c) == baseline
    counts(collector(c).collect_cleanup(), 1, 1)
    absent(c, broken)
    assert memory(c) == baseline


def test_durably_purged_pending_uses_sql_reconciliation_without_physical_open(coordination, monkeypatch):
    c = coordination
    baseline = memory(c)
    w = new_attempt(c, payload="both")
    normal_input = c.job.input_pdf_path.read_bytes()
    c.jobs.remove_artifacts(w.job.job_id, "user:1", "user:1")
    before_markers = markers(c, w.job.job_id)
    assert before_markers[1] is not None and before_markers[2] is not None
    assert not w.job.working_dir.exists()
    charged(c, w)
    public_gone(c, w, "deleted")
    recovery = collector(c)
    calls = []
    opens = []
    original_reconcile = c.store.reconcile_purged_job

    def reconcile(job_id, **kwargs):
        calls.append(job_id)
        return original_reconcile(job_id, **kwargs)

    def forbidden_open(*args, **kwargs):
        opens.append(args)
        raise AssertionError(MARKER)

    with monkeypatch.context() as patch:
        patch.setattr(c.store, "reconcile_purged_job", reconcile)
        patch.setattr(recovery.physical, "open", forbidden_open)
        counts(recovery.collect_cleanup(), 1, 1)
    assert calls == [w.job.job_id] and opens == []
    absent(c, w)
    assert markers(c, w.job.job_id) == before_markers
    public_gone(c, w, "deleted")
    with c.jobs.artifact_lease(c.job.job_id, "user:1") as normal:
        assert normal.input_pdf_path.read_bytes() == normal_input
    assert memory(c) == baseline


@pytest.mark.parametrize("retirement", ["expired", "deleted"])
def test_maintenance_lease_defers_real_purge_until_recovery_finishes(
        coordination, monkeypatch, tmp_path, retirement):
    c = coordination
    baseline = memory(c)
    w = new_attempt(c)
    deadline = datetime.now(timezone.utc) + timedelta(
        days=1 if retirement == "deleted" else -1)
    # Expiry is immutable through the public update API. Establish the test's
    # clock boundary in the durable/cache fixture under the shared lock.
    with c.jobs._lock:
        cached = c.jobs.jobs[w.job.job_id]
        cached.artifacts_expires_at = deadline
        c.jobs._persist(cached)
    external = tmp_path / (retirement + "-sentinel")
    external.write_bytes(MARKER.encode())
    if retirement == "deleted":
        # A real rejected purge leaves a durable tombstone without inventing a
        # purge marker or keeping a public lease across maintenance recovery.
        blocker = w.job.working_dir / "purge-blocker"
        blocker.symlink_to(external)
        try:
            c.jobs.remove_artifacts(w.job.job_id, "user:1", "user:1")
        finally:
            blocker.unlink()
    initial = markers(c, w.job.job_id)
    assert initial[0] == deadline and initial[2] is None
    assert (initial[1] is not None) == (retirement == "deleted")
    public_gone(c, w, retirement)
    assert memory(c) == baseline
    assert w.job.job_id not in c.jobs._artifact_leases

    recovery = collector(c)
    entered, resume = threading.Event(), threading.Event()
    physical_closed, sql_released = threading.Event(), threading.Event()
    maintenance_finished, retention_done = threading.Event(), threading.Event()
    results, recovery_errors, retention_errors, retention_results = [], [], [], []
    original_unlink = os.unlink
    original_open = recovery.physical.open
    original_release = c.store._release_pending_after_cleanup_locked
    original_purge = c.jobs._purge_locked

    def unlink(path, *args, **kwargs):
        if Path(os.fsdecode(path)).name == w.final.name and not entered.is_set():
            entered.set()
            if not resume.wait(20):
                raise OSError(MARKER)
        return original_unlink(path, *args, **kwargs)

    @contextmanager
    def physical_open(job):
        with original_open(job) as directory:
            yield directory
        physical_closed.set()

    def release_pending(conn, pending_id):
        if pending_id == w.pending.pending_id:
            assert physical_closed.is_set()
            original_release(conn, pending_id)
            assert c.store._pending(conn, pending_id) is None
            sql_released.set()
        else:
            original_release(conn, pending_id)

    def purge(job):
        finishing = (job.job_id == w.job.job_id
                     and not c.jobs._artifact_leases.get(job.job_id))
        if finishing:
            assert physical_closed.is_set() and sql_released.is_set()
            conn = c.jobs._get_conn()
            assert not conn.in_transaction
            assert c.store._pending(conn, w.pending.pending_id) is None
        result = original_purge(job)
        if finishing:
            assert job.artifacts_purged_at is not None
            assert not job.working_dir.exists()
            maintenance_finished.set()
        return result

    def recover():
        try:
            result = recovery.collect_cleanup()
            assert physical_closed.is_set() and sql_released.is_set()
            assert maintenance_finished.is_set()
            results.append(result)
        except BaseException as error:
            recovery_errors.append(error)

    def retire():
        try:
            if retirement == "expired":
                retention_results.append(c.jobs.cleanup_expired())
            else:
                retention_results.append(c.jobs.remove_artifacts(
                    w.job.job_id, "user:1", "user:1"))
        except BaseException as error:
            retention_errors.append(error)
        finally:
            retention_done.set()

    recovering = threading.Thread(target=recover, daemon=True)
    retiring = threading.Thread(target=retire, daemon=True)
    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", unlink)
        patch.setattr(recovery.physical, "open", physical_open)
        patch.setattr(c.store, "_release_pending_after_cleanup_locked", release_pending)
        patch.setattr(c.jobs, "_purge_locked", purge)
        try:
            recovering.start()
            assert entered.wait(10)
            retiring.start()
            # Completion while unlink is paused proves retention can acquire
            # the shared lock; a locking regression still has a finite exit.
            assert retention_done.wait(5)
            assert retention_errors == []
            if retirement == "expired":
                assert w.job.job_id in retention_results[0]
            assert c.jobs._lock.acquire(timeout=5)
            try:
                assert c.jobs._artifact_leases[w.job.job_id] == 1
                assert c.store._active_writes == baseline[0] | {w.pending.pending_id}
            finally:
                c.jobs._lock.release()
            during = markers(c, w.job.job_id)
            assert during[0] == initial[0] and during[1] is not None
            assert during[2] is None
            if retirement == "deleted":
                assert during[1] == initial[1]
            assert w.final.read_bytes() == w.data
            charged(c, w)
            public_gone(c, w, retirement)
            assert results == []
            assert not physical_closed.is_set() and not sql_released.is_set()
            assert not maintenance_finished.is_set()
        finally:
            resume.set()
            for thread in (recovering, retiring):
                if thread.ident is not None:
                    thread.join(10)
            assert not recovering.is_alive() and not retiring.is_alive()
    assert recovery_errors == [] and retention_errors == []
    assert len(results) == 1
    counts(results[0], 1, 1)
    absent(c, w)
    after = markers(c, w.job.job_id)
    assert after[:2] == during[:2] and after[2] is not None
    assert not w.job.working_dir.exists()
    assert external.read_bytes() == MARKER.encode()
    public_gone(c, w, retirement)
    assert memory(c) == baseline
