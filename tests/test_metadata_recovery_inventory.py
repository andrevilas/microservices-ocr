"""Real Linux B2/B1 faults under the case fixture's external artifact lease.

Healthy global SQL diagnosis and recovery authority are fixture assumptions only;
these tests exercise physical recovery, not an integrated SQL coordinator.
"""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
import os
from types import SimpleNamespace

import pytest

from app.metadata_models import MAX_SNAPSHOT_BYTES
from app.services import metadata_files as files
from test_metadata_files import MARKER, case, final, temporary, unavailable
from test_metadata_recovery_files import closed, recovery


pytestmark = pytest.mark.skipif(not files._SUPPORTED, reason='Linux dirfd required')


def seed(c, count=1):
    c.directory.mkdir(parents=True, exist_ok=True)
    paths = [c.directory / (f'{index:032x}.json') for index in range(count)]
    for path in paths:
        path.write_bytes(b'partial untrusted')
    return paths


def identity(path):
    info = path.lstat()
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


@contextmanager
def descriptors(monkeypatch):
    original_open, original_close = os.open, os.close
    state = SimpleNamespace(opened=[], live=set(), peak=0, names=[])

    def opening(name, *args, **kwargs):
        fd = original_open(name, *args, **kwargs)
        state.opened.append(fd)
        state.names.append(name)
        state.live.add(fd)
        state.peak = max(state.peak, len(state.live))
        return fd

    def closing(fd):
        original_close(fd)
        state.live.discard(fd)

    with monkeypatch.context() as patch:
        patch.setattr(files.os, 'open', opening)
        patch.setattr(files.os, 'close', closing)
        yield state
    assert not state.live
    closed(state.opened)


@pytest.mark.parametrize('count', [0, 128])
def test_inventory_bounds_immutable_records_and_original_pins(case, monkeypatch, count):
    paths = seed(case, count)
    with descriptors(monkeypatch) as tracked:
        with recovery(case).open(case.job) as session:
            baseline = len(tracked.live)
            snapshot = session.inventory()
            assert type(snapshot.entries) is tuple
            assert len(snapshot.entries) == count
            with pytest.raises(FrozenInstanceError):
                snapshot.entries = ()
            for entry, path in zip(snapshot.entries, paths):
                info = path.stat()
                assert (entry.name, entry.dev, entry.inode, entry.size,
                        entry.mtime_ns, entry.ctime_ns, entry.nlink) == (
                    path.name, info.st_dev, info.st_ino, info.st_size,
                    info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)
                with pytest.raises(FrozenInstanceError):
                    entry.size = 0
            assert len(tracked.live) == baseline + count
            opened = len(tracked.opened)
            session.remove_orphans(snapshot, (), protected_names=())
            assert len(tracked.opened) == opened
            assert len(tracked.live) == baseline
            assert tracked.peak == baseline + count
    assert [path.read_bytes() for path in paths] == [b'partial untrusted'] * count


def test_129th_entry_stops_individual_scan_before_unlink(case, monkeypatch):
    paths = seed(case, 129)
    original_scan = os.scandir
    scan_counts, unlinks = [], []

    @contextmanager
    def bounded(fd):
        scan_counts.append(0)
        index = len(scan_counts) - 1
        with original_scan(fd) as iterator:
            def entries():
                for entry in iterator:
                    scan_counts[index] += 1
                    yield entry
            yield entries()

    def operation():
        with recovery(case).open(case.job) as session:
            snapshot = session.inventory()
            session.remove_orphans(snapshot, (), protected_names=())

    with descriptors(monkeypatch):
        with monkeypatch.context() as patch:
            patch.setattr(files.os, 'scandir', bounded)
            patch.setattr(files.os, 'unlink', lambda *a, **k: unlinks.append(a))
            unavailable(operation)
    assert scan_counts and max(scan_counts) == 129
    assert unlinks == []
    assert all(path.read_bytes() == b'partial untrusted' for path in paths)


