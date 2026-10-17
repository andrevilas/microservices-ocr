"""Pending-only recovery rejects contradictory references and job bindings."""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.metadata_models import MetadataSnapshot, snapshot_bytes
from app.services import metadata_files as files
from app.services.metadata_recovery import RecoveryCounters
from test_metadata_files import case, snapshot
from test_metadata_storage import coordination, make_write, write
from test_metadata_recovery_progress import (
    recovery, pending_job, final, durable_pending, assert_clean_memory,
)


TABLES = (
    "metadata_heads", "metadata_revisions", "metadata_operations",
    "metadata_audit", "metadata_pending",
)


def sql_rows(c):
    """Observe the existing connection without beginning or committing SQL."""
    with c.jobs._lock:
        conn = c.jobs._conn
        assert conn is not None and conn.in_transaction is False
        result = {
            table: tuple(tuple(row) for row in conn.execute(
                "SELECT * FROM " + table + " ORDER BY rowid"))
            for table in TABLES
        }
        result["jobs"] = tuple(tuple(row) for row in conn.execute(
            "SELECT * FROM jobs ORDER BY job_id"))
        assert conn.in_transaction is False
        return result


def physical_state(writes):
    result = {}
    for w in writes:
        paths = (
            final(w),
            final(w).parent / ("." + w.pending.pending_id + ".tmp"),
        )
        for path in paths:
            if not path.exists():
                result[path] = None
                continue
            data = path.read_bytes()
            info = path.lstat()
            result[path] = (
                data, info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            )
    return result


def confirmed_job(c):
    """Create a complete ledger through the real publication coordinator."""
    job = c.jobs.create("confirmed.pdf", b"%PDF-1.4\nconfirmed\n", owner_user_id=1)
    c.jobs.update(job.job_id, status="completed", progress_percent=100)
    with c.jobs.artifact_lease(job.job_id, "user:1") as leased:
        w = make_write(c, job=leased, model=snapshot(leased))
        assert write(c, w) == w.receipt
    assert c.jobs._artifact_leases == c.baseline_leases
    assert final(w).read_bytes() == w.data
    return w


