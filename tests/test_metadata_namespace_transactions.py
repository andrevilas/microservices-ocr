"""Startup coordinator SQL faults; synthetic SQLite, files and RuntimeLease only."""
import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.application import ApplicationError
from app.services import artifact_retention as retention_module
from app.services.metadata_namespace_recovery import MetadataNamespaceRecovery
from test_metadata_namespace_recovery import (
    case, coordination, collector, runtime, orphan, committed, memory,
)


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux namespace recovery')
CANARY = 'SYNTHETIC_PRIVATE_NAMESPACE_SQL_FAILURE'
TABLES = ('jobs', 'metadata_pending', 'metadata_heads', 'metadata_revisions',
          'metadata_operations', 'metadata_audit')


def rows(actual):
    # Direct observations remain usable after product quarantine.
    return {table: tuple(tuple(row) for row in actual.execute(
        'SELECT * FROM ' + table + ' ORDER BY rowid')) for table in TABLES}


def charges(actual):
    return tuple(actual.execute(
        'SELECT (SELECT COUNT(*) FROM metadata_revisions), '
        '(SELECT COALESCE(SUM(size_bytes),0) FROM metadata_revisions), '
        '(SELECT COALESCE(SUM(approval_slots),0) FROM metadata_heads), '
        '(SELECT COALESCE(SUM(approval_bytes),0) FROM metadata_heads), '
        '(SELECT COALESCE(SUM(charged_slots),0) FROM metadata_pending), '
        '(SELECT COALESCE(SUM(charged_bytes),0) FROM metadata_pending)'
    ).fetchone())


class FaultConnection:
    """Forward SQLite; arm only after a real authority diagnosis completes."""

    def __init__(self, actual, effect, rollback):
        self.actual = actual
        self.effect = effect
        self.rollback_mode = rollback
        self.armed = False
        self.rollback_due = False
        self.arms = self.hits = self.fault_rollbacks = 0
        self.commits = self.rollbacks = self.closes = 0

    def arm(self):
        self.arms += 1
        self.armed = True

    @property
    def in_transaction(self):
        return self.actual.in_transaction

    def commit(self):
        self.commits += 1
        if not self.armed:
            return self.actual.commit()
        self.armed = False
        self.hits += 1
        if self.effect == 'none':
            return self.actual.commit()
        self.rollback_due = True
        if self.effect == 'after':
            self.actual.commit()
        raise OSError(CANARY)

    def rollback(self):
        self.rollbacks += 1
        if not self.rollback_due:
            return self.actual.rollback()
        self.rollback_due = False
        self.fault_rollbacks += 1
        if self.rollback_mode == 'before':
            raise OSError(CANARY)
        result = self.actual.rollback()
        if self.rollback_mode == 'after':
            raise OSError(CANARY)
        return result

    def close(self):
        self.closes += 1
        return self.actual.close()

    def effects(self):
        return self.commits, self.rollbacks, self.closes

    def __getattr__(self, name):
        return getattr(self.actual, name)


