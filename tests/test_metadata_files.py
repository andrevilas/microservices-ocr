"""C3b physical files only; Linux execution and artifact leases are required.

No index commit is performed here: every cleanup aborts an uncommitted attempt.
The ancestry-swap regression intentionally requires rejection of lost ownership.
"""
import hashlib
import json
import os
import stat
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.application import ApplicationError
from app.document_models import FieldSpec, compose_schema
from app.metadata_models import (
    MAX_SNAPSHOT_BYTES, MetadataSnapshot, ProvenanceRecord,
    SchemaSnapshot, SegmentRecord, snapshot_bytes,
)
from app.services import metadata_files as files
from app.services.storage_service import JobStore


MARKER = "SENSITIVE_METADATA_MARKER"


def unavailable(operation):
    with pytest.raises(ApplicationError) as caught:
        operation()
    error = caught.value
    assert getattr(error, "status_code", getattr(error, "status", None)) == 503
    assert error.code == "metadata_unavailable"
    assert error.message == "Stored metadata is unavailable."
    assert error.__cause__ is None
    assert error.__context__ is None
    assert MARKER not in str(error) + repr(error) + repr(vars(error))


def snapshot(job):
    composed = compose_schema(
        [FieldSpec(name="title", type="string", description="Synthetic")], []
    )
    now = datetime.now(timezone.utc)
    return MetadataSnapshot(
        revision_id=uuid4().hex, parent_revision_id=None,
        job_id=job.job_id, document_id=job.job_id,
        canonical_sha256="a" * 64, input_pdf_sha256="b" * 64,
        output_pdf_sha256="c" * 64, page_count=1,
        parent_job_id=job.parent_job_id, created_at=now,
        author=ProvenanceRecord(
            actor_subject="user:1", auth_source="local_jwt", recorded_at=now
        ),
        schemas=[SchemaSnapshot(
            schema=json.loads(composed.canonical), schema_sha256=composed.sha256
        )],
        segments=[SegmentRecord(
            segment_id=uuid4().hex, page_start=1, page_end=1,
            context_id=uuid4().hex, type_id=uuid4().hex, type_version=1,
            schema_sha256=composed.sha256, fields=[]
        )], state="draft",
    )


@pytest.fixture
def case(tmp_path, request):
    jobs = JobStore(tmp_path.absolute() / "jobs")
    job = jobs.create("synthetic.pdf", b"%PDF-1.4\nsynthetic\n", owner_user_id=1)
    jobs.update(job.job_id, status="completed", progress_percent=100)
    try:
        with jobs.artifact_lease(job.job_id, "user:1") as leased:
            if getattr(request, "param", "legacy") == "admitted":
                directory = jobs.root_dir / "admitted-jobs" / leased.job_id
                directory.mkdir(parents=True)
                input_path = directory / "original.pdf"
                input_path.write_bytes(b"%PDF-1.4\nsynthetic admitted\n")
                leased = replace(leased, working_dir=directory, input_pdf_path=input_path)
            model = snapshot(leased)
            data = snapshot_bytes(model)
            yield SimpleNamespace(
                jobs=jobs, job=leased, model=model, data=data,
                digest=hashlib.sha256(data).hexdigest(),
                service=files.MetadataFiles(jobs.root_dir),
                directory=leased.working_dir / "results" / "metadata",
                pending=uuid4().hex,
            )
    finally:
        if jobs._conn is not None:
            jobs._conn.close()


def arguments(c, data=None):
    data = c.data if data is None else data
    return dict(revision_id=c.model.revision_id,
                sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))


def prepare(c, **changes):
    options = dict(pending_id=c.pending, **arguments(c))
    options.update(changes)
    return c.service.prepare(c.job, **options)


def read(c, service=None, **changes):
    options = arguments(c)
    options.update(changes)
    return (service or c.service).read(c.job, **options)


def final(c):
    return c.directory / (c.model.revision_id + ".json")


def temporary(c):
    return c.directory / ("." + c.pending + ".tmp")


@pytest.mark.parametrize("case", ["legacy", "admitted"], indirect=True)
def test_prepare_is_pure_and_publication_round_trips(case, monkeypatch):
    c = case
    assert not (c.job.working_dir / "results").exists()
    def forbidden(*args, **kwargs):
        raise AssertionError("prepare attempted filesystem I/O")
    with monkeypatch.context() as patch:
        for name in ("open", "stat", "fstat", "mkdir", "link", "unlink",
                     "read", "write", "fsync", "close"):
            patch.setattr(files.os, name, forbidden)
        attempt = prepare(c)
    assert not (c.job.working_dir / "results").exists()
    attempt.publish(c.data)
    assert stat.S_ISREG(final(c).lstat().st_mode)
    assert final(c).read_bytes() == c.data
    assert not temporary(c).exists()
    first = read(c)
    second = read(c, files.MetadataFiles(c.jobs.root_dir))
    assert first == second == c.model
    assert first is not second and first is not c.model
    assert first.segments is not second.segments
    assert first.schemas[0].schema_document is not second.schemas[0].schema_document
    assert snapshot_bytes(first) == c.data
    unavailable(lambda: attempt.publish(c.data))
    unrelated = c.directory / "unrelated.txt"
    unrelated.write_bytes(b"keep")
    attempt.cleanup()
    attempt.cleanup()
    assert not final(c).exists() and not temporary(c).exists()
    assert unrelated.read_bytes() == b"keep"


