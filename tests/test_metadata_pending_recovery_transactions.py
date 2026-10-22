"""C3cC1: real pending recovery at SQL and physical failure boundaries."""
from contextlib import contextmanager

import pytest

from app.application import ApplicationError
from app.services import artifact_retention as retention
from app.services import metadata_files as files
from app.services.metadata_recovery import RecoveryCounters
from app.services.runtime_lease import RuntimeLease
from test_metadata_files import case
from test_metadata_storage import coordination
from test_metadata_recovery_progress import (
    recovery, pending_job, final, durable_pending, assert_clean_memory,
)


MARKER = "SENSITIVE_RECOVERY_FAILURE"
FAILED = RecoveryCounters(1, 0, 0, 1)
RECOVERED = RecoveryCounters(1, 1, 0, 0)


class ConnectionFault:
    """Forward to SQLite; arm once, only after the target mutation executes."""

    def __init__(self, actual, pending_id, mutation, effect, *, poison=False,
                 on_arm=None):
        self.actual = actual
        self.pending_id = pending_id
        self.mutation = mutation
        self.effect = effect
        self.poison = poison
        self.on_arm = on_arm
        self.armed = False
        self.used = False
        self.events = []

    def __getattr__(self, name):
        return getattr(self.actual, name)

    @property
    def in_transaction(self):
        return self.actual.in_transaction

    def execute(self, sql, parameters=()):
        cursor = self.actual.execute(sql, parameters)
        normalized = " ".join(sql.upper().split())
        prefix = ("UPDATE METADATA_PENDING SET STATE=" if self.mutation == "update"
                  else "DELETE FROM METADATA_PENDING WHERE PENDING_ID=")
        if (not self.used and not self.armed and normalized.startswith(prefix)
                and parameters and parameters[-1] == self.pending_id):
            self.armed = True
            self.events.append(self.mutation)
            if self.on_arm is not None:
                self.on_arm()
        return cursor

    def commit(self):
        self.events.append("commit")
        if not self.armed or self.used:
            return self.actual.commit()
        self.used = True
        if self.effect == "after":
            self.actual.commit()
        raise OSError(MARKER)

    def rollback(self):
        self.events.append("rollback")
        if self.used and self.poison:
            raise OSError(MARKER)
        return self.actual.rollback()


@contextmanager
def connection_fault(c, monkeypatch, w, mutation, effect, **options):
    actual = c.jobs._conn
    assert actual is not None and not actual.in_transaction
    proxy = ConnectionFault(actual, w.pending.pending_id, mutation, effect,
                            **options)
    monkeypatch.setattr(c.jobs, "_conn", proxy)
    try:
        assert c.jobs._get_conn() is proxy
        yield proxy
    finally:
        # Only the test repairs a poisoned connection, after its assertions.
        c.jobs._conn = actual
        if actual.in_transaction:
            actual.rollback()


def pending_row(c, w):
    """Observe actual SQL without a transaction context or implicit commit."""
    with c.jobs._lock:
        return c.jobs._conn.execute(
            "SELECT * FROM metadata_pending WHERE pending_id=?",
            (w.pending.pending_id,)).fetchone()


def assert_charged(c, w, state="cleanup"):
    row = pending_row(c, w)
    assert row is not None
    assert tuple(row)[-1] == state
    assert tuple(row)[14:18] == (
        w.pending.charged_slots, w.pending.charged_bytes,
        w.pending.approval_slots, w.pending.approval_bytes,
    )
    with c.jobs._lock:
        conn = c.jobs._conn
        for table in ("metadata_heads", "metadata_revisions",
                      "metadata_operations", "metadata_audit"):
            assert conn.execute("SELECT 1 FROM " + table + " WHERE job_id=?",
                                (w.job.job_id,)).fetchone() is None


def deny_physical(c, monkeypatch):
    opened = []

    @contextmanager
    def forbidden(self, job):
        opened.append(job.job_id)
        raise AssertionError("diagnostic commit did not authorize physical I/O")
        yield  # pragma: no cover

    monkeypatch.setattr(type(c.recovery.physical), "open", forbidden)
    return opened


