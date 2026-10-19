"""C3cC2a physical verification on real Linux files and retained pins.

The case fixture supplies an external artifact lease. Healthy SQL diagnosis and
recovery authority are assumptions, not an exercised coordinator or quota flow.
All verification results remain provisional until the context finishes normally.
"""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta, timezone
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from app.metadata_models import MAX_SNAPSHOT_BYTES, snapshot_bytes
from app.services import metadata_files as files
from app.services.metadata_recovery_files import ConfirmedCheck, ConfirmedExpectation
from test_metadata_files import (
    MARKER, case, snapshot, prepare, final, temporary, unavailable,
)
from test_metadata_recovery_files import recovery, closed
from test_metadata_recovery_inventory import descriptors, identity


pytestmark = pytest.mark.skipif(not files._SUPPORTED, reason='Linux dirfd required')


def expectation(c, data=None, **changes):
    data = c.data if data is None else data
    model = c.model
    return replace(ConfirmedExpectation(
        model.revision_id, model.parent_revision_id,
        hashlib.sha256(data).hexdigest(), len(data), model.state,
        model.created_at, model.author.actor_subject, model.author.auth_source,
    ), **changes)


def seed(c, data=None):
    if data is None:
        prepare(c).publish(c.data)
    else:
        c.directory.mkdir(parents=True)
        final(c).write_bytes(data)


def preserved(paths):
    return tuple((identity(path), path.read_bytes()) for path in paths)


@contextmanager
def io_watch(monkeypatch):
    original_read, original_unlink, original_sync = os.pread, os.unlink, os.fsync
    state = SimpleNamespace(reads=[], unlinks=[], syncs=[])

    def reading(fd, count, offset):
        data = original_read(fd, count, offset)
        state.reads.append((fd, count, offset, len(data)))
        return data

    def unlinking(*args, **kwargs):
        state.unlinks.append(args)
        return original_unlink(*args, **kwargs)

    def syncing(fd):
        state.syncs.append(fd)
        return original_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(files.os, 'pread', reading)
        patch.setattr(files.os, 'unlink', unlinking)
        patch.setattr(files.os, 'fsync', syncing)
        yield state


def test_valid_pins_repeat_bounded_reads_and_protected_cleanup(case, monkeypatch):
    c = case
    seed(c)
    orphan = c.directory / ('e' * 32 + '.json')
    orphan.write_bytes(b'unreferenced partial')
    before = preserved([final(c)])
    expected = expectation(c)
    with pytest.raises(FrozenInstanceError):
        expected.state = 'approved'
    with descriptors(monkeypatch) as tracked, io_watch(monkeypatch) as io:
        with recovery(c).open(c.job) as session:
            inventory = session.inventory()
            pins = tuple(session._inventory_fds)
            pin_stats = tuple(os.fstat(fd) for fd in pins)
            opened = len(tracked.opened)
            original = os.pread

            def small(fd, count, offset):
                assert 0 < count <= min(65536, MAX_SNAPSHOT_BYTES + 1 - offset)
                return original(fd, min(count, 7), offset)

            with monkeypatch.context() as patch:
                patch.setattr(files.os, 'pread', small)
                for _ in range(2):
                    start = len(io.reads)
                    result = session.verify_confirmed(inventory, (expected,))
                    assert result == (ConfirmedCheck(c.model.revision_id, 'valid'),)
                    assert type(result[0]) is ConfirmedCheck
                    with pytest.raises(FrozenInstanceError):
                        result[0].status = 'missing'
                    offset = 0
                    for fd, count, position, length in io.reads[start:]:
                        assert fd in pins and position == offset and 0 < count <= 7
                        offset += length
                    assert offset == len(c.data) <= MAX_SNAPSHOT_BYTES + 1
            assert len(tracked.opened) == opened
            assert tuple(session._inventory_fds) == pins
            assert tuple(os.fstat(fd) for fd in pins) == pin_stats
            assert set(pins) <= tracked.live
            assert io.unlinks == io.syncs == []
            session.remove_orphans(inventory, (orphan.name,),
                                   protected_names=(final(c).name,))
            assert len(tracked.opened) == opened
        assert result[0].status == 'valid'
    closed(pins)
    assert not orphan.exists() and preserved([final(c)]) == before