def assert_refused(c, monkeypatch, *writes):
    """Capture after corruption; neither diagnosis nor cleanup may change it."""
    before_sql = sql_rows(c)
    before_pending = durable_pending(c)
    before_files = physical_state(writes)
    before_claims = set(c.store._active_writes)
    before_leases = dict(c.jobs._artifact_leases)
    assert before_claims == set()
    assert before_leases == c.baseline_leases
    opens, unlinks = [], []
    physical_type = type(c.recovery.physical)
    original_open = physical_type.open
    original_unlink = files.os.unlink

    @contextmanager
    def observed_open(self, job):
        opens.append(job.job_id)
        with original_open(self, job) as directory:
            yield directory

    def observed_unlink(*args, **kwargs):
        unlinks.append((args, kwargs))
        return original_unlink(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(physical_type, "open", observed_open)
        patch.setattr(files.os, "unlink", observed_unlink)
        result = c.recovery.collect_cleanup(limit=1)
    assert opens == []
    assert unlinks == []
    assert result == RecoveryCounters(scanned=1, failed=1)
    assert sql_rows(c) == before_sql
    assert durable_pending(c) == before_pending
    assert physical_state(writes) == before_files
    assert c.store._active_writes == before_claims
    assert c.jobs._artifact_leases == before_leases
    assert_clean_memory(c)


def test_pending_revision_collides_with_confirmed_other_job(recovery, monkeypatch):
    c = recovery
    target = pending_job(c, monkeypatch, 1)
    committed = confirmed_job(c)
    assert target.job.job_id != committed.job.job_id
    with c.store._transaction() as conn:
        conn.execute(
            "UPDATE metadata_pending SET revision_id=? WHERE pending_id=?",
            (committed.pending.revision_id, target.pending.pending_id))
    assert_refused(c, monkeypatch, target, committed)


def test_duplicate_pending_revision_is_rejected_by_database(recovery, monkeypatch):
    c = recovery
    target = pending_job(c, monkeypatch, 1)
    other = pending_job(c, monkeypatch, 2)
    before_sql = sql_rows(c)
    before_files = physical_state((target, other))
    with c.store._transaction() as conn:
        with pytest.raises(sqlite3.IntegrityError) as caught:
            conn.execute(
                "UPDATE metadata_pending SET revision_id=? WHERE pending_id=?",
                (target.pending.revision_id, other.pending.pending_id))
        assert caught.value.sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE"
    assert sql_rows(c) == before_sql
    assert physical_state((target, other)) == before_files
    assert_clean_memory(c)


def test_other_pending_references_target_revision(recovery, monkeypatch):
    c = recovery
    target = pending_job(c, monkeypatch, 1)
    other = pending_job(c, monkeypatch, 2)
    assert target.pending.pending_id < other.pending.pending_id
    assert target.job.job_id != other.job.job_id
    with c.store._transaction() as conn:
        conn.execute(
            "UPDATE metadata_pending SET expected_head=?,expected_sha256=? "
            "WHERE pending_id=?",
            (target.pending.revision_id, target.pending.sha256,
             other.pending.pending_id))
    assert_refused(c, monkeypatch, target, other)


@pytest.mark.parametrize("table", ["metadata_revisions", "metadata_audit"])
def test_confirmed_other_job_has_parent_reference(recovery, monkeypatch, table):
    c = recovery
    target = pending_job(c, monkeypatch, 1)
    committed = confirmed_job(c)
    assert target.job.job_id != committed.job.job_id
    with c.store._transaction() as conn:
        conn.execute(
            "UPDATE " + table + " SET parent_revision_id=? WHERE revision_id=?",
            (target.pending.revision_id, committed.pending.revision_id))
    assert_refused(c, monkeypatch, target, committed)


def second_pending(c, committed):
    """Reserve a legitimate successor with its own operation key and bytes."""
    with c.jobs.artifact_lease(committed.job.job_id, "user:1") as leased:
        document = snapshot(leased).model_dump(mode="python", by_alias=True)
        document["revision_id"] = uuid4().hex
        document["parent_revision_id"] = committed.pending.revision_id
        model = MetadataSnapshot.model_validate(document, strict=True)
        data = snapshot_bytes(model)
        digest = hashlib.sha256(data).hexdigest()
        admission = c.store.reserve(
            c.principal, leased.job_id,
            key_hash="3" * 64, request_hash="4" * 64,
            expected_head=committed.pending.revision_id,
            expected_sha256=committed.pending.sha256,
            revision_id=model.revision_id, sha256=digest,
            size_bytes=len(data), created_at=model.created_at,
        )
        pending = admission.pending
        assert pending is not None and admission.receipt is None
        c.service.prepare(
            leased, pending_id=pending.pending_id,
            revision_id=pending.revision_id, sha256=pending.sha256,
            size_bytes=pending.size_bytes,
        ).publish(data)
        with c.store._transaction() as conn:
            c.store._mark_cleanup_locked(conn, pending.pending_id)
    return SimpleNamespace(job=leased, pending=pending, data=data)


def test_same_job_operation_key_collision_with_valid_head(recovery, monkeypatch):
    c = recovery
    committed = confirmed_job(c)
    target = second_pending(c, committed)
    assert target.job.job_id == committed.job.job_id
    assert target.pending.revision_id != committed.pending.revision_id
    assert target.pending.key_hash != committed.pending.key_hash
    assert target.pending.expected_head == committed.pending.revision_id
    with c.store._transaction() as conn:
        # Establish that the successor is valid before changing only its key.
        c.store._quota(conn, target.pending.owner, target.pending.job_id)
        head = c.store._head(conn, target.pending.owner, target.pending.job_id)
        assert head.revision_id == committed.pending.revision_id
        conn.execute(
            "UPDATE metadata_pending SET key_hash=? WHERE pending_id=?",
            (committed.pending.key_hash, target.pending.pending_id))
    assert_refused(c, monkeypatch, target, committed)


@pytest.mark.parametrize("contradiction", [
    "sql_working_dir", "cache_parent", "sql_owner", "purge_marker",
])
def test_durable_and_cached_job_authority_must_agree(recovery, monkeypatch, contradiction):
    c = recovery
    target = pending_job(c, monkeypatch, 1)
    job_id = target.job.job_id
    cached = c.jobs.jobs[job_id]
    try:
        with c.store._transaction() as conn:
            if contradiction == "sql_working_dir":
                # An actual other job's recognized directory grants no authority.
                conn.execute(
                    "UPDATE jobs SET working_dir=? WHERE job_id=?",
                    (str(c.job.working_dir), job_id))
            elif contradiction == "cache_parent":
                c.jobs.jobs[job_id] = replace(cached, parent_job_id=c.job.job_id)
            elif contradiction == "sql_owner":
                conn.execute(
                    "UPDATE jobs SET owner_principal_id=? WHERE job_id=?",
                    ("user:2", job_id))
            else:
                conn.execute(
                    "UPDATE jobs SET artifacts_removed_at=?,artifacts_purged_at=? "
                    "WHERE job_id=?",
                    (datetime.now(timezone.utc).isoformat(), "not-a-time", job_id))
        assert_refused(c, monkeypatch, target)
    finally:
        if contradiction == "cache_parent":
            with c.jobs._lock:
                c.jobs.jobs[job_id] = cached
