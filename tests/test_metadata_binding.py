"""Synthetic C4b preparation and real reserved-write callback coverage.

The harness supplies trusted HEAD snapshots and leases explicitly. It is not a
public DocumentMetadataService or full save/replay/CAS integration test suite.
"""
import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.application import ApplicationError, Principal
from app.document_models import ContextCreate, FieldSpec, TypeDraft
from app.metadata_models import (
    FieldValueRecord, ManualFieldInput, MetadataSnapshot, ProvenanceRecord,
    SaveMetadataInput, SaveMetadataReceipt, SegmentInput, SegmentRecord,
    snapshot_bytes,
)
from app.services.context_service import ContextService
from app.services.document_service import MetadataBindingPreparer
from app.services.metadata_files import MetadataFiles
from app.services.metadata_storage import MetadataStorage
from app.services.metadata_store import MetadataStore
from app.services.storage_service import JobStore
from app.services.type_service import TypeService


CATALOG = {"contexts:read", "contexts:write", "contexts:publish"}
METADATA = {"metadata:read", "metadata:write", "artifacts:read", "jobs:read"}
PRIVATE = "PRIVATE-SYNTHETIC-MARKER"


def who(subject="user:1", scopes=None, admin=False):
    return Principal(subject=subject,
                     user_id=int(subject[5:]) if subject.startswith("user:") else None,
                     scopes=frozenset(CATALOG | METADATA if scopes is None else scopes),
                     auth_source="service" if subject.startswith("service:") else "local_jwt",
                     is_admin=admin)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def failure(operation, code=None, status=None):
    with pytest.raises(ApplicationError) as caught:
        operation()
    error = caught.value
    if code is not None:
        assert error.code == code
    if status is not None:
        assert error.status == status
    assert error.__cause__ is None and error.__context__ is None
    assert PRIVATE not in str(error) + repr(error) + repr(vars(error))
    return error


def publish(e, label):
    draft, _ = e.types.create(
        e.owner, e.cid,
        TypeDraft(name=label, specific_fields=[
            FieldSpec(name="title", type="string", description="Synthetic")
        ]), "type-key-" + label,
    )
    version, _ = e.types.publish(
        e.owner, draft.type_id, expected_revision=1,
        expected_context_revision=1, key="publication-key-" + label,
    )
    return version


@pytest.fixture
def env(tmp_path):
    jobs = JobStore(tmp_path.absolute() / "jobs")
    try:
        contexts = ContextService(jobs)
        types = TypeService(jobs)
        owner = who()
        context, _ = contexts.create(owner, ContextCreate(name="Synthetic"),
                                     "context-key-binding-0001")
        job = jobs.create("synthetic.pdf", b"%PDF-1.4\nsynthetic\n", owner_user_id=1)
        jobs.update(job.job_id, status="completed", progress_percent=100)
        e = SimpleNamespace(jobs=jobs, contexts=contexts, types=types, owner=owner,
                            cid=context.context_id, job=job)
        e.first = publish(e, "first-synthetic")
        e.second = publish(e, "second-synthetic")
        e.store = MetadataStore(jobs)
        e.files = MetadataFiles(jobs.root_dir)
        e.storage = MetadataStorage(e.store, e.files)
        e.preparer = MetadataBindingPreparer(jobs, types)
        yield e
    finally:
        with jobs._lock:
            if jobs._conn is not None:
                jobs._conn.close()


def segment(e, key="main", sid=None, version=None, start=1, end=1, fields=None):
    version = e.first if version is None else version
    return SegmentInput(client_key=key, segment_id=sid, page_start=start, page_end=end,
                        type_id=version.type_id, type_version=version.version,
                        fields=[] if fields is None else fields)


def request(segments, head=None):
    return SaveMetadataInput(
        expected_head=None if head is None else head.revision_id,
        expected_sha256=None if head is None else digest(snapshot_bytes(head)),
        segments=segments,
    )


def prepare(e, body, head=None, principal=None, pages=3):
    return e.preparer.prepare(e.owner if principal is None else principal,
                              e.job.job_id, pages, body, head)