@pytest.mark.parametrize('existing', [False, True])
def test_missing_on_stable_empty_namespace_creates_nothing(case, monkeypatch, existing):
    c = case
    if existing:
        c.directory.mkdir(parents=True)
    before = tuple(sorted(c.job.working_dir.rglob('*')))
    with descriptors(monkeypatch) as tracked, io_watch(monkeypatch) as io:
        with recovery(c).open(c.job) as session:
            inventory = session.inventory()
            opened = len(tracked.opened)
            result = session.verify_confirmed(inventory, (expectation(c),))
            assert len(tracked.opened) == opened
        assert result == (ConfirmedCheck(c.model.revision_id, 'missing'),)
        assert io.reads == io.unlinks == io.syncs == []
    assert tuple(sorted(c.job.working_dir.rglob('*'))) == before
    assert c.directory.exists() is existing
    assert c.directory.parent.exists() is existing


def test_mixed_results_preserve_expectation_order(case, monkeypatch):
    seed(case)
    bad_id, missing_id = 'e' * 32, 'f' * 32
    bad = case.directory / (bad_id + '.json')
    bad.write_bytes(b'not JSON')
    paths = [final(case), bad]
    before = preserved(paths)
    expected = (expectation(case, b'not JSON', revision_id=bad_id),
                expectation(case, revision_id=missing_id), expectation(case))
    with descriptors(monkeypatch), io_watch(monkeypatch) as io:
        with recovery(case).open(case.job) as session:
            result = session.verify_confirmed(session.inventory(), expected)
        assert result == tuple(ConfirmedCheck(item.revision_id, status) for item, status in
                               zip(expected, ('content_invalid', 'missing', 'valid')))
        assert io.unlinks == io.syncs == []
    assert preserved(paths) == before


@pytest.mark.parametrize('damage', [
    'zero', 'size', 'hash', 'json', 'utf8', 'noncanonical', 'structure',
    'parent_revision', 'created_at', 'actor', 'auth', 'recorded_at', 'execution',
    'job', 'document', 'parent_job', 'revision',
])
def test_content_failures_preserve_every_byte_and_stat(case, monkeypatch, damage):
    c = case
    data, changes = c.data, {}
    document = json.loads(data)
    if damage in ('zero', 'json', 'utf8'):
        data = {'zero': b'', 'json': b'{', 'utf8': b'\xff'}[damage]
    elif damage == 'size':
        changes['size_bytes'] = len(data) + 1
    elif damage == 'hash':
        changes['sha256'] = '0' * 64
    elif damage == 'noncanonical':
        data = json.dumps(document, indent=2).encode()
    elif damage in ('parent_revision', 'created_at', 'actor', 'auth'):
        key, value = {
            'parent_revision': ('parent_revision_id', 'd' * 32),
            'created_at': ('created_at', c.model.created_at + timedelta(seconds=1)),
            'actor': ('actor_subject', 'user:2'), 'auth': ('auth_source', 'oauth'),
        }[damage]
        changes[key] = value
    elif damage in ('recorded_at', 'execution'):
        # Both author variants are permitted by the structural C1 model.
        author = replace_author = c.model.author.model_copy(update={
            ('recorded_at' if damage == 'recorded_at' else 'execution_id'):
            (c.model.created_at + timedelta(seconds=1) if damage == 'recorded_at'
             else 'd' * 32),
        })
        data = snapshot_bytes(c.model.model_copy(update={'author': replace_author}))
        assert author != c.model.author
    else:
        if damage == 'structure':
            document['page_count'] = True
        elif damage == 'job':
            document['job_id'] = document['document_id'] = 'd' * 32
        else:
            document[{'document': 'document_id', 'parent_job': 'parent_job_id',
                      'revision': 'revision_id'}[damage]] = 'd' * 32
        data = json.dumps(document, sort_keys=True, separators=(',', ':')).encode()
    # Zero bytes cannot be a valid SQL size; keep the valid positive expectation.
    expected = expectation(c, data if data else c.data, **changes)
    seed(c, data)
    before = preserved([final(c)])
    with descriptors(monkeypatch), io_watch(monkeypatch) as io:
        with recovery(c).open(c.job) as session:
            result = session.verify_confirmed(session.inventory(), (expected,))
        assert result == (ConfirmedCheck(c.model.revision_id, 'content_invalid'),)
        assert io.reads and io.unlinks == io.syncs == []
    assert preserved([final(c)]) == before


