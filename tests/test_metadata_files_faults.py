"""Linux physical-file fault tests; no SQL or integration acceptance."""
import json
import os
import stat

import pytest

from app.document_models import FieldSpec, compose_schema
from app.metadata_models import (
    MAX_SNAPSHOT_BYTES, FieldValueRecord, SchemaSnapshot, snapshot_bytes,
)
from app.services import metadata_files as files
from test_metadata_files import (
    MARKER, case, prepare, read, final, temporary, arguments, unavailable,
)


def sentinel(c):
    c.directory.mkdir(parents=True, exist_ok=True)
    path = c.directory / "unrelated.txt"
    path.write_bytes(MARKER.encode())
    return path


def assert_only_sentinel(c, path):
    assert path.read_bytes() == MARKER.encode()
    assert list(c.directory.iterdir()) == [path]


def test_exclusive_open_creates_then_raises_without_returning_fd(case, monkeypatch):
    c = case
    kept = sentinel(c)
    attempt = prepare(c)
    original = files.os.open
    intercepted = []
    exclusive = os.O_CREAT | os.O_EXCL

    def fault(name, flags, *args, **kwargs):
        fd = original(name, flags, *args, **kwargs)
        if name == temporary(c).name and flags & exclusive == exclusive:
            intercepted.append(name)
            os.close(fd)
            raise OSError(MARKER)
        return fd

    with monkeypatch.context() as patch:
        patch.setattr(files.os, "open", fault)
        unavailable(lambda: attempt.publish(c.data))
    assert intercepted == [temporary(c).name]
    assert temporary(c).is_file() and temporary(c).read_bytes() == b""
    inode = temporary(c).stat().st_ino
    unavailable(attempt.cleanup)
    assert temporary(c).stat().st_ino == inode
    assert not final(c).exists()
    assert kept.read_bytes() == MARKER.encode()
    # Fixture operator proves absence; cleanup must not adopt the unknown inode.
    temporary(c).unlink()
    attempt.cleanup()
    attempt.cleanup()
    assert_only_sentinel(c, kept)


def test_fstat_identity_failure_closes_fd_but_retains_unknown_temp(case, monkeypatch):
    c = case
    kept = sentinel(c)
    attempt = prepare(c)
    original = files.os.fstat
    intercepted = []

    def fault(fd):
        info = original(fd)
        if stat.S_ISREG(info.st_mode):
            intercepted.append(fd)
            raise OSError(MARKER)
        return info

    before = len(os.listdir("/proc/self/fd"))
    stable = before == len(os.listdir("/proc/self/fd"))
    with monkeypatch.context() as patch:
        patch.setattr(files.os, "fstat", fault)
        unavailable(lambda: attempt.publish(c.data))
    after = len(os.listdir("/proc/self/fd"))
    assert len(intercepted) == 1
    if stable:
        assert after == before
    with pytest.raises(OSError):
        original(intercepted[0])
    assert temporary(c).is_file() and temporary(c).read_bytes() == b""
    inode = temporary(c).stat().st_ino
    unavailable(attempt.cleanup)
    assert temporary(c).stat().st_ino == inode
    assert not final(c).exists()
    temporary(c).unlink()
    attempt.cleanup()
    assert_only_sentinel(c, kept)


def test_link_takes_effect_then_raises_without_adopting_final(case, monkeypatch):
    c = case
    kept = sentinel(c)
    attempt = prepare(c)
    original = files.os.link
    calls = []

    def fault(*args, **kwargs):
        original(*args, **kwargs)
        calls.append(args)
        raise OSError(MARKER)

    with monkeypatch.context() as patch:
        patch.setattr(files.os, "link", fault)
        unavailable(lambda: attempt.publish(c.data))
    assert len(calls) == 1
    inode = final(c).stat().st_ino
    assert inode == temporary(c).stat().st_ino
    assert final(c).read_bytes() == temporary(c).read_bytes() == c.data
    unavailable(attempt.cleanup)
    assert not temporary(c).exists()
    assert final(c).stat().st_ino == inode
    assert final(c).read_bytes() == c.data
    assert kept.read_bytes() == MARKER.encode()
    final(c).unlink()
    attempt.cleanup()
    attempt.cleanup()
    assert_only_sentinel(c, kept)


