"""Type draft CRUD service: synthetic data only, real JobStore on tmp_path, no network."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.application import ApplicationError, Principal
from app.document_models import ContextCreate, TypeDraft
from app.services.context_service import ContextService
from app.services.storage_service import JobStore
from app.services.type_service import TypeService

BOTH = frozenset({"contexts:read", "contexts:write"})
FIELD = dict(name="synthetic_field", type="string", description="synthetic field")
WAIT = 10


def who(subject="user:1", scopes=BOTH, **extra):
    return Principal(subject=subject, user_id=1, scopes=frozenset(scopes), **extra)


def draft(name="Synthetic", *fields):
    return TypeDraft(name=name, description="synthetic",
                     specific_fields=[dict(name=f, type="string", description="synthetic field") for f in fields])


def code(call, *args, **kwargs):
    with pytest.raises(ApplicationError) as error:
        call(*args, **kwargs)
    return error.value.status, error.value.code


def type_rows(store):
    with store._lock:
        return store._get_conn().execute("SELECT COUNT(*) FROM document_types").fetchone()[0]


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path)


@pytest.fixture
def contexts(store):
    return ContextService(store)  # precondition: initialized before TypeService


@pytest.fixture
def svc(store, contexts):
    return TypeService(store)


def new_context(contexts, principal, n=1):
    return contexts.create(principal, ContextCreate(name="Ctx"), f"ctx-key-{n:08d}")[0].context_id


def test_crud_roundtrip_and_reopen(tmp_path, store, contexts, svc):
    p = who()
    cid = new_context(contexts, p)
    receipt, replayed = svc.create(p, cid, draft("Alpha", "cpf_like"), "key-00000001")
    assert (replayed, receipt.revision, receipt.context_id) == (False, 1, cid)
    assert svc.get(p, receipt.type_id).body == draft("Alpha", "cpf_like")
    updated = svc.update(p, receipt.type_id, draft("Beta"), expected_revision=1)
    assert (updated.revision, updated.created_at, updated.context_id) == (2, receipt.created_at, cid)
    assert updated.updated_at >= updated.created_at
    page = svc.list(p, cid)
    assert [(i.name, i.revision) for i in page.items] == [("Beta", 2)] and page.next_cursor is None
    assert not hasattr(page.items[0], "body") and not hasattr(page.items[0], "specific_fields")
    TypeService(store)  # second init must not wipe
    reopened = TypeService(JobStore(tmp_path))
    assert reopened.get(p, receipt.type_id) == updated


def test_scopes_enforced(contexts, svc):
    p = who()
    cid = new_context(contexts, p)
    tid = svc.create(p, cid, draft(), "key-00000001")[0].type_id
    reader, writer, nobody = who(scopes={"contexts:read"}), who(scopes={"contexts:write"}), who(scopes=set())
    assert svc.get(reader, tid).type_id == tid and svc.list(reader, cid).items
    for call in (lambda: svc.create(reader, cid, draft(), "key-00000002"),
                 lambda: svc.update(reader, tid, draft(), expected_revision=1),
                 lambda: svc.create(writer, cid, draft(), "key-00000002"),
                 lambda: svc.update(writer, tid, draft(), expected_revision=1),
                 lambda: svc.get(writer, tid), lambda: svc.list(writer, cid),
                 lambda: svc.get(nobody, tid), lambda: svc.list(nobody, cid)):
        assert code(call) == (403, "insufficient_scope")
    assert svc.get(p, tid).revision == 1


def test_owner_isolation_and_admin_has_no_bypass(contexts, svc):
    owner, other = who("user:1"), who("user:2", is_admin=True, kind="service")
    cid = new_context(contexts, owner)
    tid = svc.create(owner, cid, draft(), "key-00000001")[0].type_id
    missing = code(svc.get, other, "0" * 32)
    assert missing == (404, "type_not_found")
    assert code(svc.get, other, tid) == missing and code(svc.get, other, "not-an-id") == missing
    assert code(svc.update, other, tid, draft("X"), expected_revision=1) == missing
    foreign_context = (404, "context_not_found")
    assert code(svc.create, other, cid, draft(), "key-00000002") == foreign_context
    assert code(svc.list, other, cid) == foreign_context
    assert code(svc.list, other, "0" * 32) == foreign_context and code(svc.list, other, "bad") == foreign_context
    assert svc.get(owner, tid).body.name == "Synthetic" and svc.get(owner, tid).revision == 1


def test_create_idempotency(contexts, svc):
    p, q = who(), who("user:2")
    cid, qcid = new_context(contexts, p), new_context(contexts, q)
    receipt, _ = svc.create(p, cid, draft("One"), "key-00000001")
    assert svc.create(p, cid, draft("One"), "key-00000001") == (receipt, True)
    svc.update(p, receipt.type_id, draft("Two"), expected_revision=1)
    assert svc.create(p, cid, draft("One"), "key-00000001") == (receipt, True)
    assert svc.get(p, receipt.type_id).revision == 2
    assert code(svc.create, p, cid, draft("Different"), "key-00000001") == (409, "idempotency_conflict")
    other_cid = new_context(contexts, p, 2)
    assert code(svc.create, p, other_cid, draft("One"), "key-00000001") == (409, "idempotency_conflict")
    other, replayed = svc.create(q, qcid, draft("Different"), "key-00000001")
    assert replayed is False and other.type_id != receipt.type_id
    assert code(svc.create, p, cid, draft(), "short") == (422, "invalid_idempotency_key")
    first, _ = svc.create(p, cid, draft("One"), "key-00000009")  # same name may repeat
    assert first.type_id != receipt.type_id


def test_missing_context_does_not_consume_key_or_quota(store, contexts, svc):
    p = who()
    for bad in ("0" * 32, "not-an-id", None):
        assert code(svc.create, p, bad, draft(), "key-00000001") == (404, "context_not_found")
    assert type_rows(store) == 0
    cid = new_context(contexts, p)
    receipt, replayed = svc.create(p, cid, draft("Later"), "key-00000001")
    assert replayed is False and receipt.context_id == cid


def test_quota_is_total_per_owner_across_contexts(contexts, svc):
    p, q = who(), who("user:2")
    first, second = new_context(contexts, p, 1), new_context(contexts, p, 2)
    for n in range(100):
        svc.create(p, first if n % 2 else second, draft(f"T{n}"), f"key-{n:08d}")
    assert code(svc.create, p, first, draft(), "key-extra-001") == (429, "type_quota_exceeded")
    assert code(svc.create, p, second, draft(), "key-extra-002") == (429, "type_quota_exceeded")
    assert svc.create(p, first, draft("T1"), "key-00000001")[1] is True  # replay precedes quota
    assert svc.create(q, new_context(contexts, q, 3), draft(), "key-00000001")[1] is False
    assert len(svc.list(p, first, limit=100).items) == 50


def test_list_summaries_pagination_and_invalid_query(contexts, svc):
    p = who()
    cid, empty = new_context(contexts, p, 1), new_context(contexts, p, 2)
    ids = sorted(svc.create(p, cid, draft(f"T{n}", "a"), f"key-{n:08d}")[0].type_id for n in range(5))
    svc.create(p, empty, draft("Elsewhere"), "key-other-001")
    seen, after = [], None
    while True:
        page = svc.list(p, cid, limit=2, after=after)
        assert len(page.items) <= 2 and all(i.context_id == cid for i in page.items)
        seen += [i.type_id for i in page.items]
        if page.next_cursor is None:
            break
        assert page.next_cursor == page.items[-1].type_id
        after = page.next_cursor
    assert seen == ids
    assert svc.list(p, cid, limit=5).next_cursor is None
    assert svc.list(p, cid, after="f" * 32).items == []  # boundary cursor need not exist
    assert [i.name for i in svc.list(p, empty).items] == ["Elsewhere"]
    for kwargs in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": "5"}, {"after": "XYZ"}, {"after": "A" * 32}, {"after": 5}):
        assert code(svc.list, p, cid, **kwargs) == (422, "invalid_type_query")


def test_update_is_strict_cas(contexts, svc):
    p = who()
    tid = svc.create(p, new_context(contexts, p), draft(), "key-00000001")[0].type_id
    for bad in (True, "1", 1.0, 0, -1, 2**31, None):
        assert code(svc.update, p, tid, draft("X"), expected_revision=bad) == (422, "invalid_type_query")
    svc.update(p, tid, draft("Two"), expected_revision=1)
    assert code(svc.update, p, tid, draft("Three"), expected_revision=1) == (409, "type_revision_conflict")
    assert svc.get(p, tid).body.name == "Two"
    assert code(svc.update, p, "0" * 32, draft(), expected_revision=1) == (404, "type_not_found")


def test_revision_limit(store, contexts, svc):
    p = who()
    tid = svc.create(p, new_context(contexts, p), draft(), "key-00000001")[0].type_id
    with store._lock:
        conn = store._get_conn()
        conn.execute("UPDATE document_types SET revision=?", (2**31 - 1,))
        conn.commit()
    assert code(svc.update, p, tid, draft("X"), expected_revision=2**31 - 1) == (409, "type_revision_limit")
    assert svc.get(p, tid).revision == 2**31 - 1


def test_concurrent_create_same_key_yields_one_type(store, contexts, svc):
    p = who()
    cid = new_context(contexts, p)
    barrier = Barrier(8, timeout=WAIT)

    def attempt(_):
        barrier.wait()
        return svc.create(p, cid, draft("Same"), "key-00000001")

    with ThreadPoolExecutor(8) as pool:
        results = [f.result(timeout=2 * WAIT) for f in [pool.submit(attempt, n) for n in range(8)]]
    assert len({receipt for receipt, _ in results}) == 1
    assert sorted(replayed for _, replayed in results) == [False] + [True] * 7
    assert type_rows(store) == 1


def test_concurrent_updates_have_one_winner(contexts, svc):
    p = who()
    tid = svc.create(p, new_context(contexts, p), draft(), "key-00000001")[0].type_id
    barrier = Barrier(8, timeout=WAIT)

    def attempt(n):
        barrier.wait()
        try:
            return svc.update(p, tid, draft(f"W{n}"), expected_revision=1).body.name
        except ApplicationError as error:
            return (error.status, error.code)

    with ThreadPoolExecutor(8) as pool:
        results = [f.result(timeout=2 * WAIT) for f in [pool.submit(attempt, n) for n in range(8)]]
    winners = [r for r in results if isinstance(r, str)]
    assert len(winners) == 1 and results.count((409, "type_revision_conflict")) == 7
    record = svc.get(p, tid)
    assert (record.revision, record.body.name) == (2, winners[0])


def test_mutated_body_is_rejected(store, contexts, svc):
    p = who()
    cid = new_context(contexts, p)
    tid = svc.create(p, cid, draft("Ok"), "key-00000001")[0].type_id
    bad = TypeDraft(name="Bad", specific_fields=[FIELD])
    bad.specific_fields.append(bad.specific_fields[0])  # duplicate name, bypassing construction-time checks
    assert code(svc.create, p, cid, bad, "key-00000002") == (422, "invalid_type_body")
    assert code(svc.update, p, tid, bad, expected_revision=1) == (422, "invalid_type_body")
    assert type_rows(store) == 1 and svc.get(p, tid).body.name == "Ok"
    with pytest.raises(ApplicationError) as error:
        svc.create(p, cid, bad, "key-00000002")
    assert "Bad" not in error.value.message and error.value.__suppress_context__


def test_stored_and_returned_values_are_isolated(contexts, svc):
    p = who()
    cid = new_context(contexts, p)
    original = TypeDraft(name="Iso", specific_fields=[FIELD])
    tid = svc.create(p, cid, original, "key-00000001")[0].type_id
    original.specific_fields.append(original.specific_fields[0])  # caller keeps mutating its object
    record = svc.get(p, tid)
    assert len(record.body.specific_fields) == 1
    record.body.specific_fields.clear()  # mutating a response does not reach storage
    assert len(svc.get(p, tid).body.specific_fields) == 1
    updated = svc.update(p, tid, TypeDraft(name="Iso2", specific_fields=[FIELD]), expected_revision=1)
    updated.body.specific_fields.clear()
    assert len(svc.get(p, tid).body.specific_fields) == 1


def test_sql_failure_rolls_back_and_next_call_works(store, contexts, svc, monkeypatch):
    p = who()
    cid = new_context(contexts, p)

    class FailOnce:
        def __init__(self, conn):
            self.conn, self.armed = conn, True

        def execute(self, sql, *args):
            if self.armed and sql.lstrip().startswith("INSERT INTO document_types"):
                self.armed = False
                raise sqlite3.OperationalError("leaky-detail Sensitive-Body")
            return self.conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    real = store._get_conn()
    flaky = FailOnce(real)
    monkeypatch.setattr(store, "_get_conn", lambda: flaky)
    with pytest.raises(ApplicationError) as error:
        svc.create(p, cid, draft("Sensitive-Body"), "key-00000001")
    assert (error.value.status, error.value.code) == (503, "storage_unavailable")
    assert "Sensitive-Body" not in error.value.message and "leaky" not in error.value.message
    assert real.in_transaction is False and type_rows(store) == 0
    receipt, replayed = svc.create(p, cid, draft("Sensitive-Body"), "key-00000001")  # same key was not consumed
    assert replayed is False and svc.get(p, receipt.type_id).body.name == "Sensitive-Body"
