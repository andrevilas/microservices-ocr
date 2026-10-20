"""Real-storage coverage for authorized synchronous document queries."""
from datetime import datetime, timedelta, timezone
import hashlib
import os

import pytest

from app.application import ApplicationError, JobApplication
from app.metadata_models import snapshot_bytes
from app.services import document_content_projection as content
from app.services import document_projection as projection
from app.services.catalog_runtime import CatalogServices
from app.services.context_service import ContextService
from app.services.context_template_service import ContextTemplateService
from app.services.document_queries import DocumentQueries
from app.services.document_runtime import DocumentProvider
from test_document_metadata import (
    QUOTE, body, deprecate, env, failure, idle, persisted, save, who,
)


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")
READ = {"jobs:read", "artifacts:read", "metadata:read"}
OPERATIONS = (
    "get_current", "get_revision", "list_revisions", "list_fields",
    "list_values", "list_evidence", "get_string", "get_schema",
)
SNAPSHOT_OPERATIONS = tuple(name for name in OPERATIONS if name != "list_revisions")
PAGED = {"list_revisions", "list_fields", "list_values", "list_evidence"}
MESSAGE = "Document metadata query failed."


@pytest.fixture
def saved(env):
    receipt, _ = save(env)
    return env, DocumentQueries(env.service), receipt, persisted(env, receipt)


def call(e, queries, receipt, operation, principal=None, **overrides):
    arguments = dict(principal=e.owner if principal is None else principal,
                     job_id=e.job.job_id)
    if operation not in {"get_current", "list_revisions"}:
        arguments.update(revision_id=receipt.revision_id, sha256=receipt.sha256)
    if operation in {"list_fields", "list_values", "list_evidence", "get_string"}:
        arguments["segment_id"] = receipt.segment_ids["main"]
    if operation in {"list_values", "list_evidence", "get_string"}:
        arguments["field_name"] = "title"
    if operation == "get_string":
        arguments["part"] = "raw"
    if operation == "get_schema":
        arguments["schema_sha256"] = e.version.schema_sha256
    arguments.update(overrides)
    return getattr(queries, operation)(**arguments)


def rejected(action, status, code=None):
    error = failure(action, status, code)
    assert error.message == MESSAGE
    return error


def forbidden(*args, **kwargs):
    pytest.fail("query reached a forbidden dependency")


def unlocked(e, *, leased):
    assert e.jobs._lock.acquire(blocking=False)
    try:
        assert not e.jobs._conn.in_transaction
    finally:
        e.jobs._lock.release()
    assert e.jobs._artifact_leases == ({e.job.job_id: 1} if leased else {})


def database(e):
    with e.jobs._lock:
        return tuple(e.jobs._conn.iterdump())


def changed_hash(value):
    return ("0" if value[0] != "0" else "1") + value[1:]


def test_all_views_equal_verified_pure_projections(saved):
    e, q, receipt, snapshot = saved
    owner, digest, sid = e.owner.subject, receipt.sha256, receipt.segment_ids["main"]
    expected_view = projection.metadata_view(owner, snapshot, digest)
    assert q.get_current(e.owner, e.job.job_id) == expected_view
    assert call(e, q, receipt, "get_revision") == expected_view
    assert call(e, q, receipt, "list_revisions") == projection.revision_page(
        owner, e.job.job_id, e.service.list_revisions(e.owner, e.job.job_id))
    assert call(e, q, receipt, "list_fields") == projection.field_page(
        owner, snapshot, digest, sid)
    assert call(e, q, receipt, "list_values") == content.value_page(
        owner, snapshot, digest, sid, "title")
    assert call(e, q, receipt, "list_evidence") == content.evidence_page(
        owner, snapshot, digest, sid, "title")
    for part in ("raw", "normalized"):
        assert call(e, q, receipt, "get_string", part=part, offset=1, max_chars=3) == (
            content.string_chunk(owner, snapshot, digest, sid, "title",
                                 part=part, offset=1, max_chars=3))
    chunks, offset = [], 0
    while True:
        chunk = call(e, q, receipt, "get_string", offset=offset, max_chars=2)
        assert chunk == content.string_chunk(owner, snapshot, digest, sid, "title",
                                              part="raw", offset=offset, max_chars=2)
        chunks.append(chunk.text)
        if chunk.next_offset is None:
            break
        offset = chunk.next_offset
    assert "".join(chunks) == QUOTE
    chunks, offset = [], 0
    while True:
        chunk = call(e, q, receipt, "get_schema", offset=offset, max_chars=97)
        assert chunk == content.schema_chunk(owner, snapshot, digest,
                                             e.version.schema_sha256,
                                             offset=offset, max_chars=97)
        chunks.append(chunk.text)
        if chunk.next_offset is None:
            break
        offset = chunk.next_offset
    encoded_schema = "".join(chunks).encode("utf-8")
    assert hashlib.sha256(encoded_schema).hexdigest() == e.version.schema_sha256
    assert snapshot_bytes(snapshot) == snapshot_bytes(persisted(e, receipt))
    idle(e, 1)


