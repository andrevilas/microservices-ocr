"""Periodic continuation with real reservations, SQLite and physical cleanup."""
from contextlib import contextmanager
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from app.services import metadata_files as files
from app.services import metadata_store as store_module
from app.services.metadata_recovery import MetadataRecovery, RecoveryCounters
from app.services.metadata_recovery_files import MetadataRecoveryFiles
from app.services.runtime_lease import RuntimeLease
from test_metadata_files import case, snapshot
from test_metadata_storage import coordination, make_write


@pytest.fixture
def recovery(coordination):
    c = coordination
    c.recovery = MetadataRecovery(c.store, MetadataRecoveryFiles(c.jobs.root_dir))
    c.baseline_leases = dict(c.jobs._artifact_leases)
    return c


def pending_job(c, monkeypatch, number, *, cleanup=True):
    job = c.jobs.create("recovery.pdf", b"%PDF-1.4\nrecovery\n", owner_user_id=1)
    c.jobs.update(job.job_id, status="completed", progress_percent=100)
    # Only reservation IDs are deterministic; job/revision bindings stay intact.
    with c.jobs.artifact_lease(job.job_id, "user:1") as leased:
        with monkeypatch.context() as patch:
            patch.setattr(store_module, "uuid4", lambda: SimpleNamespace(hex=f"{number:032x}"))
            w = make_write(c, job=leased, model=snapshot(leased))
        c.service.prepare(
            leased, pending_id=w.pending.pending_id,
            revision_id=w.pending.revision_id, sha256=w.pending.sha256,
            size_bytes=w.pending.size_bytes,
        ).publish(w.data)
        if cleanup:
            with c.store._transaction() as conn:
                c.store._mark_cleanup_locked(conn, w.pending.pending_id)
    assert c.jobs._artifact_leases == c.baseline_leases
    return w


def final(w):
    return w.job.working_dir / "results" / "metadata" / (w.pending.revision_id + ".json")


def durable_pending(c):
    with c.store._transaction() as conn:
        return tuple(tuple(row) for row in conn.execute(
            "SELECT pending_id,state,charged_slots,charged_bytes "
            "FROM metadata_pending ORDER BY pending_id"))


def assert_clean_memory(c):
    assert c.store._active_writes == set()
    assert c.jobs._artifact_leases == c.baseline_leases
    assert c.jobs._conn.in_transaction is False


