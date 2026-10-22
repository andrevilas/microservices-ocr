"""Synthetic integration coverage for internal metadata revision reads."""
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timedelta, timezone
import hashlib
import os

import pytest

from app.application import JobApplication
from app.metadata_models import snapshot_bytes
from app.services.document_service import DocumentMetadataService
from app.services.metadata_store import MetadataRevision
from app.services.storage_service import JobStore
from app.services.type_service import TypeService
from test_document_metadata import (
    BASE, body, env, failure, idle, persisted, runtime, save, who, deprecate,
)

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")
READ = {"jobs:read", "artifacts:read", "metadata:read"}


def get(e, receipt, principal=None):
    return e.service.get(e.owner if principal is None else principal,
                         e.job.job_id, receipt.revision_id, sha256=receipt.sha256)


def forbidden(*args, **kwargs):
    pytest.fail("read consulted a forbidden dependency")


def test_exact_get_detached_and_read_only_scopes(env, monkeypatch):
    e = env
    receipt, _ = save(e)
    expected = snapshot_bytes(persisted(e, receipt))
    monkeypatch.setattr(e.service.canonical, "open", forbidden)
    monkeypatch.setattr(e.types, "get_version", forbidden)
    result = get(e, receipt, who(scopes=READ))
    assert snapshot_bytes(result) == expected
    assert hashlib.sha256(expected).hexdigest() == receipt.sha256
    result.schemas[0].schema_document.clear()
    result.segments[0].fields.clear()
    assert snapshot_bytes(get(e, receipt, who(scopes=READ))) == expected
    idle(e, 1)


def test_historical_get_and_ordered_frozen_summaries_after_restart(env, monkeypatch):
    e = env
    first, _ = save(e)
    expected = snapshot_bytes(persisted(e, first))
    second, _ = save(e, body(e, first), key="document-read-second-0001")
    deprecate(e)
    root, job_id = e.jobs.root_dir, e.job.job_id
    with e.jobs._lock:
        e.jobs._conn.close()
    e.jobs = JobStore(root)
    e.job = e.jobs.jobs[job_id]
    runtime(e)
    monkeypatch.setattr(e.service.canonical, "open", forbidden)
    monkeypatch.setattr(e.types, "get_version", forbidden)
    reader = who(scopes=READ)
    assert snapshot_bytes(get(e, first, reader)) == expected
    assert get(e, second, reader).parent_revision_id == first.revision_id
    with monkeypatch.context() as patch:
        patch.setattr(e.bundle.files, "read", forbidden)
        summaries = e.service.list_revisions(reader, job_id)
    assert type(summaries) is tuple and len(summaries) == 2
    assert all(isinstance(row, MetadataRevision) for row in summaries)
    assert [row.revision_id for row in summaries] == [first.revision_id, second.revision_id]
    assert list(summaries) == sorted(summaries, key=lambda row: (row.created_at, row.revision_id))
    assert {field.name for field in fields(MetadataRevision)} == {
        "revision_id", "owner", "job_id", "parent_revision_id", "sha256",
        "size_bytes", "state", "created_at",
    }
    with pytest.raises(FrozenInstanceError):
        summaries[0].state = "approved"
    again = e.service.list_revisions(reader, job_id)
    assert again == summaries and again[0] is not summaries[0]
    idle(e, 2)


def test_empty_list_and_unknown_job_without_artifact_reads(env, monkeypatch):
    e = env
    monkeypatch.setattr(e.bundle.files, "read", forbidden)
    monkeypatch.setattr(e.service.canonical, "open", forbidden)
    monkeypatch.setattr(e.types, "get_version", forbidden)
    assert e.service.list_revisions(who(scopes=READ), e.job.job_id) == ()
    failure(lambda: e.service.list_revisions(e.owner, "f" * 32), 404, "job_not_found")
    failure(lambda: e.service.get(e.owner, "f" * 32, "a" * 32, sha256="b" * 64),
            404, "job_not_found")
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("operation", ["get", "list"])
@pytest.mark.parametrize("missing", sorted(READ))
def test_each_read_scope_required(env, operation, missing):
    e = env
    receipt, _ = save(e)
    reader = who(scopes=READ - {missing})
    action = (lambda: get(e, receipt, reader)) if operation == "get" else (
        lambda: e.service.list_revisions(reader, e.job.job_id))
    failure(action, 403, "insufficient_scope")
    idle(e, 1)