def test_empty_metadata_and_specific_missing_revision(env):
    q = DocumentQueries(env.service)
    rejected(lambda: q.get_current(env.owner, env.job.job_id), 404, "metadata_not_found")
    page = q.list_revisions(env.owner, env.job.job_id)
    assert page.revisions == [] and page.next_cursor is None
    rejected(lambda: q.get_revision(env.owner, env.job.job_id, "f" * 32,
                                     sha256="b" * 64),
             404, "metadata_revision_not_found")
    idle(env, 0)


def test_two_revisions_paginate_without_reordering_saved_history(saved, monkeypatch):
    e, q, first, _ = saved
    second, _ = save(e, body(e, first), key="document-queries-second-0001")
    rows = e.service.list_revisions(e.owner, e.job.job_id)
    assert [row.revision_id for row in rows] == [first.revision_id, second.revision_id]
    monkeypatch.setattr(e.bundle.files, "read", forbidden)
    monkeypatch.setattr(e.service.canonical, "open", forbidden)
    monkeypatch.setattr(e.types, "get_version", forbidden)
    real = projection.revision_page

    def checked(*args, **kwargs):
        unlocked(e, leased=False)
        return real(*args, **kwargs)

    monkeypatch.setattr(projection, "revision_page", checked)
    before = database(e)
    page = q.list_revisions(who(scopes=READ), e.job.job_id, limit=1)
    assert page == real(e.owner.subject, e.job.job_id, rows, limit=1)
    assert page.next_cursor is not None
    following = q.list_revisions(e.owner, e.job.job_id, limit=1, after=page.next_cursor)
    assert following == real(e.owner.subject, e.job.job_id, rows,
                             limit=1, after=page.next_cursor)
    assert following.next_cursor is None
    assert [row.revision_id for row in page.revisions + following.revisions] == sorted(
        [first.revision_id, second.revision_id])
    assert database(e) == before
    assert e.service.list_revisions(e.owner, e.job.job_id) == rows


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("missing", sorted(READ))
def test_read_scopes_precede_cursor_parsing_and_artifact_reads(saved, monkeypatch,
                                                              operation, missing):
    e, q, receipt, _ = saved
    monkeypatch.setattr(e.bundle.files, "read", forbidden)
    monkeypatch.setattr(projection, "decode_cursor", forbidden)
    arguments = {"after": "!"} if operation in PAGED else {}
    if operation == "get_revision":
        arguments["revision_id"] = "malformed"
    elif operation in {"get_string", "get_schema"}:
        arguments["offset"] = -1
    rejected(lambda: call(e, q, receipt, operation,
                          principal=who(scopes=READ - {missing}), **arguments),
             403, "insufficient_scope")
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("principal", [who("user:2"), who("user:3", admin=True),
                                        who("service:" + "f" * 32, admin=True)])
