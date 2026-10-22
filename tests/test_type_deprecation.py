"""Synthetic SQLite coverage for L1b; no transport or DocumentService integration."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import sqlite3
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.application import ApplicationError, Principal
from app.catalog_models import TypeDeprecateInput, TypeDeprecationReceipt, VersionLifecycle
from app.document_models import ContextCreate, TypeDraft
from app.services.context_service import ContextService
from app.services.storage_service import JobStore
from app.services.type_service import TypeService


PRIVATE = "PRIVATE-SYNTHETIC-MARKER"
PUBLISH_KEY = "publish-key-00000001"
DEPRECATE_KEY = "deprecate-key-00000001"
DEADLINE = 15


def who(subject="owner", *, scopes=None, is_admin=False):
    if scopes is None:
        scopes = {"contexts:read", "contexts:write", "contexts:publish"}
    return Principal(subject=subject, user_id=None, scopes=frozenset(scopes), is_admin=is_admin)


@contextmanager
def opened(path):
    store = JobStore(path)
    try:
        contexts = ContextService(store)
        yield SimpleNamespace(store=store, contexts=contexts, service=TypeService(store), path=path)
    finally:
        with store._lock:
            if store._conn is not None:
                store._conn.close()


def pub(e, tid=None, *, key=PUBLISH_KEY, revision=1, context_revision=1):
    return e.service.publish(e.principal, tid or e.tid, expected_revision=revision,
                             expected_context_revision=context_revision, key=key)[0]


def seed(e):
    e.principal = who()
    context, replayed = e.contexts.create(e.principal, ContextCreate(name="Ctx"), "context-key-00000001")
    assert not replayed
    e.cid = context.context_id
    draft, replayed = e.service.create(
        e.principal, e.cid, TypeDraft(name="Synthetic", specific_fields=[]), "type-key-00000001")
    assert not replayed
    e.tid = draft.type_id
    e.publication = pub(e)
    return e


@pytest.fixture
def published_type(tmp_path):
    with opened(tmp_path) as e:
        yield seed(e)


def rows(e, statement, parameters=()):
    with e.store._lock:
        return [tuple(row) for row in e.store._get_conn().execute(statement, parameters).fetchall()]


def write(e, statement, parameters=()):
    return e.service._write(lambda conn: conn.execute(statement, parameters).fetchall())


def ledger(e):
    return rows(e, "SELECT * FROM document_type_deprecations ORDER BY type_id, version")


def versions(e):
    return rows(e, "SELECT * FROM document_type_versions ORDER BY type_id, version")


def immutable(e):
    return (versions(e), rows(e, "SELECT * FROM document_types ORDER BY type_id"),
            rows(e, "SELECT * FROM document_contexts ORDER BY context_id"))


def fails(status, code, action):
    with pytest.raises(ApplicationError) as caught:
        action()
    error = caught.value
    assert getattr(error, "status_code", getattr(error, "status", None)) == status
    if code is not None:
        assert error.code == code
    assert PRIVATE not in str(error)
    return error


def deprecate(e, *, principal=None, tid=None, version=1, expected=1, key=DEPRECATE_KEY):
    return e.service.deprecate(principal or e.principal, tid or e.tid, version,
                              expected_lifecycle_revision=expected, key=key)


def binding(e, *, principal=None, tid=None, version=1, sha=None):
    return e.service._write(lambda conn: e.service._binding_version(
        conn, principal or e.principal, tid or e.tid, version,
        e.publication.schema_sha256 if sha is None else sha))


def test_dtos_are_closed_frozen_and_hide_inputs():
    value = TypeDeprecateInput(expected_lifecycle_revision=1)
    with pytest.raises(ValidationError):
        value.expected_lifecycle_revision = 2
    for field in ("state", "reason", "owner", "actor"):
        with pytest.raises(ValidationError) as caught:
            TypeDeprecateInput.model_validate({"expected_lifecycle_revision": 1, field: PRIVATE})
        assert PRIVATE not in str(caught.value)
    lifecycle = VersionLifecycle(state="deprecated", revision=2,
                                 changed_at=datetime(2026, 10, 16, tzinfo=timezone.utc))
    receipt = TypeDeprecationReceipt(type_id="a" * 32, version=1, schema_sha256="b" * 64,
                                     lifecycle=lifecycle)
    assert set(receipt.model_dump()) == {"type_id", "version", "schema_sha256", "lifecycle"}
    with pytest.raises(ValidationError):
        receipt.version = 2
    with pytest.raises(ValidationError):
        receipt.lifecycle.state = "published"
    with pytest.raises(ValidationError) as caught:
        TypeDeprecationReceipt.model_validate({**receipt.model_dump(), "actor": PRIVATE})
    assert PRIVATE not in str(caught.value)
    for state, revision in (("published", 1), ("published", 2), ("deprecated", 1)):
        with pytest.raises(ValidationError):
            TypeDeprecationReceipt(type_id="a" * 32, version=1, schema_sha256="b" * 64,
                                   lifecycle=VersionLifecycle(state=state, revision=revision,
                                                             changed_at=lifecycle.changed_at))
    for changed_at in (datetime(2026, 10, 16),
                       datetime(2026, 10, 16, tzinfo=timezone(timedelta(hours=1)))):
        with pytest.raises(ValidationError):
            TypeDeprecationReceipt(type_id="a" * 32, version=1, schema_sha256="b" * 64,
                                   lifecycle={"state": "deprecated", "revision": 2,
                                              "changed_at": changed_at})


@pytest.mark.parametrize("value", [True, False, "1", 1.0, 0, -1, 2**31, None])
def test_input_revision_is_strict(value):
    with pytest.raises(ValidationError):
        TypeDeprecateInput(expected_lifecycle_revision=value)


def test_transition_restart_and_historical_publication(tmp_path):
    with opened(tmp_path) as e:
        seed(e)
        tid, cid = e.tid, e.cid
        publication = e.publication.model_dump(mode="json")
        initial = e.service.get_version(e.principal, tid, 1)
        assert initial.lifecycle.state == "published"
        assert initial.lifecycle.revision == 1
        before = immutable(e)
        receipt, replayed = deprecate(e)
        assert not replayed
        assert immutable(e) == before
        assert receipt.lifecycle.state == "deprecated"
        assert receipt.lifecycle.revision == 2
        assert receipt.lifecycle.changed_at.utcoffset() == timedelta(0)
        current = e.service.get_version(e.principal, tid, 1)
        assert current.model_dump(exclude={"lifecycle"}) == initial.model_dump(exclude={"lifecycle"})
        assert current.lifecycle == receipt.lifecycle
        page = e.service.list_versions(e.principal, tid)
        assert len(page.items) == 1
        assert page.items[0].lifecycle == receipt.lifecycle
        assert page.items[0].schema_sha256 == publication["schema_sha256"]
        stored = rows(e, "SELECT owner, actor, state, lifecycle_revision, changed_at, key_hash, request_hash "
                         "FROM document_type_deprecations")[0]
        assert stored[:4] == (e.principal.subject, e.principal.subject, "deprecated", 2)
        assert datetime.fromisoformat(stored[4]) == receipt.lifecycle.changed_at
        assert all(len(value) == 64 and set(value) <= set("0123456789abcdef") for value in stored[5:])
        assert DEPRECATE_KEY not in repr(ledger(e))
        e.service.update(e.principal, tid, TypeDraft(name=PRIVATE, specific_fields=[]), expected_revision=1)
        e.contexts.update(e.principal, cid, ContextCreate(name=PRIVATE), expected_revision=1)
        assert versions(e) == before[0]
        original_ledger = ledger(e)
    with opened(tmp_path) as restarted:
        restarted.principal, restarted.tid = who(), tid
        replay, replayed = deprecate(restarted)
        assert replayed and replay == receipt
        assert ledger(restarted) == original_ledger
        current = restarted.service.get_version(restarted.principal, tid, 1)
        assert current.lifecycle == receipt.lifecycle
        assert current.model_dump(exclude={"lifecycle"}) == initial.model_dump(exclude={"lifecycle"})
        assert restarted.service.list_versions(restarted.principal, tid).items[0].lifecycle == receipt.lifecycle
        historical, replayed = restarted.service.publish(
            restarted.principal, tid, expected_revision=1, expected_context_revision=1, key=PUBLISH_KEY)
        assert replayed and historical.model_dump(mode="json") == publication
        assert "lifecycle" not in historical.model_dump()


def test_edits_do_not_change_target_and_new_publication_is_published(published_type):
    e = published_type
    original = versions(e)
    e.service.update(e.principal, e.tid, TypeDraft(name="Synthetic edited", specific_fields=[]),
                     expected_revision=1)
    e.contexts.update(e.principal, e.cid, ContextCreate(name="Ctx edited"), expected_revision=1)
    receipt, _ = deprecate(e)
    assert versions(e) == original
    assert receipt.schema_sha256 == e.publication.schema_sha256
    second = pub(e, key="publish-version-two", revision=2, context_revision=2)
    assert second.version == 2
    assert versions(e)[:1] == original
    first_view = e.service.get_version(e.principal, e.tid, 1)
    second_view = e.service.get_version(e.principal, e.tid, 2)
    assert (first_view.type_name, first_view.context_name, first_view.draft_revision,
            first_view.context_revision) == ("Synthetic", "Ctx", 1, 1)
    assert first_view.lifecycle == receipt.lifecycle
    assert (second_view.lifecycle.state, second_view.lifecycle.revision) == ("published", 1)
    page = e.service.list_versions(e.principal, e.tid, limit=1)
    assert page.next_cursor == 1 and page.items[0].lifecycle == receipt.lifecycle
    assert e.service.list_versions(e.principal, e.tid, after=1).items[0].lifecycle == second_view.lifecycle


def test_cas_replay_conflicts_and_rejected_keys_are_not_consumed(published_type):
    e = published_type
    fails(409, "type_lifecycle_revision_conflict", lambda: deprecate(e, expected=2))
    assert ledger(e) == []
    receipt, replayed = deprecate(e)
    assert not replayed
    saved = ledger(e)
    assert deprecate(e) == (receipt, True)
    fails(409, "idempotency_conflict", lambda: deprecate(e, expected=2))
    fails(409, "idempotency_conflict", lambda: deprecate(e, version=2))
    fails(409, "idempotency_conflict", lambda: deprecate(e, tid="f" * 32))
    new_key = "new-deprecation-key"
    fails(409, "type_lifecycle_revision_conflict", lambda: deprecate(e, key=new_key))
    fails(409, "type_version_already_deprecated", lambda: deprecate(e, expected=2, key=new_key))
    assert ledger(e) == saved
    pub(e, key="publish-second-for-key")
    second, replayed = deprecate(e, version=2, key=new_key)
    assert not replayed and second.version == 2
    assert len(ledger(e)) == 2


def test_publication_and_deprecation_key_namespaces_are_separate(published_type):
    e = published_type
    receipt, replayed = deprecate(e, key=PUBLISH_KEY)
    assert not replayed and receipt.version == 1
    historical, replayed = e.service.publish(e.principal, e.tid, expected_revision=1,
                                            expected_context_revision=1, key=PUBLISH_KEY)
    assert replayed and historical == e.publication


@pytest.mark.parametrize("admin", [False, True])
def test_foreign_and_foreign_admin_are_not_owners(published_type, admin):
    e = published_type
    stranger = who("foreign", is_admin=admin)
    fails(404, "type_version_not_found", lambda: deprecate(e, principal=stranger))
    fails(404, "type_version_not_found", lambda: binding(e, principal=stranger))
    assert ledger(e) == []


@pytest.mark.parametrize("scopes", [set(), {"contexts:read", "contexts:write"}, {"contexts:publish"}])
@pytest.mark.parametrize("replay", [False, True])
def test_current_scopes_required_even_for_replay_and_before_parsing(published_type, scopes, replay):
    e = published_type
    if replay:
        deprecate(e)
    restricted = who(scopes=scopes)
    saved = ledger(e)
    fails(403, None, lambda: deprecate(e, principal=restricted))
    fails(403, None, lambda: e.service.deprecate(
        restricted, PRIVATE, True, expected_lifecycle_revision="1", key=""))
    assert ledger(e) == saved


@pytest.mark.parametrize("value", [True, False, "1", 1.0, 0, -1, 2**63, None])
def test_service_version_is_strict(published_type, value):
    e = published_type
    fails(422, "invalid_type_query", lambda: deprecate(e, version=value))
    fails(422, "invalid_type_query", lambda: binding(e, version=value))
    assert ledger(e) == []


@pytest.mark.parametrize("value", [True, False, "1", 1.0, 0, -1, 2**31, None])
def test_service_cas_is_strict(published_type, value):
    e = published_type
    fails(422, "invalid_type_query", lambda: deprecate(e, expected=value))
    assert ledger(e) == []


def test_missing_and_invalid_targets(published_type):
    e = published_type
    for tid, version in (("f" * 32, 1), (e.tid, 2), (PRIVATE, 1)):
        fails(404, "type_version_not_found", lambda tid=tid, version=version:
              e.service.deprecate(e.principal, tid, version, expected_lifecycle_revision=1,
                                  key=DEPRECATE_KEY))
        fails(404, "type_version_not_found", lambda tid=tid, version=version:
              binding(e, tid=tid, version=version))
    assert ledger(e) == []


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("damage", ["type_owner", "context_owner", "type_context", "missing_type", "missing_context"])
def test_same_owner_type_context_links_required_for_action_replay_and_binding(published_type, replay, damage):
    e = published_type
    if replay:
        deprecate(e)
    statements = {
        "type_owner": ("UPDATE document_types SET owner=? WHERE type_id=?", ("foreign", e.tid)),
        "context_owner": ("UPDATE document_contexts SET owner=? WHERE context_id=?", ("foreign", e.cid)),
        "type_context": ("UPDATE document_types SET context_id=? WHERE type_id=?", ("f" * 32, e.tid)),
        "missing_type": ("DELETE FROM document_types WHERE type_id=?", (e.tid,)),
        "missing_context": ("DELETE FROM document_contexts WHERE context_id=?", (e.cid,)),
    }
    write(e, *statements[damage])
    saved = ledger(e)
    fails(404, "type_version_not_found", lambda: deprecate(e))
    fails(404, "type_version_not_found", lambda: binding(e))
    assert ledger(e) == saved


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("damage", ["canonical", "schema_sha256"])
def test_corrupt_immutable_reference_rejects_action_replay_and_binding(published_type, replay, damage):
    e = published_type
    if replay:
        deprecate(e)
    if damage == "canonical":
        write(e, "UPDATE document_type_versions SET canonical=? WHERE type_id=? AND version=1",
              (PRIVATE.encode(), e.tid))
    else:
        write(e, "UPDATE document_type_versions SET schema_sha256=? WHERE type_id=? AND version=1",
              ("0" * 64, e.tid))
    saved = ledger(e)
    fails(503, "storage_unavailable", lambda: deprecate(e))
    fails(503, "storage_unavailable", lambda: binding(e))
    assert ledger(e) == saved


def test_wrong_owner_on_global_deprecation_row_never_falls_back_to_published(published_type):
    e = published_type
    deprecate(e)
    write(e, "UPDATE document_type_deprecations SET owner=? WHERE type_id=?", ("foreign", e.tid))
    saved = ledger(e)
    fails(503, "storage_unavailable", lambda: deprecate(e))
    fails(503, "storage_unavailable", lambda: deprecate(e, key="another-valid-key"))
    fails(503, "storage_unavailable", lambda: binding(e))
    fails(503, "storage_unavailable", lambda: e.service.get_version(e.principal, e.tid, 1))
    fails(503, "storage_unavailable", lambda: e.service.list_versions(e.principal, e.tid))
    assert ledger(e) == saved


class FailOnce:
    def __init__(self, real, stage):
        self.real = real
        self.stage = stage
        self.armed = True

    def execute(self, statement, *args):
        if (self.armed and self.stage == "insert"
                and statement.lstrip().upper().startswith("INSERT INTO DOCUMENT_TYPE_DEPRECATIONS")):
            self.armed = False
            raise sqlite3.OperationalError(PRIVATE)
        return self.real.execute(statement, *args)

    def commit(self):
        if self.armed and self.stage == "commit":
            self.armed = False
            raise sqlite3.OperationalError(PRIVATE)
        return self.real.commit()

    def __getattr__(self, name):
        return getattr(self.real, name)


@pytest.mark.parametrize("stage", ["insert", "commit"])
def test_storage_failure_rolls_back_ledger_key_and_preserves_snapshot(published_type, monkeypatch, stage):
    e = published_type
    before = immutable(e)
    with e.store._lock:
        real = e.store._get_conn()
    proxy = FailOnce(real, stage)
    monkeypatch.setattr(e.store, "_get_conn", lambda: proxy)
    fails(503, "storage_unavailable", lambda: deprecate(e))
    assert not proxy.armed
    assert not real.in_transaction
    assert ledger(e) == []
    assert immutable(e) == before
    receipt, replayed = deprecate(e)
    assert not replayed
    assert deprecate(e) == (receipt, True)
    assert len(ledger(e)) == 1
    assert immutable(e) == before


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_deprecations_are_one_action(published_type, same_key):
    e = published_type
    workers = 4
    start = Barrier(workers)
    before = immutable(e)

    def act(index):
        start.wait(timeout=DEADLINE)
        key = DEPRECATE_KEY if same_key else f"concurrent-key-{index:08d}"
        try:
            receipt, replayed = deprecate(e, key=key)
            return "ok", replayed, receipt
        except ApplicationError as error:
            assert PRIVATE not in str(error)
            return "error", error.code, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(act, index) for index in range(workers)]
        results = [future.result(timeout=DEADLINE) for future in futures]
    assert all(not thread.is_alive() for thread in pool._threads)
    successes = [result for result in results if result[0] == "ok"]
    assert sum(not result[1] for result in successes) == 1
    if same_key:
        assert len(successes) == workers
        assert sum(result[1] for result in successes) == workers - 1
        assert all(result[2] == successes[0][2] for result in successes)
    else:
        assert len(successes) == 1
        assert [result[1] for result in results if result[0] == "error"] == [
            "type_lifecycle_revision_conflict"] * (workers - 1)
    assert len(ledger(e)) == 1
    assert immutable(e) == before


def test_full_publication_quota_allows_deprecation_and_persistent_replay(tmp_path):
    with opened(tmp_path) as e:
        seed(e)
        tid = e.tid
        for index in range(5):
            if index == 0:
                target, first = tid, 2
            else:
                created, replayed = e.service.create(
                    e.principal, e.cid, TypeDraft(name="Synthetic", specific_fields=[]),
                    f"quota-type-key-{index:08d}")
                assert not replayed
                target, first = created.type_id, 1
            for number in range(first, 21):
                pub(e, target, key=f"quota-publish-{index:02d}-{number:02d}")
        assert rows(e, "SELECT COUNT(*) FROM document_type_versions") == [(100,)]
        assert rows(e, "SELECT COUNT(*) FROM document_type_versions WHERE type_id=?", (tid,)) == [(20,)]
        fails(429, "type_version_quota_exceeded", lambda: pub(e, key="over-quota-publication"))
        before = immutable(e)
        receipt, replayed = deprecate(e)
        assert not replayed and immutable(e) == before
        assert rows(e, "SELECT COUNT(*) FROM document_type_versions") == [(100,)]
        e.service.update(e.principal, tid, TypeDraft(name="Edited", specific_fields=[]), expected_revision=1)
        e.contexts.update(e.principal, e.cid, ContextCreate(name="Edited"), expected_revision=1)
        saved = ledger(e)
        publication = e.publication
    with opened(tmp_path) as restarted:
        restarted.principal, restarted.tid = who(), tid
        assert deprecate(restarted) == (receipt, True)
        assert ledger(restarted) == saved
        assert rows(restarted, "SELECT COUNT(*) FROM document_type_versions") == [(100,)]
        historical, replayed = restarted.service.publish(
            restarted.principal, tid, expected_revision=1, expected_context_revision=1, key=PUBLISH_KEY)
        assert replayed and historical == publication


def test_binding_uses_existing_connection_transaction_and_read_scope_only(published_type, monkeypatch):
    e = published_type
    real_get_conn = e.store._get_conn
    calls = []

    def counted():
        calls.append(1)
        return real_get_conn()

    def forbidden(*args, **kwargs):
        raise AssertionError("Binding must not use public reads or acquire a second read lock")

    monkeypatch.setattr(e.store, "_get_conn", counted)
    monkeypatch.setattr(e.service, "get_version", forbidden)
    monkeypatch.setattr(e.service, "_read", forbidden)
    before = immutable(e)
    calls.clear()
    record = binding(e, principal=who(scopes={"contexts:read"}))
    assert len(calls) == 1
    assert record.lifecycle.state == "published"
    assert record.schema_sha256 == e.publication.schema_sha256
    assert immutable(e) == before
    assert ledger(e) == []
    with e.store._lock:
        conn = real_get_conn()
        assert not conn.in_transaction
        fails(503, "storage_unavailable", lambda: e.service._binding_version(
            conn, e.principal, e.tid, 1, e.publication.schema_sha256))


def test_binding_rejects_hash_mismatch_deprecation_and_missing_scope(published_type):
    e = published_type
    fails(409, "type_schema_mismatch", lambda: binding(e, sha="0" * 64))
    fails(403, None, lambda: e.service._write(lambda conn: e.service._binding_version(
        conn, who(scopes={"contexts:publish"}), PRIVATE, True, PRIVATE)))
    deprecate(e)
    fails(409, "type_schema_mismatch", lambda: binding(e, sha="0" * 64))
    fails(409, "type_version_deprecated", lambda: binding(e))


@pytest.mark.parametrize("sha", ["", "a" * 63, "a" * 65, "A" * 64, "g" * 64, True, 1])
def test_binding_expected_hash_is_validated(published_type, sha):
    fails(422, "invalid_type_query", lambda: binding(published_type, sha=sha))


@pytest.mark.parametrize("binding_first", [True, False])
def test_synthetic_binding_and_deprecation_have_explicit_commit_order(published_type, binding_first):
    e = published_type
    write(e, "CREATE TABLE synthetic_bindings (type_id TEXT NOT NULL, version INTEGER NOT NULL, "
             "schema_sha256 TEXT NOT NULL)")
    start = Barrier(2)
    first_committed = Event()
    order = []
    before = immutable(e)

    def bind_action():
        def work(conn):
            record = e.service._binding_version(conn, e.principal, e.tid, 1,
                                                e.publication.schema_sha256)
            conn.execute("INSERT INTO synthetic_bindings VALUES (?,?,?)",
                         (record.type_id, record.version, record.schema_sha256))
            return record
        return e.service._write(work)

    def bind_worker():
        start.wait(timeout=DEADLINE)
        if not binding_first:
            assert first_committed.wait(timeout=DEADLINE)
        try:
            try:
                result = bind_action()
            except ApplicationError as error:
                assert not binding_first
                assert error.code == "type_version_deprecated"
                assert getattr(error, "status_code", getattr(error, "status", None)) == 409
                order.append(error.code)
                return error.code
            order.append("binding")
            return result
        finally:
            if binding_first:
                first_committed.set()

    def deprecate_worker():
        start.wait(timeout=DEADLINE)
        if binding_first:
            assert first_committed.wait(timeout=DEADLINE)
        try:
            result = deprecate(e)
            order.append("deprecate")
            return result
        finally:
            if not binding_first:
                first_committed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        binding_future = pool.submit(bind_worker)
        deprecation_future = pool.submit(deprecate_worker)
        binding_result = binding_future.result(timeout=DEADLINE)
        receipt, replayed = deprecation_future.result(timeout=DEADLINE)
    assert all(not thread.is_alive() for thread in pool._threads)
    assert not replayed and receipt.lifecycle.state == "deprecated"
    assert len(ledger(e)) == 1
    assert immutable(e) == before
    stored = rows(e, "SELECT type_id, version, schema_sha256 FROM synthetic_bindings")
    if binding_first:
        assert order == ["binding", "deprecate"]
        assert binding_result.lifecycle.state == "published"
        assert stored == [(e.tid, 1, e.publication.schema_sha256)]
        # The already committed association remains after deprecation.
        fails(409, "type_version_deprecated", lambda: binding(e))
        assert rows(e, "SELECT type_id, version, schema_sha256 FROM synthetic_bindings") == stored
    else:
        assert order == ["deprecate", "type_version_deprecated"]
        assert binding_result == "type_version_deprecated"
        assert stored == []
