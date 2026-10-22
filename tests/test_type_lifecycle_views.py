"""L1a: additive lifecycle views over immutable synthetic publications. No deprecation action."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import warnings

import pytest
from pydantic import ValidationError

from app.application import ApplicationError, JobApplication, Principal
from app.catalog_models import (TypeInput, TypeRecord, TypeSummary, TypeVersionSummary,
                                TypeVersionViewSummary, VersionLifecycle)
from app.document_models import ContextCreate, TypeDraft
from app.services.context_service import ContextService
from app.services.storage_service import JobStore
from app.services.type_service import TypeService

OWNER = Principal(subject="user:1", user_id=1,
                  scopes=frozenset({"contexts:read", "contexts:write", "contexts:publish"}))
PUBLISH_KEY = "publish-key-00000001"
CHANGED_AT = "2026-10-16T18:19:20.123456+00:00"
NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)


@contextmanager
def opened(root):
    store = JobStore(root)
    try:
        contexts = ContextService(store)
        yield store, contexts, TypeService(store)
    finally:
        with store._lock:
            if store._conn is not None:
                store._conn.close()


def seeded(contexts, service):
    cid = contexts.create(OWNER, ContextCreate(name="Ctx"), "ctx-key-00000001")[0].context_id
    tid = service.create(OWNER, cid, TypeDraft(name="Synthetic", description="synthetic", specific_fields=[]),
                         "type-key-00000001")[0].type_id
    summary, replayed = publish(service, tid)
    assert not replayed
    return cid, tid, summary


def publish(service, tid):
    return service.publish(OWNER, tid, expected_revision=1, expected_context_revision=1, key=PUBLISH_KEY)


def stored_row(store, tid, version=1):
    with store._lock:
        return tuple(store._get_conn().execute(
            "SELECT * FROM document_type_versions WHERE owner=? AND type_id=? AND version=?",
            (OWNER.subject, tid, version)).fetchone())


def insert_metadata(store, tid, *, owner=OWNER.subject, changed_at=CHANGED_AT):
    with store._lock, store._get_conn() as conn:
        conn.execute("INSERT INTO document_type_deprecations "
                     "(owner,type_id,version,state,lifecycle_revision,changed_at,actor,key_hash,request_hash) "
                     "VALUES (?,?,1,'deprecated',2,?,?,?,?)",
                     (owner, tid, changed_at, owner, "synthetic-key-hash", "synthetic-request-hash"))


def assert_view(view, receipt, state="published", changed_at=None):
    assert view.model_dump(exclude={"lifecycle", "schema_document"}) == receipt.model_dump()
    assert view.lifecycle.model_dump() == {
        "state": state, "revision": 1 if state == "published" else 2,
        "changed_at": receipt.created_at if changed_at is None else datetime.fromisoformat(changed_at)}


def assert_error(call, status, code):
    with pytest.raises(ApplicationError) as error:
        call()
    assert error.value.status == status and error.value.code == code
    return error.value


def test_draft_and_lifecycle_dto_contracts():
    common = dict(type_id="a" * 32, context_id="b" * 32, revision=1, created_at=NOW, updated_at=NOW)
    for model, extra in ((TypeRecord, {"body": TypeDraft(name="Synthetic")}),
                         (TypeSummary, {"name": "Synthetic"})):
        record = model(**common, **extra)
        assert record.state == "draft"
        with pytest.raises(ValidationError):
            model(**common, **extra, state="published")
        with pytest.raises(ValidationError):
            record.state = "draft"
    for model in (TypeDraft, TypeInput):
        with pytest.raises(ValidationError):
            model(name="Synthetic", state="draft")
    valid = dict(state="published", revision=1, changed_at=NOW)
    lifecycle = VersionLifecycle(**valid)
    with pytest.raises(ValidationError):
        lifecycle.state = "deprecated"
    for revision in (True, "1", 1.0, 0, 2**31):
        with pytest.raises(ValidationError):
            VersionLifecycle(**{**valid, "revision": revision})
    assert VersionLifecycle(**{**valid, "revision": 2**31 - 1})
    for changed_at in (NOW.replace(tzinfo=None), NOW.astimezone(timezone(timedelta(hours=1)))):
        with pytest.raises(ValidationError):
            VersionLifecycle(**{**valid, "changed_at": changed_at})
    with pytest.raises(ValidationError) as error:
        VersionLifecycle(**valid, secret="private-lifecycle-marker")
    assert "private-lifecycle-marker" not in str(error.value)
    assert set(TypeVersionSummary.model_fields) == {
        "type_id", "context_id", "version", "draft_revision", "context_revision", "type_name",
        "context_name", "schema_version", "schema_sha256", "created_at"}


def test_publication_restart_and_additive_old_database_migration(tmp_path):
    root = tmp_path / "old"
    with opened(root) as (store, contexts, service):
        cid, tid, receipt = seeded(contexts, service)
        before = stored_row(store, tid)
        record = service.get_version(OWNER, tid, 1)
        canonical = json.dumps(record.schema_document, sort_keys=True, separators=(",", ":"),
                               ensure_ascii=False, allow_nan=False).encode("utf-8")
        assert hashlib.sha256(canonical).hexdigest() == receipt.schema_sha256
        assert canonical in before
        assert "lifecycle" not in receipt.model_dump()
        assert "lifecycle" not in record.schema_document
        assert "state" not in service.create(OWNER, cid, TypeDraft(name="Another"),
                                             "another-type-key")[0].model_dump()
        assert_view(record, receipt)
        contexts.update(OWNER, cid, ContextCreate(name="Edited context"), expected_revision=1)
        service.update(OWNER, tid, TypeDraft(name="Edited draft"), expected_revision=1)
        with store._lock, store._get_conn() as conn:
            conn.execute("DROP TABLE document_type_deprecations")
    with opened(root) as (store, contexts, service):
        assert stored_row(store, tid) == before
        with store._lock:
            assert store._get_conn().execute("SELECT COUNT(*) FROM document_type_deprecations").fetchone()[0] == 0
            trace = []
            store._get_conn().set_trace_callback(trace.append)
        try:
            TypeService(store)
        finally:
            with store._lock:
                store._get_conn().set_trace_callback(None)
        assert not any(sql.lstrip().upper().startswith(("UPDATE ", "INSERT ", "ALTER ")) for sql in trace)
        current = service.get_version(OWNER, tid, 1)
        assert current == record
        assert_view(current, receipt)
        page = service.list_versions(OWNER, tid)
        assert len(page.items) == 1 and page.next_cursor is None
        assert_view(page.items[0], receipt)
        assert publish(service, tid) == (receipt, True)
        assert stored_row(store, tid) == before


def test_synthetic_deprecation_view_survives_restart_without_changing_snapshots(tmp_path):
    root = tmp_path / "deprecated"
    with opened(root) as (store, contexts, service):
        cid, tid, receipt = seeded(contexts, service)
        before = stored_row(store, tid)
        record = service.get_version(OWNER, tid, 1)
        second, replayed = service.publish(OWNER, tid, expected_revision=1, expected_context_revision=1,
                                           key="publish-second-key")
        assert second.version == 2 and not replayed
        second_before = stored_row(store, tid, 2)
        insert_metadata(store, tid)
        contexts.update(OWNER, cid, ContextCreate(name="Later context"), expected_revision=1)
        service.update(OWNER, tid, TypeDraft(name="Later draft"), expected_revision=1)
    with opened(root) as (store, contexts, service):
        current = service.get_version(OWNER, tid, 1)
        assert_view(current, receipt, "deprecated", CHANGED_AT)
        assert current.model_dump(exclude={"lifecycle"}) == record.model_dump(exclude={"lifecycle"})
        assert stored_row(store, tid) == before
        assert stored_row(store, tid, 2) == second_before
        assert publish(service, tid) == (receipt, True)
        with store._lock:
            conn = store._get_conn()
            columns = {row[1] for row in conn.execute("PRAGMA table_info(document_type_deprecations)")}
            metadata = conn.execute("SELECT changed_at FROM document_type_deprecations").fetchall()
            trace = []
            conn.set_trace_callback(trace.append)
        try:
            first = service.list_versions(OWNER, tid, limit=1)
            last = service.list_versions(OWNER, tid, limit=1, after=first.next_cursor)
        finally:
            with store._lock:
                store._get_conn().set_trace_callback(None)
        assert columns == {"owner", "type_id", "version", "state", "lifecycle_revision", "changed_at",
                           "actor", "key_hash", "request_hash"}
        assert [row[0] for row in metadata] == [CHANGED_AT]
        assert first.next_cursor == 1 and last.next_cursor is None
        assert len(first.items) == len(last.items) == 1
        assert_view(first.items[0], receipt, "deprecated", CHANGED_AT)
        assert_view(last.items[0], second)
        for item in first.items + last.items:
            assert isinstance(item, TypeVersionViewSummary)
            assert not {"schema", "schema_document", "canonical"} & item.model_dump().keys()
        assert not any("canonical" in sql.lower() for sql in trace)
        assert_view(service.get_version(OWNER, tid, 2), second)


@pytest.mark.parametrize("subject,user_id,is_admin", [("user:2", 2, False), ("admin:3", 3, True)])
def test_foreign_identity_cannot_read_owner_versions(tmp_path, subject, user_id, is_admin):
    with opened(tmp_path / "ownership") as (store, contexts, service):
        _, tid, _ = seeded(contexts, service)
        foreign = Principal(subject=subject, user_id=user_id, scopes=OWNER.scopes, is_admin=is_admin)
        assert_error(lambda: service.get_version(foreign, tid, 1), 404, "type_version_not_found")
        assert_error(lambda: service.list_versions(foreign, tid), 404, "type_not_found")
        no_read = Principal(subject=OWNER.subject, user_id=1, scopes=frozenset({"contexts:publish"}))
        for call in (lambda: service.get_version(no_read, tid, 1), lambda: service.list_versions(no_read, tid)):
            with pytest.raises(ApplicationError) as error:
                call()
            assert error.value.status == 403
        read_only = Principal(subject=OWNER.subject, user_id=1, scopes=frozenset({"contexts:read"}))
        assert service.get_version(read_only, tid, 1)
        assert service.list_versions(read_only, tid).items
        with pytest.raises(ApplicationError) as error:
            service.publish(read_only, tid, expected_revision=1, expected_context_revision=1,
                            key="not-authorized-key")
        assert error.value.status == 403


def test_operational_owner_mismatch_is_sanitized_without_published_fallback(tmp_path):
    with opened(tmp_path / "join") as (store, contexts, service):
        _, tid, receipt = seeded(contexts, service)
        before = stored_row(store, tid)
        insert_metadata(store, tid, owner="user:2", changed_at="private-lifecycle-marker")
        for call in (lambda: service.get_version(OWNER, tid, 1), lambda: service.list_versions(OWNER, tid)):
            error = assert_error(call, 503, "storage_unavailable")
            assert error.message == "Type storage is unavailable."
            assert "private-lifecycle-marker" not in str(error)
            assert "user:2" not in str(error)
        for foreign in (
            Principal(subject="user:2", user_id=2, scopes=OWNER.scopes),
            Principal(subject="admin:3", user_id=3, scopes=OWNER.scopes, is_admin=True),
        ):
            assert_error(lambda: service.get_version(foreign, tid, 1), 404, "type_version_not_found")
            assert_error(lambda: service.list_versions(foreign, tid), 404, "type_not_found")
        no_read = Principal(subject=OWNER.subject, user_id=1, scopes=frozenset({"contexts:publish"}))
        for call in (lambda: service.get_version(no_read, tid, 1), lambda: service.list_versions(no_read, tid)):
            with pytest.raises(ApplicationError) as error:
                call()
            assert error.value.status == 403
            assert "private-lifecycle-marker" not in str(error.value)
        assert stored_row(store, tid) == before
        assert publish(service, tid) == (receipt, True)
        assert stored_row(store, tid) == before


@pytest.mark.parametrize("column,value", [
    ("state", "private-lifecycle-marker"),
    ("lifecycle_revision", 3),
    ("changed_at", "private-lifecycle-marker"),
    ("changed_at", "2026-10-16T18:19:20"),
    ("changed_at", "2026-10-16T18:19:20-03:00"),
])
def test_corrupt_lifecycle_is_sanitized_without_published_fallback(tmp_path, column, value):
    with opened(tmp_path / "corrupt") as (store, contexts, service):
        _, tid, receipt = seeded(contexts, service)
        before = stored_row(store, tid)
        insert_metadata(store, tid)
        assert column in {"state", "lifecycle_revision", "changed_at"}
        with store._lock, store._get_conn() as conn:
            conn.execute("PRAGMA ignore_check_constraints=ON")
            try:
                conn.execute("UPDATE document_type_deprecations SET " + column + "=? WHERE type_id=?",
                             (value, tid))
            finally:
                conn.execute("PRAGMA ignore_check_constraints=OFF")
        for call in (lambda: service.get_version(OWNER, tid, 1), lambda: service.list_versions(OWNER, tid)):
            error = assert_error(call, 503, "storage_unavailable")
            assert error.message == "Type storage is unavailable."
            assert "private-lifecycle-marker" not in str(error)
        assert stored_row(store, tid) == before
        assert publish(service, tid) == (receipt, True)


def test_import_examples_and_openapi_keep_historical_receipt_and_schema_alias(tmp_path):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from app.routes import catalog
        from app.main import create_app
        with opened(tmp_path / "openapi") as (store, contexts, service):
            app = create_app(lambda: JobApplication(store))
            document = app.openapi()
    assert not any("shadow" in str(w.message).lower() and "schema" in str(w.message).lower() for w in caught)
    assert type(catalog._VERSION) is TypeVersionSummary
    assert "lifecycle" not in catalog._VERSION.model_dump()
    assert_view(catalog._EXAMPLES[catalog.TypeVersionRecord], catalog._VERSION)
    assert_view(catalog._EXAMPLES[catalog.TypeVersionPage].items[0], catalog._VERSION)
    schemas = document["components"]["schemas"]
    assert "lifecycle" not in schemas["TypeVersionSummary"]["properties"]
    assert "lifecycle" in schemas["TypeVersionViewSummary"]["required"]
    record = schemas["TypeVersionRecord"]
    assert {"schema", "lifecycle"} <= set(record["required"])
    assert "schema" in record["properties"] and "schema_document" not in record["properties"]
    path = document["paths"]["/api/v1/document-types/{type_id}/versions"]
    receipt = path["post"]["responses"]["201"]["content"]["application/json"]["example"]
    page = path["get"]["responses"]["200"]["content"]["application/json"]["example"]
    assert "lifecycle" not in receipt
    assert page["items"][0]["lifecycle"] == {
        "state": "published", "revision": 1, "changed_at": receipt["created_at"]}
    assert {k: v for k, v in page["items"][0].items() if k != "lifecycle"} == receipt