@pytest.fixture
def instrument(coordination, monkeypatch):
    c = coordination
    states = []

    def setup(phase='initial', effect='before', rollback='healthy',
              confirmed=False, control=None):
        w = committed(c) if confirmed else None
        job = w.job if w is not None else c.job
        garbage = orphan(job.working_dir / 'results' / 'metadata')
        recovery = collector(c)
        namespace = MetadataNamespaceRecovery(recovery)
        actual = c.jobs._conn
        probe = FaultConnection(actual, effect, rollback)
        s = SimpleNamespace(
            c=c, job=job, w=w, recovery=recovery, namespace=namespace,
            actual=actual, probe=probe, garbage=garbage,
            payload=garbage.read_bytes(), before=rows(actual),
            accounting=charges(actual), baseline=memory(c),
            phase=phase, enabled=True, control=control,
            loads=0, plans=0, final_loads=0, opens=0, inventories=0,
            verifies=0, removals=0, exits=0, unlinks=0, purges=0,
            get_calls=0, armed_in_transaction=[],
        )
        states.append(s)
        c.jobs._conn = probe
        original_load = namespace.authority.load_locked
        original_plan = namespace.authority.plan_locked
        original_open = recovery.physical.open
        original_get = c.jobs._get_conn
        original_unlink = os.unlink
        original_rmtree = retention_module.safe_rmtree

        def arm():
            s.armed_in_transaction.append(actual.in_transaction is True)
            probe.arm()

        def load(conn, job_id):
            result = original_load(conn, job_id)
            if job_id == job.job_id:
                s.loads += 1
                if s.exits:
                    s.final_loads += 1
                if s.enabled and probe.arms == 0:
                    if phase == 'initial' and s.opens == 0:
                        arm()
                    elif phase == 'final' and s.exits == 1:
                        arm()
            return result

        def plan(conn, job_id, names):
            result = original_plan(conn, job_id, names)
            if job_id == job.job_id:
                s.plans += 1
                if s.enabled and phase == 'classification':
                    if control is not None:
                        raise control
                    if probe.arms == 0:
                        arm()
            return result

        @contextmanager
        def opened(file_job):
            target = file_job.job_id == job.job_id
            if target:
                s.opens += 1
            with original_open(file_job) as directory:
                if not target:
                    yield directory
                else:
                    def inventory():
                        s.inventories += 1
                        return directory.inventory()

                    def verify(*args, **kwargs):
                        s.verifies += 1
                        return directory.verify_confirmed(*args, **kwargs)

                    def remove(*args, **kwargs):
                        s.removals += 1
                        return directory.remove_orphans(*args, **kwargs)

                    yield SimpleNamespace(
                        inventory=inventory, verify_confirmed=verify,
                        remove_orphans=remove)
            if target:
                s.exits += 1

        def get_conn():
            s.get_calls += 1
            return original_get()

        def unlink(*args, **kwargs):
            s.unlinks += 1
            return original_unlink(*args, **kwargs)

        def rmtree(*args, **kwargs):
            s.purges += 1
            return original_rmtree(*args, **kwargs)

        monkeypatch.setattr(namespace.authority, 'load_locked', load)
        monkeypatch.setattr(namespace.authority, 'plan_locked', plan)
        monkeypatch.setattr(recovery.physical, 'open', opened)
        monkeypatch.setattr(c.jobs, '_get_conn', get_conn)
        monkeypatch.setattr(os, 'unlink', unlink)
        monkeypatch.setattr(retention_module, 'safe_rmtree', rmtree)
        return s

    try:
        yield setup
    finally:
        # Test-owned cleanup only; never clear the product quarantine flag.
        for s in states:
            try:
                if s.actual.in_transaction:
                    s.actual.rollback()
            finally:
                s.c.jobs._conn = s.actual


def unavailable(call):
    with pytest.raises(ApplicationError) as caught:
        call()
    error = caught.value
    assert error.status == 503 and error.code == 'metadata_unavailable'
    assert error.message == 'Metadata is unavailable.'
    assert error.__cause__ is None and error.__context__ is None
    assert CANARY not in str(error) + repr(error) + repr(vars(error))


def unchanged(s):
    assert rows(s.actual) == s.before
    assert charges(s.actual) == s.accounting
    assert memory(s.c) == s.baseline
    assert s.purges == 0
    assert s.c.jobs._lock.acquire(blocking=False)
    s.c.jobs._lock.release()


def diagnosed_fault(s):
    assert s.probe.arms == s.probe.hits == s.probe.fault_rollbacks == 1
    assert s.armed_in_transaction == [True]
    assert s.loads >= 1
    if s.phase == 'initial':
        assert (s.opens, s.inventories, s.plans) == (0, 0, 0)
    else:
        assert (s.opens, s.inventories, s.plans) == (1, 1, 1)
    assert (s.verifies, s.removals, s.exits, s.unlinks) == (0, 0, 0, 0)
    assert s.garbage.read_bytes() == s.payload


def successful_retry(s, held):
    # Keep the same coordinator, recovery object, proxy and runtime lease.
    s.enabled = False
    result = s.namespace.recover_startup(held)
    assert result.counters.orphan_files_removed == 1
    assert result.pending.failed == 0
    assert not s.garbage.exists()
    assert s.probe.arms == s.probe.hits == 1
    assert s.actual.in_transaction is False
    assert s.c.jobs._sqlite_quarantined is False
    unchanged(s)


@pytest.mark.parametrize('phase', ['initial', 'classification'])
@pytest.mark.parametrize('effect', ['before', 'after'])
def test_diagnostic_commit_failure_blocks_unauthorized_physical_work(
        instrument, caplog, phase, effect):
    s = instrument(phase=phase, effect=effect)
    # The original fixture owns a real lease; recovery must preserve it.
    assert s.baseline[1][s.job.job_id] >= 1
    with runtime(s.c.jobs.root_dir) as held:
        unavailable(lambda: s.namespace.recover_startup(held))
        diagnosed_fault(s)
        unchanged(s)
        assert s.actual.in_transaction is False
        assert s.c.jobs._sqlite_quarantined is False
        successful_retry(s, held)
    assert CANARY not in caplog.text


