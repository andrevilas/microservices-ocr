"""Context CRUD service: synthetic data only, real JobStore on tmp_path, no network."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.application import ApplicationError, Principal
from app.document_models import ContextCreate
from app.services.context_service import ContextService
from app.services.storage_service import JobStore

BOTH = frozenset({"contexts:read", "contexts:write"})


def who(subject="user:1", scopes=BOTH, **extra):
    return Principal(subject=subject, user_id=1, scopes=frozenset(scopes), **extra)


def body(name="Synthetic", *fields):
    return ContextCreate(name=name, description="synthetic", common_fields=[
        dict(name=f, type="string", description="synthetic field") for f in fields])


def code(call, *args, **kwargs):
    with pytest.raises(ApplicationError) as error:
        call(*args, **kwargs)
    return error.value.status, error.value.code


def rows(store):
    with store._lock:
        return store._get_conn().execute("SELECT COUNT(*) FROM document_contexts").fetchone()[0]


@pytest.fixture
def store(tmp_path):
    return JobStore(tmp_path)


@pytest.fixture
def svc(store):
    return ContextService(store)


def test_crud_roundtrip_and_reopen(tmp_path, store, svc):
    p = who()
    receipt, replayed = svc.create(p, body("Alpha", "cpf_like"), "key-00000001")
    assert (replayed, receipt.revision) == (False, 1)
    assert svc.get(p, receipt.context_id).body == body("Alpha", "cpf_like")
    updated = svc.update(p, receipt.context_id, body("Beta"), expected_revision=1)
    assert (updated.revision, updated.created_at) == (2, receipt.created_at) and updated.updated_at >= updated.created_at
    page = svc.list(p)
    assert [(i.name, i.revision) for i in page.items] == [("Beta", 2)] and page.next_cursor is None
    assert not hasattr(page.items[0], "body")
    ContextService(store)  # second init must not wipe
    reopened = ContextService(JobStore(tmp_path))
    assert reopened.get(p, receipt.context_id) == updated


def test_scopes_enforced(svc):
    p = who()
    cid = svc.create(p, body(), "key-00000001")[0].context_id
    reader, writer, nobody = who(scopes={"contexts:read"}), who(scopes={"contexts:write"}), who(scopes=set())
    assert svc.get(reader, cid).context_id == cid and svc.list(reader).items
    for call in (lambda: svc.create(reader, body(), "key-00000002"), lambda: svc.update(reader, cid, body(), expected_revision=1),
                 lambda: svc.create(writer, body(), "key-00000002"), lambda: svc.update(writer, cid, body(), expected_revision=1),
                 lambda: svc.get(writer, cid), lambda: svc.list(writer), lambda: svc.get(nobody, cid), lambda: svc.list(nobody)):
        assert code(call) == (403, "insufficient_scope")
    assert svc.get(p, cid).revision == 1


def test_owner_isolation_and_admin_has_no_bypass(svc):
    owner, other = who("user:1"), who("user:2", is_admin=True, kind="service")
    cid = svc.create(owner, body(), "key-00000001")[0].context_id
    missing = code(svc.get, other, "0" * 32)
    assert missing == (404, "context_not_found")
    assert code(svc.get, other, cid) == missing and code(svc.get, other, "not-an-id") == missing
    assert code(svc.update, other, cid, body("X"), expected_revision=1) == missing
    assert svc.list(other).items == [] and svc.get(owner, cid).body.name == "Synthetic"


def test_create_idempotency(svc):
    p, q = who(), who("user:2")
    receipt, _ = svc.create(p, body("One"), "key-00000001")
    assert svc.create(p, body("One"), "key-00000001") == (receipt, True)
    svc.update(p, receipt.context_id, body("Two"), expected_revision=1)
    assert svc.create(p, body("One"), "key-00000001") == (receipt, True)
    assert svc.get(p, receipt.context_id).revision == 2
    assert code(svc.create, p, body("Different"), "key-00000001") == (409, "idempotency_conflict")
    other, replayed = svc.create(q, body("Different"), "key-00000001")
    assert not replayed and other.context_id != receipt.context_id
    for bad in ("short", "bad key with spaces", "é" * 10, None):
        assert code(svc.create, p, body(), bad) == (422, "invalid_idempotency_key")


def test_concurrent_create_and_update(store, svc):
    p = who()

    def race(fn):
        gate = Barrier(2, timeout=10)

        def run(i):
            gate.wait()
            try:
                return fn(i)
            except ApplicationError as error:
                return error
        with ThreadPoolExecutor(2) as pool:
            return [f.result(timeout=20) for f in [pool.submit(run, i) for i in range(2)]]

    results = race(lambda i: svc.create(p, body("Same"), "key-00000001"))
    assert len({r[0].context_id for r in results}) == 1 and sorted(r[1] for r in results) == [False, True] and rows(store) == 1
    cid = results[0][0].context_id
    outcomes = race(lambda i: svc.update(p, cid, body(f"Writer{i}"), expected_revision=1))
    assert sorted(type(o).__name__ for o in outcomes) == ["ApplicationError", "ContextRecord"]
    assert [o.code for o in outcomes if isinstance(o, ApplicationError)] == ["context_revision_conflict"]
    assert svc.get(p, cid).revision == 2


def test_quota_and_pagination(svc):
    p, q = who(), who("user:2")
    ids = [svc.create(p, body(f"N{i % 3}"), f"key-{i:08d}")[0].context_id for i in range(100)]
    assert code(svc.create, p, body(), "key-99999999") == (429, "context_quota_exceeded")
    assert svc.create(p, body("N0"), "key-00000000")[1] is True
    foreign = svc.create(q, body(), "key-99999999")[0].context_id
    seen, after = [], None
    while True:
        page = svc.list(p, limit=40, after=after)
        seen += [i.context_id for i in page.items]
        after = page.next_cursor
        if after is None:
            break
    assert seen == sorted(ids) and foreign not in seen
    assert [i.context_id for i in svc.list(q).items] == [foreign]
    assert svc.list(p, limit=100).next_cursor is None and svc.list(p, after="f" * 32).items == []
    for kwargs in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": "5"}, {"after": "X" * 32}, {"after": "abc"}, {"after": 5}):
        assert code(svc.list, p, **kwargs) == (422, "invalid_context_query")


@pytest.mark.parametrize("revision", [0, -1, 2**31, True, "1", 1.0, None])
def test_expected_revision_must_be_strict_int(svc, revision):
    p = who()
    cid = svc.create(p, body(), "key-00000001")[0].context_id
    assert code(svc.update, p, cid, body(), expected_revision=revision) == (422, "invalid_context_query")
    assert code(svc.update, p, cid, body(), expected_revision=2) == (409, "context_revision_conflict")


def test_revision_limit(store, svc):
    p = who()
    cid = svc.create(p, body(), "key-00000001")[0].context_id
    with store._lock, store._get_conn() as conn:
        conn.execute("UPDATE document_contexts SET revision=?", (2**31 - 1,))
    assert code(svc.update, p, cid, body(), expected_revision=2**31 - 1) == (409, "context_revision_limit")


def test_mutated_body_is_revalidated_and_returns_are_copies(svc):
    p = who()
    bad = body("Mutated", "dup_field")
    bad.common_fields.append(bad.common_fields[0])
    assert code(svc.create, p, bad, "key-00000001") == (422, "invalid_context_body")
    good = body("Safe", "one_field")
    cid = svc.create(p, good, "key-00000002")[0].context_id
    good.common_fields.clear()
    assert code(svc.update, p, cid, bad, expected_revision=1) == (422, "invalid_context_body")
    record = svc.get(p, cid)
    assert [f.name for f in record.body.common_fields] == ["one_field"]
    record.body.common_fields.clear()
    assert len(svc.get(p, cid).body.common_fields) == 1
    assert code(svc.create, p, {"name": "dict"}, "key-00000003") == (422, "invalid_context_body")


def test_sql_failure_rolls_back(store, svc):
    p = who()
    cid = svc.create(p, body("Original"), "key-00000001")[0].context_id

    def trigger(sql):
        with store._lock, store._get_conn() as conn:
            conn.execute(sql)

    trigger("CREATE TRIGGER fail_update BEFORE UPDATE ON document_contexts BEGIN SELECT RAISE(ABORT, 'boom'); END")
    assert code(svc.update, p, cid, body("secret-name"), expected_revision=1) == (503, "storage_unavailable")
    record = svc.get(p, cid)
    assert (record.revision, record.body.name) == (1, "Original")
    trigger("DROP TRIGGER fail_update")
    assert svc.update(p, cid, body("Next"), expected_revision=1).revision == 2

    trigger("CREATE TRIGGER fail_insert BEFORE INSERT ON document_contexts BEGIN SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(ApplicationError) as error:
        svc.create(p, body("secret-name"), "key-00000002")
    assert (error.value.status, error.value.code) == (503, "storage_unavailable")
    assert "secret-name" not in repr(error.value.args) + str(error.value.__cause__) and error.value.__suppress_context__
    assert rows(store) == 1
    trigger("DROP TRIGGER fail_insert")
    receipt, replayed = svc.create(p, body("secret-name"), "key-00000002")  # key was not consumed
    assert not replayed and rows(store) == 2 and receipt.revision == 1
