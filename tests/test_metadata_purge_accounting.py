"""C3cA: real physical purge, durable SQL accounting and bounded history."""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.metadata_models import MAX_SNAPSHOT_BYTES
from app.services import metadata_store as ledger
from test_metadata_files import case, snapshot
from test_metadata_storage import (
    coordination, make_write, write, sql_state, principal, assert_error,
)


DAY30 = timedelta(days=30)
TABLES = ("metadata_heads", "metadata_revisions", "metadata_operations",
          "metadata_audit", "metadata_pending")


def completed(c, *, pending=False):
    job = c.jobs.create("purge.pdf", b"%PDF-1.4\npurge\n", owner_user_id=1)
    c.jobs.update(job.job_id, status="completed", progress_percent=100)
    with c.jobs.artifact_lease(job.job_id, "user:1") as leased:
        w = make_write(c, job=leased, model=snapshot(leased))
        if not pending:
            assert write(c, w) == w.receipt
    return w


def purge_time(c, w):
    with c.jobs._lock:
        value = c.jobs._get_conn().execute(
            "SELECT artifacts_purged_at FROM jobs WHERE job_id=?",
            (w.job.job_id,)).fetchone()[0]
    return None if value is None else datetime.fromisoformat(value)


def capacity(c, w, **extra):
    with c.store._transaction() as conn:
        c.store._quota(conn, "user:1", w.job.job_id, **extra)
        return c.store._reserved_disk_bytes_locked(conn, "user:1", w.job.job_id)


def remove(c, w):
    c.jobs.remove_artifacts(w.job.job_id, "user:1", "user:1")
    assert not w.job.working_dir.exists()
    assert purge_time(c, w) is not None


def unchanged_failure(c, operation):
    before, statements = sql_state(c), []
    with c.jobs._lock:
        conn = c.jobs._get_conn()
        conn.set_trace_callback(statements.append)
    try:
        assert_error(operation)
    finally:
        with c.jobs._lock:
            conn.set_trace_callback(None)
    assert sql_state(c) == before
    assert not any(sql.lstrip().upper().startswith("DELETE") for sql in statements)


@pytest.mark.parametrize("expired", [False, True])
def test_real_purge_releases_capacity_but_preserves_complete_history(coordination, monkeypatch, expired):
    c = coordination
    w = completed(c)
    charge = len(w.data) + MAX_SNAPSHOT_BYTES
    monkeypatch.setattr(ledger, "MAX_GLOBAL_BYTES", charge)
    before = sql_state(c)
    assert capacity(c, w) == MAX_SNAPSHOT_BYTES
    assert_error(lambda: capacity(c, w, add_bytes=1), 413, "metadata_quota_exceeded")
    with c.jobs.artifact_lease(w.job.job_id, "user:1"):
        if expired:
            c.jobs.cleanup_expired(retention_seconds=0)
        else:
            c.jobs.remove_artifacts(w.job.job_id, "user:1", "user:1")
        assert w.job.working_dir.exists() and purge_time(c, w) is None
        assert capacity(c, w) == MAX_SNAPSHOT_BYTES
        assert_error(lambda: capacity(c, w, add_slots=32), 413, "metadata_quota_exceeded")
        assert c.store.reconcile_purged_job(w.job.job_id) is False
        assert sql_state(c) == before
    assert not w.job.working_dir.exists()
    purged = purge_time(c, w)
    assert purged is not None
    assert capacity(c, w, add_bytes=charge, add_slots=32) == 0
    assert sql_state(c) == before
    with c.jobs._lock:
        audit = tuple(c.jobs._get_conn().execute(
            "SELECT * FROM artifact_audit WHERE job_id=?", (w.job.job_id,)))
    assert c.store.reconcile_purged_job(w.job.job_id, now=purged + DAY30 - timedelta(microseconds=1))
    assert sql_state(c) == before
    assert c.store.reconcile_purged_job(w.job.job_id, now=purged + DAY30)
    assert all(not rows for rows in sql_state(c).values())
    assert c.store.reconcile_purged_job(w.job.job_id, now=purged + DAY30)
    with c.jobs._lock:
        conn = c.jobs._get_conn()
        assert conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (w.job.job_id,)).fetchone()
        assert tuple(conn.execute("SELECT * FROM artifact_audit WHERE job_id=?", (w.job.job_id,))) == audit
    assert_error(lambda: c.store.get_head(principal(), w.job.job_id), 410,
                 "artifacts_expired" if expired else "artifacts_deleted")


def test_pending_only_released_after_real_purge(coordination):
    c = coordination
    w = completed(c, pending=True)
    assert capacity(c, w) == len(w.data) + MAX_SNAPSHOT_BYTES
    with c.jobs.artifact_lease(w.job.job_id, "user:1"):
        c.jobs.remove_artifacts(w.job.job_id, "user:1", "user:1")
        assert not c.store.reconcile_purged_job(w.job.job_id)
        assert sql_state(c)["metadata_pending"]
    assert capacity(c, w) == 0
    assert sql_state(c)["metadata_pending"]
    assert c.store.reconcile_purged_job(w.job.job_id)
    assert c.store.reconcile_purged_job(w.job.job_id)
    assert all(not rows for rows in sql_state(c).values())


def test_cached_marker_cannot_release_durable_charges(coordination):
    c = coordination
    w = completed(c)
    # Synthetic cache contradiction; this is not evidence of physical purge.
    with c.jobs._lock:
        c.jobs.jobs[w.job.job_id].artifacts_purged_at = datetime.now(timezone.utc)
    assert w.job.working_dir.exists()
    assert capacity(c, w) == MAX_SNAPSHOT_BYTES
    assert not c.store.reconcile_purged_job(w.job.job_id)