@pytest.mark.parametrize('error_type', [OSError, ValueError, KeyboardInterrupt, SystemExit])
def test_pread_errors_are_fatal_and_control_exception_identity_survives(case, monkeypatch, error_type):
    seed(case)
    before, error, returned, injected = preserved([final(case)]), error_type(MARKER), [], []
    with descriptors(monkeypatch), io_watch(monkeypatch) as io:
        original = os.pread

        def fault(*args):
            original(*args)
            injected.append(True)
            raise error

        def operation():
            with recovery(case).open(case.job) as session:
                inventory = session.inventory()
                with monkeypatch.context() as patch:
                    patch.setattr(files.os, 'pread', fault)
                    returned.append(session.verify_confirmed(inventory, (expectation(case),)))

        if error_type in (KeyboardInterrupt, SystemExit):
            with pytest.raises(error_type) as caught:
                operation()
            assert caught.value is error
        else:
            unavailable(operation)
        assert injected == [True] and returned == [] and io.reads
        assert io.unlinks == io.syncs == []
    assert preserved([final(case)]) == before


@pytest.mark.parametrize('change', ['truncate', 'replace', 'ancestry', 'unselected'])
def test_mutations_during_real_pread_reject_and_snapshot_reuse_still_rejects(case, tmp_path, monkeypatch, change):
    c = case
    seed(c)
    other = c.directory / ('e' * 32 + '.json')
    other.write_bytes(b'unselected original')
    outside = tmp_path / 'outside'
    outside.write_bytes(MARKER.encode())
    held = c.job.working_dir / 'held-confirmed'
    moved = c.directory.with_name('held-metadata')
    injected = []
    with descriptors(monkeypatch), io_watch(monkeypatch) as io:
        original = os.pread

        def fault(*args):
            data = original(*args)
            if not injected:
                injected.append(True)
                if change == 'truncate':
                    final(c).write_bytes(b'x')
                elif change == 'replace':
                    final(c).rename(held)
                    final(c).write_bytes(MARKER.encode())
                elif change == 'ancestry':
                    c.directory.rename(moved)
                    c.directory.mkdir()
                    final(c).write_bytes(MARKER.encode())
                else:
                    other.write_bytes(MARKER.encode())
            return data

        def operation():
            with recovery(c).open(c.job) as session:
                inventory = session.inventory()
                with monkeypatch.context() as patch:
                    patch.setattr(files.os, 'pread', fault)
                    unavailable(lambda: session.verify_confirmed(inventory, (expectation(c),)))
                reads = len(io.reads)
                assert session._inventory is inventory
                unavailable(lambda: session.verify_confirmed(inventory, (expectation(c),)))
                assert len(io.reads) == reads

        if change == 'ancestry':
            unavailable(operation)  # Normal finish independently checks ancestry.
        else:
            operation()
        assert injected == [True] and io.reads and io.unlinks == io.syncs == []
    assert outside.read_bytes() == MARKER.encode()
    assert final(c).read_bytes() == (b'x' if change == 'truncate' else
                                   c.data if change == 'unselected' else MARKER.encode())
    if change == 'replace':
        assert held.read_bytes() == c.data
    if change == 'ancestry':
        assert (moved / final(c).name).read_bytes() == c.data
        assert (moved / other.name).read_bytes() == b'unselected original'
    else:
        assert other.read_bytes() == (MARKER.encode() if change == 'unselected'
                                      else b'unselected original')


def test_pin_close_after_effect_invalidates_provisional_valid_result(case, monkeypatch):
    seed(case)
    before, results, close_calls, injected = preserved([final(case)]), [], [], []
    with descriptors(monkeypatch) as tracked, io_watch(monkeypatch) as io:
        tracked_close = os.close

        def operation(patch):
            with recovery(case).open(case.job) as session:
                inventory = session.inventory()
                pins = tuple(session._inventory_fds)
                results.append(session.verify_confirmed(inventory, (expectation(case),)))
                assert results[-1][0].status == 'valid'

                def close_after(fd):
                    close_calls.append(fd)
                    tracked_close(fd)
                    if fd == pins[0]:
                        injected.append(fd)
                        raise OSError(MARKER)

                patch.setattr(files.os, 'close', close_after)

        with monkeypatch.context() as patch:
            unavailable(lambda: operation(patch))
        assert len(injected) == 1 and len(close_calls) == len(tracked.opened)
        assert set(close_calls) == set(tracked.opened) and not tracked.live
        assert io.unlinks == io.syncs == []
    assert preserved([final(case)]) == before