@pytest.mark.parametrize("stage", ["publish", "cleanup"])
def test_unlink_takes_effect_then_raises_and_absence_is_proven(case, monkeypatch, stage):
    c = case
    kept = sentinel(c)
    attempt = prepare(c)
    original = files.os.unlink
    name = temporary(c).name if stage == "publish" else final(c).name
    calls = []

    def fault(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == name:
            calls.append(path)
            raise OSError(MARKER)
        return result

    if stage == "cleanup":
        attempt.publish(c.data)
        assert snapshot_bytes(read(c)) == c.data
    with monkeypatch.context() as patch:
        patch.setattr(files.os, "unlink", fault)
        if stage == "publish":
            attempt.publish(c.data)
            assert not temporary(c).exists()
            assert snapshot_bytes(read(c)) == c.data
        else:
            attempt.cleanup()
            assert not final(c).exists()
    assert calls == [name]
    attempt.cleanup()
    attempt.cleanup()
    assert_only_sentinel(c, kept)


@pytest.mark.parametrize("takes_effect", [False, True])
def test_cleanup_requires_successful_directory_sync_retry(case, monkeypatch, takes_effect):
    c = case
    kept = sentinel(c)
    attempt = prepare(c)
    attempt.publish(c.data)
    original = files.os.fsync
    calls = []

    def fault(fd):
        assert stat.S_ISDIR(os.fstat(fd).st_mode)
        calls.append(fd)
        if takes_effect:
            original(fd)
        raise OSError(MARKER)

    with monkeypatch.context() as patch:
        patch.setattr(files.os, "fsync", fault)
        unavailable(attempt.cleanup)
    assert len(calls) == 1
    assert_only_sentinel(c, kept)
    synced = []

    def tracked(fd):
        original(fd)
        synced.append(fd)

    with monkeypatch.context() as patch:
        patch.setattr(files.os, "fsync", tracked)
        attempt.cleanup()
    assert len(synced) == 1
    attempt.cleanup()
    assert_only_sentinel(c, kept)


def test_same_length_write_corruption_requires_verified_read_before_commit(case, monkeypatch):
    c = case
    kept = sentinel(c)
    attempt = prepare(c)
    original = files.os.write
    counts = []

    def altered(fd, data):
        count = original(fd, bytes(value ^ 1 for value in data))
        counts.append(count)
        return count

    with monkeypatch.context() as patch:
        patch.setattr(files.os, "write", altered)
        attempt.publish(c.data)
    assert sum(counts) == len(c.data)
    assert final(c).stat().st_size == len(c.data)
    assert final(c).read_bytes() != c.data
    # Publish validates input, not physical contents. A coordinator must read
    # verified bytes before committing its SQL index; no SQL is exercised here.
    unavailable(lambda: read(c))
    attempt.cleanup()
    assert_only_sentinel(c, kept)


def boundary_snapshot(c):
    values = [[""] * 100 for _ in range(2)]
    description = "Boundary"
    author = c.model.author

    def build():
        specs = [FieldSpec(name=name, type="string", cardinality="many",
                           max_items=100, description=description)
                 for name in ("first", "second")]
        composed = compose_schema(specs, [])
        fields = [FieldValueRecord(
            field_name=spec.name, raw=list(items), normalized=list(items),
            origin="manual", status="needs_review", evidence=[], provenance=author,
        ) for spec, items in zip(specs, values)]
        segment = c.model.segments[0].model_copy(update={
            "schema_sha256": composed.sha256, "fields": fields,
        })
        return c.model.model_copy(update={
            "author": author, "segments": [segment],
            "schemas": [SchemaSnapshot(schema=json.loads(composed.canonical),
                                       schema_sha256=composed.sha256)],
        })

    baseline = snapshot_bytes(build())
    if (MAX_SNAPSHOT_BYTES - len(baseline)) % 2:
        description += "x"
        baseline = snapshot_bytes(build())
        # Composed envelopes may repeat descriptions, preserving parity.
        # One extra actor digit appears three times: author and both fields.
        if (MAX_SNAPSHOT_BYTES - len(baseline)) % 2:
            author = author.model_copy(update={"actor_subject": "user:10"})
            baseline = snapshot_bytes(build())
    remaining = MAX_SNAPSHOT_BYTES - len(baseline)
    assert remaining >= 0 and remaining % 2 == 0
    remaining //= 2
    for items in values:
        for index in range(len(items)):
            count = min(4000, remaining)
            items[index] = "x" * count
            remaining -= count
    assert remaining == 0
    model = build()
    data = snapshot_bytes(model)  # Strict recursive validation before any I/O.
    assert len(data) == MAX_SNAPSHOT_BYTES
    return model, data


def test_exact_maximum_canonical_snapshot_and_plus_one_rejections(case, monkeypatch):
    c = case
    c.model, c.data = boundary_snapshot(c)
    attempt = prepare(c)
    attempt.publish(c.data)
    assert final(c).stat().st_size == MAX_SNAPSHOT_BYTES
    assert final(c).read_bytes() == c.data
    original = files.os.read
    requests, returned = [], []

    def tracked(fd, count):
        requests.append(count)
        result = original(fd, count)
        returned.append(len(result))
        return result

    with monkeypatch.context() as patch:
        patch.setattr(files.os, "read", tracked)
        assert snapshot_bytes(read(c)) == c.data
        assert requests and all(0 < count <= 65536 for count in requests)
        assert sum(returned) == MAX_SNAPSHOT_BYTES
        assert sum(requests) <= MAX_SNAPSHOT_BYTES + 1
        with final(c).open("ab") as stream:
            stream.write(b"x")
        assert final(c).stat().st_size == MAX_SNAPSHOT_BYTES + 1
        requests.clear()
        unavailable(lambda: read(c))
        assert requests == []
    options = arguments(c)
    options["size_bytes"] = MAX_SNAPSHOT_BYTES + 1
    unavailable(lambda: c.service.prepare(c.job, pending_id=c.pending, **options))
    attempt.cleanup()
    assert not final(c).exists() and not temporary(c).exists()


def test_unsupported_primitives_reject_publish_and_read(case, monkeypatch):
    c = case
    published = prepare(c)
    published.publish(c.data)
    attempt = prepare(c)
    with monkeypatch.context() as patch:
        patch.setattr(files, "_SUPPORTED", False)
        unavailable(lambda: attempt.publish(c.data))
        unavailable(lambda: read(c))
    assert final(c).read_bytes() == c.data
    assert not temporary(c).exists()
    attempt.cleanup()
    published.cleanup()