def snapshot(e, plan, parent=None, now=None, principal=None):
    now = datetime.now(timezone.utc) if now is None else now
    principal = e.owner if principal is None else principal
    author = ProvenanceRecord(actor_subject=principal.subject,
                              auth_source=principal.auth_source, recorded_at=now)
    records = []
    for item in plan.segments:
        fields = [FieldValueRecord(
            field_name=value.field_name, raw=value.raw, normalized=value.raw,
            origin=value.origin,
            status="not_found" if value.raw is None else "needs_review",
            evidence=value.evidence, provenance=author,
        ) for value in item.fields]
        records.append(SegmentRecord(
            segment_id=item.segment_id, page_start=item.page_start, page_end=item.page_end,
            context_id=item.context_id, type_id=item.type_id, type_version=item.type_version,
            schema_sha256=item.schema_sha256, fields=fields,
        ))
    return MetadataSnapshot(
        revision_id=uuid4().hex,
        parent_revision_id=None if parent is None else parent.revision_id,
        job_id=e.job.job_id, document_id=e.job.job_id,
        canonical_sha256="a" * 64, input_pdf_sha256="b" * 64,
        output_pdf_sha256="c" * 64, page_count=3,
        parent_job_id=e.job.parent_job_id, created_at=now, author=author,
        schemas=list(plan.schemas), segments=records, state="draft",
    )


def seeded_head(e):
    return snapshot(e, prepare(e, request([segment(e)])))


def deprecate(e, version=None):
    version = e.first if version is None else version
    return e.types.deprecate(
        e.owner, version.type_id, version.version, expected_lifecycle_revision=1,
        key="deprecate-binding-" + version.type_id,
    )[0]


def check(e, plan):
    with e.store._transaction() as conn:
        before = conn.total_changes
        plan.check_locked(conn)
        assert conn.total_changes == before


def test_generated_ids_mapping_deduplication_and_manual_entries(env):
    e = env
    body = request([
        segment(e, "first", fields=[ManualFieldInput(field_name="title", raw="Synthetic")]),
        segment(e, "second", start=2, end=3),
    ])
    plan = prepare(e, body)
    assert set(plan.segment_ids) == {"first", "second"}
    assert len(set(plan.segment_ids.values())) == 2
    assert all(len(value) == 32 and uuid4().__class__(value).hex == value
               for value in plan.segment_ids.values())
    assert [item.segment_id for item in plan.segments] == list(plan.segment_ids.values())
    assert plan.segments[0].fields[0].raw == "Synthetic"
    assert len(plan.schemas) == 1
    assert plan.schemas[0].schema_sha256 == e.first.schema_sha256
    catalog = e.types.get_version(e.owner, e.first.type_id, e.first.version)
    assert type(catalog.schema_document) is dict
    assert plan.schemas[0].schema_document == catalog.schema_document
    assert set(prepare(e, body).segment_ids.values()).isdisjoint(plan.segment_ids.values())
    check(e, plan)


@pytest.mark.parametrize("damage", ["revision", "hash", "missing", "unexpected"])
def test_exact_head_preconditions(env, damage):
    e = env
    head = seeded_head(e)
    body = request([segment(e, sid=head.segments[0].segment_id)], head)
    if damage == "revision":
        body = body.model_copy(update={"expected_head": uuid4().hex})
    elif damage == "hash":
        body = body.model_copy(update={"expected_sha256": "0" * 64})
    elif damage == "missing":
        body = body.model_copy(update={"expected_head": None, "expected_sha256": None})
    else:
        head = None
    failure(lambda: prepare(e, body, head), "metadata_head_conflict", 409)


@pytest.mark.parametrize("damage", ["job", "pages", "corrupt"])
def test_trusted_head_identity_and_revalidation(env, damage):
    e = env
    head = seeded_head(e)
    body = request([segment(e)], head)
    if damage == "job":
        other = uuid4().hex
        head = head.model_copy(update={"job_id": other, "document_id": other})
    elif damage == "pages":
        head = head.model_copy(update={"page_count": 2})
    else:
        head = head.model_copy(update={"page_count": True})
    failure(lambda: prepare(e, body, head), "metadata_unavailable", 503)


