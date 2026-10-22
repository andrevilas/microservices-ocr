"""Atomicity, real quotas, receipt integrity, and catalog cache publication."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from app.application import ApplicationError, Principal
from app.catalog_models import ContextCreate, TypeDraft
from app.context_templates import TemplateCloneReceipt
from app.services import catalog_runtime
from app.services.storage_service import JobStore
from test_context_template_service import close_store, clone_input, services, snapshot

TABLES = ("document_contexts", "document_types", "context_template_clones")
SENTINEL = "private-atomicity-sentinel"

@pytest.fixture
def seeded(services):
    services.template = next(t for t in services.templates.list(services.owner).items
                             if t.template_id == "generic")
    assert len(services.template.types) == 1
    return services

def clone(s, key):
    return s.templates.clone(s.owner, s.template.template_id, clone_input(s.template), key)

def counts(store):
    rows = snapshot(store)
    return tuple(len(rows.get(table, ())) for table in TABLES)

def sql(store, statement, parameters=()):
    with store._lock, store._get_conn() as conn:
        return conn.execute(statement, parameters).rowcount

def check_error(error, status, code):
    assert getattr(error, "status_code", getattr(error, "status", None)) == status
    assert error.code == code
    assert SENTINEL not in str(error) + repr(vars(error))

def fails(status, code, call):
    with pytest.raises(ApplicationError) as caught:
        call()
    check_error(caught.value, status, code)

def race(s, keys):
    barrier = Barrier(2, timeout=5)
    def worker(key):
        barrier.wait()
        try:
            return clone(s, key)
        except ApplicationError as error:
            return error
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        futures = [pool.submit(worker, key) for key in keys]
        return [future.result(timeout=15) for future in futures]
    finally:
        barrier.abort()
        pool.shutdown(wait=False, cancel_futures=True)

def test_context_quota_race(seeded):
    s = seeded
    body = ContextCreate.model_validate(s.template.context.model_dump())
    for index in range(99):
        s.contexts.create(s.owner, body, f"quota-context-{index:03}")
    assert counts(s.store) == (99, 0, 0)
    results = race(s, ("quota-clone-first", "quota-clone-second"))
    errors = [result for result in results if isinstance(result, ApplicationError)]
    successes = [result for result in results if not isinstance(result, ApplicationError)]
    assert len(errors) == len(successes) == 1
    check_error(errors[0], 429, "context_quota_exceeded")
    assert successes[0][1] is False
    assert counts(s.store) == (100, 1, 1)

def test_same_key_concurrent_clone(seeded):
    results = race(seeded, ("concurrent-same-key", "concurrent-same-key"))
    assert all(isinstance(result, tuple) for result in results)
    assert results[0][0] == results[1][0]
    assert sorted(result[1] for result in results) == [False, True]
    assert counts(seeded.store) == (1, 1, 1)

def test_type_quota_includes_deprecated_versions(seeded):
    s = seeded
    context, _ = s.contexts.create(s.owner, ContextCreate.model_validate(
        s.template.context.model_dump()), "quota-context")
    body = TypeDraft.model_validate(s.template.types[0].body.model_dump())
    types = [s.types.create(s.owner, context.context_id, body, f"quota-type-{i:03}")[0]
             for i in range(100)]
    for deprecated in (False, True):
        if deprecated:
            s.types.publish(s.owner, types[0].type_id, expected_revision=1,
                            expected_context_revision=1, key="publish-key")
            s.types.deprecate(s.owner, types[0].type_id, 1,
                              expected_lifecycle_revision=1, key="deprecate-key")
        before = snapshot(s.store)
        fails(429, "type_quota_exceeded", lambda: clone(s, "quota-clone-retry"))
        assert snapshot(s.store) == before
        assert counts(s.store) == (1, 100, 0)

def test_exact_replay_at_both_full_quotas(seeded):
    receipts = [clone(seeded, f"full-clone-{i:03}")[0] for i in range(100)]
    assert counts(seeded.store) == (100, 100, 100)
    before = snapshot(seeded.store)
    assert clone(seeded, "full-clone-000") == (receipts[0], True)
    assert snapshot(seeded.store) == before

@pytest.mark.parametrize("table", ["document_types", "context_template_clones"])
def test_insert_failure_rolls_back_and_same_key_retries(seeded, table):
    s = seeded
    sql(s.store, f"CREATE TRIGGER reject_clone BEFORE INSERT ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{SENTINEL}'); END")
    before = snapshot(s.store)
    fails(503, "storage_unavailable", lambda: clone(s, "trigger-retry-key"))
    assert snapshot(s.store) == before
    assert counts(s.store) == (0, 0, 0)
    sql(s.store, "DROP TRIGGER reject_clone")
    assert clone(s, "trigger-retry-key")[1] is False
    assert counts(s.store) == (1, 1, 1)

def test_commit_failure_rolls_back_and_same_key_retries(seeded, monkeypatch):
    s = seeded
    before = snapshot(s.store)
    with s.store._lock:
        conn = s.store._get_conn()
    class FailingCommit:
        failures = 0
        rollbacks = 0
        def __getattr__(self, name):
            return getattr(conn, name)
        def commit(self):
            if self.failures == 0:
                self.failures += 1
                raise sqlite3.OperationalError(SENTINEL)
            return conn.commit()
        def rollback(self):
            self.rollbacks += 1
            return conn.rollback()
    wrapped = FailingCommit()
    with monkeypatch.context() as patch:
        patch.setattr(s.store, "_get_conn", lambda: wrapped)
        fails(503, "storage_unavailable", lambda: clone(s, "commit-retry-key"))
        assert snapshot(s.store) == before
        assert wrapped.failures == wrapped.rollbacks == 1
        assert not conn.in_transaction
    assert clone(s, "commit-retry-key")[1] is False
    assert counts(s.store) == (1, 1, 1)

@pytest.mark.parametrize("case", [
    "malformed", "template-id", "template-version", "template-sha",
    "foreign-context", "foreign-type", "wrong-parent", "second-receipt",
])
def test_corrupt_receipt_is_rejected_without_repair(seeded, case):
    s = seeded
    receipt, _ = clone(s, "corruption-original")
    original = receipt.model_dump_json()
    data = json.loads(original)
    if case.startswith("foreign"):
        other_owner = Principal(subject="user:2", user_id=2, scopes=s.owner.scopes)
        other, _ = s.templates.clone(other_owner, s.template.template_id,
                                     clone_input(s.template), "corruption-foreign")
        if case == "foreign-context":
            data["context_id"] = other.context_id
        else:
            data["types"] = json.loads(other.model_dump_json())["types"]
    elif case in ("wrong-parent", "second-receipt"):
        other, _ = clone(s, "corruption-second")
        second = json.loads(other.model_dump_json())
        if case == "wrong-parent":
            data["types"] = second["types"]
        else:
            data = second
    elif case == "template-id":
        data["template_id"] = "procurement-notice"
    elif case == "template-version":
        data["template_version"] += 1
    elif case == "template-sha":
        data["template_sha256"] = ("1" if data["template_sha256"] != "1" * 64 else "2") * 64
    raw = "{" + SENTINEL if case == "malformed" else json.dumps(data)
    if case != "malformed":
        TemplateCloneReceipt.model_validate_json(raw)
    assert sql(s.store, "UPDATE context_template_clones SET receipt=? WHERE owner=? AND receipt=?",
               (raw, s.owner.subject, original)) == 1
    before = snapshot(s.store)
    fails(503, "storage_unavailable", lambda: clone(s, "corruption-original"))
    assert snapshot(s.store) == before

def test_provider_cache_publication_switch_and_recovery(seeded, tmp_path, monkeypatch):
    stores = [JobStore(tmp_path / "provider-a"), JobStore(tmp_path / "provider-b")]
    application = SimpleNamespace(store=stores[0])
    events, fault = [], [False]
    try:
        for name, attribute in (("contexts", "ContextService"), ("types", "TypeService"),
                                ("templates", "ContextTemplateService")):
            real = getattr(catalog_runtime, attribute)
            def factory(store, real=real, name=name):
                events.append((store, name))
                if name == "templates" and store is stores[1] and fault[0]:
                    raise RuntimeError(SENTINEL)
                return real(store)
            monkeypatch.setattr(catalog_runtime, attribute, factory)
        provider = catalog_runtime.CatalogProvider(lambda: application)
        assert events == []
        first = provider()
        assert provider() is first
        assert events == [(stores[0], name) for name in ("contexts", "types", "templates")]
        args = (seeded.owner, seeded.template.template_id, clone_input(seeded.template), "provider-clone")
        first_receipt, replay = first.templates.clone(*args)
        assert replay is False
        saved = snapshot(stores[0])
        application.store, fault[0] = stores[1], True
        with pytest.raises(RuntimeError, match=SENTINEL):
            provider()
        assert provider._services is first and provider._store is stores[0]
        assert snapshot(stores[0]) == saved
        assert counts(stores[1]) == (0, 0, 0)
        fault[0] = False
        second = provider()
        assert second is not first and provider() is second
        assert all(getattr(second, name) is not getattr(first, name)
                   for name in ("contexts", "types", "templates"))
        assert events == ([(stores[0], name) for name in ("contexts", "types", "templates")]
                          + [(stores[1], name) for name in ("contexts", "types", "templates")] * 2)
        second_receipt, replay = second.templates.clone(*args)
        assert replay is False and second_receipt.context_id != first_receipt.context_id
        assert snapshot(stores[0]) == saved
        assert counts(stores[0]) == counts(stores[1]) == (1, 1, 1)
    finally:
        for store in stores:
            close_store(store)