@pytest.mark.parametrize('phase', ['initial', 'classification'])
@pytest.mark.parametrize('effect', ['before', 'after'])
@pytest.mark.parametrize('rollback', ['before', 'after'])
def test_rollback_failure_quarantines_even_after_real_rollback(
        instrument, caplog, phase, effect, rollback):
    s = instrument(phase=phase, effect=effect, rollback=rollback)
    with runtime(s.c.jobs.root_dir) as held:
        unavailable(lambda: s.namespace.recover_startup(held))
        diagnosed_fault(s)
        unchanged(s)
        assert s.c.jobs._sqlite_quarantined is True
        assert s.c.jobs._conn is s.probe
        assert s.actual.in_transaction is (effect == 'before' and rollback == 'before')
        effects = s.probe.effects()
        calls = s.get_calls
        # A repeated startup fails without transaction repair or reconnection.
        unavailable(lambda: s.namespace.recover_startup(held))
        assert s.probe.effects() == effects
        assert s.get_calls >= calls
        with s.c.jobs._lock:
            unavailable(s.c.jobs._get_conn)
        unavailable(lambda: s.c.jobs.update(
            s.job.job_id, status='completed', progress_percent=100))
        assert s.c.jobs.check_db() is False
        assert s.c.jobs._sqlite_quarantined is True
        assert s.c.jobs._conn is s.probe
        assert s.probe.effects() == effects and s.probe.closes == 0
        diagnosed_fault(s)
        unchanged(s)
    assert CANARY not in caplog.text


@pytest.mark.parametrize('kind', [KeyboardInterrupt, SystemExit])
def test_classification_body_control_survives_healthy_rollback_and_release(
        instrument, kind):
    control = kind(CANARY)
    s = instrument(phase='classification', control=control)
    with runtime(s.c.jobs.root_dir) as held:
        with pytest.raises(kind) as caught:
            s.namespace.recover_startup(held)
        assert caught.value is control
    assert s.plans == s.opens == s.inventories == 1
    assert (s.verifies, s.removals, s.exits, s.unlinks) == (0, 0, 0, 0)
    assert s.probe.arms == s.probe.hits == s.probe.fault_rollbacks == 0
    assert s.probe.rollbacks == 1
    assert s.actual.in_transaction is False
    assert s.c.jobs._sqlite_quarantined is False
    assert s.garbage.read_bytes() == s.payload
    unchanged(s)


def test_forwarding_instrumentation_allows_real_success(instrument):
    s = instrument(phase='classification', effect='none')
    with runtime(s.c.jobs.root_dir) as held:
        result = s.namespace.recover_startup(held)
    assert result.counters.jobs_verified == 1
    assert result.counters.orphan_files_removed == 1
    assert s.probe.arms == s.probe.hits == 1
    assert s.armed_in_transaction == [True]
    assert s.probe.rollbacks == s.probe.fault_rollbacks == 0
    assert (s.opens, s.inventories, s.verifies, s.removals, s.exits) == (1, 1, 1, 1, 1)
    assert s.unlinks >= 1 and not s.garbage.exists()
    assert s.actual.in_transaction is False
    assert s.c.jobs._sqlite_quarantined is False
    unchanged(s)


@pytest.mark.parametrize('effect', ['before', 'after'])
def test_final_commit_failure_reports_failure_after_authorized_file_removal(
        instrument, caplog, effect):
    s = instrument(phase='final', effect=effect, confirmed=True)
    confirmed_bytes = s.w.final.read_bytes()
    assert s.accounting[0] == 1 and s.accounting[2] == 1
    with runtime(s.c.jobs.root_dir) as held:
        unavailable(lambda: s.namespace.recover_startup(held))
        assert s.probe.arms == s.probe.hits == s.probe.fault_rollbacks == 1
        assert s.armed_in_transaction == [True] and s.final_loads == 1
        assert (s.opens, s.inventories, s.verifies, s.removals, s.exits) == (1, 1, 1, 1, 1)
        # SQL rollback cannot undo the earlier, authorized physical deletion.
        assert s.unlinks >= 1 and not s.garbage.exists()
        assert s.w.final.read_bytes() == confirmed_bytes
        assert s.actual.in_transaction is False
        assert s.c.jobs._sqlite_quarantined is False
        unchanged(s)
        s.enabled = False
        result = s.namespace.recover_startup(held)
        assert result.counters.orphan_files_removed == 0
        assert result.counters.confirmed_valid == 1
        assert s.w.final.read_bytes() == confirmed_bytes
        unchanged(s)
    assert CANARY not in caplog.text