def test_only_exact_head_can_supply_existing_ids(env):
    e = env
    older = seeded_head(e)
    removed_id = older.segments[0].segment_id
    current = snapshot(e, prepare(e, request([segment(e)], older), older), older)
    assert current.segments[0].segment_id != removed_id
    for invented in (removed_id, uuid4().hex):
        # Matching client key and schema hash cannot revive an ID.
        body = request([segment(e, sid=invented)], current)
        failure(lambda: prepare(e, body, current), "invalid_metadata_input", 422)
    failure(lambda: prepare(e, request([segment(e, sid=removed_id)])),
            "invalid_metadata_input", 422)


@pytest.mark.parametrize("damage", ["bounds", "bool_pages", "invalid_copy", "nested_copy", "overlap"])
def test_actual_page_bounds_and_original_dto_types(env, damage):
    e = env
    body, pages = request([segment(e)]), 3
    if damage == "bounds":
        body = request([segment(e, end=4)])
    elif damage == "bool_pages":
        pages = True
    elif damage == "invalid_copy":
        body = body.model_copy(update={"segments": PRIVATE})
    elif damage == "nested_copy":
        bad = segment(e).model_copy(update={"page_end": True})
        body = body.model_copy(update={"segments": [bad]})
    else:
        body = body.model_copy(update={"segments": [segment(e), segment(e, "other")]})
    failure(lambda: prepare(e, body, pages=pages), "invalid_metadata_input", 422)


def test_new_requires_read_but_values_only_reuses_embedded_schema(env, monkeypatch):
    e = env
    no_catalog = who(scopes=METADATA)
    failure(lambda: prepare(e, request([segment(e)]), principal=no_catalog), status=403)
    head = seeded_head(e)
    deprecate(e)
    body = request([segment(e, key="renamed", sid=head.segments[0].segment_id,
                            fields=[ManualFieldInput(field_name="title", raw="Edited")])], head)

    def forbidden(*args, **kwargs):
        raise AssertionError("unchanged association consulted the catalog")

    monkeypatch.setattr(e.types, "get_version", forbidden)
    monkeypatch.setattr(e.types, "_binding_version", forbidden)
    plan = prepare(e, body, head, no_catalog)
    assert plan.segment_ids == {"renamed": head.segments[0].segment_id}
    assert plan.schemas[0] == head.schemas[0]
    assert plan.segments[0].context_id == head.segments[0].context_id
    assert plan.segments[0].fields[0].raw == "Edited"
    check(e, plan)


@pytest.mark.parametrize("change", ["interval", "type", "version", "new_segment"])
def test_changed_associations_require_scope_and_commit_eligibility(env, change):
    e = env
    head = seeded_head(e)
    sid = head.segments[0].segment_id
    target = e.first
    if change == "version":
        target, _ = e.types.publish(
            e.owner, e.first.type_id, expected_revision=1,
            expected_context_revision=1, key="publication-second-version-binding",
        )
    elif change == "type":
        target = e.second
    body = request([segment(e, sid=None if change == "new_segment" else sid,
                            version=target, end=2 if change == "interval" else 1)], head)
    failure(lambda: prepare(e, body, head, who(scopes=METADATA)), status=403)
    plan = prepare(e, body, head)
    deprecate(e, target)
    failure(lambda: check(e, plan), "type_version_deprecated", 409)
    failure(lambda: prepare(e, body, head), "type_version_deprecated", 409)


def test_removed_deprecated_segment_has_no_eligibility_check(env):
    e = env
    initial = prepare(e, request([
        segment(e, "removed"),
        segment(e, "kept", version=e.second, start=2, end=2),
    ]))
    head = snapshot(e, initial)
    deprecate(e)
    kept = head.segments[1]
    body = request([segment(e, "kept", sid=kept.segment_id,
                            version=e.second, start=2, end=2)], head)
    plan = prepare(e, body, head, who(scopes=METADATA))
    assert len(plan.segments) == 1
    assert [schema.schema_sha256 for schema in plan.schemas] == [kept.schema_sha256]
    check(e, plan)


@pytest.mark.parametrize("principal", [
    who("user:2", admin=True),
    who("service:" + "f" * 32, admin=True),
])
def test_other_owner_and_service_cannot_use_user_catalog(env, principal):
    failure(lambda: prepare(env, request([segment(env)]), principal=principal),
            "type_version_not_found", 404)