def test_failed_purge_marker_commit_keeps_charges_until_real_retry(coordination, monkeypatch):
    c = coordination
    w = completed(c)
    original = c.jobs._retention_write

    def fail_marker(job, changes, event, actor):
        if "artifacts_purged_at" in changes:
            raise sqlite3.OperationalError("synthetic precommit failure")
        return original(job, changes, event, actor)

    with monkeypatch.context() as patch:
        patch.setattr(c.jobs, "_retention_write", fail_marker)
        c.jobs.remove_artifacts(w.job.job_id, "user:1", "user:1")
    assert not w.job.working_dir.exists() and purge_time(c, w) is None
    assert capacity(c, w) == MAX_SNAPSHOT_BYTES
    before = sql_state(c)
    assert not c.store.reconcile_purged_job(w.job.job_id)
    assert sql_state(c) == before
    c.jobs.cleanup_expired()
    assert purge_time(c, w) is not None and capacity(c, w) == 0


@pytest.mark.parametrize("field", ["artifacts_removed_at", "artifacts_purged_at"])
@pytest.mark.parametrize("variant", ["naive", "empty", "malformed", "noncanonical", "future"])
def test_synthetic_invalid_markers_fail_closed(coordination, field, variant):
    c = coordination
    w = completed(c)
    remove(c, w)
    purged = purge_time(c, w)
    value = {"naive": purged.replace(tzinfo=None).isoformat(), "empty": "",
             "malformed": "not-a-time", "noncanonical": purged.isoformat().replace("+00:00", "Z"),
             "future": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}[variant]
    with c.store._transaction() as conn:
        conn.execute("UPDATE jobs SET " + field + "=? WHERE job_id=?", (value, w.job.job_id))
    unchanged_failure(c, lambda: capacity(c, w))
    unchanged_failure(c, lambda: c.store.reconcile_purged_job(w.job.job_id))


@pytest.mark.parametrize("corruption", ["missing_removed", "inverted", "owner", "head",
                                        "receipt", "audit_orphan", "foreign_operation", "missing_job"])
def test_synthetic_corrupt_purged_history_is_never_erased(coordination, corruption):
    c = coordination
    w = completed(c)
    remove(c, w)
    purged = purge_time(c, w)
    with c.store._transaction() as conn:
        if corruption in ("missing_removed", "inverted"):
            value = None if corruption == "missing_removed" else (purged + timedelta(microseconds=1)).isoformat()
            conn.execute("UPDATE jobs SET artifacts_removed_at=? WHERE job_id=?", (value, w.job.job_id))
        elif corruption == "owner":
            conn.execute("UPDATE metadata_revisions SET owner='user:2' WHERE job_id=?", (w.job.job_id,))
        elif corruption == "head":
            conn.execute("UPDATE metadata_heads SET approval_slots=0,approval_bytes=0 WHERE job_id=?", (w.job.job_id,))
        elif corruption == "receipt":
            conn.execute("UPDATE metadata_operations SET receipt_json='{}' WHERE job_id=?", (w.job.job_id,))
        elif corruption == "audit_orphan":
            conn.execute("INSERT INTO metadata_audit VALUES (?,?,?,?,?,?,?,?,?)",
                         ("user:1", w.job.job_id, "f" * 32, None, "a" * 64,
                          "save", "user:1", "local_jwt", w.model.created_at.isoformat()))
        elif corruption == "foreign_operation":
            conn.execute("UPDATE metadata_operations SET job_id=? WHERE job_id=?", (c.job.job_id, w.job.job_id))
        else:
            conn.execute("DELETE FROM jobs WHERE job_id=?", (w.job.job_id,))
    unchanged_failure(c, lambda: c.store.reconcile_purged_job(w.job.job_id, now=purged + DAY30))
    if corruption not in ("receipt", "audit_orphan", "foreign_operation"):
        unchanged_failure(c, lambda: capacity(c, w))


def test_synthetic_active_claim_contradiction_preserves_pending(coordination):
    c = coordination
    w = completed(c, pending=True)
    remove(c, w)
    with c.jobs._lock:
        c.store._active_writes.add(w.pending.pending_id)
    try:
        unchanged_failure(c, lambda: capacity(c, w))
        unchanged_failure(c, lambda: c.store.reconcile_purged_job(w.job.job_id))
        assert sql_state(c)["metadata_pending"][0][14:18] == (
            2, len(w.data) + MAX_SNAPSHOT_BYTES, 1, MAX_SNAPSHOT_BYTES)
    finally:
        with c.jobs._lock:
            c.store._active_writes.discard(w.pending.pending_id)
    assert c.store.reconcile_purged_job(w.job.job_id)


def test_collection_not_blocked_by_unrelated_over_capacity_job(coordination, monkeypatch):
    c = coordination
    w = completed(c)
    other = completed(c)
    remove(c, w)
    monkeypatch.setattr(ledger, "MAX_GLOBAL_BYTES", 0)
    assert_error(lambda: capacity(c, other), 413, "metadata_quota_exceeded")
    assert c.store.reconcile_purged_job(w.job.job_id, now=purge_time(c, w) + DAY30)
    with c.jobs._lock:
        conn = c.jobs._get_conn()
        for table in TABLES[:-1]:
            assert conn.execute("SELECT COUNT(*) FROM " + table + " WHERE job_id=?", (other.job.job_id,)).fetchone()[0] == 1
