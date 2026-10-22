"""Integrated Linux startup faults; synthetic files, real SQL and RuntimeLease.

These tests exercise recovery methods, not application startup or readiness.
Late failures do not imply rollback of already authorized physical deletion.
"""
from contextlib import contextmanager
import errno
import os
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.metadata_namespace_authority import MetadataNamespaceAuthority
from app.services.metadata_namespace_recovery import NamespaceRecoveryCounters
from app.services.metadata_recovery import RecoveryCounters
from test_metadata_files import case
from test_metadata_namespace_recovery import charges, committed, orphan
from test_metadata_pending_recovery import collector, memory, runtime
from test_metadata_storage import assert_error, coordination, sql_state


pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='Linux namespace recovery')
CANARY = 'SYNTHETIC_PHYSICAL_FAULT_CANARY'


def identity(path):
    info = path.lstat()
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def baseline(c):
    return sql_state(c), memory(c), charges(c)


def unchanged(c, before):
    assert baseline(c) == before


def rejected(recovery, held):
    errors = []

    def invoke():
        try:
            return recovery.recover_startup(held)
        except Exception as exc:
            errors.append(exc)
            raise

    # Existing flow helper requires the sanitized public 503 error contract.
    assert_error(invoke)
    assert len(errors) == 1
    assert CANARY not in str(errors[0])
    assert CANARY not in repr(errors[0])


