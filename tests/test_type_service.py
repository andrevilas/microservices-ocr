"""Type draft CRUD service: synthetic data only, real JobStore on tmp_path, no network."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from app.application import ApplicationError, Principal
from app.document_models import ContextCreate, RequiredWhen, TypeDraft
from app.services.context_service import ContextService
from app.services.storage_service import JobStore
from app.services.type_service import TypeService, TypeVersionRecord, _snapshot

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
    assert "Bad" not in error.value.message
    assert error.value.__context__ is None and error.value.__cause__ is None


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


@pytest.mark.parametrize("kind", ["copied_field", "constructed", "copied", "tuple", "condition", "mutated", "dict"])
def test_invalid_internal_type_dto_is_atomic(store, contexts, svc, caplog, kind):
    marker = "internal-type-secret-marker"
    p = who()
    cid = new_context(contexts, p)
    tid = svc.create(p, cid, draft("Original", "source"), "key-original-001")[0].type_id
    before = svc.get(p, tid)
    candidate = draft(marker, "source", "dependent")
    if kind == "copied_field":
        candidate = candidate.model_copy(update={"specific_fields": [
            candidate.specific_fields[0].model_copy(update={"max_items": True})]})
    elif kind == "constructed":
        candidate = TypeDraft.model_construct(name={"secret": marker}, description="", specific_fields=[])
    elif kind == "copied":
        candidate = candidate.model_copy(update={"name": {"secret": marker}})
    elif kind == "tuple":
        candidate = candidate.model_copy(update={"specific_fields": tuple(candidate.specific_fields)})
    elif kind == "condition":
        condition = RequiredWhen(field="source", equals="ok").model_copy(update={"equals": [marker]})
        candidate = candidate.model_copy(update={"specific_fields": [candidate.specific_fields[0],
            candidate.specific_fields[1].model_copy(update={"required_when": condition})]})
    elif kind == "mutated":
        candidate.specific_fields.append(candidate.specific_fields[0])
    else:
        candidate = candidate.model_dump(mode="python")

    for operation in (lambda: svc.create(p, cid, candidate, "key-retry-0001"),
                      lambda: svc.update(p, tid, candidate, expected_revision=1)):
        with pytest.raises(ApplicationError) as error:
            operation()
        exc = error.value
        assert (exc.status, exc.code, exc.message) == (422, "invalid_type_body", "Type body is invalid.")
        assert exc.__cause__ is None and exc.__context__ is None
        assert marker not in repr(exc.args) + str(exc) + caplog.text
        assert type_rows(store) == 1 and svc.get(p, tid) == before
    receipt, replayed = svc.create(p, cid, draft("Corrected"), "key-retry-0001")
    assert not replayed and type_rows(store) == 2
    assert svc.get(p, receipt.type_id).body.name == "Corrected"


@pytest.mark.parametrize("equals,field_type", [(False, "boolean"), (0, "integer")])
def test_type_snapshot_preserves_types_and_detaches_lists(contexts, svc, equals, field_type):
    candidate = TypeDraft(name="Valid", description="synthetic", specific_fields=[
        dict(name="source", type=field_type, description="synthetic"),
        dict(name="dependent", type="string", description="synthetic", examples=["sample"],
             required_when=dict(field="source", equals=equals))])
    detached = _snapshot(candidate)
    assert type(detached.specific_fields) is list
    assert type(detached.specific_fields[1].examples) is list
    assert type(detached.specific_fields[1].required_when.equals) is type(equals)
    assert detached.specific_fields[1].required_when.equals == equals
    assert detached.specific_fields is not candidate.specific_fields
    assert detached.specific_fields[1].examples is not candidate.specific_fields[1].examples
    p = who()
    cid = new_context(contexts, p)
    receipt, _ = svc.create(p, cid, candidate, "key-valid-0001")
    updated = svc.update(p, receipt.type_id, candidate, expected_revision=1)
    candidate.specific_fields[1].examples.append("input change")
    candidate.specific_fields.clear()
    detached.specific_fields[1].examples.append("snapshot change")
    assert updated.body.specific_fields[1].examples == ["sample"]
    updated.body.specific_fields[1].examples.clear()
    assert svc.get(p, receipt.type_id).body.specific_fields[1].examples == ["sample"]
    assert detached.specific_fields[1].examples == ["sample", "snapshot change"]


# ---------------------------------------------------------------- A05b: immutable publication
PUB = frozenset({"contexts:read", "contexts:write", "contexts:publish"})


def spec(name, **extra):
    return dict(name=name, type="string", description="synthetic field", **extra)


def published_type(contexts, svc, p, common=(), specific=(), n=1):
    """Context + type with synthetic fields; returns (context_id, type_id)."""
    cid = contexts.create(p, ContextCreate(name="Ctx", common_fields=list(common)), f"ctx-key-{n:08d}")[0].context_id
    tid = svc.create(p, cid, TypeDraft(name="Typ", description="synthetic", specific_fields=list(specific)),
                     f"typ-key-{n:08d}")[0].type_id
    return cid, tid


def pub(svc, p, tid, key, rev=1, crev=1):
    return svc.publish(p, tid, expected_revision=rev, expected_context_revision=crev, key=key)


def version_rows(store):
    with store._lock:
        return store._get_conn().execute("SELECT COUNT(*) FROM document_type_versions").fetchone()[0]


def stored(store, tid, version):
    with store._lock:
        return store._get_conn().execute(
            "SELECT canonical, schema_sha256, type_name, context_name, draft_revision, context_revision "
            "FROM document_type_versions WHERE type_id=? AND version=?", (tid, version)).fetchone()


def test_publish_snapshots_stay_intact_after_edits_and_reopen(tmp_path, store, contexts, svc):
    import hashlib
    import json
    p = who(scopes=PUB)
    cid, tid = published_type(contexts, svc, p, common=[spec("common_one")], specific=[spec("own_one")])
    s1, replayed = pub(svc, p, tid, "pub-key-0001")
    assert replayed is False and (s1.version, s1.draft_revision, s1.context_revision) == (1, 1, 1)
    before = stored(store, tid, 1)
    assert hashlib.sha256(before[0]).hexdigest() == before[1] == s1.schema_sha256
    contexts.update(p, cid, ContextCreate(name="Ctx2", common_fields=[spec("common_two")]), expected_revision=1)
    svc.update(p, tid, TypeDraft(name="Typ2", specific_fields=[spec("own_two")]), expected_revision=1)
    s2, _ = pub(svc, p, tid, "pub-key-0002", rev=2, crev=2)
    assert (s2.version, s2.draft_revision, s2.context_revision, s2.type_name, s2.context_name) == (2, 2, 2, "Typ2", "Ctx2")
    assert s2.schema_sha256 != s1.schema_sha256
    assert stored(store, tid, 1) == before and stored(store, tid, 1)[2:4] == ("Typ", "Ctx")
    reopened = TypeService(JobStore(tmp_path))
    record = reopened.get_version(p, tid, 1)
    assert record.schema_document == json.loads(before[0]) and record.schema_sha256 == before[1]
    assert [f["name"] for f in record.schema_document["common_fields"]] == ["common_one"]
    assert [f["name"] for f in record.schema_document["specific_fields"]] == ["own_one"]
    assert [f["name"] for f in record.schema_document["fields"]] == ["common_one", "own_one"]
    assert (record.type_name, record.context_name, record.draft_revision) == ("Typ", "Ctx", 1)
    assert "Typ" not in before[0].decode() and cid not in before[0].decode() and tid not in before[0].decode()
    assert svc.get(p, tid).revision == 2  # the draft stays editable; no published state in the draft
    assert code(svc.get_version, p, tid, 3) == (404, "type_version_not_found")


def test_required_field_publishes_and_cross_field_condition_is_rejected(store, contexts, svc):
    p = who(scopes=PUB)
    _, ok = published_type(contexts, svc, p, specific=[spec("must_have", required=True)])
    assert pub(svc, p, ok, "pub-key-0001")[0].version == 1  # complete=False: no document values needed
    bad_fields = [dict(name="count", type="integer", description="synthetic field"),
                  spec("other", required_when=dict(field="count", equals="abc"))]
    _, bad = published_type(contexts, svc, p, specific=bad_fields, n=2)
    assert code(pub, svc, p, bad, "pub-key-0002") == (422, "invalid_type_schema")
    assert version_rows(store) == 1


def test_schema_limits_and_conflicts_are_rejected_without_writes(store, contexts, svc):
    p = who(scopes=PUB)
    _, dup = published_type(contexts, svc, p, common=[spec("same_name")], specific=[spec("same_name")])
    assert code(pub, svc, p, dup, "pub-key-0001") == (422, "invalid_type_schema")
    _, many = published_type(contexts, svc, p, common=[spec(f"c_{n}") for n in range(60)],
                             specific=[spec(f"s_{n}") for n in range(41)], n=2)
    assert code(pub, svc, p, many, "pub-key-0002") == (422, "invalid_type_schema")
    big = "x" * 4000
    _, heavy = published_type(contexts, svc, p, common=[spec(f"c_{n}", examples=[big]) for n in range(40)],
                              specific=[spec(f"s_{n}", examples=[big]) for n in range(40)], n=3)
    assert code(pub, svc, p, heavy, "pub-key-0003") == (422, "invalid_type_schema")
    assert version_rows(store) == 0


def test_revision_mismatch_conflicts_do_not_write_or_consume_key(store, contexts, svc):
    p = who(scopes=PUB)
    cid, tid = published_type(contexts, svc, p, common=[spec("common_one")])
    contexts.update(p, cid, ContextCreate(name="Ctx", common_fields=[spec("common_changed")]), expected_revision=1)
    assert code(pub, svc, p, tid, "pub-key-0001", 1, 1) == (409, "context_revision_conflict")
    svc.update(p, tid, TypeDraft(name="Typ2"), expected_revision=1)
    assert code(pub, svc, p, tid, "pub-key-0001", 1, 2) == (409, "type_revision_conflict")
    assert version_rows(store) == 0
    assert pub(svc, p, tid, "pub-key-0001", 2, 2)[1] is False  # same key still usable
    for bad in (True, "1", 1.0, 0, -1, 2**31, None):
        assert code(pub, svc, p, tid, "pub-key-0002", bad, 2) == (422, "invalid_type_query")
        assert code(pub, svc, p, tid, "pub-key-0002", 2, bad) == (422, "invalid_type_query")
    assert code(pub, svc, p, tid, "short") == (422, "invalid_idempotency_key")


def test_publish_replay_survives_edits_and_key_reuse_conflicts(store, contexts, svc):
    p = who(scopes=PUB)
    cid, tid = published_type(contexts, svc, p)
    first, _ = pub(svc, p, tid, "pub-key-0001")
    contexts.update(p, cid, ContextCreate(name="Other"), expected_revision=1)
    svc.update(p, tid, TypeDraft(name="Other"), expected_revision=1)
    assert pub(svc, p, tid, "pub-key-0001") == (first, True)  # original summary, despite stale revisions
    assert code(pub, svc, p, tid, "pub-key-0001", 2, 2) == (409, "idempotency_conflict")
    assert version_rows(store) == 1
    second, replayed = pub(svc, p, tid, "pub-key-0002", 2, 2)  # new key: explicit publication even if schema equal
    assert replayed is False and second.version == 2
    with store._lock:
        assert "pub-key-0001" not in str(store._get_conn().execute("SELECT * FROM document_type_versions").fetchall())


def test_publish_scopes_owner_isolation_and_admin_without_bypass(store, contexts, svc):
    owner = who(scopes=PUB)
    _, tid = published_type(contexts, svc, owner)
    pub(svc, owner, tid, "pub-key-0001")
    for scopes in ({"contexts:read", "contexts:write"}, {"contexts:publish"}, set()):
        assert code(pub, svc, who(scopes=scopes), tid, "pub-key-0002") == (403, "insufficient_scope")
    assert code(svc.get_version, who(scopes={"contexts:publish"}), tid, 1) == (403, "insufficient_scope")
    assert code(svc.list_versions, who(scopes=set()), tid) == (403, "insufficient_scope")
    other = who("user:2", scopes=PUB, is_admin=True, kind="service")
    missing = (404, "type_not_found")
    assert code(pub, svc, other, tid, "pub-key-0003") == missing == code(pub, svc, other, "0" * 32, "pub-key-0003")
    assert code(pub, svc, other, "bad-id", "pub-key-0003") == missing
    assert code(svc.get_version, other, tid, 1) == (404, "type_version_not_found")
    assert code(svc.list_versions, other, tid) == missing
    assert code(pub, svc, other, tid, "pub-key-0001") == missing  # replay never crosses owners
    assert version_rows(store) == 1


def test_returned_values_are_isolated(contexts, svc):
    p = who(scopes=PUB)
    _, tid = published_type(contexts, svc, p, specific=[spec("own_one")])
    pub(svc, p, tid, "pub-key-0001")
    record = svc.get_version(p, tid, 1)
    record.schema_document["fields"].clear()
    record.schema_document["schema_version"] = "tampered"
    again = svc.get_version(p, tid, 1)
    assert len(again.schema_document["fields"]) == 1 and again.schema_document["schema_version"] != "tampered"


def test_version_record_wire_name_is_schema(contexts, svc):
    p = who(scopes=PUB)
    _, tid = published_type(contexts, svc, p, specific=[spec("own_one")])
    summary, _ = pub(svc, p, tid, "pub-key-0001")
    record = svc.get_version(p, tid, 1)
    dumped = record.model_dump(by_alias=True)
    assert "schema" in dumped and "schema_document" not in dumped
    assert dumped["schema"] == record.schema_document
    assert "schema" in TypeVersionRecord.model_json_schema(by_alias=True)["properties"]
    assert "schema" not in summary.model_dump(by_alias=True)
    assert "schema_document" not in summary.model_dump(by_alias=True)


def test_concurrent_publish_same_key_yields_one_version(store, contexts, svc):
    p = who(scopes=PUB)
    _, tid = published_type(contexts, svc, p)
    barrier = Barrier(8, timeout=WAIT)

    def attempt(_):
        barrier.wait()
        return pub(svc, p, tid, "pub-key-0001")

    with ThreadPoolExecutor(8) as pool:
        results = [f.result(timeout=2 * WAIT) for f in [pool.submit(attempt, n) for n in range(8)]]
    assert len({summary for summary, _ in results}) == 1
    assert sorted(replayed for _, replayed in results) == [False] + [True] * 7
    assert version_rows(store) == 1


def test_publish_versus_context_update_freezes_expected_revision_or_conflicts(store, contexts, svc):
    p = who(scopes=PUB)
    cid, tid = published_type(contexts, svc, p, common=[spec("common_one")])
    barrier = Barrier(2, timeout=WAIT)

    def publisher():
        barrier.wait()
        try:
            return pub(svc, p, tid, "pub-key-0001")[0]
        except ApplicationError as error:
            return (error.status, error.code)

    def editor():
        barrier.wait()
        contexts.update(p, cid, ContextCreate(name="Ctx", common_fields=[spec("common_two")]), expected_revision=1)

    with ThreadPoolExecutor(2) as pool:
        a, b = pool.submit(publisher), pool.submit(editor)
        outcome, _ = a.result(timeout=2 * WAIT), b.result(timeout=2 * WAIT)
    if isinstance(outcome, tuple):
        assert outcome == (409, "context_revision_conflict") and version_rows(store) == 0
    else:
        assert outcome.context_revision == 1
        names = [f["name"] for f in svc.get_version(p, tid, 1).schema_document["common_fields"]]
        assert names == ["common_one"]


def test_quotas_per_type_and_per_owner_with_replay_when_full(store, contexts, svc):
    p = who(scopes=PUB)
    _, first = published_type(contexts, svc, p, n=1)
    for n in range(20):
        pub(svc, p, first, f"pub-key-{n:04d}")
    assert code(pub, svc, p, first, "pub-key-9999") == (429, "type_version_quota_exceeded")
    assert pub(svc, p, first, "pub-key-0003")[1] is True  # replay precedes quota
    assert version_rows(store) == 20
    for t in range(2, 6):  # fill the owner to the global limit using other types
        _, other = published_type(contexts, svc, p, n=t)
        for n in range(20):
            pub(svc, p, other, f"pub-t{t}-{n:04d}")
    assert version_rows(store) == 100
    _, extra = published_type(contexts, svc, p, n=7)
    assert code(pub, svc, p, extra, "pub-key-8888") == (429, "type_version_quota_exceeded")
    assert pub(svc, p, first, "pub-key-0000")[1] is True
    q = who("user:2", scopes=PUB)
    _, qtid = published_type(contexts, svc, q, n=9)
    assert pub(svc, q, qtid, "pub-key-0000")[1] is False  # other owners are unaffected, no global dedup


def test_list_versions_pagination_summaries_and_query_validation(contexts, svc):
    p = who(scopes=PUB)
    _, tid = published_type(contexts, svc, p, specific=[spec("own_one")])
    for n in range(5):
        pub(svc, p, tid, f"pub-key-{n:04d}")
    seen, after = [], None
    while True:
        page = svc.list_versions(p, tid, limit=2, after=after)
        assert len(page.items) <= 2 and all("schema" not in i.model_dump(by_alias=True) for i in page.items)
        seen += [i.version for i in page.items]
        if page.next_cursor is None:
            break
        assert page.next_cursor == page.items[-1].version
        after = page.next_cursor
    assert seen == [1, 2, 3, 4, 5]
    assert svc.list_versions(p, tid, limit=5).next_cursor is None and svc.list_versions(p, tid, after=5).items == []
    for kwargs in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": "5"}, {"after": 0}, {"after": -1},
                   {"after": True}, {"after": "1"}):
        assert code(svc.list_versions, p, tid, **kwargs) == (422, "invalid_type_query")
    for bad in ("latest", 0, -1, True, 1.0, None):
        assert code(svc.get_version, p, tid, bad) == (422, "invalid_type_query")
    assert code(svc.get_version, p, "bad-id", 1) == (404, "type_not_found")
    assert code(svc.list_versions, p, "0" * 32) == (404, "type_not_found")


def test_corrupted_stored_version_is_503_without_content(store, contexts, svc):
    p = who(scopes=PUB)
    _, tid = published_type(contexts, svc, p, specific=[spec("sensitive_field")])
    pub(svc, p, tid, "pub-key-0001")
    pub(svc, p, tid, "pub-key-0002")
    pub(svc, p, tid, "pub-key-0003")
    with store._lock:
        conn = store._get_conn()
        conn.execute("UPDATE document_type_versions SET canonical=? WHERE version=1", (b'{"schema_version":"1"}',))
        conn.execute("UPDATE document_type_versions SET schema_sha256=? WHERE version=2", ("0" * 64,))
        conn.execute("UPDATE document_type_versions SET created_at='garbage' WHERE version=3")
        conn.commit()
    for version in (1, 2, 3):
        with pytest.raises(ApplicationError) as error:
            svc.get_version(p, tid, version)
        assert (error.value.status, error.value.code) == (503, "storage_unavailable")
        assert "sensitive_field" not in error.value.message
    with pytest.raises(ApplicationError) as error:
        svc.list_versions(p, tid)
    assert error.value.status == 503


def test_publish_sql_failure_rolls_back_and_key_is_not_consumed(store, contexts, svc, monkeypatch):
    p = who(scopes=PUB)
    _, tid = published_type(contexts, svc, p, specific=[spec("own_one")])

    class FailOnce:
        def __init__(self, conn):
            self.conn, self.armed = conn, True

        def execute(self, sql, *args):
            if self.armed and sql.lstrip().startswith("INSERT INTO document_type_versions"):
                self.armed = False
                raise sqlite3.OperationalError("leaky-detail own_one")
            return self.conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    real = store._get_conn()
    flaky = FailOnce(real)
    monkeypatch.setattr(store, "_get_conn", lambda: flaky)
    with pytest.raises(ApplicationError) as error:
        pub(svc, p, tid, "pub-key-0001")
    assert (error.value.status, error.value.code) == (503, "storage_unavailable")
    assert "leaky" not in error.value.message and "own_one" not in error.value.message
    assert real.in_transaction is False and version_rows(store) == 0
    summary, replayed = pub(svc, p, tid, "pub-key-0001")
    assert replayed is False and summary.version == 1