@pytest.mark.parametrize('kind', ['unknown', 'fifo', 'symlink', 'directory', 'oversize'])
def test_unsafe_inventory_entries_are_preserved(case, tmp_path, monkeypatch, kind):
    good = seed(case)[0]
    outside = tmp_path / 'outside'
    outside.write_bytes(MARKER.encode())
    bad = case.directory / ('unknown.txt' if kind == 'unknown' else 'f' * 32 + '.json')
    if kind == 'fifo':
        os.mkfifo(bad)
    elif kind == 'symlink':
        bad.symlink_to(outside)
    elif kind == 'directory':
        bad.mkdir()
    elif kind == 'oversize':
        with bad.open('wb') as stream:
            stream.truncate(MAX_SNAPSHOT_BYTES + 1)
    else:
        bad.write_bytes(MARKER.encode())
    before = identity(bad)
    unlinks = []

    def operation():
        with recovery(case).open(case.job) as session:
            session.inventory()

    with descriptors(monkeypatch):
        with monkeypatch.context() as patch:
            patch.setattr(files.os, 'unlink', lambda *a, **k: unlinks.append(a))
            unavailable(operation)
    assert unlinks == [] and identity(bad) == before
    assert good.read_bytes() == b'partial untrusted'
    assert outside.read_bytes() == MARKER.encode()


@pytest.mark.parametrize('invalid', [
    'foreign', 'forged', 'list', 'duplicate', 'absent', 'bad_name',
    'protected_list', 'protected_duplicate',
])
def test_snapshot_and_strict_inputs_reject_and_consume(case, monkeypatch, invalid):
    path = seed(case)[0]
    with recovery(case).open(case.job) as other:
        foreign = other.inventory()
    unlinks = []
    with recovery(case).open(case.job) as session:
        snapshot = session.inventory()
        supplied, names, protected = snapshot, (path.name,), ()
        if invalid == 'foreign':
            supplied = foreign
        elif invalid == 'forged':
            supplied = replace(snapshot)
        elif invalid == 'list':
            names = [path.name]
        elif invalid == 'duplicate':
            names = (path.name, path.name)
        elif invalid == 'absent':
            names = ('e' * 32 + '.json',)
        elif invalid == 'bad_name':
            names = (MARKER,)
        elif invalid == 'protected_list':
            protected = []
        else:
            protected = (path.name, path.name)
        with monkeypatch.context() as patch:
            patch.setattr(files.os, 'unlink', lambda *a, **k: unlinks.append(a))
            unavailable(lambda: session.remove_orphans(supplied, names, protected_names=protected))
            unavailable(lambda: session.remove_orphans(snapshot, (path.name,), protected_names=()))
            unavailable(session.inventory)
    assert unlinks == [] and path.read_bytes() == b'partial untrusted'


def test_protected_confirmed_file_unchanged_and_success_cannot_be_reused(case):
    orphan, protected = seed(case, 2)
    before = identity(protected), protected.read_bytes()
    with recovery(case).open(case.job) as session:
        snapshot = session.inventory()
        unavailable(session.inventory)
        session.remove_orphans(snapshot, (orphan.name,), protected_names=(protected.name,))
        unavailable(lambda: session.remove_orphans(snapshot, (), protected_names=()))
    assert not orphan.exists()
    assert (identity(protected), protected.read_bytes()) == before


def test_selected_protected_name_rejects_before_unlink(case, monkeypatch):
    path = seed(case)[0]
    before = identity(path)
    unlinks = []

    def operation():
        with recovery(case).open(case.job) as session:
            snapshot = session.inventory()
            session.remove_orphans(snapshot, (path.name,), protected_names=(path.name,))

    with monkeypatch.context() as patch:
        patch.setattr(files.os, 'unlink', lambda *a, **k: unlinks.append(a))
        unavailable(operation)
    assert unlinks == [] and identity(path) == before


@pytest.mark.parametrize('protected', [False, True])
def test_unselected_hardlink_preserves_entire_group(case, monkeypatch, protected):
    first = seed(case)[0]
    second = case.directory / ('.' + 'f' * 32 + '.tmp')
    os.link(first, second)
    before = [identity(path) for path in (first, second)]
    unlinks = []

    def operation():
        with recovery(case).open(case.job) as session:
            snapshot = session.inventory()
            session.remove_orphans(snapshot, (first.name,),
                                   protected_names=(second.name,) if protected else ())

    with monkeypatch.context() as patch:
        patch.setattr(files.os, 'unlink', lambda *a, **k: unlinks.append(a))
        unavailable(operation)
    assert unlinks == []
    assert [identity(path) for path in (first, second)] == before
    assert first.read_bytes() == second.read_bytes() == b'partial untrusted'