def test_wrong_owner_including_admin_precedes_cursor_and_source(saved, monkeypatch,
                                                               operation, principal):
    e, q, receipt, _ = saved
    monkeypatch.setattr(e.bundle.files, "read", forbidden)
    monkeypatch.setattr(projection, "decode_cursor", forbidden)
    arguments = {"after": "!"} if operation in PAGED else {}
    rejected(lambda: call(e, q, receipt, operation, principal=principal, **arguments),
             404, "job_not_found")
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("deleted", [False, True])
def test_retention_precedes_queries(saved, monkeypatch, operation, deleted):
    e, q, receipt, _ = saved
    if deleted:
        JobApplication(e.jobs).delete_artifacts(e.owner, e.job.job_id)
    else:
        with e.jobs._lock:
            e.job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            e.jobs._persist(e.job)
    monkeypatch.setattr(e.bundle.files, "read", forbidden)
    arguments = {"after": "!"} if operation in PAGED else {}
    rejected(lambda: call(e, q, receipt, operation, **arguments), 410,
             "artifacts_deleted" if deleted else "artifacts_expired")
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("status", ["queued", "running"])
def test_noncompleted_jobs_keep_existing_conflict_authority(saved, monkeypatch, status):
    e, q, receipt, _ = saved
    e.jobs.update(e.job.job_id, status=status)
    expected = failure(lambda: e.bundle.index.get_head(e.owner, e.job.job_id), 409)
    monkeypatch.setattr(e.bundle.files, "read", forbidden)
    for operation in OPERATIONS:
        rejected(lambda operation=operation: call(e, q, receipt, operation),
                 409, expected.code)
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("operation,arguments", [
    ("get_revision", {"job_id": "../job"}),
    ("get_revision", {"revision_id": "bad"}),
    ("get_revision", {"sha256": "A" * 64}),
    ("list_revisions", {"limit": True}),
    ("list_revisions", {"limit": 101}),
    ("list_fields", {"segment_id": "bad"}),
    ("list_fields", {"limit": 0}),
    ("list_fields", {"limit": 51}),
    ("list_values", {"field_name": None}),
    ("list_values", {"limit": "20"}),
    ("list_evidence", {"limit": 51}),
    ("list_values", {"revision_id": None}),
    ("list_evidence", {"sha256": None}),
    ("get_string", {"part": "other"}),
    ("get_string", {"item_index": -1}),
    ("get_string", {"item_index": True}),
    ("get_string", {"offset": -1}),
    ("get_string", {"offset": True}),
    ("get_string", {"max_chars": 1001}),
    ("get_schema", {"schema_sha256": "bad"}),
    ("get_schema", {"offset": -1}),
    ("get_schema", {"max_chars": 0}),
])
def test_invalid_query_coordinates_precede_file_io(saved, monkeypatch, operation, arguments):
    e, q, receipt, _ = saved
    monkeypatch.setattr(e.bundle.files, "read", forbidden)
    rejected(lambda: call(e, q, receipt, operation, **arguments),
             422, "invalid_metadata_query")
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("operation", sorted(PAGED))
def test_malformed_cursor_precedes_file_io(saved, monkeypatch, operation):
    e, q, receipt, _ = saved
    monkeypatch.setattr(e.bundle.files, "read", forbidden)
    rejected(lambda: call(e, q, receipt, operation, after="!"),
             422, "invalid_metadata_cursor")
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("coordinate", ["owner", "job_id", "view", "revision_id",
                                        "sha256", "segment_id", "field_name"])