def test_mutating_inputs_views_and_preparer_cannot_change_captured_checks(env):
    e = env
    head = seeded_head(e)
    body = request([
        segment(e, "old", sid=head.segments[0].segment_id),
        segment(e, "new", start=2, end=2, version=e.second,
                fields=[ManualFieldInput(field_name="title", raw="Synthetic")]),
    ], head)
    plan = prepare(e, body, head)
    expected_ids = plan.segment_ids
    body.segments.clear()
    head.schemas[0].schema_document.clear()
    head.segments.clear()
    plan.segment_ids.clear()
    view = plan.segments[1]
    view.fields.clear()
    object.__setattr__(view, "type_id", e.first.type_id)
    plan.schemas[0].schema_document.clear()
    e.preparer.types = None
    assert plan.segment_ids == expected_ids
    assert plan.segments[1].type_id == e.second.type_id
    assert plan.segments[1].fields[0].raw == "Synthetic"
    assert plan.schemas[0].schema_document
    deprecate(e, e.second)
    failure(lambda: check(e, plan), "type_version_deprecated", 409)


def test_wrong_connection_and_missing_transaction_fail_without_sql_changes(env):
    e = env
    plan = prepare(e, request([segment(e)]))
    foreign = sqlite3.connect(":memory:")
    try:
        with e.store._transaction() as conn:
            before = conn.total_changes
            failure(lambda: plan.check_locked(None), "metadata_unavailable", 503)
            assert conn.total_changes == before and conn.in_transaction
            foreign.execute("BEGIN IMMEDIATE")
            foreign_before = foreign.total_changes
            failure(lambda: plan.check_locked(foreign), "metadata_unavailable", 503)
            assert conn.total_changes == before
            assert foreign.total_changes == foreign_before
            assert foreign.in_transaction
            foreign.rollback()
        with e.jobs._lock:
            conn = e.jobs._conn
            assert conn.in_transaction is False
            before = conn.total_changes
            failure(lambda: plan.check_locked(conn), "metadata_unavailable", 503)
            assert conn.total_changes == before and conn.in_transaction is False
    finally:
        foreign.close()


def test_callback_does_not_reconnect_or_use_public_getters(env, monkeypatch):
    e = env
    plan = prepare(e, request([segment(e)]))

    def forbidden(*args, **kwargs):
        raise AssertionError("callback attempted connection acquisition or a public lookup")

    with e.store._transaction() as conn:
        with monkeypatch.context() as patch:
            patch.setattr(e.jobs, "_get_conn", forbidden)
            patch.setattr(e.types, "get_version", forbidden)
            before = conn.total_changes
            plan.check_locked(conn)
            assert conn.total_changes == before and conn.in_transaction


def test_quarantined_connection_is_rejected(env):
    e = env
    plan = prepare(e, request([segment(e)]))
    with e.jobs._lock:
        conn = e.jobs._conn
        conn.execute("BEGIN IMMEDIATE")
        before = conn.total_changes
        e.jobs._quarantine_locked()
        assert e.jobs._sqlite_quarantined is True
        statements = []
        conn.set_trace_callback(statements.append)
        try:
            failure(lambda: plan.check_locked(conn), "metadata_unavailable", 503)
        finally:
            conn.set_trace_callback(None)
        assert statements == []
        assert conn.total_changes == before and conn.in_transaction
        assert e.jobs._conn is conn


def test_shared_jobstore_is_required(env, tmp_path):
    other = JobStore(tmp_path.absolute() / "other")
    try:
        failure(lambda: MetadataBindingPreparer(other, env.types), "metadata_unavailable", 503)
    finally:
        with other._lock:
            if other._conn is not None:
                other._conn.close()


def reserved_write(e, plan, label, parent=None, principal=None):
    """Trusted test harness only; no public save/replay implementation."""
    principal = e.owner if principal is None else principal
    model = snapshot(e, plan, parent, principal=principal)
    data = snapshot_bytes(model)
    receipt = SaveMetadataReceipt(revision_id=model.revision_id, sha256=digest(data),
                                  segment_ids=plan.segment_ids)
    with e.jobs.artifact_lease(e.job.job_id, principal.subject) as leased:
        admission = e.store.reserve(
            principal, leased.job_id,
            key_hash=digest(("key:" + label).encode()),
            request_hash=digest(("request:" + label).encode()),
            expected_head=None if parent is None else parent.revision_id,
            expected_sha256=None if parent is None else digest(snapshot_bytes(parent)),
            revision_id=model.revision_id, sha256=receipt.sha256,
            size_bytes=len(data), created_at=model.created_at,
        )
        assert admission.pending is not None
        result = e.storage.write_reserved(
            principal, leased, admission.pending, receipt, data,
            check_bindings_locked=plan.check_locked,
        )
    return result, model, data