@pytest.mark.parametrize('shape', ['partial_temp', 'final', 'pair', 'two_finals'])
def test_orphan_publication_shapes(case, monkeypatch, shape):
    seed(case, 0)
    first = final(case) if shape in ('final', 'two_finals') else temporary(case)
    first.write_bytes(b'partial not JSON')
    paths = [first]
    if shape in ('pair', 'two_finals'):
        second = (final(case) if shape == 'pair' else
                  case.directory / ('e' * 32 + '.json'))
        os.link(first, second)
        paths.append(second)
    before = [identity(path) for path in paths]
    original_unlink, calls = os.unlink, []

    def unlink(*args, **kwargs):
        calls.append(args)
        return original_unlink(*args, **kwargs)

    def operation():
        with recovery(case).open(case.job) as session:
            snapshot = session.inventory()
            session.remove_orphans(snapshot, tuple(path.name for path in paths), protected_names=())

    with monkeypatch.context() as patch:
        patch.setattr(files.os, 'unlink', unlink)
        if shape == 'two_finals':
            unavailable(operation)
        else:
            operation()
    if shape == 'two_finals':
        assert calls == [] and [identity(path) for path in paths] == before
    else:
        assert len(calls) == len(paths) and all(not path.exists() for path in paths)


@pytest.mark.parametrize('change', ['replacement', 'added', 'ancestry', 'protected', 'unselected'])
def test_inventory_changes_reject_before_any_deletion(case, monkeypatch, change):
    orphan, retained = seed(case, 2)
    moved = case.directory.with_name('held-metadata')
    held = case.job.working_dir / 'held-original'
    unlinks = []
    try:
        def operation():
            with recovery(case).open(case.job) as session:
                snapshot = session.inventory()
                if change == 'replacement':
                    orphan.rename(held)
                    orphan.write_bytes(MARKER.encode())
                elif change == 'added':
                    (case.directory / ('e' * 32 + '.json')).write_bytes(MARKER.encode())
                elif change == 'ancestry':
                    case.directory.rename(moved)
                    case.directory.mkdir()
                    orphan.write_bytes(MARKER.encode())
                else:
                    retained.write_bytes(MARKER.encode())
                session.remove_orphans(snapshot, (orphan.name,),
                                       protected_names=(retained.name,) if change == 'protected' else ())

        with descriptors(monkeypatch):
            with monkeypatch.context() as patch:
                patch.setattr(files.os, 'unlink', lambda *a, **k: unlinks.append(a))
                unavailable(operation)
        assert unlinks == []
        if change == 'ancestry':
            assert (moved / orphan.name).read_bytes() == b'partial untrusted'
            assert orphan.read_bytes() == MARKER.encode()
        elif change == 'replacement':
            assert held.read_bytes() == b'partial untrusted'
            assert orphan.read_bytes() == MARKER.encode()
        else:
            assert orphan.read_bytes() == b'partial untrusted'
            assert retained.read_bytes() == (MARKER.encode() if change in ('protected', 'unselected')
                                             else b'partial untrusted')
    finally:
        if moved.exists():
            orphan.unlink()
            case.directory.rmdir()
            moved.rename(case.directory)


@pytest.mark.parametrize('effect', [False, True])
def test_orphan_sync_failure_crosses_context_and_empty_fresh_retry_syncs(case, monkeypatch, effect):
    path = seed(case)[0]
    original_sync, calls = os.fsync, []

    def fault(fd):
        calls.append(fd)
        if effect:
            original_sync(fd)
        raise OSError(MARKER)

    def operation():
        with recovery(case).open(case.job) as session:
            snapshot = session.inventory()
            session.remove_orphans(snapshot, (path.name,), protected_names=())

    with descriptors(monkeypatch):
        with monkeypatch.context() as patch:
            patch.setattr(files.os, 'fsync', fault)
            unavailable(operation)
    assert len(calls) == 1 and not path.exists()
    synced = []

    def sync(fd):
        synced.append(fd)
        return original_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(files.os, 'fsync', sync)
        with recovery(case).open(case.job) as session:
            snapshot = session.inventory()
            assert snapshot.entries == ()
            session.remove_orphans(snapshot, (), protected_names=())
    assert len(synced) == 1