def test_foreign_cursor_scope_precedes_file_io(saved, monkeypatch, coordinate):
    e, q, receipt, _ = saved
    scope = dict(owner=e.owner.subject, job_id=e.job.job_id, view="values",
                 revision_id=receipt.revision_id, sha256=receipt.sha256,
                 segment_id=receipt.segment_ids["main"], field_name="title")
    alternatives = dict(owner="user:2", job_id="f" * 32, view="evidence",
                        revision_id="f" * 32, sha256=changed_hash(receipt.sha256),
                        segment_id="f" * 32, field_name="other")
    scope[coordinate] = alternatives[coordinate]
    cursor = projection.encode_cursor(0, **scope)
    monkeypatch.setattr(e.bundle.files, "read", forbidden)
    rejected(lambda: call(e, q, receipt, "list_values", after=cursor),
             422, "invalid_metadata_cursor")
    assert not e.jobs._artifact_leases


def test_resource_errors_remain_distinct(saved):
    e, q, receipt, _ = saved
    cases = [
        ("get_revision", {"revision_id": "f" * 32}, 404, "metadata_revision_not_found"),
        ("get_revision", {"sha256": changed_hash(receipt.sha256)},
         409, "metadata_revision_conflict"),
        ("list_fields", {"segment_id": "f" * 32}, 404, "metadata_segment_not_found"),
        ("list_values", {"field_name": "missing"}, 404, "metadata_field_not_found"),
        ("list_evidence", {"field_name": "missing"}, 404, "metadata_field_not_found"),
        ("get_string", {"field_name": "missing"}, 404, "metadata_field_not_found"),
        ("get_schema", {"schema_sha256": changed_hash(e.version.schema_sha256)},
         404, "metadata_schema_not_found"),
        ("get_string", {"item_index": 0}, 422, "invalid_metadata_query"),
        ("get_string", {"offset": len(QUOTE) + 1}, 422, "invalid_metadata_query"),
    ]
    for operation, arguments, status, code in cases:
        rejected(lambda: call(e, q, receipt, operation, **arguments), status, code)
        assert not e.jobs._artifact_leases


@pytest.mark.parametrize("operation", SNAPSHOT_OPERATIONS)
def test_one_read_one_lease_no_sql_lock_and_no_mutation(saved, monkeypatch, operation):
    e, q, receipt, _ = saved
    before = database(e)
    read = e.bundle.files.read
    reads, projections = [], []

    def checked_read(*args, **kwargs):
        unlocked(e, leased=True)
        reads.append(kwargs["revision_id"])
        return read(*args, **kwargs)

    module, name = {
        "get_current": (projection, "metadata_view"),
        "get_revision": (projection, "metadata_view"),
        "list_fields": (projection, "field_page"),
        "list_values": (content, "value_page"),
        "list_evidence": (content, "evidence_page"),
        "get_string": (content, "string_chunk"),
        "get_schema": (content, "schema_chunk"),
    }[operation]
    project = getattr(module, name)

    def checked_projection(*args, **kwargs):
        unlocked(e, leased=True)
        result = project(*args, **kwargs)
        unlocked(e, leased=True)
        projections.append(result)
        return result

    monkeypatch.setattr(e.bundle.files, "read", checked_read)
    monkeypatch.setattr(module, name, checked_projection)
    monkeypatch.setattr(e.service.canonical, "open", forbidden)
    monkeypatch.setattr(e.types, "get_version", forbidden)
    result = call(e, q, receipt, operation, principal=who(scopes=READ))
    assert projections == [result] and reads == [receipt.revision_id]
    assert database(e) == before
    idle(e, 1)


