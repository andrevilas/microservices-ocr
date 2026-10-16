"""Quota and concurrency tests for the SQLite index, not file durability or approval."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4
import hashlib
import json

import pytest

from app.application import ApplicationError, Principal
from app.metadata_models import MAX_SNAPSHOT_BYTES, SaveMetadataReceipt
from app.services import metadata_store as metadata
from app.services.storage_service import JobStore


MIB = 1024 * 1024
NOW = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
SCOPES = frozenset({"jobs:read", "artifacts:read", "metadata:read", "metadata:write"})


def principal(number=1, *, scopes=SCOPES):
    return Principal(subject=f"user:{number}", user_id=number,
                     scopes=scopes, auth_source="local_jwt")


def service_principal():
    return Principal(kind="service", subject="service:" + uuid4().hex,
                     user_id=None, scopes=SCOPES, auth_source="service")


def digest():
    return hashlib.sha256(uuid4().bytes).hexdigest()


def request(*, size=MIB, head=None, revision_id=None, key_hash=None):
    return dict(key_hash=key_hash or digest(), request_hash=digest(),
                expected_head=head.revision_id if head else None,
                expected_sha256=head.sha256 if head else None,
                revision_id=revision_id or uuid4().hex, sha256=digest(),
                size_bytes=size, created_at=NOW)


def receipt(pending):
    return SaveMetadataReceipt(revision_id=pending.revision_id,
                               sha256=pending.sha256,
                               segment_ids={"s": uuid4().hex})


def error(code, status, call):
    with pytest.raises(ApplicationError) as caught:
        call()
    exc = caught.value
    assert (exc.status, exc.code) == (status, code)
    assert isinstance(exc.message, str) and exc.message
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert "SENSITIVE_MARKER" not in str(exc)
    assert "SENSITIVE_MARKER" not in exc.message
    return exc


class Environment:
    def __init__(self, path):
        self.path = path
        self.jobs = JobStore(path)
        self.svc = metadata.MetadataStore(self.jobs)

    def close(self):
        with self.jobs._lock:
            self.jobs._get_conn().close()

    def restart(self):
        self.close()
        self.jobs = JobStore(self.path)
        self.svc = metadata.MetadataStore(self.jobs)

    def job(self, who=None):
        who = who or principal()
        kwargs = {"owner_user_id": who.user_id}
        if who.subject.startswith("service:"):
            kwargs["owner_principal_id"] = who.subject
        created = self.jobs.create("s.pdf", b"x", **kwargs)
        job_id = created if isinstance(created, str) else created.job_id
        self.jobs.update(job_id, status="completed", progress_percent=100)
        return job_id

    def reserve(self, job_id, who=None, **kwargs):
        return self.svc.reserve(who or principal(), job_id, **kwargs)

    def commit(self, pending, who=None):
        result = receipt(pending)
        with self.svc._transaction() as conn:
            actual = self.svc._commit_index_locked(
                conn, who or principal(), pending.pending_id, result)
        assert actual == result
        return result

    def seed(self, who, count=31):
        """Controlled index fixture: no snapshot payload and no approval call."""
        job_id = self.job(who)
        rows = []
        parent = None
        for _ in range(count):
            revision_id, sha = uuid4().hex, digest()
            rows.append((revision_id, who.subject, job_id, parent, sha,
                         MIB, "draft", NOW.isoformat()))
            parent = revision_id
        with self.svc._transaction() as conn:
            conn.executemany(
                "INSERT INTO metadata_revisions (" + metadata._REVISION_COLUMNS +
                ") VALUES (?,?,?,?,?,?,?,?)", rows)
            conn.execute(
                "INSERT INTO metadata_heads "
                "(owner,job_id,revision_id,sha256,approval_slots,approval_bytes) "
                "VALUES (?,?,?,?,?,?)",
                (who.subject, job_id, parent, rows[-1][4], 1, MIB))
        return job_id

    def rows(self, table):
        assert table in TABLES
        with self.svc._transaction() as conn:
            return tuple(conn.execute("SELECT * FROM " + table + " ORDER BY rowid"))

    def snapshot(self):
        return {table: self.rows(table) for table in TABLES}

    def capacity(self, *, owner=None, job_id=None):
        clause, args = "", ()
        if owner is not None:
            clause, args = " WHERE owner=?", (owner,)
        if job_id is not None:
            clause, args = " WHERE job_id=?", (job_id,)
        size = slots = 0
        with self.svc._transaction() as conn:
            for table, byte_col, slot_col in (
                ("metadata_revisions", "size_bytes", "1"),
                ("metadata_heads", "approval_bytes", "approval_slots"),
                ("metadata_pending", "charged_bytes", "charged_slots"),
            ):
                row = conn.execute(
                    "SELECT COALESCE(SUM(" + byte_col + "),0), "
                    "COALESCE(SUM(" + slot_col + "),0) FROM " + table + clause,
                    args).fetchone()
                size += row[0]
                slots += row[1]
        return size, slots


TABLES = ("metadata_revisions", "metadata_heads", "metadata_pending",
          "metadata_operations", "metadata_audit")


@pytest.fixture
def env(tmp_path):
    instance = Environment(tmp_path)
    try:
        yield instance
    finally:
        instance.close()


def owner_boundary(env, *, full=False):
    who = principal()
    jobs = [env.seed(who) for _ in range(3)]
    jobs.append(env.seed(who, 31 if full else 29))
    assert env.capacity(owner=who.subject)[0] == (128 if full else 126) * MIB
    return who, jobs


def global_boundary(env, *, full=False):
    for index in range(16):
        env.seed(principal(index // 4 + 1),
                 29 if index == 15 and not full else 31)
    assert env.capacity()[0] == (512 if full else 510) * MIB


def race(env, entries):
    barrier = Barrier(len(entries))

    def run(entry):
        who, job_id, kwargs = entry
        barrier.wait(timeout=10)
        try:
            return env.reserve(job_id, who, **kwargs)
        except ApplicationError as exc:
            assert exc.__cause__ is None and exc.__context__ is None
            assert "SENSITIVE_MARKER" not in str(exc)
            return exc

    with ThreadPoolExecutor(max_workers=len(entries)) as executor:
        futures = [executor.submit(run, entry) for entry in entries]
        results = [future.result(timeout=10) for future in futures]
    winners = [(index, result) for index, result in enumerate(results)
               if not isinstance(result, ApplicationError)]
    failures = [result for result in results if isinstance(result, ApplicationError)]
    assert len(winners) == len(failures) == 1
    return winners[0], failures[0]


@pytest.mark.parametrize("size", [MIB, 1])
def test_default_limits_and_real_job_boundary(env, size):
    assert (MAX_SNAPSHOT_BYTES, metadata.MAX_REVISIONS_PER_JOB,
            metadata.MAX_JOB_BYTES, metadata.MAX_OWNER_BYTES,
            metadata.MAX_GLOBAL_BYTES) == (MIB, 32, 32 * MIB, 128 * MIB, 512 * MIB)
    job_id = env.job()
    head = None
    for index in range(31):
        admitted = env.reserve(job_id, **request(size=size, head=head))
        assert admitted.pending is not None
        before_commit = env.capacity(job_id=job_id)
        env.commit(admitted.pending)
        assert env.capacity(job_id=job_id) == before_commit
        head = env.svc.get_head(principal(), job_id)
        assert env.capacity(job_id=job_id) == (MIB + (index + 1) * size, index + 2)
    assert len(env.svc.list_revisions(principal(), job_id)) == 31
    assert env.capacity(job_id=job_id) == (MIB + 31 * size, 32)
    before = env.snapshot()
    error("metadata_quota_exceeded", 413,
          lambda: env.reserve(job_id, **request(size=1, head=head)))
    assert env.snapshot() == before
    assert env.svc.get_head(principal(), job_id) == head
    assert not env.rows("metadata_pending")


def test_first_pending_and_commit_keep_identical_charges(env):
    job_id = env.job()
    pending = env.reserve(job_id, **request()).pending
    assert (pending.charged_slots, pending.charged_bytes,
            pending.approval_slots, pending.approval_bytes) == (2, 2 * MIB, 1, MIB)
    assert env.capacity(job_id=job_id) == (2 * MIB, 2)
    env.commit(pending)
    assert env.capacity(job_id=job_id) == (2 * MIB, 2)
    assert len(env.rows("metadata_revisions")) == 1
    assert not env.rows("metadata_pending")
    assert env.svc.get_head(principal(), job_id).approval_bytes == MIB


def test_controlled_approved_head_requires_new_approval_reserve(env):
    job_id = env.seed(principal(), 1)
    # A valid SQL state fixture, not evidence of an implemented approval service.
    with env.svc._transaction() as conn:
        conn.execute("UPDATE metadata_revisions SET state='approved' WHERE job_id=?", (job_id,))
        conn.execute("UPDATE metadata_heads SET approval_slots=0,approval_bytes=0 WHERE job_id=?",
                     (job_id,))
    head = env.svc.get_head(principal(), job_id)
    assert head.state == "approved"
    assert env.capacity(job_id=job_id) == (MIB, 1)
    pending = env.reserve(job_id, **request(head=head)).pending
    assert (pending.charged_slots, pending.charged_bytes,
            pending.approval_slots, pending.approval_bytes) == (2, 2 * MIB, 1, MIB)
    assert env.capacity(job_id=job_id) == (3 * MIB, 3)
    env.commit(pending)
    assert env.capacity(job_id=job_id) == (3 * MIB, 3)


def test_owner_full_and_other_owner_independent(env):
    who, _ = owner_boundary(env, full=True)
    job_id = env.job(who)
    before = env.snapshot()
    error("metadata_quota_exceeded", 413,
          lambda: env.reserve(job_id, who, **request(size=1)))
    assert env.snapshot() == before
    other = principal(2)
    other_job = env.job(other)
    assert env.reserve(other_job, other, **request()).pending is not None
    assert env.capacity(owner=who.subject)[0] == 128 * MIB
    assert env.capacity(owner=other.subject) == (2 * MIB, 2)


def test_owner_exact_boundary_admission(env):
    who, _ = owner_boundary(env)
    job_id = env.job(who)
    pending = env.reserve(job_id, who, **request()).pending
    assert pending is not None
    assert env.capacity(owner=who.subject)[0] == 128 * MIB
    env.commit(pending, who)
    assert env.capacity(owner=who.subject)[0] == 128 * MIB
    blocked = env.job(who)
    before = env.snapshot()
    error("metadata_quota_exceeded", 413,
          lambda: env.reserve(blocked, who, **request(size=1)))
    assert env.snapshot() == before


@pytest.mark.parametrize("full", [False, True])
def test_global_boundary_counts_all_owners(env, full):
    global_boundary(env, full=full)
    who = principal(5)
    job_id = env.job(who)
    if not full:
        pending = env.reserve(job_id, who, **request()).pending
        assert env.capacity()[0] == 512 * MIB
        env.commit(pending, who)
        assert env.capacity()[0] == 512 * MIB
        job_id = env.job(who)
    before = env.snapshot()
    error("metadata_quota_exceeded", 413,
          lambda: env.reserve(job_id, who, **request(size=1)))
    assert env.snapshot() == before
    assert env.capacity()[0] == 512 * MIB


@pytest.mark.parametrize("field", ["artifacts_expires_at", "artifacts_removed_at"])
def test_logical_expiry_or_removal_does_not_release_capacity(env, field):
    who, seeded = owner_boundary(env, full=True)
    before_capacity = env.capacity()
    with env.jobs._lock:
        for job_id in seeded:
            job = env.jobs.jobs[job_id]
            previous = getattr(job, field)
            past = datetime.now(timezone.utc) - timedelta(days=2)
            setattr(job, field, past.isoformat() if isinstance(previous, str) else past)
            if field == "artifacts_removed_at":
                job.artifacts_removal_reason = "deleted"
            env.jobs._persist(job)
    blocked = env.job(who)
    before = env.snapshot()
    error("metadata_quota_exceeded", 413,
          lambda: env.reserve(blocked, who, **request()))
    assert env.snapshot() == before
    assert env.capacity() == before_capacity
    # No purge or physical cleanup is invoked by this index test.


@pytest.mark.parametrize("boundary", ["owner", "global"])
def test_active_cleanup_restart_and_explicit_release(env, boundary):
    if boundary == "owner":
        who, seeded = owner_boundary(env)
    else:
        global_boundary(env)
        who, seeded = principal(5), []
    job_id, blocked = env.job(who), env.job(who)
    pending = env.reserve(job_id, who, **request()).pending
    charged = env.capacity()
    baseline_heads = env.rows("metadata_heads")
    baseline_revisions = env.rows("metadata_revisions")
    before = env.snapshot()
    error("metadata_quota_exceeded", 413,
          lambda: env.reserve(blocked, who, **request()))
    assert env.snapshot() == before

    def release():
        with env.svc._transaction() as conn:
            env.svc._release_pending_after_cleanup_locked(conn, pending.pending_id)

    error("metadata_unavailable", 503, release)
    assert env.snapshot() == before
    with env.svc._transaction() as conn:
        env.svc._mark_cleanup_locked(conn, pending.pending_id)
    assert env.capacity() == charged
    env.restart()
    assert env.capacity() == charged
    with env.svc._transaction() as conn:
        recovered = env.svc._pending(conn, pending.pending_id)
    assert recovered.state == "cleanup"
    assert (recovered.charged_slots, recovered.charged_bytes) == (2, 2 * MIB)
    before = env.snapshot()
    error("metadata_quota_exceeded", 413,
          lambda: env.reserve(blocked, who, **request()))
    assert env.snapshot() == before
    # This test never creates pending snapshot files; their absence is the
    # explicit precondition for exercising the private reconciliation helper.
    release()
    assert env.capacity() == (charged[0] - 2 * MIB, charged[1] - 2)
    assert env.rows("metadata_heads") == baseline_heads
    assert env.rows("metadata_revisions") == baseline_revisions
    for seeded_job in seeded:
        assert env.svc.get_head(who, seeded_job).approval_bytes == MIB
    assert env.reserve(blocked, who, **request()).pending is not None
    assert env.capacity() == charged


def test_same_job_race_and_stale_cas_after_winner_commit(env):
    who, job_id = principal(), env.job()
    entries = [(who, job_id, request()) for _ in range(2)]
    (winner_index, admitted), failure = race(env, entries)
    assert (failure.status, failure.code) == (409, "metadata_write_pending")
    assert admitted.pending is not None
    assert len(env.rows("metadata_pending")) == 1
    assert env.capacity(job_id=job_id) == (2 * MIB, 2)
    assert not env.rows("metadata_operations")
    assert not env.rows("metadata_audit")
    env.commit(admitted.pending)
    assert len(env.rows("metadata_revisions")) == 1
    assert len(env.rows("metadata_operations")) == len(env.rows("metadata_audit")) == 1
    assert not env.rows("metadata_pending")
    before = env.snapshot()
    loser_request = entries[1 - winner_index][2]
    error("metadata_revision_conflict", 409,
          lambda: env.reserve(job_id, who, **loser_request))
    assert env.snapshot() == before


@pytest.mark.parametrize("boundary", ["owner", "global"])
def test_cross_job_capacity_race_has_one_winner(env, boundary):
    if boundary == "owner":
        who, _ = owner_boundary(env)
        owners = [who, who]
        expected_global = 128 * MIB
    else:
        global_boundary(env)
        owners = [principal(5), principal(6)]
        expected_global = 512 * MIB
    entries = [(who, env.job(who), request()) for who in owners]
    baseline = env.snapshot()
    initial_capacity = env.capacity()
    (winner_index, admitted), failure = race(env, entries)
    assert (failure.status, failure.code) == (413, "metadata_quota_exceeded")
    assert env.capacity() == (expected_global, initial_capacity[1] + 2)
    assert len(env.rows("metadata_pending")) == 1
    assert env.capacity(job_id=entries[winner_index][1]) == (2 * MIB, 2)
    assert env.capacity(job_id=entries[1 - winner_index][1]) == (0, 0)
    for table in TABLES:
        if table != "metadata_pending":
            assert env.rows(table) == baseline[table]
    env.commit(admitted.pending, entries[winner_index][0])
    assert env.capacity() == (expected_global, initial_capacity[1] + 2)
    assert len(env.rows("metadata_operations")) == len(baseline["metadata_operations"]) + 1
    assert len(env.rows("metadata_audit")) == len(baseline["metadata_audit"]) + 1


@pytest.mark.parametrize("confirmed", [False, True])
def test_revision_collision_across_jobs_is_atomic(env, confirmed):
    first, second = env.job(), env.job()
    pending = env.reserve(first, **request()).pending
    if confirmed:
        env.commit(pending)
    before = env.snapshot()
    error("metadata_revision_conflict", 409,
          lambda: env.reserve(second, **request(revision_id=pending.revision_id)))
    assert env.snapshot() == before
    assert env.capacity(job_id=second) == (0, 0)


def test_same_key_is_scoped_to_job_and_does_not_bypass_owner(env):
    key = digest()
    first, second = env.job(), env.job()
    a = env.reserve(first, **request(key_hash=key)).pending
    b = env.reserve(second, **request(key_hash=key)).pending
    assert a.key_hash == b.key_hash and a.pending_id != b.pending_id
    env.commit(a)
    env.commit(b)
    assert len(env.rows("metadata_operations")) == 2
    before = env.snapshot()
    error("job_not_found", 404,
          lambda: env.reserve(first, principal(2), **request(key_hash=key)))
    assert env.snapshot() == before


@pytest.mark.parametrize("revocation,code,status", [
    ("owner", "job_not_found", 404),
    ("status", "job_not_completed", 409),
    ("scope", "insufficient_scope", 403),
])
def test_commit_rechecks_current_admission_without_releasing_charge(env, revocation, code, status):
    who, job_id = principal(), env.job()
    pending = env.reserve(job_id, who, **request()).pending
    if revocation == "scope":
        who = principal(scopes=SCOPES - {"metadata:write"})
    else:
        with env.jobs._lock:
            job = env.jobs.jobs[job_id]
            if revocation == "owner":
                job.owner_user_id = 2
                job.owner_principal_id = "user:2"
            else:
                job.status = "processing"
            env.jobs._persist(job)
    before = env.snapshot()
    charged = env.capacity(job_id=job_id)
    error(code, status, lambda: env.commit(pending, who))
    assert env.snapshot() == before
    assert env.capacity(job_id=job_id) == charged == (2 * MIB, 2)
    assert not env.rows("metadata_revisions")
    assert not env.rows("metadata_operations")
    assert not env.rows("metadata_audit")


def test_service_owned_index_and_foreign_admin_denial(env):
    who = service_principal()
    job_id = env.job(who)
    admin = replace(principal(2, scopes=SCOPES | {"admin"}), is_admin=True)
    assert admin.is_admin is True
    before = env.snapshot()
    error("job_not_found", 404,
          lambda: env.reserve(job_id, admin, **request()))
    assert env.snapshot() == before
    pending = env.reserve(job_id, who, **request()).pending
    env.commit(pending, who)
    head = env.svc.get_head(who, job_id)
    assert head.state == "draft"
    assert (head.approval_slots, head.approval_bytes) == (1, MIB)
    with env.svc._transaction() as conn:
        audit = conn.execute("SELECT actor,auth_source FROM metadata_audit WHERE job_id=?",
                             (job_id,)).fetchone()
    assert audit == (who.subject, "service")
    error("job_not_found", 404, lambda: env.svc.get_head(admin, job_id))


def test_pending_conflicts_do_not_grow_ledger_and_columns_are_private(env):
    job_id = env.job()
    pending = env.reserve(job_id, **request()).pending
    before = env.snapshot()
    for _ in range(8):
        error("metadata_write_pending", 409,
              lambda: env.reserve(job_id, **request()))
        assert env.snapshot() == before
    env.commit(pending)
    expected = {
        "metadata_heads": "owner job_id revision_id sha256 approval_slots approval_bytes",
        "metadata_revisions": "revision_id owner job_id parent_revision_id sha256 size_bytes state created_at",
        "metadata_operations": "owner job_id operation key_hash request_hash revision_id receipt_json created_at",
        "metadata_pending": "pending_id job_id owner operation key_hash request_hash revision_id sha256 size_bytes expected_head expected_sha256 created_at actor auth_source charged_slots charged_bytes approval_slots approval_bytes state",
        "metadata_audit": "owner job_id revision_id parent_revision_id sha256 action actor auth_source created_at",
    }
    with env.svc._transaction() as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'metadata_%'")}
        assert tables == set(expected)
        for table, names in expected.items():
            columns = {row[1] for row in conn.execute("PRAGMA table_info(" + table + ")")}
            assert columns == set(names.split())
    for rows in env.snapshot().values():
        for row in rows:
            encoded = json.dumps(row)
            assert len(encoded) < 8192
            assert "SENSITIVE_MARKER" not in encoded
            assert "s.pdf" not in encoded
            assert str(env.path) not in encoded


def test_commit_audit_prior_revision_and_retention_are_preserved(env):
    who, job_id = principal(), env.job()
    job = env.jobs.jobs[job_id]
    retention = job.artifacts_expires_at
    first = env.reserve(job_id, who, **request(size=7)).pending
    first_receipt = env.commit(first, who)
    prior = env.rows("metadata_revisions")
    head = env.svc.get_head(who, job_id)
    second = env.reserve(job_id, who, **request(size=11, head=head)).pending
    second_receipt = env.commit(second, who)
    assert env.rows("metadata_revisions")[:1] == prior
    assert env.jobs.jobs[job_id].artifacts_expires_at == retention
    with env.svc._transaction() as conn:
        audits = tuple(conn.execute(
            "SELECT revision_id,parent_revision_id,sha256,actor,auth_source,created_at "
            "FROM metadata_audit ORDER BY rowid"))
        operations = tuple(conn.execute(
            "SELECT revision_id,receipt_json,created_at FROM metadata_operations ORDER BY rowid"))
    assert audits == (
        (first.revision_id, None, first.sha256, who.subject, "local_jwt", NOW.isoformat()),
        (second.revision_id, first.revision_id, second.sha256, who.subject,
         "local_jwt", NOW.isoformat()),
    )
    for row, expected in zip(operations, (first_receipt, second_receipt)):
        assert row[0] == expected.revision_id
        assert json.loads(row[1]) == expected.model_dump(mode="json")
        assert row[2] == NOW.isoformat()
    assert len(operations) == 2
    assert env.capacity(job_id=job_id) == (MIB + 18, 3)


@pytest.mark.parametrize("legacy_owner", [None, ""])
def test_quota_effective_owner_legacy_fallback_preserves_history(env, legacy_owner):
    who, job_id = principal(), env.job()
    first = env.reserve(job_id, who, **request()).pending
    env.commit(first, who)
    history = env.snapshot()
    head = env.svc.get_head(who, job_id)
    with env.jobs._lock:
        job = env.jobs.jobs[job_id]
        assert job.owner_user_id == 1
        job.owner_principal_id = legacy_owner
        env.jobs._persist(job)
        assert job.effective_owner == who.subject
    # An unrelated ownerless job has no metadata and consumes no quota.
    env.jobs.create("s.pdf", b"x", owner_user_id=None)
    # Check before reopening: JobStore may backfill the legacy owner on load.
    pending = env.reserve(job_id, who, **request(head=head)).pending
    assert pending.owner == who.subject
    assert (pending.charged_slots, pending.charged_bytes,
            pending.approval_slots, pending.approval_bytes) == (1, MIB, 0, 0)
    assert env.rows("metadata_pending") == env.snapshot()["metadata_pending"]
    assert len(env.rows("metadata_pending")) == 1
    for table in TABLES:
        if table != "metadata_pending":
            assert env.rows(table) == history[table]
    assert env.capacity(owner=who.subject) == (3 * MIB, 3)
    env.commit(pending, who)
    assert env.rows("metadata_revisions")[:1] == history["metadata_revisions"]
    assert len(env.rows("metadata_revisions")) == 2
    assert not env.rows("metadata_pending")
    assert len(env.rows("metadata_heads")) == 1
    assert env.svc.get_head(who, job_id).approval_bytes == MIB
    assert env.capacity(owner=who.subject) == env.capacity() == (3 * MIB, 3)


@pytest.mark.parametrize("legacy_owner", [None, ""])
@pytest.mark.parametrize("invalid_user_id", ["SENSITIVE_MARKER", 1.5])
def test_quota_rejects_corrupt_durable_fallback_owner(env, legacy_owner, invalid_user_id):
    referenced = env.job()
    env.commit(env.reserve(referenced, **request()).pending)
    other = principal(2)
    target = env.job(other)
    # Only durable SQL is corrupted; the separate target's cached job is valid.
    with env.svc._transaction() as conn:
        conn.execute(
            "UPDATE jobs SET owner_principal_id=?,owner_user_id=? WHERE job_id=?",
            (legacy_owner, invalid_user_id, referenced))
        stored_type = conn.execute(
            "SELECT typeof(owner_user_id) FROM jobs WHERE job_id=?",
            (referenced,)).fetchone()[0]
    assert stored_type == ("text" if isinstance(invalid_user_id, str) else "real")
    before, charged = env.snapshot(), env.capacity()
    error("metadata_unavailable", 503,
          lambda: env.reserve(target, other, **request()))
    assert env.snapshot() == before
    assert env.capacity() == charged == (2 * MIB, 2)
    assert env.capacity(job_id=target) == (0, 0)


def test_quota_rejects_missing_durable_job_with_metadata(env):
    referenced = env.job()
    env.commit(env.reserve(referenced, **request()).pending)
    other = principal(2)
    target = env.job(other)
    with env.svc._transaction() as conn:
        conn.execute("DELETE FROM jobs WHERE job_id=?", (referenced,))
    before, charged = env.snapshot(), env.capacity()
    error("metadata_unavailable", 503,
          lambda: env.reserve(target, other, **request()))
    assert env.snapshot() == before
    assert env.capacity() == charged == (2 * MIB, 2)
    assert env.capacity(job_id=target) == (0, 0)