def test_cleanup_before_publication_closes_attempt(case):
    attempt = prepare(case)
    attempt.cleanup()
    attempt.cleanup()
    unavailable(lambda: attempt.publish(case.data))
    assert not case.directory.exists()


@pytest.mark.parametrize("invalid", [
    "relative", "outside", "dotdot", "job", "parent", "revision", "pending",
    "hash", "bool", "zero", "oversize", "relative_root",
])
def test_invalid_primitives_and_paths_reject_without_touching_outside(case, tmp_path, invalid):
    c = case
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(MARKER.encode())
    job, options = c.job, dict(pending_id=c.pending, **arguments(c))
    if invalid == "relative_root":
        unavailable(lambda: files.MetadataFiles("relative"))
    else:
        paths = {"relative": Path(c.job.job_id), "outside": outside,
                 "dotdot": c.jobs.root_dir / ".." / "jobs" / c.job.job_id}
        if invalid in paths:
            job = replace(job, working_dir=paths[invalid])
        elif invalid in ("job", "parent"):
            job = replace(job, **{("job_id" if invalid == "job" else "parent_job_id"): MARKER})
        else:
            key, value = {
                "revision": ("revision_id", MARKER), "pending": ("pending_id", MARKER),
                "hash": ("sha256", MARKER), "bool": ("size_bytes", True),
                "zero": ("size_bytes", 0), "oversize": ("size_bytes", MAX_SNAPSHOT_BYTES + 1),
            }[invalid]
            options[key] = value
        unavailable(lambda: c.service.prepare(job, **options))
        if invalid != "pending":
            read_options = {key: value for key, value in options.items() if key != "pending_id"}
            unavailable(lambda: c.service.read(job, **read_options))
    assert sentinel.read_bytes() == MARKER.encode()
    assert not c.directory.exists()


@pytest.mark.parametrize("collision", ["foreign_final", "identical_final", "temp"])
def test_collisions_are_never_overwritten_or_adopted(case, collision):
    c = case
    c.directory.mkdir(parents=True)
    path = temporary(c) if collision == "temp" else final(c)
    data = c.data if collision == "identical_final" else MARKER.encode()
    path.write_bytes(data)
    inode = path.stat().st_ino
    attempt = prepare(c)
    unavailable(lambda: attempt.publish(c.data))
    attempt.cleanup()
    attempt.cleanup()
    assert path.read_bytes() == data and path.stat().st_ino == inode
    if collision != "temp":
        assert not temporary(c).exists()


@pytest.mark.parametrize("component", ["root", "job", "results", "metadata", "final", "temp"])
def test_symlinks_reject_without_touching_external_target(case, tmp_path, component):
    c = case
    c.directory.mkdir(parents=True)
    outside = tmp_path / "external"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(MARKER.encode())
    paths = dict(root=c.jobs.root_dir, job=c.job.working_dir,
                 results=c.directory.parent, metadata=c.directory,
                 final=final(c), temp=temporary(c))
    path = paths[component]
    kept = path.with_name("kept-" + path.name)
    directory = component not in ("final", "temp")
    if directory:
        path.rename(kept)
    path.symlink_to(outside if directory else sentinel, target_is_directory=directory)
    try:
        attempt = prepare(c)
        unavailable(lambda: attempt.publish(c.data))
        unavailable(lambda: read(c))
        assert path.is_symlink()
        assert sentinel.read_bytes() == MARKER.encode()
        assert list(outside.iterdir()) == [sentinel]
    finally:
        path.unlink()
        if directory:
            kept.rename(path)


