"""Real Linux files under the existing external artifact-lease fixture.

SQL recovery authority is a documented fixture assumption, not an exercised
coordinator, cleanup claim, or accounting implementation.
"""
import os
from dataclasses import replace

import pytest

from app.application import ApplicationError
from app.metadata_models import MAX_SNAPSHOT_BYTES
from app.services import metadata_files as files
from app.services.metadata_recovery_files import MetadataRecoveryFiles
from test_metadata_files import (
    MARKER, case, final, prepare, temporary, unavailable,
)


pytestmark = pytest.mark.skipif(not files._SUPPORTED, reason='Linux dirfd required')


def recovery(c):
    return MetadataRecoveryFiles(c.jobs.root_dir)


def remove(c, session):
    session.remove_pending(c.pending, c.model.revision_id)


def closed(fds):
    for fd in fds:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize('shape', ['empty', 'partial', 'final', 'linked', 'different'])
def test_authorized_names_removed_without_reading_contents(case, monkeypatch, shape):
    c = case
    c.directory.mkdir(parents=True)
    sentinel = c.directory / 'unrelated.txt'
    sentinel.write_bytes(MARKER.encode())
    if shape in ('empty', 'partial', 'linked', 'different'):
        temporary(c).write_bytes(b'' if shape == 'empty' else b'partial untrusted')
    if shape == 'linked':
        os.link(temporary(c), final(c))
    elif shape in ('final', 'different'):
        final(c).write_bytes(b'not JSON')
    original_open = os.open
    opened = []

    def tracked(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def forbidden(*args, **kwargs):
        raise AssertionError('recovery must not read contents')

    with monkeypatch.context() as patch:
        patch.setattr(files.os, 'open', tracked)
        patch.setattr(files.os, 'read', forbidden)
        with recovery(c).open(c.job) as session:
            remove(c, session)
    closed(opened)
    assert list(c.directory.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == MARKER.encode()
    unavailable(lambda: remove(c, session))


def test_existing_publication_can_be_removed_only_under_fixture_recovery_authority(case):
    attempt = prepare(case)
    attempt.publish(case.data)
    # No SQL commit exists in this fixture. This is separate recovery authority,
    # never mutation/adoption of the old WriteAttempt's ownership state.
    with recovery(case).open(case.job) as session:
        remove(case, session)
    assert not final(case).exists()


@pytest.mark.parametrize('depth', [0, 1, 2])
def test_absence_syncs_nearest_existing_directory_without_creation(case, monkeypatch, depth):
    c = case
    if depth >= 1:
        c.directory.parent.mkdir()
    if depth == 2:
        c.directory.mkdir()
    original, synced = os.fsync, []

    def tracked(fd):
        synced.append(os.fstat(fd).st_ino)
        return original(fd)

    ancestor = [c.job.working_dir, c.directory.parent, c.directory][depth]
    with monkeypatch.context() as patch:
        patch.setattr(files.os, 'fsync', tracked)
        with recovery(c).open(c.job) as session:
            remove(c, session)
    assert synced == [ancestor.stat().st_ino]
    assert c.directory.exists() == (depth == 2)
    assert c.directory.parent.exists() == (depth >= 1)


def test_missing_job_is_not_treated_as_absence(case):
    job = replace(case.job, job_id='f' * 32,
                  working_dir=case.jobs.root_dir / ('f' * 32))
    def operation():
        with recovery(case).open(job):
            pytest.fail('missing job accepted')
    unavailable(operation)


@pytest.mark.parametrize('bad', ['symlink', 'fifo', 'directory', 'oversize', 'hardlink'])
def test_entire_plan_validated_before_any_unlink(case, tmp_path, monkeypatch, bad):
    c = case
    c.directory.mkdir(parents=True)
    temporary(c).write_bytes(b'valid partial')
    outside = tmp_path / 'protected'
    outside.write_bytes(MARKER.encode())
    if bad == 'symlink':
        final(c).symlink_to(outside)
    elif bad == 'fifo':
        os.mkfifo(final(c))
    elif bad == 'directory':
        final(c).mkdir()
    elif bad == 'oversize':
        with final(c).open('wb') as stream:
            stream.truncate(MAX_SNAPSHOT_BYTES + 1)
    else:
        final(c).write_bytes(b'protected inode')
        os.link(final(c), tmp_path / 'external-link')
    original, calls = os.unlink, []

    def tracked(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(files.os, 'unlink', tracked)
        with recovery(c).open(c.job) as session:
            unavailable(lambda: remove(c, session))
    assert calls == []
    assert temporary(c).read_bytes() == b'valid partial'
    assert os.path.lexists(final(c))
    assert outside.read_bytes() == MARKER.encode()


@pytest.mark.parametrize('pending,revision', [
    (MARKER, 'a' * 32), ('a' * 32, MARKER),
    ('A' * 32, 'a' * 32), ('a' * 32, None), (True, 'a' * 32),
])
def test_ids_reject_before_additional_os_work(case, monkeypatch, pending, revision):
    with recovery(case).open(case.job) as session:
        def forbidden(*args, **kwargs):
            raise AssertionError('invalid ID caused filesystem work')
        with monkeypatch.context() as patch:
            for name in ('open', 'stat', 'fstat', 'unlink', 'fsync'):
                patch.setattr(files.os, name, forbidden)
            unavailable(lambda: session.remove_pending(pending, revision))


@pytest.mark.parametrize('swap', ['entry', 'ancestry'])
def test_swap_after_observation_preserves_replacement_and_outside(case, tmp_path, monkeypatch, swap):
    c = case
    c.directory.mkdir(parents=True)
    temporary(c).write_bytes(b'original')
    outside = tmp_path / 'outside'
    outside.write_bytes(MARKER.encode())
    original = os.open
    moved = c.directory.with_name('held-metadata')
    intercepted = []

    def fault(name, *args, **kwargs):
        fd = original(name, *args, **kwargs)
        if name == temporary(c).name and not intercepted:
            intercepted.append(fd)
            if swap == 'entry':
                temporary(c).rename(c.directory / 'held-temp')
                temporary(c).write_bytes(MARKER.encode())
            else:
                c.directory.rename(moved)
                c.directory.mkdir()
                temporary(c).write_bytes(MARKER.encode())
        return fd

    try:
        with recovery(c).open(c.job) as session:
            with monkeypatch.context() as patch:
                patch.setattr(files.os, 'open', fault)
                unavailable(lambda: remove(c, session))
            if swap == 'ancestry':
                # Restore for normal session exit; operation already rejected.
                temporary(c).unlink()
                c.directory.rmdir()
                moved.rename(c.directory)
        assert intercepted
        closed(intercepted)
        if swap == 'entry':
            assert temporary(c).read_bytes() == MARKER.encode()
            assert (c.directory / 'held-temp').read_bytes() == b'original'
        else:
            assert temporary(c).read_bytes() == b'original'
        assert outside.read_bytes() == MARKER.encode()
    finally:
        if moved.exists():
            temporary(c).unlink()
            c.directory.rmdir()
            moved.rename(c.directory)


@pytest.mark.parametrize('effect', [False, True])
def test_unlink_error_requires_actual_absence(case, monkeypatch, effect):
    c = case
    c.directory.mkdir(parents=True)
    temporary(c).write_bytes(b'partial')
    original, calls, syncs = os.unlink, [], []
    original_sync = os.fsync

    def fault(*args, **kwargs):
        calls.append(args)
        if effect:
            original(*args, **kwargs)
        raise OSError(MARKER)

    def sync(fd):
        syncs.append(fd)
        return original_sync(fd)

    with recovery(c).open(c.job) as session:
        with monkeypatch.context() as patch:
            patch.setattr(files.os, 'unlink', fault)
            patch.setattr(files.os, 'fsync', sync)
            if effect:
                remove(c, session)
            else:
                unavailable(lambda: remove(c, session))
    assert len(calls) == 1
    assert temporary(c).exists() == (not effect)
    assert len(syncs) == int(effect)


@pytest.mark.parametrize('effect', [False, True])
def test_sync_error_requires_fresh_retry_to_sync_already_absent_names(case, monkeypatch, effect):
    c = case
    c.directory.mkdir(parents=True)
    final(c).write_bytes(b'untrusted')
    original, calls = os.fsync, []

    def fault(fd):
        calls.append(fd)
        if effect:
            original(fd)
        raise OSError(MARKER)

    with recovery(c).open(c.job) as session:
        with monkeypatch.context() as patch:
            patch.setattr(files.os, 'fsync', fault)
            unavailable(lambda: remove(c, session))
    assert len(calls) == 1 and not final(c).exists()
    synced = []

    def tracked(fd):
        synced.append(fd)
        return original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(files.os, 'fsync', tracked)
        with recovery(c).open(c.job) as session:
            remove(c, session)
    assert len(synced) == 1


@pytest.mark.parametrize('error', [
    ValueError(MARKER), ApplicationError(418, 'caller', MARKER),
    KeyboardInterrupt(MARKER), SystemExit(MARKER),
])
@pytest.mark.parametrize('close_error', [False, True])
def test_body_exception_identity_survives_close_and_all_fds_close(case, monkeypatch, error, close_error):
    original_open, original_close = os.open, os.close
    opened = []

    def tracked(*args, **kwargs):
        fd = original_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def close(fd):
        original_close(fd)
        if close_error:
            raise OSError(MARKER)

    with monkeypatch.context() as patch:
        patch.setattr(files.os, 'open', tracked)
        patch.setattr(files.os, 'close', close)
        with pytest.raises(type(error)) as caught:
            with recovery(case).open(case.job):
                raise error
    assert caught.value is error
    closed(opened)


def test_normal_exit_close_error_is_sanitized(case, monkeypatch):
    original, descriptors = os.close, []

    def fault(fd):
        descriptors.append(fd)
        original(fd)
        raise OSError(MARKER)

    def operation():
        with recovery(case).open(case.job):
            pass

    with monkeypatch.context() as patch:
        patch.setattr(files.os, 'close', fault)
        unavailable(operation)
    assert descriptors
    closed(descriptors)


def test_normal_exit_detects_changed_ancestry(case):
    c = case
    c.directory.mkdir(parents=True)
    moved = c.directory.with_name('held-metadata')

    def operation():
        with recovery(c).open(c.job):
            c.directory.rename(moved)
            c.directory.mkdir()

    try:
        unavailable(operation)
        assert list(c.directory.iterdir()) == []
        assert list(moved.iterdir()) == []
    finally:
        c.directory.rmdir()
        moved.rename(c.directory)
