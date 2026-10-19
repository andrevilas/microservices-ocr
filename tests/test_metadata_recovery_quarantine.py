"""Recovery must quarantine uncertain SQL before releasing its maintenance lease."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.application import ApplicationError
from app.services import storage_service as storage_module
from app.services.metadata_recovery import RecoveryCounters
from test_metadata_files import case
from test_metadata_storage import coordination
from test_metadata_recovery_progress import recovery, pending_job, final


MARKER = "SYNTHETIC_PRIVATE_RECOVERY_FAILURE"
TABLES = ("metadata_pending", "metadata_heads", "metadata_revisions",
          "metadata_operations", "metadata_audit")


class ConnectionProbe:
    """Forward real SQLite operations; alter only the armed health observation."""

    def __init__(self, actual):
        self.actual = actual
        self.armed = False
        self.fault = None
        self.commits = self.rollbacks = self.closes = 0

    @property
    def in_transaction(self):
        if self.armed:
            if isinstance(self.fault, BaseException):
                raise self.fault
            if self.fault == "zero":
                return 0
        return self.actual.in_transaction

    def commit(self):
        self.commits += 1
        return self.actual.commit()

    def rollback(self):
        self.rollbacks += 1
        return self.actual.rollback()

    def close(self):
        self.closes += 1
        return self.actual.close()

    def __getattr__(self, name):
        return getattr(self.actual, name)

    def effects(self):
        return self.commits, self.rollbacks, self.closes


def sql_rows(conn):
    # Quarantined stores intentionally reject the transaction-based helpers.
    return {table: tuple(tuple(row) for row in conn.execute(
        "SELECT * FROM " + table + " ORDER BY rowid")) for table in TABLES}


@pytest.fixture
def incident(recovery, monkeypatch):
    c = recovery
    w = pending_job(c, monkeypatch, 2)
    actual = c.jobs._conn
    probe = ConnectionProbe(actual)
    state = SimpleNamespace(
        c=c, w=w, actual=actual, probe=probe,
        before=sql_rows(actual), leases=dict(c.jobs._artifact_leases),
        original=w.job.input_pdf_path.read_bytes(),
        enabled=True, mode="transaction", tombstone=False,
        physical_error=OSError(MARKER), entered=0, purges=[],
        get_calls=0, effects=None, calls_at_failure=None,
        removed=None,
    )
    assert state.before["metadata_pending"]
    for table in TABLES[1:]:
        assert state.before[table] == ()
    state.pending = tuple(actual.execute(
        "SELECT pending_id,state,charged_slots,charged_bytes "
        "FROM metadata_pending").fetchone())
    assert state.pending == (w.pending.pending_id, "cleanup",
                             w.pending.charged_slots, w.pending.charged_bytes)
    assert state.pending[2] > 0 and state.pending[3] > 0
    assert final(w).read_bytes() == w.data
    original_open = type(c.recovery.physical).open
    original_get = c.jobs._get_conn
    original_rmtree = storage_module.safe_rmtree
    c.jobs._conn = probe

    def get_conn():
        state.get_calls += 1
        return original_get()

    def safe_rmtree(*args, **kwargs):
        state.purges.append((args, kwargs))
        return original_rmtree(*args, **kwargs)

    @contextmanager
    def physical_open(self, job):
        with original_open(self, job) as directory:
            def remove_pending(pending_id, revision_id):
                if not state.enabled:
                    return directory.remove_pending(pending_id, revision_id)
                state.entered += 1
                assert (pending_id, revision_id) == (
                    w.pending.pending_id, w.pending.revision_id)
                assert job.job_id == w.job.job_id
                # The diagnostic transaction really committed before this hook.
                assert actual.in_transaction is False
                assert probe.commits > 0
                assert c.store._active_writes == {pending_id}
                expected = dict(state.leases)
                expected[job.job_id] = expected.get(job.job_id, 0) + 1
                assert c.jobs._artifact_leases == expected
                assert sql_rows(actual) == state.before
                assert final(w).read_bytes() == w.data
                if state.tombstone:
                    # Real logical removal, protected by recovery's real lease.
                    c.jobs.remove_artifacts(job.job_id)
                    assert actual.in_transaction is False
                    state.removed, purged = actual.execute(
                        "SELECT artifacts_removed_at,artifacts_purged_at "
                        "FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()
                    assert state.removed is not None and purged is None
                    assert state.purges == []
                    assert final(w).read_bytes() == w.data
                if state.mode != "healthy":
                    with c.jobs._lock:
                        actual.execute("BEGIN IMMEDIATE")
                        actual.execute("UPDATE jobs SET filename=? WHERE job_id=?",
                                       (MARKER, job.job_id))
                        probe.armed = True
                        if state.mode == "none":
                            c.jobs._conn = None
                state.effects = probe.effects()
                state.calls_at_failure = state.get_calls
                # Never invoke the real deletion on this failing attempt.
                raise state.physical_error

            yield SimpleNamespace(remove_pending=remove_pending)

    monkeypatch.setattr(c.jobs, "_get_conn", get_conn)
    monkeypatch.setattr(storage_module, "safe_rmtree", safe_rmtree)
    monkeypatch.setattr(type(c.recovery.physical), "open", physical_open)
    try:
        yield state
    finally:
        # Test-only repair after assertions. Never reset the quarantine flag or
        # ask product code to repair/commit the uncertain transaction.
        try:
            if actual.in_transaction:
                actual.rollback()
        finally:
            c.jobs._conn = actual


def assert_memory_released(s):
    assert s.entered == 1
    assert s.c.store._active_writes == set()
    assert s.c.jobs._artifact_leases == s.leases
    assert s.c.jobs._lock.acquire(blocking=False)
    s.c.jobs._lock.release()


def assert_quarantined(s, caplog):
    c = s.c
    assert_memory_released(s)
    assert s.probe.effects() == s.effects
    assert s.probe.closes == 0
    assert s.actual.in_transaction is True
    assert s.actual.execute("SELECT filename FROM jobs WHERE job_id=?",
                            (s.w.job.job_id,)).fetchone()[0] == MARKER
    assert sql_rows(s.actual) == s.before
    assert tuple(s.actual.execute(
        "SELECT pending_id,state,charged_slots,charged_bytes "
        "FROM metadata_pending").fetchone()) == s.pending
    assert final(s.w).read_bytes() == s.w.data
    assert s.w.job.input_pdf_path.read_bytes() == s.original
    assert s.purges == []
    if s.mode == "none":
        assert c.jobs._conn is None
        assert s.get_calls == s.calls_at_failure
    # These are genuine operations on the quarantined store, before any repair.
    with c.jobs._lock:
        with pytest.raises(ApplicationError) as caught:
            c.jobs._get_conn()
    errors = [caught.value]
    for operation in (
        lambda: c.jobs.update(s.w.job.job_id, status="completed",
                              progress_percent=100),
        c.jobs.check_db,
    ):
        with pytest.raises(ApplicationError) as caught:
            operation()
        errors.append(caught.value)
    for error in errors:
        assert error.status == 503
        assert MARKER not in str(error) + repr(error) + repr(vars(error))
    assert s.probe.effects() == s.effects
    assert s.actual.in_transaction is True
    assert sql_rows(s.actual) == s.before
    assert MARKER not in caplog.text


@pytest.mark.parametrize("mode", ["transaction", "oserror", "zero", "none"])
def test_physical_failure_quarantines_unsafe_release_health(incident, caplog, mode):
    s = incident
    s.mode = mode
    if mode == "oserror":
        s.probe.fault = OSError(MARKER)
    elif mode == "zero":
        s.probe.fault = "zero"
    assert s.c.recovery.collect_cleanup(limit=1) == RecoveryCounters(1, 0, 0, 1)
    assert_quarantined(s, caplog)


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
def test_release_health_control_is_preserved_after_ordinary_failure(
        incident, caplog, kind):
    s = incident
    control = kind(MARKER)
    s.probe.fault = control
    with pytest.raises(kind) as caught:
        s.c.recovery.collect_cleanup(limit=1)
    assert caught.value is control
    assert_quarantined(s, caplog)


@pytest.mark.parametrize("kind", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("health_control", [False, True])
def test_physical_control_survives_unsafe_release_health(
        incident, caplog, kind, health_control):
    s = incident
    original = kind(MARKER)
    s.physical_error = original
    if health_control:
        # _recover_one explicitly preserves its original control exception.
        s.probe.fault = SystemExit("SECONDARY_SYNTHETIC_CONTROL")
    with pytest.raises(kind) as caught:
        s.c.recovery.collect_cleanup(limit=1)
    assert caught.value is original
    assert_quarantined(s, caplog)


def test_tombstone_during_physical_phase_cannot_trigger_uncertain_purge(
        incident, caplog):
    s = incident
    s.tombstone = True
    assert s.c.recovery.collect_cleanup(limit=1) == RecoveryCounters(1, 0, 0, 1)
    assert_quarantined(s, caplog)
    assert tuple(s.actual.execute(
        "SELECT artifacts_removed_at,artifacts_purged_at FROM jobs WHERE job_id=?",
        (s.w.job.job_id,)).fetchone()) == (s.removed, None)
    assert s.c.jobs.jobs[s.w.job.job_id].artifacts_removed_at is not None
    assert s.purges == []


def test_healthy_ordinary_physical_failure_remains_usable_and_retries(incident, caplog):
    s = incident
    s.mode = "healthy"
    assert s.c.recovery.collect_cleanup(limit=1) == RecoveryCounters(1, 0, 0, 1)
    assert_memory_released(s)
    assert s.actual.in_transaction is False
    assert s.probe.effects() == s.effects
    assert sql_rows(s.actual) == s.before
    assert final(s.w).read_bytes() == s.w.data
    assert s.purges == []
    with s.c.jobs._lock:
        assert s.c.jobs._get_conn() is s.probe
    s.c.jobs.check_db()
    s.c.jobs.update(s.w.job.job_id, status="completed", progress_percent=100)
    s.enabled = False
    assert s.c.recovery.collect_cleanup(limit=1) == RecoveryCounters(1, 1, 0, 0)
    assert all(not rows for rows in sql_rows(s.actual).values())
    assert not final(s.w).exists()
    assert s.w.job.input_pdf_path.read_bytes() == s.original
    assert_memory_released(s)
    assert s.actual.in_transaction is False
    assert MARKER not in caplog.text