@contextmanager
def probe(monkeypatch, recovery, job, *, fault=None, effect=False, mutate=None):
    """Observe real calls; inject only against this job's recovery descriptors."""
    state = SimpleNamespace(
        active=False, session=None, fd=None, plans=0, hits=0,
        opened=[], live=set(), closes=[], scans=[], unlinks=[], syncs=[],
        confirmed_fds=set(), preads=[], mutations=0,
    )
    real_open, real_close = os.open, os.close
    real_scan, real_unlink = os.scandir, os.unlink
    real_sync, real_pread = os.fsync, os.pread
    real_session = recovery.physical.open
    real_plan = MetadataNamespaceAuthority.plan_locked

    def opening(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        if state.active:
            state.opened.append(fd)
            state.live.add(fd)
        return fd

    def closing(fd):
        tracked = fd in state.live
        real_close(fd)
        if tracked:
            state.live.remove(fd)
            state.closes.append(fd)
        if (tracked and fd == state.fd and state.plans
                and fault == 'close' and not state.hits):
            state.hits += 1
            raise OSError(CANARY)

    @contextmanager
    def scanning(fd):
        with real_scan(fd) as iterator:
            if not state.active or fd != state.fd:
                yield iterator
            else:
                counts = [0]
                state.scans.append(counts)

                def entries():
                    for entry in iterator:
                        counts[0] += 1
                        yield entry

                yield entries()

    def unlinking(name, *args, **kwargs):
        selected = state.active and kwargs.get('dir_fd') == state.fd
        if selected:
            state.unlinks.append(name)
        if selected and state.plans and fault == 'unlink' and not state.hits:
            state.hits += 1
            raise OSError(CANARY)
        return real_unlink(name, *args, **kwargs)

    def syncing(fd):
        selected = state.active and fd == state.fd and state.plans
        if selected:
            state.syncs.append(fd)
        if selected and fault == 'fsync' and not state.hits:
            state.hits += 1
            if effect:
                real_sync(fd)
            raise OSError(CANARY)
        return real_sync(fd)

    def reading(fd, size, offset):
        selected = state.active and fd in state.confirmed_fds
        if selected:
            state.preads.append((fd, size, offset))
        if selected and fault == 'pread' and not state.hits:
            state.hits += 1
            raise OSError(CANARY)
        return real_pread(fd, size, offset)

    @contextmanager
    def session(actual_job):
        targeted = actual_job.job_id == job.job_id
        if targeted:
            state.active = True
        try:
            with real_session(actual_job) as directory:
                if targeted:
                    state.session = directory
                    state.fd = directory._directories.fd
                yield directory
        finally:
            if targeted:
                state.active = False
                state.session = None

    def planning(authority, conn, job_id, names):
        plan = real_plan(authority, conn, job_id, names)
        if job_id == job.job_id:
            state.plans += 1
            pins = state.session._inventory_pins
            state.confirmed_fds.update(
                pins[name][0] for expected in plan.state.confirmed
                if (name := expected.revision_id + '.json') in pins)
            if mutate is not None and not state.mutations:
                mutate(plan)
                state.mutations += 1
        return plan

    with monkeypatch.context() as patch:
        for name, value in (
            ('open', opening), ('close', closing), ('scandir', scanning),
            ('unlink', unlinking), ('fsync', syncing), ('pread', reading),
        ):
            patch.setattr(os, name, value)
        patch.setattr(recovery.physical, 'open', session)
        patch.setattr(MetadataNamespaceAuthority, 'plan_locked', planning)
        yield state
    assert state.opened
    assert not state.live
    assert len(state.closes) == len(state.opened)
    # Check before any further file/SQL operation can reuse these numbers.
    for fd in set(state.opened):
        with pytest.raises(OSError) as caught:
            os.fstat(fd)
        assert caught.value.errno == errno.EBADF


@pytest.mark.parametrize('pair', [False, True])
def test_integrated_positive_control_calls_real_io(coordination, monkeypatch, pair):
    c = coordination
    w = committed(c)
    first = orphan(w.directory)
    paths = [first]
    if pair:
        second = w.directory / ('.' + uuid4().hex + '.tmp')
        os.link(first, second)
        paths.append(second)
        assert identity(first)[:2] == identity(second)[:2]
        assert first.stat().st_nlink == second.stat().st_nlink == 2
    else:
        assert first.stat().st_nlink == 1
    before, confirmed = baseline(c), identity(w.final)
    recovery = collector(c)
    with runtime(c.jobs.root_dir) as held:
        with probe(monkeypatch, recovery, w.job) as observed:
            result = recovery.recover_startup(held)
    assert result.pending == RecoveryCounters()
    assert result.counters == NamespaceRecoveryCounters(
        jobs_scanned=2, jobs_verified=2, confirmed_valid=1,
        orphan_files_removed=len(paths))
    assert result.occurrences == () and result.truncated_occurrences == 0
    assert observed.plans == 1 and observed.hits == 0
    assert observed.preads and observed.syncs == [observed.fd]
    assert observed.scans and max(n[0] for n in observed.scans) == len(paths) + 1
    assert set(observed.unlinks) == {path.name for path in paths}
    assert len(observed.unlinks) == len(paths)
    assert all(not path.exists() for path in paths)
    assert identity(w.final) == confirmed and w.final.read_bytes() == w.data
    unchanged(c, before)


def test_129_canonical_orphans_abort_before_plan_or_unlink(coordination, monkeypatch):
    c = coordination
    directory = c.job.working_dir / 'results' / 'metadata'
    paths = [orphan(directory) for _ in range(129)]
    originals = [(identity(path), path.read_bytes()) for path in paths]
    before, recovery = baseline(c), collector(c)
    with runtime(c.jobs.root_dir) as held:
        with probe(monkeypatch, recovery, c.job) as observed:
            rejected(recovery, held)
    assert observed.scans and max(n[0] for n in observed.scans) == 129
    assert observed.plans == 0 and observed.unlinks == []
    assert [(identity(path), path.read_bytes()) for path in paths] == originals
    unchanged(c, before)


@pytest.mark.parametrize('kind', ['unknown', 'symlink', 'external_link', 'two_finals'])
def test_unsafe_namespace_preserves_all_entries_and_outside(
        coordination, monkeypatch, tmp_path, kind):
    c = coordination
    w = committed(c)
    outside = tmp_path / 'outside-sentinel'
    outside.write_bytes(CANARY.encode())
    good = orphan(w.directory)
    bad = w.directory / ('unknown.txt' if kind == 'unknown' else uuid4().hex + '.json')
    if kind == 'symlink':
        bad.symlink_to(outside)
    elif kind == 'external_link':
        os.link(outside, bad)
        assert identity(outside)[:2] == identity(bad)[:2]
        assert bad.stat().st_nlink == outside.stat().st_nlink == 2
    elif kind == 'two_finals':
        os.link(good, bad)
        assert identity(good)[:2] == identity(bad)[:2]
        assert good.stat().st_nlink == bad.stat().st_nlink == 2
    else:
        bad.write_bytes(CANARY.encode())
    paths = [outside, good, bad, w.final]
    identities = [identity(path) for path in paths]
    before, recovery = baseline(c), collector(c)
    with runtime(c.jobs.root_dir) as held:
        with probe(monkeypatch, recovery, w.job) as observed:
            rejected(recovery, held)
    assert observed.scans and observed.unlinks == []
    assert observed.plans == int(kind in ('external_link', 'two_finals'))
    assert [identity(path) for path in paths] == identities
    assert outside.read_bytes() == CANARY.encode()
    assert w.final.read_bytes() == w.data and good.exists()
    unchanged(c, before)


def test_confirmed_inode_linked_to_canonical_temp_is_never_deleted(coordination, monkeypatch):
    c = coordination
    w = committed(c)
    temporary = w.directory / ('.' + uuid4().hex + '.tmp')
    os.link(w.final, temporary)
    garbage = orphan(w.directory)
    assert identity(w.final)[:2] == identity(temporary)[:2]
    assert w.final.stat().st_nlink == temporary.stat().st_nlink == 2
    paths = [w.final, temporary, garbage]
    identities = [identity(path) for path in paths]
    before, recovery = baseline(c), collector(c)
    with runtime(c.jobs.root_dir) as held:
        with probe(monkeypatch, recovery, w.job) as observed:
            rejected(recovery, held)
    assert observed.plans == 1 and observed.unlinks == [] and observed.preads == []
    assert [identity(path) for path in paths] == identities
    assert w.final.read_bytes() == temporary.read_bytes() == w.data
    unchanged(c, before)


@pytest.mark.parametrize('change', ['entry', 'ancestry'])
def test_change_after_real_sql_plan_rejects_without_additional_deletion(
        coordination, monkeypatch, tmp_path, change):
    c = coordination
    w = committed(c)
    garbage = orphan(w.directory)
    original_payload = garbage.read_bytes()
    outside = tmp_path / 'outside-sentinel'
    outside.write_bytes(CANARY.encode())
    outside_identity = identity(outside)
    held_directory = w.directory.with_name('held-metadata')
    held_entry = w.job.working_dir / 'held-orphan'
    plans = []

    def mutate(plan):
        plans.append(plan)
        if change == 'entry':
            garbage.rename(held_entry)
        else:
            w.directory.rename(held_directory)
            w.directory.mkdir()
        garbage.write_bytes(CANARY.encode())

    before, recovery = baseline(c), collector(c)
    with runtime(c.jobs.root_dir) as held:
        with probe(monkeypatch, recovery, w.job, mutate=mutate) as observed:
            rejected(recovery, held)
    assert observed.plans == observed.mutations == len(plans) == 1
    assert garbage.name in plans[0].orphans
    assert w.final.name in plans[0].protected_names
    assert observed.unlinks == [] and observed.preads == []
    assert garbage.read_bytes() == CANARY.encode()
    original = held_entry if change == 'entry' else held_directory / garbage.name
    confirmed = w.final if change == 'entry' else held_directory / w.final.name
    assert original.read_bytes() == original_payload
    assert confirmed.read_bytes() == w.data
    assert identity(outside) == outside_identity and outside.read_bytes() == CANARY.encode()
    unchanged(c, before)


def test_confirmed_pread_fault_aborts_instead_of_reporting_content_invalid(coordination, monkeypatch):
    c = coordination
    w = committed(c)
    recovery = collector(c)
    with runtime(c.jobs.root_dir) as held:
        with probe(monkeypatch, recovery, w.job) as control:
            result = recovery.recover_startup(held)
        assert control.preads and result.counters.confirmed_valid == 1
        garbage = orphan(w.directory)
        before, confirmed = baseline(c), identity(w.final)
        garbage_identity = identity(garbage)
        with probe(monkeypatch, recovery, w.job, fault='pread') as observed:
            rejected(recovery, held)
    assert observed.plans == observed.hits == len(observed.preads) == 1
    assert observed.preads[0][0] in observed.confirmed_fds
    assert observed.unlinks == [] and identity(garbage) == garbage_identity
    assert identity(w.final) == confirmed and w.final.read_bytes() == w.data
    unchanged(c, before)


@pytest.mark.parametrize('fault,effect', [('fsync', False), ('fsync', True), ('close', True)])
def test_late_physical_fault_aborts_public_result_without_sql_rollback_claim(
        coordination, monkeypatch, fault, effect):
    c = coordination
    w = committed(c)
    garbage = orphan(w.directory)
    before, confirmed = baseline(c), identity(w.final)
    recovery = collector(c)
    with runtime(c.jobs.root_dir) as held:
        with probe(monkeypatch, recovery, w.job, fault=fault, effect=effect) as observed:
            rejected(recovery, held)
        assert observed.plans == observed.hits == 1
        assert observed.preads and observed.unlinks == [garbage.name]
        assert observed.syncs == [observed.fd]
        assert not garbage.exists()  # Authorized unlink preceded the late failure.
        assert identity(w.final) == confirmed and w.final.read_bytes() == w.data
        unchanged(c, before)
        with probe(monkeypatch, recovery, w.job) as retry:
            result = recovery.recover_startup(held)
    assert retry.syncs == [retry.fd] and retry.unlinks == []
    assert result.counters.confirmed_valid == 1
    assert result.counters.orphan_files_removed == 0
    unchanged(c, before)


def test_unlink_failure_before_effect_preserves_orphan_and_aborts(coordination, monkeypatch):
    c = coordination
    w = committed(c)
    garbage = orphan(w.directory)
    originals = identity(garbage), garbage.read_bytes(), identity(w.final)
    before, recovery = baseline(c), collector(c)
    with runtime(c.jobs.root_dir) as held:
        with probe(monkeypatch, recovery, w.job, fault='unlink') as observed:
            rejected(recovery, held)
    assert observed.plans == observed.hits == 1
    assert observed.preads and observed.unlinks == [garbage.name]
    assert observed.syncs == []
    assert (identity(garbage), garbage.read_bytes(), identity(w.final)) == originals
    assert w.final.read_bytes() == w.data
    unchanged(c, before)