@pytest.mark.parametrize("effect", ["before", "after"])
def test_initial_diagnostic_commit_authorizes_no_physical_action(
        recovery, monkeypatch, effect):
    c = recovery
    w = pending_job(c, monkeypatch, 2, cleanup=False)
    original = tuple(pending_row(c, w))
    lease = RuntimeLease(c.jobs.root_dir)
    lease.acquire()
    try:
        with monkeypatch.context() as patch:
            opened = deny_physical(c, patch)
            with connection_fault(c, patch, w, "update", effect) as proxy:
                assert c.recovery.recover_pending_startup(lease) == FAILED
                assert proxy.used
                mutation = proxy.events.index("update")
                assert proxy.events[mutation:mutation + 3] == [
                    "update", "commit", "rollback"]
                assert opened == []
                expected = original[:-1] + (
                    "active" if effect == "before" else "cleanup",)
                assert tuple(pending_row(c, w)) == expected
                assert_charged(c, w, expected[-1])
                assert final(w).read_bytes() == w.data
                assert_clean_memory(c)
        assert c.recovery.recover_pending_startup(lease) == RECOVERED
    finally:
        lease.release()
    assert pending_row(c, w) is None and not final(w).exists()
    assert_clean_memory(c)


def test_failed_rollback_releases_memory_without_repair_or_retention_purge(
        recovery, monkeypatch):
    c = recovery
    w = pending_job(c, monkeypatch, 2)
    actual_rmtree = retention.safe_rmtree
    initial_purges = []

    def fail_first_purge(*args, **kwargs):
        initial_purges.append(args)
        raise OSError(MARKER)

    # Legitimate removal commits the tombstone, then fails its physical purge.
    with monkeypatch.context() as patch:
        patch.setattr(retention, "safe_rmtree", fail_first_purge)
        c.jobs.remove_artifacts(w.job.job_id, "user:1", "user:1")
    assert retention.safe_rmtree is actual_rmtree
    assert len(initial_purges) == 1
    cached = c.jobs.jobs[w.job.job_id]
    assert cached.artifacts_removed_at is not None
    assert cached.artifacts_purged_at is None
    assert w.job.job_id not in c.jobs._artifact_leases
    with c.jobs._lock:
        removed, purged = c.jobs._conn.execute(
            "SELECT artifacts_removed_at,artifacts_purged_at FROM jobs WHERE job_id=?",
            (w.job.job_id,)).fetchone()
    assert removed == cached.artifacts_removed_at.isoformat() and purged is None
    before = tuple(pending_row(c, w))
    purges = []

    def observe_purge(job):
        purges.append(job.job_id)
        raise AssertionError("poisoned connection must prevent retention purge")

    with monkeypatch.context() as patch:
        opened = deny_physical(c, patch)
        patch.setattr(c.jobs, "_purge_locked", observe_purge)
        with connection_fault(c, patch, w, "update", "before", poison=True) as proxy:
            health_observations = []
            original_get_conn = c.jobs._get_conn

            def get_conn():
                if proxy.used:
                    acquired = c.jobs._lock.acquire(blocking=False)
                    if acquired:
                        c.jobs._lock.release()
                    health_observations.append((not acquired, proxy.in_transaction))
                return original_get_conn()

            patch.setattr(c.jobs, "_get_conn", get_conn)
            assert c.recovery.collect_cleanup(limit=1) == FAILED
            # Listing commits precede arming; nothing repairs SQL after rollback.
            mutation = proxy.events.index("update")
            assert proxy.events[mutation:] == ["update", "commit", "rollback"]
            assert proxy.actual.in_transaction is True
            assert health_observations == [(True, True)]
            assert opened == purges == []
            assert c.store._active_writes == set()
            assert c.jobs._artifact_leases == c.baseline_leases
            assert c.jobs.jobs[w.job.job_id] is cached
            assert tuple(pending_row(c, w)) == before
            assert_charged(c, w)
            assert final(w).read_bytes() == w.data
            assert cached.artifacts_purged_at is None
    assert c.jobs._conn.in_transaction is False
    assert_clean_memory(c)


