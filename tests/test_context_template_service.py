"""Focused template service contracts; synthetic data and real local services only."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.application import ApplicationError, Principal
from app.catalog_models import ContextCreate, TypeDraft
from app.context_templates import TemplateCloneInput, TemplateCloneReceipt
from app.services.context_service import ContextService
from app.services.context_template_service import ContextTemplateService
from app.services.storage_service import JobStore
from app.services.type_service import TypeService

SCOPES = frozenset({"contexts:read", "contexts:write", "contexts:publish"})
TEMPLATE_IDS = ("generic", "real-estate-deed", "procurement-notice")


def open_services(path):
    store = JobStore(path)
    contexts = ContextService(store)
    types = TypeService(store)
    templates = ContextTemplateService(store)
    owner = Principal(subject="user:1", user_id=1, scopes=SCOPES)
    return SimpleNamespace(store=store, contexts=contexts, types=types,
                           templates=templates, owner=owner)


def close_store(store):
    with store._lock:
        if store._conn is not None:
            store._conn.close()
            store._conn = None


@pytest.fixture
def services(tmp_path):
    env = open_services(tmp_path)
    try:
        yield env
    finally:
        close_store(env.store)


def snapshot(store):
    with store._lock:
        conn = store._get_conn()
        names = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name: tuple(sorted((tuple(row) for row in conn.execute(
            'SELECT * FROM "' + name.replace('"', '""') + '"')), key=repr))
                for name in names}


def clone_input(template):
    return TemplateCloneInput(expected_version=template.template_version,
                              expected_sha256=template.template_sha256)


def assert_error(status, call):
    with pytest.raises(ApplicationError) as caught:
        call()
    error = caught.value
    assert getattr(error, "status_code", getattr(error, "status", None)) == status


def utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    assert parsed.utcoffset() == timedelta(0)
    return parsed.astimezone(timezone.utc)


def receipt_ids(receipt):
    ids = {receipt.context_id, *(item.type_id for item in receipt.types)}
    assert len(ids) == 1 + len(receipt.types)
    return ids


def test_catalog_clones_and_replays(services):
    s = services
    before = snapshot(s.store)
    catalog = s.templates.list(s.owner)
    assert tuple(item.template_id for item in catalog.items) == TEMPLATE_IDS
    assert snapshot(s.store) == before
    for table in ("document_contexts", "document_types", "context_template_clones"):
        assert before[table] == ()
    all_ids = set()
    for template in catalog.items:
        body = clone_input(template)
        key = "clone-" + template.template_id
        receipt, replay = s.templates.clone(s.owner, template.template_id, body, key)
        assert replay is False
        assert (receipt.template_id, receipt.template_version, receipt.template_sha256) == (
            template.template_id, template.template_version, template.template_sha256)
        assert receipt.context_revision == 1
        assert TemplateCloneReceipt.model_validate_json(receipt.model_dump_json()) == receipt
        context = s.contexts.get(s.owner, receipt.context_id)
        assert context.body == template.context
        assert context.revision == 1
        assert utc(context.created_at) == utc(receipt.created_at)
        assert [item.type_key for item in receipt.types] == [item.type_key for item in template.types]
        for cloned, definition in zip(receipt.types, template.types):
            typ = s.types.get(s.owner, cloned.type_id)
            assert typ.body == definition.body
            assert typ.context_id == receipt.context_id
            assert typ.revision == cloned.revision == 1
            assert utc(typ.created_at) == utc(receipt.created_at)
            assert s.types.list_versions(s.owner, cloned.type_id).items == []
        saved = snapshot(s.store)
        assert s.templates.clone(s.owner, template.template_id, body, key) == (receipt, True)
        assert snapshot(s.store) == saved
        fresh, replay = s.templates.clone(s.owner, template.template_id, body, key + "-fresh")
        assert replay is False
        for result in (receipt, fresh):
            ids = receipt_ids(result)
            assert ids.isdisjoint(all_ids)
            all_ids.update(ids)
    assert s.templates.list(s.owner) == catalog


@pytest.mark.parametrize("scopes", [frozenset({"contexts:read"}), frozenset({"contexts:write"})])
def test_scope_checks_precede_validation(services, scopes):
    s = services
    principal = Principal(subject="user:2", user_id=2, scopes=scopes)
    before = snapshot(s.store)
    assert_error(403, lambda: s.templates.clone(principal, "INVALID ID", {"owner": True}, ""))
    if "contexts:read" not in scopes:
        assert_error(403, lambda: s.templates.list(principal))
    assert snapshot(s.store) == before


def test_service_can_clone_but_cannot_publish(services):
    s = services
    principal = Principal(subject="service:synthetic", kind="service",
                          scopes=frozenset({"contexts:read", "contexts:write"}))
    template = s.templates.list(principal).items[0]
    receipt, replay = s.templates.clone(principal, template.template_id, clone_input(template), "shared-key")
    assert replay is False
    before = snapshot(s.store)
    assert_error(403, lambda: s.types.publish(principal, receipt.types[0].type_id,
                 expected_revision=1, expected_context_revision=1, key="publish"))
    assert snapshot(s.store) == before


@pytest.mark.parametrize("admin", [False, True])
def test_owner_and_admin_clones_are_owner_scoped(services, admin):
    s = services
    template = s.templates.list(s.owner).items[0]
    body = clone_input(template)
    original, _ = s.templates.clone(s.owner, template.template_id, body, "shared-key")
    other = Principal(subject="user:2", user_id=2, scopes=SCOPES, is_admin=admin)
    own, replay = s.templates.clone(other, template.template_id, body, "shared-key")
    assert replay is False
    assert receipt_ids(own).isdisjoint(receipt_ids(original))
    assert s.contexts.get(other, own.context_id).body == template.context
    assert s.types.get(other, own.types[0].type_id).body == template.types[0].body
    assert_error(404, lambda: s.contexts.get(other, original.context_id))
    assert_error(404, lambda: s.types.get(other, original.types[0].type_id))
    assert s.templates.clone(other, template.template_id, body, "shared-key") == (own, True)


@pytest.mark.parametrize("case,status", [
    ("owner", 422), ("bool-version", 422), ("hash", 422), ("string-version", 422),
    ("key", 422), ("id", 422), ("unknown", 404), ("version-mismatch", 409),
    ("hash-mismatch", 409), ("changed-body", 409),
])
def test_rejected_requests_do_not_write(services, case, status):
    s = services
    template = s.templates.list(s.owner).items[0]
    body = clone_input(template)
    s.templates.clone(s.owner, template.template_id, body, "used-key")
    raw = body.model_dump(mode="python")
    changes = {"owner": {"owner": "user:999"}, "bool-version": {"expected_version": True},
               "hash": {"expected_sha256": "bad"}, "string-version": {"expected_version": "1"},
               "version-mismatch": {"expected_version": body.expected_version + 1},
               "hash-mismatch": {"expected_sha256": "0" * 64},
               "changed-body": {"expected_version": body.expected_version + 1}}
    raw.update(changes.get(case, {}))
    submitted = TemplateCloneInput.model_construct(**raw) if case == "bool-version" else raw
    template_id = {"id": "INVALID ID", "unknown": "unknown-template"}.get(case, template.template_id)
    key = "" if case == "key" else "used-key" if case == "changed-body" else "unused-key"
    before = snapshot(s.store)
    assert_error(status, lambda: s.templates.clone(s.owner, template_id, submitted, key))
    assert snapshot(s.store) == before


def test_public_create_keys_have_separate_namespaces(services):
    s = services
    template = s.templates.list(s.owner).items[0]
    body, key = clone_input(template), "same-public-key"
    receipt, _ = s.templates.clone(s.owner, template.template_id, body, key)
    context, replay = s.contexts.create(s.owner, ContextCreate.model_validate(template.context.model_dump()), key)
    assert replay is False
    typ, replay = s.types.create(s.owner, context.context_id,
                                TypeDraft.model_validate(template.types[0].body.model_dump()), key)
    assert replay is False
    assert len(receipt_ids(receipt) | {context.context_id, typ.type_id}) == 4
    assert s.templates.clone(s.owner, template.template_id, body, key) == (receipt, True)


def test_historical_replay_survives_edits_lifecycle_and_reopen(services, tmp_path, monkeypatch):
    s = services
    template = s.templates.list(s.owner).items[0]
    body = clone_input(template)
    receipt, _ = s.templates.clone(s.owner, template.template_id, body, "historical")
    context_body = ContextCreate.model_validate({**template.context.model_dump(), "name": "Synthetic edited context"})
    type_body = TypeDraft.model_validate({**template.types[0].body.model_dump(), "name": "Synthetic edited type"})
    s.contexts.update(s.owner, receipt.context_id, context_body, expected_revision=1)
    type_id = receipt.types[0].type_id
    s.types.update(s.owner, type_id, type_body, expected_revision=1)
    s.types.publish(s.owner, type_id, expected_revision=2, expected_context_revision=2, key="publish")
    s.types.deprecate(s.owner, type_id, 1, expected_lifecycle_revision=1, key="deprecate")
    close_store(s.store)
    s.__dict__.update(open_services(tmp_path).__dict__)
    def unavailable_template(*args, **kwargs):
        raise AssertionError("Historical replay must not consult the current template")
    monkeypatch.setattr("app.services.context_template_service.get_context_template", unavailable_template)
    before = snapshot(s.store)
    assert s.templates.clone(s.owner, template.template_id, body, "historical") == (receipt, True)
    assert snapshot(s.store) == before
    context, typ = s.contexts.get(s.owner, receipt.context_id), s.types.get(s.owner, type_id)
    assert context.body == context_body and context.revision == 2
    assert typ.body == type_body and typ.revision == 2
    assert utc(context.created_at) == utc(typ.created_at) == utc(receipt.created_at)
    assert TemplateCloneReceipt.model_validate_json(receipt.model_dump_json()) == receipt