def sql_rows(e, statement, parameters=()):
    with e.jobs._lock:
        return [tuple(row) for row in e.jobs._get_conn().execute(statement, parameters).fetchall()]


@pytest.mark.skipif(os.name != "posix", reason="MetadataFiles requires Linux filesystem operations")
def test_real_write_rechecks_after_publication_and_releases_failed_pending(env, monkeypatch):
    e = env
    plan = prepare(e, request([segment(e)]))
    original = e.files.read
    observed = []

    def read_then_deprecate(job, **options):
        # MetadataStorage calls this after publication and before its final SQL
        # transaction. Deterministic ordering avoids a timing-dependent race.
        model = original(job, **options)
        final = job.working_dir / "results" / "metadata" / (model.revision_id + ".json")
        assert final.is_file()
        observed.append(final)
        deprecate(e)
        return model

    with monkeypatch.context() as patch:
        patch.setattr(e.files, "read", read_then_deprecate)
        failure(lambda: reserved_write(e, plan, "rejected-binding"),
                "type_version_deprecated", 409)
    assert len(observed) == 1 and not observed[0].exists()
    assert sql_rows(e, "SELECT revision_id FROM metadata_revisions WHERE job_id=?",
                    (e.job.job_id,)) == []
    assert sql_rows(e, "SELECT pending_id FROM metadata_pending WHERE job_id=?",
                    (e.job.job_id,)) == []
    admission = e.store.preflight(
        e.owner, e.job.job_id, key_hash=digest(b"key:rejected-binding"),
        request_hash=digest(b"request:rejected-binding"),
    )
    assert admission.head is None and admission.receipt is None


@pytest.mark.skipif(os.name != "posix", reason="MetadataFiles requires Linux filesystem operations")
def test_real_accepted_binding_survives_deprecation_and_values_only_write(env):
    e = env
    plan = prepare(e, request([segment(e)]))
    receipt, model, data = reserved_write(e, plan, "accepted-binding")
    admission = e.store.preflight(
        e.owner, e.job.job_id, key_hash=digest(b"head-read-key"),
        request_hash=digest(b"head-read-request"),
    )
    assert admission.head.revision_id == receipt.revision_id
    assert admission.head.sha256 == receipt.sha256 == digest(data)
    with e.jobs.artifact_lease(e.job.job_id, e.owner.subject) as leased:
        head = e.files.read(leased, revision_id=admission.head.revision_id,
                            sha256=admission.head.sha256, size_bytes=len(data))
    assert head == model and snapshot_bytes(head) == data
    deprecate(e)
    limited = who(scopes=METADATA)
    body = request([segment(e, sid=head.segments[0].segment_id,
                            fields=[ManualFieldInput(field_name="title", raw="Edited")])], head)
    edit_plan = prepare(e, body, head, limited)
    edited_receipt, edited, edited_bytes = reserved_write(
        e, edit_plan, "values-after-deprecation", head, limited,
    )
    assert edited_receipt.segment_ids == receipt.segment_ids
    assert edited.parent_revision_id == receipt.revision_id
    assert edited.segments[0].fields[0].raw == "Edited"
    assert sql_rows(e, "SELECT pending_id FROM metadata_pending WHERE job_id=?",
                    (e.job.job_id,)) == []
    assert len(sql_rows(e, "SELECT revision_id FROM metadata_revisions WHERE job_id=?",
                        (e.job.job_id,))) == 2
    with e.jobs.artifact_lease(e.job.job_id, limited.subject) as leased:
        persisted = e.files.read(leased, revision_id=edited_receipt.revision_id,
                                 sha256=edited_receipt.sha256, size_bytes=len(edited_bytes))
    assert persisted == edited
    assert snapshot_bytes(persisted) == edited_bytes