def test_current_pins_head_once_and_historical_read_survives_deprecation(saved, monkeypatch):
    e, q, first, snapshot = saved
    read, get_head = e.bundle.files.read, e.bundle.index.get_head
    later, heads = [], []

    def counted_head(*args, **kwargs):
        result = get_head(*args, **kwargs)
        heads.append(result)
        return result

    def advance_head(*args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(e.bundle.files, "read", read)
            later.append(save(e, body(e, first), key="document-query-head-change-0001")[0])
        return read(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(e.bundle.index, "get_head", counted_head)
        patch.setattr(e.bundle.files, "read", advance_head)
        assert q.get_current(e.owner, e.job.job_id) == projection.metadata_view(
            e.owner.subject, snapshot, first.sha256)
    assert len(heads) == len(later) == 1
    deprecate(e)
    monkeypatch.setattr(e.service.canonical, "open", forbidden)
    monkeypatch.setattr(e.types, "get_version", forbidden)
    assert call(e, q, first, "get_revision", principal=who(scopes=READ)) == (
        projection.metadata_view(e.owner.subject, snapshot, first.sha256))
    assert q.get_current(e.owner, e.job.job_id).revision_id == later[0].revision_id
    idle(e, 2)


@pytest.mark.parametrize("operation", SNAPSHOT_OPERATIONS)
@pytest.mark.parametrize("deleted", [False, True])
def test_retention_change_during_io_finishes_admitted_query(saved, monkeypatch,
                                                          operation, deleted):
    e, q, receipt, _ = saved
    expected = call(e, q, receipt, operation)
    read = e.bundle.files.read

    def change_retention(*args, **kwargs):
        unlocked(e, leased=True)
        if deleted:
            JobApplication(e.jobs).delete_artifacts(e.owner, e.job.job_id)
        else:
            with e.jobs._lock:
                e.job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                e.jobs._persist(e.job)
        return read(*args, **kwargs)

    monkeypatch.setattr(e.bundle.files, "read", change_retention)
    assert call(e, q, receipt, operation) == expected
    assert not e.jobs._artifact_leases
    rejected(lambda: call(e, q, receipt, operation), 410,
             "artifacts_deleted" if deleted else "artifacts_expired")


def test_corrupt_snapshot_is_sanitized_and_releases_lease(saved):
    e, q, receipt, _ = saved
    path = e.job.working_dir / "results" / "metadata" / (receipt.revision_id + ".json")
    encoded = path.read_bytes()
    path.write_bytes(b"!" + encoded[1:])
    rejected(lambda: call(e, q, receipt, "get_revision"), 503, "metadata_unavailable")
    assert not e.jobs._artifact_leases and not e.jobs._lock.locked()
    assert len(q.list_revisions(e.owner, e.job.job_id).revisions) == 1


@pytest.mark.parametrize("operation", OPERATIONS)
def test_bad_checkpoint_rejected_before_admission(saved, monkeypatch, operation):
    e, q, receipt, _ = saved
    monkeypatch.setattr(e.bundle.index, "get_head", forbidden)
    monkeypatch.setattr(e.bundle.index, "list_revisions", forbidden)
    rejected(lambda: call(e, q, receipt, operation, checkpoint=42),
             422, "invalid_metadata_query")
    assert not e.jobs._artifact_leases


@pytest.mark.parametrize("stage", ["before_admission", "after_authorization",
                                   "after_read", "after_projection"])
@pytest.mark.parametrize("kind", ["timeout", "keyboard", "exit"])
def test_checkpoint_errors_and_controls_release_lease(saved, monkeypatch, stage, kind):
    e, q, receipt, _ = saved
    state = dict(authorized=False, read=False, projected=False)
    get_head, read, project = e.bundle.index.get_head, e.bundle.files.read, projection.metadata_view
    error = {"timeout": ApplicationError(408, "document_execution_timeout", "sensitive timeout"),
             "keyboard": KeyboardInterrupt("sensitive interruption"),
             "exit": SystemExit("sensitive exit")}[kind]

    def admitted(*args, **kwargs):
        result = get_head(*args, **kwargs)
        state["authorized"] = True
        return result

    def read_snapshot(*args, **kwargs):
        result = read(*args, **kwargs)
        state["read"] = True
        return result

    def projected(*args, **kwargs):
        unlocked(e, leased=True)
        result = project(*args, **kwargs)
        state["projected"] = True
        return result

    def checkpoint():
        unlocked(e, leased=state["read"])
        reached = {
            "before_admission": not state["authorized"],
            "after_authorization": state["authorized"] and not state["read"],
            "after_read": state["read"] and not state["projected"],
            "after_projection": state["projected"],
        }[stage]
        if reached:
            raise error

    monkeypatch.setattr(e.bundle.index, "get_head", admitted)
    monkeypatch.setattr(e.bundle.files, "read", read_snapshot)
    monkeypatch.setattr(projection, "metadata_view", projected)
    if kind == "timeout":
        rejected(lambda: call(e, q, receipt, "get_revision", checkpoint=checkpoint),
                 408, "document_execution_timeout")
    else:
        with pytest.raises(type(error)) as caught:
            call(e, q, receipt, "get_revision", checkpoint=checkpoint)
        assert caught.value is error
    assert not e.jobs._artifact_leases and not e.jobs._lock.locked()
    idle(e, 1)


@pytest.mark.parametrize("after_projection", [False, True])
def test_list_checkpoint_runs_after_sql_and_after_projection(saved, monkeypatch,
                                                           after_projection):
    e, q, receipt, _ = saved
    state = dict(sql=False, projected=False)
    listing, project = e.service.list_revisions, projection.revision_page
    marker = KeyboardInterrupt("list checkpoint")

    def listed(*args, **kwargs):
        result = listing(*args, **kwargs)
        state["sql"] = True
        return result

    def projected(*args, **kwargs):
        result = project(*args, **kwargs)
        state["projected"] = True
        return result

    def checkpoint():
        unlocked(e, leased=False)
        if state["sql"] and (state["projected"] or not after_projection):
            raise marker

    monkeypatch.setattr(e.service, "list_revisions", listed)
    monkeypatch.setattr(projection, "revision_page", projected)
    with pytest.raises(KeyboardInterrupt) as caught:
        call(e, q, receipt, "list_revisions", checkpoint=checkpoint)
    assert caught.value is marker
    assert state["projected"] is after_projection
    idle(e, 1)


@pytest.mark.parametrize("error,status,code", [
    (RuntimeError("private path and field value"), 503, "metadata_unavailable"),
    (ApplicationError(429, "query_budget", "private message"), 429, "query_budget"),
    (ApplicationError(200, "bad_status", "private message"), 503, "metadata_unavailable"),
    (ApplicationError(503, "INVALID", "private message"), 503, "metadata_unavailable"),
    (ApplicationError(503, "x" * 81, "private message"), 503, "metadata_unavailable"),
])
def test_public_error_boundary_discards_sensitive_messages_and_context(saved, monkeypatch,
                                                                     error, status, code):
    e, q, receipt, _ = saved

    def broken(*args, **kwargs):
        try:
            raise ValueError("sensitive original cause")
        except ValueError as cause:
            raise error from cause

    monkeypatch.setattr(projection, "metadata_view", broken)
    rejected(lambda: call(e, q, receipt, "get_revision"), status, code)
    assert not e.jobs._artifact_leases


def test_provider_caches_queries_with_exact_document_and_shared_execution(env):
    e = env
    contexts, templates = ContextService(e.jobs), ContextTemplateService(e.jobs)
    catalog = CatalogServices(contexts=contexts, types=e.types, templates=templates)
    selected = [catalog]
    provider = DocumentProvider(lambda: e.bundle, lambda: selected[0])
    first = provider()
    assert provider() is first
    assert first.queries.document is first.document
    assert first.queries.document.bundle is e.bundle
    assert first.queries.document.bundle.jobs is first.jobs is e.jobs
    assert first.queries.document.bundle.index.jobs is e.jobs
    assert first.queries.document.bundle.storage.store is e.bundle.index
    assert first.queries.document.canonical is first.canonical
    selected[0] = CatalogServices(contexts=contexts, types=e.types, templates=templates)
    second = provider()
    assert second is not first and second.queries is not first.queries
    assert second.queries.document is second.document
    assert second.jobs is first.jobs and second.execution is first.execution
    selected[0] = catalog
    assert provider() is first
    first.queries.document = second.document
    try:
        failure(provider, 503, "metadata_unavailable")
    finally:
        first.queries.document = first.document
    assert provider() is first
    assert first.execution is second.execution