@pytest.mark.parametrize("blocked", ["unlink", "claimed"])
def test_failed_or_claimed_prefix_does_not_starve_next_call(recovery, monkeypatch, blocked):
    c = recovery
    writes = [pending_job(c, monkeypatch, n) for n in (2, 4)]
    low, high = sorted(writes, key=lambda w: w.pending.pending_id)
    before = durable_pending(c)
    original_unlink = files.os.unlink
    faults = []

    def unlink(name, *args, **kwargs):
        if Path(name).name == final(low).name:
            faults.append(name)
            raise OSError("synthetic unlink failure before effect")
        return original_unlink(name, *args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            if blocked == "unlink":
                patch.setattr(files.os, "unlink", unlink)
            else:
                c.store._active_writes.add(low.pending.pending_id)
            expected = (RecoveryCounters(1, 0, 0, 1) if blocked == "unlink"
                        else RecoveryCounters(1, 0, 1, 0))
            assert c.recovery.collect_cleanup(limit=1) == expected
            assert durable_pending(c) == before
            assert final(low).read_bytes() == low.data
            assert c.jobs._artifact_leases == c.baseline_leases
            assert c.recovery.collect_cleanup(limit=1) == RecoveryCounters(1, 1, 0, 0)
            assert durable_pending(c) == (before[0],)
            assert final(low).read_bytes() == low.data
            assert not final(high).exists()
            assert c.store._active_writes == ({low.pending.pending_id}
                                              if blocked == "claimed" else set())
            assert c.jobs._artifact_leases == c.baseline_leases
            assert bool(faults) == (blocked == "unlink")
    finally:
        c.store._active_writes.discard(low.pending.pending_id)
    assert c.recovery.collect_cleanup(limit=1) == RecoveryCounters(1, 1, 0, 0)
    assert durable_pending(c) == ()
    assert not final(low).exists()
    assert_clean_memory(c)


def test_cycle_freezes_ceiling_and_defers_wrap_until_next_call(recovery, monkeypatch):
    c = recovery
    first = pending_job(c, monkeypatch, 2)
    last = pending_job(c, monkeypatch, 4)
    assert c.recovery.collect_cleanup(limit=1) == RecoveryCounters(1, 1, 0, 0)
    below = pending_job(c, monkeypatch, 1)
    above = pending_job(c, monkeypatch, 6)
    assert below.pending.pending_id < first.pending.pending_id < last.pending.pending_id < above.pending.pending_id
    # Spare budget cannot admit arrivals above the frozen ceiling or wrap below.
    assert c.recovery.collect_cleanup(limit=10) == RecoveryCounters(1, 1, 0, 0)
    assert tuple(row[0] for row in durable_pending(c)) == (
        below.pending.pending_id, above.pending.pending_id)
    assert final(below).read_bytes() == below.data
    assert final(above).read_bytes() == above.data
    assert c.recovery.collect_cleanup(limit=10) == RecoveryCounters(2, 2, 0, 0)
    assert durable_pending(c) == ()
    assert not any(final(w).exists() for w in (first, last, below, above))
    assert_clean_memory(c)


def test_empty_tail_resets_cycle_for_next_call(recovery, monkeypatch):
    c = recovery
    pending_job(c, monkeypatch, 2)
    tail = pending_job(c, monkeypatch, 4)
    assert c.recovery.collect_cleanup(limit=1) == RecoveryCounters(1, 1, 0, 0)
    # Another independent recovery can remove the captured tail.
    other = MetadataRecovery(c.store, MetadataRecoveryFiles(c.jobs.root_dir))
    assert other.collect_cleanup(limit=10) == RecoveryCounters(1, 1, 0, 0)
    below = pending_job(c, monkeypatch, 1)
    assert c.recovery.collect_cleanup(limit=10) == RecoveryCounters()
    assert final(below).read_bytes() == below.data
    assert not final(tail).exists()
    assert c.recovery.collect_cleanup(limit=10) == RecoveryCounters(1, 1, 0, 0)
    assert durable_pending(c) == ()
    assert_clean_memory(c)


def test_startup_ignores_periodic_cursor_and_preserves_it(recovery, monkeypatch):
    c = recovery
    pending_job(c, monkeypatch, 2)
    tail = pending_job(c, monkeypatch, 4)
    assert c.recovery.collect_cleanup(limit=1) == RecoveryCounters(1, 1, 0, 0)
    active = pending_job(c, monkeypatch, 1, cleanup=False)
    lower_cleanup = pending_job(c, monkeypatch, 0)
    # Periodic eligibility still excludes an active row, even below the cursor.
    assert durable_pending(c)[1][1] == "active"
    state = (c.recovery._periodic_after, c.recovery._periodic_ceiling)
    lease = RuntimeLease(c.jobs.root_dir)
    lease.acquire()
    try:
        assert c.recovery.recover_pending_startup(lease) == RecoveryCounters(3, 3, 0, 0)
        assert (c.recovery._periodic_after, c.recovery._periodic_ceiling) == state
        assert c.recovery.recover_pending_startup(lease) == RecoveryCounters()
    finally:
        lease.release()
    assert durable_pending(c) == ()
    assert not any(final(w).exists() for w in (tail, active, lower_cleanup))
    assert_clean_memory(c)


def test_periodic_calls_serialize_while_physical_cleanup_is_blocked(recovery, monkeypatch):
    c = recovery
    low = pending_job(c, monkeypatch, 2)
    high = pending_job(c, monkeypatch, 4)
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    results, errors, opened = {}, {}, []
    original_open = type(c.recovery.physical).open

    @contextmanager
    def physical_open(self, job):
        with original_open(self, job) as directory:
            opened.append(job.job_id)
            if job.job_id == low.job.job_id:
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("physical cleanup timeout")
            yield directory

    def run(name):
        try:
            if name == "second":
                second_started.set()
            results[name] = c.recovery.collect_cleanup(limit=1)
        except BaseException as exc:
            errors[name] = exc

    monkeypatch.setattr(type(c.recovery.physical), "open", physical_open)
    first = threading.Thread(target=run, args=("first",), daemon=True)
    second = threading.Thread(target=run, args=("second",), daemon=True)
    try:
        first.start()
        assert entered.wait(10)
        # Real SQL remains accessible while physical I/O owns the periodic lock.
        assert len(durable_pending(c)) == 2
        assert c.store._active_writes == {low.pending.pending_id}
        expected_leases = dict(c.baseline_leases)
        expected_leases[low.job.job_id] = 1
        assert c.jobs._artifact_leases == expected_leases
        assert not c.recovery._periodic_lock.acquire(blocking=False)
        second.start()
        assert second_started.wait(10)
    finally:
        release.set()
        for thread in (first, second):
            if thread.ident is not None:
                thread.join(10)
    assert not first.is_alive() and not second.is_alive()
    assert errors == {}
    assert results == {"first": RecoveryCounters(1, 1, 0, 0),
                       "second": RecoveryCounters(1, 1, 0, 0)}
    assert opened == [low.job.job_id, high.job.job_id]
    assert durable_pending(c) == ()
    assert not final(low).exists() and not final(high).exists()
    assert_clean_memory(c)
