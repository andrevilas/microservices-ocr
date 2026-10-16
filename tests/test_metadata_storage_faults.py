"""C3b T2: real SQLite outcomes and owned POSIX files under one artifact lease."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.application import ApplicationError
from app.metadata_models import MetadataSnapshot, SaveMetadataReceipt, snapshot_bytes
from app.services import metadata_files as files
from test_metadata_files import case, snapshot
from test_metadata_storage import (
    MARKER, TABLES, assert_error, assert_unindexed, coordination, make_write,
    sql_state, write,
)
import hashlib


def expire(c):
    # Caller holds jobs._lock, including when invoked inside commit().
    cached = c.jobs.jobs[c.job.job_id]
    cached.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    c.jobs._persist(cached)


def unavailable(c, operation):
    error = assert_error(operation)
    text = str(error) + repr(error) + repr(vars(error))
    assert str(c.job.working_dir) not in text
    assert 'SELECT ' not in text and 'INSERT ' not in text
    return error


def retained_error(c):
    with pytest.raises(ApplicationError) as caught:
        c.jobs.check_retained(c.jobs.jobs[c.job.job_id])
    assert caught.value.status == 410
    return caught.value.code


def final(c, w):
    return c.directory / (w.pending.revision_id + '.json')


def pending_state(c, w, state):
    with c.jobs._lock:
        pending = c.store._pending(c.jobs._get_conn(), w.pending.pending_id)
    assert pending is not None and pending.state == state
    assert (pending.charged_slots, pending.charged_bytes,
            pending.approval_slots, pending.approval_bytes) == (
                w.pending.charged_slots, w.pending.charged_bytes,
                w.pending.approval_slots, w.pending.approval_bytes)
    assert all(not sql_state(c)[t] for t in TABLES[:-1])
    assert c.store._active_writes == set()


def empty(c):
    assert_unindexed(c)
    assert not list(c.directory.glob('*.json'))
    assert not list(c.directory.glob('.*.tmp'))


class ConnectionFault:
    """Keep connection identity while forwarding every real SQLite operation."""

    def __init__(self, c, w, mode, after=False, on_effect=None):
        self.real = c.jobs._conn
        self.revision = w.pending.revision_id
        self.pending = w.pending.pending_id
        self.mode, self.after, self.on_effect = mode, after, on_effect
        self.phase = 'initial' if mode == 'initial' else 'final'
        self.fired = []
        self.rollbacks = 0

    def __getattr__(self, name):
        return getattr(self.real, name)

    def execute(self, sql, parameters=()):
        result = self.real.execute(sql, parameters)
        if (self.mode == 'query' and self.phase == 'diagnosis'
                and 'FROM metadata_operations WHERE revision_id=?' in sql
                and parameters and parameters[0] == self.revision):
            self.phase = 'stopped'
            self.fired.append('query')
            raise RuntimeError(MARKER)
        return result

    def rollback(self):
        self.rollbacks += 1
        return self.real.rollback()

    def commit(self):
        visible = self.real.execute(
            'SELECT 1 FROM metadata_revisions WHERE revision_id=?',
            (self.revision,)).fetchone() is not None
        row = self.real.execute(
            'SELECT state FROM metadata_pending WHERE pending_id=?',
            (self.pending,)).fetchone()
        state = row[0] if row is not None else None
        phase = self.phase
        trigger = (
            (phase == 'initial' and not visible and state == 'active')
            or (phase == 'final' and visible)
            or (phase == 'diagnosis' and not visible and state == 'cleanup'
                and self.mode == 'diagnosis')
            or (phase == 'release' and not visible and state is None
                and self.mode == 'release'))
        if trigger:
            self.fired.append(phase)
            self.phase = 'diagnosis' if phase in ('initial', 'final') else 'stopped'
            # Multi-fault scenarios first roll back the final index transaction.
            effect = self.after if phase != 'final' or self.mode == 'final' else False
            if effect:
                self.real.commit()
                if self.on_effect is not None:
                    self.on_effect()
            raise RuntimeError(MARKER)
        result = self.real.commit()
        if phase == 'diagnosis' and not visible and state == 'cleanup':
            self.phase = 'release'
        return result


@pytest.fixture
def fault_connection(coordination):
    c = coordination
    original = c.jobs._conn

    def install(w, mode='final', after=False, on_effect=None):
        proxy = ConnectionFault(c, w, mode, after, on_effect)
        c.jobs._conn = proxy
        assert c.jobs._get_conn() is proxy
        return proxy

    try:
        yield install
    finally:
        # The case fixture alone closes the underlying connection.
        c.jobs._conn = original


def observe(c, w, monkeypatch):
    events = []
    diagnosis = c.store._index_outcome_locked
    cleanup = files.WriteAttempt.cleanup
    absent = files.WriteAttempt.verify_absent
    release = c.store._release_pending_after_cleanup_locked

    def diagnosed(conn, reservation, receipt):
        assert final(c, w).read_bytes() == w.data
        result = diagnosis(conn, reservation, receipt)
        events.append(('diagnosis', result))
        return result

    def cleaned(attempt):
        cleanup(attempt)
        events.append('cleanup')

    def verified(attempt):
        absent(attempt)
        events.append('absent')

    def released(conn, pending_id):
        assert events[-2:] == ['cleanup', 'absent']
        assert not final(c, w).exists()
        result = release(conn, pending_id)
        events.append('release')
        return result

    # The second diagnosis occurs after physical absence, so observe it separately.
    def outcome(conn, reservation, receipt):
        if not events:
            return diagnosed(conn, reservation, receipt)
        return diagnosis(conn, reservation, receipt)

    monkeypatch.setattr(c.store, '_index_outcome_locked', outcome)
    monkeypatch.setattr(files.WriteAttempt, 'cleanup', cleaned)
    monkeypatch.setattr(files.WriteAttempt, 'verify_absent', verified)
    monkeypatch.setattr(c.store, '_release_pending_after_cleanup_locked', released)
    return events


@pytest.mark.parametrize('expired', [False, True])
def test_final_commit_effect_then_raise_preserves_confirmed_write(
        coordination, fault_connection, monkeypatch, expired):
    c = coordination
    w = make_write(c)
    events = observe(c, w, monkeypatch)
    proxy = fault_connection(w, after=True, on_effect=(lambda: expire(c)) if expired else None)
    callbacks = []
    unavailable(c, lambda: write(c, w, lambda conn: callbacks.append(conn)))
    assert callbacks == [proxy]
    assert proxy.fired == ['final'] and proxy.rollbacks >= 1
    assert events == [('diagnosis', 'confirmed')]
    assert [len(sql_state(c)[t]) for t in TABLES] == [1, 1, 1, 1, 0]
    assert c.store._active_writes == set()
    assert snapshot_bytes(c.service.read(
        c.job, revision_id=w.pending.revision_id, sha256=w.pending.sha256,
        size_bytes=w.pending.size_bytes)) == w.data
    if expired:
        code = retained_error(c)
        assert_error(lambda: c.store.preflight(
            c.principal, c.job.job_id, key_hash=w.key, request_hash=w.request), 410, code)
    else:
        replay = c.store.preflight(c.principal, c.job.job_id,
                                   key_hash=w.key, request_hash=w.request)
        assert replay.receipt == w.receipt and replay.receipt is not w.receipt


def test_final_commit_before_effect_cleans_only_after_fresh_diagnosis(
        coordination, fault_connection, monkeypatch):
    c = coordination
    w = make_write(c)
    events = observe(c, w, monkeypatch)
    proxy = fault_connection(w)
    unavailable(c, lambda: write(c, w))
    assert proxy.fired == ['final'] and proxy.rollbacks >= 1
    assert events == [('diagnosis', 'uncommitted'), 'cleanup', 'absent', 'release']
    empty(c)


def test_expiry_after_verified_read_prevents_callback_and_releases(
        coordination, monkeypatch):
    c = coordination
    w = make_write(c)
    original = files.MetadataFiles.read
    callbacks = []

    def read(service, *args, **kwargs):
        result = original(service, *args, **kwargs)
        assert snapshot_bytes(result) == w.data
        with c.jobs._lock:
            expire(c)
        return result

    monkeypatch.setattr(files.MetadataFiles, 'read', read)
    with pytest.raises(ApplicationError) as caught:
        write(c, w, lambda conn: callbacks.append(conn))
    code = retained_error(c)
    error = caught.value
    assert (error.status, error.code) == (410, code)
    assert error.__cause__ is None and error.__context__ is None
    assert MARKER not in str(error) + repr(vars(error))
    assert callbacks == []
    empty(c)


@pytest.mark.parametrize('mode,after,state', [
    ('diagnosis', False, 'active'), ('diagnosis', True, 'cleanup'),
    ('query', False, 'active'),
])
def test_uncertain_diagnosis_never_authorizes_physical_cleanup(
        coordination, fault_connection, monkeypatch, mode, after, state):
    c = coordination
    w = make_write(c)
    events = observe(c, w, monkeypatch)
    proxy = fault_connection(w, mode, after)
    unavailable(c, lambda: write(c, w))
    assert proxy.fired == ['final', mode]
    assert not any(event in ('cleanup', 'absent', 'release') for event in events)
    assert final(c, w).read_bytes() == w.data
    pending_state(c, w, state)


@pytest.mark.parametrize('collision', ['final', 'temp'])
def test_foreign_collision_remains_charged(coordination, collision):
    c = coordination
    w = make_write(c)
    c.directory.mkdir(parents=True)
    path = final(c, w) if collision == 'final' else c.directory / ('.' + w.pending.pending_id + '.tmp')
    path.write_bytes(MARKER.encode())
    inode = path.stat().st_ino
    unavailable(c, lambda: write(c, w))
    assert path.read_bytes() == MARKER.encode() and path.stat().st_ino == inode
    assert list(c.directory.iterdir()) == [path]
    pending_state(c, w, 'cleanup')


def callback_failure(conn):
    raise ApplicationError(503, 'metadata_unavailable', 'Metadata is unavailable.')


def test_owned_unlink_failure_retains_file_and_charge(coordination, monkeypatch):
    c = coordination
    w = make_write(c)
    original = files.os.unlink
    release = c.store._release_pending_after_cleanup_locked
    attempts, releases = [], []

    def unlink(name, *args, **kwargs):
        fd = kwargs.get('dir_fd')
        if name == final(c, w).name and fd is not None:
            info = files.os.fstat(fd)
            expected = c.directory.stat()
            if (info.st_dev, info.st_ino) == (expected.st_dev, expected.st_ino):
                attempts.append(name)
                raise RuntimeError(MARKER)
        return original(name, *args, **kwargs)

    def released(*args):
        releases.append(args)
        return release(*args)

    with monkeypatch.context() as patch:
        patch.setattr(files.os, 'unlink', unlink)
        patch.setattr(c.store, '_release_pending_after_cleanup_locked', released)
        unavailable(c, lambda: write(c, w, callback_failure))
    assert attempts == [final(c, w).name] and releases == []
    assert final(c, w).read_bytes() == w.data
    pending_state(c, w, 'cleanup')


@pytest.mark.parametrize('after', [False, True])
def test_release_commit_failure_reports_actual_durable_reservation(
        coordination, fault_connection, monkeypatch, after):
    c = coordination
    w = make_write(c)
    events = observe(c, w, monkeypatch)
    proxy = fault_connection(w, 'release', after)
    # No final index commit occurs: the callback fails first.
    proxy.phase = 'diagnosis'
    unavailable(c, lambda: write(c, w, callback_failure))
    assert proxy.fired == ['release']
    assert events == [('diagnosis', 'uncommitted'), 'cleanup', 'absent', 'release']
    assert not final(c, w).exists() and not list(c.directory.glob('.*.tmp'))
    if after:
        empty(c)
    else:
        pending_state(c, w, 'cleanup')


@pytest.mark.parametrize('after', [False, True])
def test_initial_commit_failure_reconciles_safe_absence(
        coordination, fault_connection, monkeypatch, after):
    c = coordination
    w = make_write(c)
    proxy = fault_connection(w, 'initial', after)
    publications = []
    original = files.WriteAttempt.publish

    def publish(attempt, data):
        publications.append(attempt)
        return original(attempt, data)

    monkeypatch.setattr(files.WriteAttempt, 'publish', publish)
    unavailable(c, lambda: write(c, w))
    assert proxy.fired == ['initial'] and proxy.rollbacks >= 1
    assert publications == []
    assert not (c.job.working_dir / 'results').exists()
    empty(c)


def test_confirmed_diagnosis_allows_legitimately_advanced_head(coordination):
    c = coordination
    first = make_write(c)
    assert write(c, first) == first.receipt
    document = snapshot(c.job).model_dump(mode='python', by_alias=True)
    document['parent_revision_id'] = first.pending.revision_id
    document['created_at'] = first.model.created_at + timedelta(seconds=1)
    document['author'] = first.model.author.model_dump(mode='python')
    document['author']['recorded_at'] = document['created_at']
    model = MetadataSnapshot.model_validate(document, strict=True)
    data = snapshot_bytes(model)
    digest = hashlib.sha256(data).hexdigest()
    admission = c.store.reserve(
        c.principal, c.job.job_id, key_hash='3' * 64, request_hash='4' * 64,
        expected_head=first.pending.revision_id, expected_sha256=first.pending.sha256,
        revision_id=model.revision_id, sha256=digest, size_bytes=len(data),
        created_at=model.created_at)
    second = SimpleNamespace(
        principal=c.principal, job=c.job, pending=admission.pending, data=data,
        receipt=SaveMetadataReceipt(revision_id=model.revision_id, sha256=digest,
                                    segment_ids={'row': model.segments[0].segment_id}))
    assert write(c, second) == second.receipt
    with c.store._transaction() as conn:
        assert c.store._index_outcome_locked(conn, first.pending, first.receipt) == 'confirmed'
    assert c.store.get_head(c.principal, c.job.job_id).revision_id == model.revision_id
    for w in (first, second):
        assert snapshot_bytes(c.service.read(
            c.job, revision_id=w.pending.revision_id, sha256=w.pending.sha256,
            size_bytes=w.pending.size_bytes)) == w.data
    assert [len(sql_state(c)[t]) for t in TABLES] == [1, 2, 2, 2, 0]
    assert c.store._active_writes == set()


def test_same_size_physical_corruption_is_rejected_before_index(coordination, monkeypatch):
    c = coordination
    w = make_write(c)
    original = files.WriteAttempt.publish
    callbacks, corrupted, events = [], [], []
    cleanup = files.WriteAttempt.cleanup
    absent = files.WriteAttempt.verify_absent
    release = c.store._release_pending_after_cleanup_locked

    def publish(attempt, data):
        original(attempt, data)
        damaged = bytes([data[0] ^ 1]) + data[1:]
        final(c, w).write_bytes(damaged)
        assert final(c, w).stat().st_size == len(data)
        corrupted.append(damaged)

    def cleaned(attempt):
        assert final(c, w).read_bytes() == corrupted[0]
        cleanup(attempt)
        events.append('cleanup')

    def verified(attempt):
        absent(attempt)
        events.append('absent')

    def released(conn, pending_id):
        assert events == ['cleanup', 'absent']
        result = release(conn, pending_id)
        events.append('release')
        return result

    monkeypatch.setattr(files.WriteAttempt, 'publish', publish)
    monkeypatch.setattr(files.WriteAttempt, 'cleanup', cleaned)
    monkeypatch.setattr(files.WriteAttempt, 'verify_absent', verified)
    monkeypatch.setattr(c.store, '_release_pending_after_cleanup_locked', released)
    unavailable(c, lambda: write(c, w, lambda conn: callbacks.append(conn)))
    assert len(corrupted) == 1 and callbacks == []
    assert events == ['cleanup', 'absent', 'release']
    empty(c)