@pytest.mark.parametrize("effect", ["before", "after"])
def test_final_release_commit_failure_follows_real_absence_and_sync(
        recovery, monkeypatch, effect):
    c = recovery
    w = pending_job(c, monkeypatch, 2)
    syncs = []
    original_fsync = files.os.fsync

    def fsync(fd):
        result = original_fsync(fd)
        syncs.append(fd)
        return result

    def release_boundary():
        assert not final(w).exists()
        assert syncs, "physical synchronization must precede the SQL DELETE"

    with monkeypatch.context() as patch:
        patch.setattr(files.os, "fsync", fsync)
        with connection_fault(c, patch, w, "delete", effect,
                              on_arm=release_boundary) as proxy:
            assert c.recovery.collect_cleanup(limit=1) == FAILED
            assert proxy.used
            mutation = proxy.events.index("delete")
            assert proxy.events[mutation:] == ["delete", "commit", "rollback"]
            assert not final(w).exists()
            if effect == "before":
                assert_charged(c, w)
            else:
                assert pending_row(c, w) is None
            assert_clean_memory(c)
    expected = RECOVERED if effect == "before" else RecoveryCounters()
    assert c.recovery.collect_cleanup(limit=1) == expected
    assert pending_row(c, w) is None
    assert_clean_memory(c)


@pytest.mark.parametrize("fault", ["unlink", "fsync", "close_after"])
def test_physical_failure_preserves_charge_until_real_retry(
        recovery, monkeypatch, fault):
    c = recovery
    w = pending_job(c, monkeypatch, 2)
    before = durable_pending(c)
    faults, releases = [], []
    original_release = c.store._release_pending_after_cleanup_locked

    def release(conn, pending_id):
        releases.append(pending_id)
        return original_release(conn, pending_id)

    with monkeypatch.context() as patch:
        patch.setattr(c.store, "_release_pending_after_cleanup_locked", release)
        if fault == "close_after":
            original_open = type(c.recovery.physical).open

            @contextmanager
            def physical_open(self, job):
                with original_open(self, job) as directory:
                    yield directory
                # The actual session has synchronized and closed successfully.
                faults.append("closed")
                raise ApplicationError(503, "metadata_unavailable",
                                       "Metadata is unavailable.")

            patch.setattr(type(c.recovery.physical), "open", physical_open)
        else:
            original = getattr(files.os, fault)

            def physical_fault(*args, **kwargs):
                target = fault == "fsync" or str(args[0]) == final(w).name
                if target and not faults:
                    faults.append(fault)
                    raise OSError(MARKER)
                return original(*args, **kwargs)

            patch.setattr(files.os, fault, physical_fault)
        assert c.recovery.collect_cleanup(limit=1) == FAILED
        assert faults and releases == []
        assert durable_pending(c) == before
        assert_charged(c, w)
        if fault == "unlink":
            assert final(w).read_bytes() == w.data
        if fault == "close_after":
            assert not final(w).exists()
        assert_clean_memory(c)
    assert c.recovery.collect_cleanup(limit=1) == RECOVERED
    assert pending_row(c, w) is None and not final(w).exists()
    assert_clean_memory(c)


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
def test_physical_control_exception_preserves_identity_and_owned_memory(
        recovery, monkeypatch, control_type):
    c = recovery
    w = pending_job(c, monkeypatch, 2)
    control = control_type(MARKER)
    original_unlink = files.os.unlink
    raised = []

    def unlink(name, *args, **kwargs):
        if str(name) == final(w).name:
            raised.append(control)
            raise control
        return original_unlink(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(files.os, "unlink", unlink)
        with pytest.raises(control_type) as caught:
            c.recovery.collect_cleanup(limit=1)
        assert caught.value is control and raised == [control]
        assert_charged(c, w)
        assert final(w).read_bytes() == w.data
        assert_clean_memory(c)
    assert c.recovery.collect_cleanup(limit=1) == RECOVERED
    assert pending_row(c, w) is None and not final(w).exists()
    assert_clean_memory(c)