@pytest.mark.parametrize('shape', ['confirmed_pair', 'two_finals', 'external', 'orphan_pair'])
def test_entire_inventory_hardlink_rules_and_unconsumed_orphan_pair(case, tmp_path, monkeypatch, shape):
    c = case
    seed(c, b'partial' if shape in ('external', 'orphan_pair') else c.data)
    second = (tmp_path / 'external-link' if shape == 'external' else
              c.directory / ('e' * 32 + '.json') if shape == 'two_finals' else temporary(c))
    os.link(final(c), second)
    paths, expected = [final(c), second], ()
    if shape in ('confirmed_pair', 'two_finals'):
        expected = (expectation(c),)
    before = preserved(paths)
    with descriptors(monkeypatch), io_watch(monkeypatch) as io:
        with recovery(c).open(c.job) as session:
            inventory = session.inventory()
            if shape == 'orphan_pair':
                result = session.verify_confirmed(inventory, expected)
                assert result == () and io.unlinks == io.syncs == []
                session.remove_orphans(inventory, tuple(path.name for path in paths),
                                       protected_names=())
            else:
                unavailable(lambda: session.verify_confirmed(inventory, expected))
                assert io.reads == io.unlinks == io.syncs == []
    if shape == 'orphan_pair':
        assert all(not path.exists() for path in paths) and len(io.unlinks) == 2
    else:
        assert preserved(paths) == before


@pytest.mark.parametrize('invalid', [
    'list', '33', 'duplicate', 'hex', 'bool_size', 'naive', 'nonutc',
    'actor', 'auth', 'approved', 'self_parent',
])
def test_invalid_expectations_reject_before_any_pread(case, monkeypatch, invalid):
    seed(case)
    good = expectation(case)
    mutations = {
        'hex': {'revision_id': 'G' * 32}, 'bool_size': {'size_bytes': True},
        'naive': {'created_at': good.created_at.replace(tzinfo=None)},
        'nonutc': {'created_at': good.created_at.astimezone(timezone(timedelta(hours=1)))},
        'actor': {'actor_subject': MARKER}, 'auth': {'auth_source': MARKER},
        'approved': {'state': 'approved'}, 'self_parent': {'parent_revision_id': good.revision_id},
    }
    supplied = ([good] if invalid == 'list' else (good,) * 33 if invalid == '33' else
                (good, good) if invalid == 'duplicate' else
                (good, replace(good, **mutations[invalid])))
    before = preserved([final(case)])
    with descriptors(monkeypatch), io_watch(monkeypatch) as io:
        with recovery(case).open(case.job) as session:
            inventory = session.inventory()
            unavailable(lambda: session.verify_confirmed(inventory, supplied))
        assert io.reads == io.unlinks == io.syncs == []
    assert preserved([final(case)]) == before


@pytest.mark.parametrize('authority', ['foreign', 'forged', 'consumed', 'closed'])
def test_snapshot_authority_rejects_without_reading(case, monkeypatch, authority):
    seed(case)
    before = preserved([final(case)])
    with descriptors(monkeypatch), io_watch(monkeypatch) as io:
        if authority == 'foreign':
            with recovery(case).open(case.job) as other:
                foreign = other.inventory()
        with recovery(case).open(case.job) as session:
            inventory = session.inventory()
            supplied = foreign if authority == 'foreign' else (
                replace(inventory) if authority == 'forged' else inventory)
            if authority == 'consumed':
                session.remove_orphans(inventory, (), protected_names=(final(case).name,))
            syncs = len(io.syncs)
            if authority != 'closed':
                unavailable(lambda: session.verify_confirmed(supplied, (expectation(case),)))
        if authority == 'closed':
            unavailable(lambda: session.verify_confirmed(supplied, (expectation(case),)))
        assert io.reads == io.unlinks == [] and len(io.syncs) == syncs
    assert preserved([final(case)]) == before


def test_exactly_32_missing_expectations_are_supported(case, monkeypatch):
    # Use an independently constructed valid snapshot to derive the draft header.
    model = snapshot(case.job)
    base = replace(expectation(case), created_at=model.created_at,
                   actor_subject=model.author.actor_subject, auth_source=model.author.auth_source)
    expected = tuple(replace(base, revision_id=f'{index:032x}') for index in range(32))
    with descriptors(monkeypatch), io_watch(monkeypatch) as io:
        with recovery(case).open(case.job) as session:
            result = session.verify_confirmed(session.inventory(), expected)
        assert result == tuple(ConfirmedCheck(item.revision_id, 'missing') for item in expected)
        assert io.reads == io.unlinks == io.syncs == []
    assert not case.directory.parent.exists()