@pytest.mark.parametrize("reader", [who("user:2"), who("user:3", admin=True),
                                    who("service:" + "f" * 32, admin=True)])
def test_owner_isolation_including_admin(env, reader):
    e = env
    receipt, _ = save(e)
    failure(lambda: get(e, receipt, reader), 404, "job_not_found")
    failure(lambda: e.service.list_revisions(reader, e.job.job_id), 404, "job_not_found")
    idle(e, 1)


def test_missing_wrong_job_and_wrong_hash(env):
    e = env
    receipt, _ = save(e)
    failure(lambda: e.service.get(e.owner, e.job.job_id, "f" * 32, sha256=receipt.sha256),
            404, "metadata_revision_not_found")
    other = e.jobs.create("other.pdf", b"%PDF-synthetic-other", owner_user_id=1)
    e.jobs.update(other.job_id, status="completed", progress_percent=100)
    failure(lambda: e.service.get(e.owner, other.job_id, receipt.revision_id,
                                  sha256=receipt.sha256), 404, "metadata_revision_not_found")
    wrong = ("0" if receipt.sha256[0] != "0" else "1") + receipt.sha256[1:]
    failure(lambda: e.service.get(e.owner, e.job.job_id, receipt.revision_id, sha256=wrong),
            409, "metadata_revision_conflict")
    failure(lambda: e.service.get(who("user:2", admin=True), e.job.job_id,
                                  receipt.revision_id, sha256=wrong), 404, "job_not_found")
    idle(e, 1)


@pytest.mark.parametrize("argument", ["job_id", "revision_id", "sha256"])
@pytest.mark.parametrize("value", [None, 1, "", "../x", "A" * 64, "g" * 32, "a" * 33])
def test_strict_get_arguments(env, argument, value):
    e = env
    args = dict(job_id=e.job.job_id, revision_id="a" * 32, sha256="b" * 64)
    args[argument] = value
    failure(lambda: e.service.get(e.owner, **args), 422, "invalid_metadata_input")
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("deleted", [False, True])
def test_retention_denies_new_get_and_list(env, deleted):
    e = env
    receipt, _ = save(e)
    if deleted:
        JobApplication(e.jobs).delete_artifacts(e.owner, e.job.job_id)
    else:
        with e.jobs._lock:
            e.job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            e.jobs._persist(e.job)
    code = "artifacts_deleted" if deleted else "artifacts_expired"
    failure(lambda: get(e, receipt), 410, code)
    failure(lambda: e.service.list_revisions(e.owner, e.job.job_id), 410, code)
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_file_failure_is_fixed_503_and_releases_lease(env, damage):
    e = env
    receipt, _ = save(e)
    path = e.job.working_dir / "results" / "metadata" / (receipt.revision_id + ".json")
    if damage == "missing":
        path.unlink()
    else:
        data = path.read_bytes()
        path.write_bytes(b"!" + data[1:])
    error = failure(lambda: get(e, receipt), 503, "metadata_unavailable")
    assert error.message == "Stored metadata is unavailable."
    assert not e.jobs._artifact_leases and not e.jobs._lock.locked()
    assert len(e.service.list_revisions(e.owner, e.job.job_id)) == 1