def test_capture_failure_closes_already_acquired_pins(case, monkeypatch):
    seed(case, 3)
    opened_names = []
    with descriptors(monkeypatch) as tracked:
        original_open = os.open

        def fault(name, *args, **kwargs):
            if type(name) is str and name.endswith('.json'):
                opened_names.append(name)
                if len(opened_names) == 2:
                    raise OSError(MARKER)
            return original_open(name, *args, **kwargs)

        def operation():
            with recovery(case).open(case.job) as session:
                session.inventory()

        with monkeypatch.context() as patch:
            patch.setattr(files.os, 'open', fault)
            unavailable(operation)
        assert len(opened_names) == 2
        assert opened_names[0] in tracked.names


@pytest.mark.parametrize('close_error', [False, True])
def test_unconsumed_pins_close_on_exit_even_after_close_effect_error(case, monkeypatch, close_error):
    paths = seed(case, 3)
    close_calls = []
    with descriptors(monkeypatch) as tracked:
        original_close = os.close

        def close(fd):
            close_calls.append(fd)
            original_close(fd)
            if close_error and len(close_calls) == 1:
                raise OSError(MARKER)

        def operation():
            with recovery(case).open(case.job) as session:
                session.inventory()
                assert len(tracked.live) >= len(paths)

        with monkeypatch.context() as patch:
            patch.setattr(files.os, 'close', close)
            if close_error:
                unavailable(operation)
            else:
                operation()
        assert len(close_calls) == len(tracked.opened)
        assert set(close_calls) == set(tracked.opened)
    assert all(path.read_bytes() == b'partial untrusted' for path in paths)


@pytest.mark.parametrize('phase', ['unlink', 'fsync'])
def test_pending_errors_cross_context_sanitized_and_close_descriptors(case, monkeypatch, phase):
    seed(case, 0)
    temporary(case).write_bytes(b'partial')
    calls = []

    def fault(*args, **kwargs):
        calls.append(args)
        raise OSError(MARKER)

    def operation():
        with recovery(case).open(case.job) as session:
            session.remove_pending(case.pending, case.model.revision_id)

    with descriptors(monkeypatch):
        with monkeypatch.context() as patch:
            patch.setattr(files.os, phase, fault)
            unavailable(operation)
    assert len(calls) == 1
    assert temporary(case).exists() == (phase == 'unlink')


def test_pending_second_unlink_failure_preserves_remaining_link_and_fresh_retry(case, monkeypatch):
    seed(case, 0)
    temporary(case).write_bytes(b'partial')
    os.link(temporary(case), final(case))
    original_unlink, original_sync = os.unlink, os.fsync
    calls, synced = [], []

    def unlink(name, *args, **kwargs):
        calls.append(name)
        if len(calls) == 2:
            raise OSError(MARKER)
        return original_unlink(name, *args, **kwargs)

    def sync(fd):
        synced.append(fd)
        return original_sync(fd)

    def operation():
        with recovery(case).open(case.job) as session:
            session.remove_pending(case.pending, case.model.revision_id)

    with descriptors(monkeypatch):
        with monkeypatch.context() as patch:
            patch.setattr(files.os, 'unlink', unlink)
            patch.setattr(files.os, 'fsync', sync)
            unavailable(operation)
    assert calls == [temporary(case).name, final(case).name]
    assert not temporary(case).exists()
    assert final(case).read_bytes() == b'partial'
    assert final(case).stat().st_nlink == 1 and synced == []
    with descriptors(monkeypatch):
        with monkeypatch.context() as patch:
            patch.setattr(files.os, 'fsync', sync)
            operation()
    assert not final(case).exists() and not temporary(case).exists()
    assert len(synced) == 1