@pytest.mark.parametrize("kind", ["ancestor_file", "fifo", "directory"])
def test_nonregular_entries_reject_without_opening_fifo(case, monkeypatch, kind):
    c = case
    if kind == "ancestor_file":
        (c.job.working_dir / "results").write_bytes(MARKER.encode())
        unavailable(lambda: prepare(c).publish(c.data))
        unavailable(lambda: read(c))
        return
    c.directory.mkdir(parents=True)
    if kind == "fifo":
        os.mkfifo(final(c))
    else:
        final(c).mkdir()
    original, opened = files.os.open, []
    def tracked(name, *args, **kwargs):
        opened.append(name)
        if name == final(c).name:
            raise AssertionError("nonregular final must be rejected before open")
        return original(name, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(files.os, "open", tracked)
        unavailable(lambda: read(c))
    assert final(c).name not in opened


def test_cleanup_preserves_replaced_final_inode(case):
    c = case
    attempt = prepare(c)
    attempt.publish(c.data)
    kept = c.directory / "kept-original.json"
    final(c).rename(kept)
    final(c).write_bytes(MARKER.encode())
    unavailable(attempt.cleanup)
    assert final(c).read_bytes() == MARKER.encode()
    assert kept.read_bytes() == c.data


@pytest.mark.parametrize("damage", [
    "missing", "truncated", "extra", "hash", "noncanonical", "duplicate",
    "revision", "job", "parent", "document",
])
def test_read_checks_bytes_canonical_form_and_snapshot_identity(case, damage):
    c = case
    c.directory.mkdir(parents=True)
    data, options = c.data, arguments(c)
    if damage == "missing":
        unavailable(lambda: read(c))
        return
    if damage == "truncated":
        data = data[:-1]
    elif damage == "extra":
        data += b"x"
    elif damage == "hash":
        options["sha256"] = "0" * 64
    else:
        document = json.loads(data)
        if damage == "noncanonical":
            data = json.dumps(document, indent=2).encode()
        elif damage == "duplicate":
            data = (b'{"revision_id":' + json.dumps(c.model.revision_id).encode()
                    + b"," + data[1:])
        else:
            if damage == "job":
                document["job_id"] = document["document_id"] = uuid4().hex
            else:
                document[{"revision": "revision_id", "parent": "parent_job_id",
                          "document": "document_id"}[damage]] = uuid4().hex
            data = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        # Matching outer integrity makes parsing/canonical/identity checks necessary.
        options = arguments(c, data)
    final(c).write_bytes(data)
    unavailable(lambda: c.service.read(c.job, **options))
    assert final(c).read_bytes() == data


def test_reads_are_bounded_and_oversized_file_is_not_read(case, monkeypatch):
    c = case
    prepare(c).publish(c.data)
    original, requests, returned = files.os.read, [], []
    def tracked(fd, count):
        requests.append(count)
        result = original(fd, count)
        returned.append(len(result))
        return result
    with monkeypatch.context() as patch:
        patch.setattr(files.os, "read", tracked)
        assert snapshot_bytes(read(c)) == c.data
        assert requests and all(0 < n <= min(65536, len(c.data) + 1) for n in requests)
        assert sum(returned) <= len(c.data) + 1
        with final(c).open("wb") as stream:
            stream.truncate(MAX_SNAPSHOT_BYTES + 1)
        requests.clear()
        unavailable(lambda: read(c))
        assert requests == []


def test_partial_writes_complete_the_real_file(case, monkeypatch):
    original, counts = files.os.write, []
    def partial(fd, data):
        count = original(fd, data[:37])
        counts.append(count)
        return count
    attempt = prepare(case)
    with monkeypatch.context() as patch:
        patch.setattr(files.os, "write", partial)
        attempt.publish(case.data)
    assert len(counts) > 1 and sum(counts) == len(case.data)
    assert final(case).read_bytes() == case.data
    assert snapshot_bytes(read(case)) == case.data
    attempt.cleanup()


@pytest.mark.parametrize("phase", ["write", "link", "fsync", "unlink"])
def test_failures_leave_only_owned_files_for_cleanup(case, monkeypatch, phase):
    c = case
    c.directory.mkdir(parents=True)
    unrelated = c.directory / "unrelated.txt"
    unrelated.write_bytes(MARKER.encode())
    attempt = prepare(c)
    original, calls, failures = getattr(files.os, phase), [], []
    def fault(*args, **kwargs):
        calls.append(args)
        if phase == "write" and len(calls) == 1:
            return original(args[0], args[1][:7])
        if phase == "fsync" and not final(c).exists():
            return original(*args, **kwargs)
        failures.append(phase)
        raise OSError(MARKER)
    with monkeypatch.context() as patch:
        patch.setattr(files.os, phase, fault)
        unavailable(lambda: attempt.publish(c.data))
    assert failures
    if phase == "write":
        assert temporary(c).read_bytes() == c.data[:7]
    if phase == "link":
        assert not final(c).exists()
    if phase in ("fsync", "unlink"):
        assert final(c).read_bytes() == c.data
    attempt.cleanup()
    attempt.cleanup()
    assert not temporary(c).exists() and not final(c).exists()
    assert unrelated.read_bytes() == MARKER.encode()


@pytest.mark.parametrize("component", ["metadata", "results", "job"])
def test_cleanup_rejects_replaced_publication_ancestry(case, component):
    c = case
    attempt = prepare(c)
    attempt.publish(c.data)
    path = {"metadata": c.directory, "results": c.directory.parent,
            "job": c.job.working_dir}[component]
    relative_final = final(c).relative_to(path)
    kept = path.with_name("kept-" + path.name)
    path.rename(kept)
    path.mkdir()
    try:
        unavailable(attempt.cleanup)
    finally:
        assert (kept / relative_final).read_bytes() == c.data
    assert list(path.iterdir()) == []