def test_one_lease_and_no_sql_lock_during_read(env, monkeypatch):
    e = env
    receipt, _ = save(e)
    read = e.bundle.files.read
    calls = []

    def checked_read(*args, **kwargs):
        assert e.jobs._artifact_leases == {e.job.job_id: 1}
        assert e.jobs._lock.acquire(blocking=False)
        try:
            assert not e.jobs._conn.in_transaction
        finally:
            e.jobs._lock.release()
        calls.append(kwargs["revision_id"])
        return read(*args, **kwargs)

    monkeypatch.setattr(e.bundle.files, "read", checked_read)
    monkeypatch.setattr(e.service.canonical, "open", forbidden)
    monkeypatch.setattr(e.types, "get_version", forbidden)
    assert get(e, receipt).revision_id == receipt.revision_id
    assert calls == [receipt.revision_id]
    idle(e, 1)


@pytest.mark.parametrize("deleted", [False, True])
def test_retention_change_during_read_finishes_admitted_read(env, monkeypatch, deleted):
    e = env
    receipt, _ = save(e)
    expected = snapshot_bytes(persisted(e, receipt))
    path = e.job.working_dir / "results" / "metadata" / (receipt.revision_id + ".json")
    read = e.bundle.files.read

    def change_retention(*args, **kwargs):
        assert e.jobs._artifact_leases == {e.job.job_id: 1}
        if deleted:
            JobApplication(e.jobs).delete_artifacts(e.owner, e.job.job_id)
        else:
            with e.jobs._lock:
                e.job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                e.jobs._persist(e.job)
        assert path.exists()
        return read(*args, **kwargs)

    monkeypatch.setattr(e.bundle.files, "read", change_retention)
    assert snapshot_bytes(get(e, receipt)) == expected
    assert not e.jobs._artifact_leases
    if deleted:
        assert not path.exists()
    failure(lambda: get(e, receipt), 410,
            "artifacts_deleted" if deleted else "artifacts_expired")


def test_head_change_during_disk_read_does_not_invalidate_revision(env, monkeypatch):
    e = env
    first, _ = save(e)
    expected = snapshot_bytes(persisted(e, first))
    read = e.bundle.files.read
    later = []

    def advance_head(*args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(e.bundle.files, "read", read)
            later.append(save(e, body(e, first), key="document-read-head-change-0001")[0])
        return read(*args, **kwargs)

    monkeypatch.setattr(e.bundle.files, "read", advance_head)
    assert snapshot_bytes(get(e, first)) == expected
    assert len(later) == 1
    assert e.service.list_revisions(e.owner, e.job.job_id)[-1].revision_id == later[0].revision_id
    idle(e, 2)


@pytest.mark.parametrize("column,value", [
    ("owner", "user:2"),
    ("parent_revision_id", "e" * 32),
    ("state", "approved"),
])
def test_index_snapshot_mismatch_rejected(env, column, value):
    e = env
    receipt, _ = save(e)
    assert column in {"owner", "parent_revision_id", "state"}
    with e.jobs._lock:
        e.jobs._conn.execute("UPDATE metadata_revisions SET " + column + "=? WHERE revision_id=?",
                             (value, receipt.revision_id))
        e.jobs._conn.commit()
    failure(lambda: get(e, receipt), 404 if column == "owner" else 503,
            "metadata_revision_not_found" if column == "owner" else "metadata_unavailable")
    assert not e.jobs._artifact_leases


def test_shared_identity_checked_for_get_and_list(env, tmp_path):
    e = env
    receipt, _ = save(e)
    other = JobStore(tmp_path.absolute() / "other-read")
    original = e.service.canonical.store
    try:
        failure(lambda: DocumentMetadataService(e.bundle, TypeService(other)),
                503, "metadata_unavailable")
        e.service.canonical.store = other
        failure(lambda: get(e, receipt), 503, "metadata_unavailable")
        failure(lambda: e.service.list_revisions(e.owner, e.job.job_id),
                503, "metadata_unavailable")
    finally:
        e.service.canonical.store = original
        other._conn.close()
    idle(e, 1)
