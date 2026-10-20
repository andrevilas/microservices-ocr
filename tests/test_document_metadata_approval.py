"""Real SQLite/artifact integration coverage for internal metadata approval."""
import copy
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from threading import Barrier, Lock, Thread

import pytest

import app.metadata_approval as policy
import app.services.document_service as documents
from app.application import ApplicationError, JobApplication, Principal
from app.metadata_approval import ApprovalInput
from app.metadata_models import snapshot_bytes
from app.services.metadata_canonical import MetadataCanonicalSource
from test_document_metadata import (
    BASE, body, canonical, deprecate, env, failure, idle, persisted,
    pytestmark, rows, save, segment, who,
)

APPROVAL_SCOPES = (BASE - {"metadata:write"}) | {"metadata:approve"}
APPROVAL_KEY = "document-approve-original-0001"


@pytest.fixture
def ready(env):
    e = env
    e.draft, _ = save(e)
    e.original = persisted(e, e.draft)
    e.original_bytes = snapshot_bytes(e.original)
    e.approver = who(scopes=APPROVAL_SCOPES)
    e.approval_body = dict(
        revision_id=e.draft.revision_id,
        expected_head=e.draft.revision_id,
        expected_sha256=e.draft.sha256,
        reviewed_fields=[dict(segment_id=s.segment_id, field_name=f.field_name)
                         for s in e.original.segments for f in s.fields
                         if f.raw is not None and f.normalized is not None],
    )
    # Exercise the existing policy with the actual schema and full canonical
    # pages, so the integration fixture is demonstrably ready for approval.
    with e.service.canonical.open(e.approver, e.job.job_id) as prepared:
        candidate = policy.prepare_approval(
            e.approver, e.job.owner_user_id, e.original, e.approval_body,
            MetadataCanonicalSource(prepared))
    assert candidate.state == "approved"
    idle(e, 1)
    return e


def approve(e, request=None, key=APPROVAL_KEY, principal=None):
    return e.service.approve(
        e.approver if principal is None else principal, e.job.job_id,
        copy.deepcopy(e.approval_body) if request is None else request, key=key)


def forbidden(*args, **kwargs):
    pytest.fail("unexpected heavy operation or reservation")


def later_draft(e, head, key="document-approve-later-draft-0001"):
    return save(e, dict(
        expected_head=head.revision_id, expected_sha256=head.sha256,
        segments=[segment(e, sid=e.draft.segment_ids["main"])],
    ), key=key)[0]


def assert_original(e):
    assert snapshot_bytes(persisted(e, e.draft)) == e.original_bytes


@pytest.mark.parametrize("auth_source", ["local_jwt", "oauth"])
def test_approval_exact_bytes_provenance_read_list_and_one_lease(ready, monkeypatch, auth_source):
    e, reads, checks = ready, [], []
    e.approver = replace(e.approver, auth_source=auth_source)
    assert not ({"metadata:write", "contexts:read"} & e.approver.scopes)
    read, check, health = e.bundle.files.read, e.service._check_shared, e.bundle.ensure_healthy
    page_read = canonical.read_page
    commit = e.bundle.index._commit_approval_index_locked

    def checked_read(*args, **kwargs):
        assert e.jobs._artifact_leases == {e.job.job_id: 1}
        assert not e.jobs._lock.locked()
        result = read(*args, **kwargs)
        reads.append(result.state)
        return result

    def checked_page(*args, **kwargs):
        assert e.jobs._artifact_leases == {e.job.job_id: 1}
        assert not e.jobs._lock.locked()
        return page_read(*args, **kwargs)

    def checked_shared():
        checks.append(e.jobs._lock.locked())
        return check()

    def checked_health(self):
        assert self is e.bundle
        assert not e.jobs._lock.locked()
        return health()

    def checked_commit(*args, **kwargs):
        assert e.jobs._artifact_leases == {e.job.job_id: 1}
        assert e.jobs._lock.locked()
        return commit(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(e.bundle.files, "read", checked_read)
        patch.setattr(canonical, "read_page", checked_page)
        patch.setattr(e.service, "_check_shared", checked_shared)
        patch.setattr(type(e.bundle), "ensure_healthy", checked_health)
        patch.setattr(e.bundle.index, "_commit_approval_index_locked", checked_commit)
        patch.setattr(documents.MetadataBindingPreparer, "prepare", forbidden)
        patch.setattr(e.types, "get_version", forbidden)
        patch.setattr(e.types, "_binding_version", forbidden)
        receipt, replayed = approve(e)
    assert not replayed and reads == ["draft", "approved"]
    assert checks == [False, True]
    approved = persisted(e, receipt)
    assert receipt.operation == "approve-v1" and receipt.state == "approved"
    assert receipt.sha256 == hashlib.sha256(snapshot_bytes(approved)).hexdigest()
    assert receipt.reviewed_revision_id == e.draft.revision_id
    assert receipt.reviewed_sha256 == e.draft.sha256
    assert approved.revision_id != e.draft.revision_id
    assert approved.parent_revision_id == e.draft.revision_id
    assert approved.schemas == e.original.schemas
    assert approved.author.actor_subject == e.approver.subject
    assert approved.author.auth_source == auth_source
    assert approved.author.execution_id is None
    assert approved.created_at == approved.author.recorded_at == approved.approval.approved_at
    assert approved.approval.actor_subject == e.approver.subject
    assert approved.approval.auth_source == auth_source
    assert approved.approval.reviewed_revision_id == e.draft.revision_id
    assert approved.approval.reviewed_sha256 == e.draft.sha256
    for before, after in zip(e.original.segments, approved.segments):
        assert before.segment_id == after.segment_id
        for original, reviewed in zip(before.fields, after.fields):
            assert reviewed.status == "reviewed"
            assert reviewed.model_copy(update={"status": original.status}) == original
    assert_original(e)
    assert e.service.get(e.approver, e.job.job_id, receipt.revision_id,
                         sha256=receipt.sha256) == approved
    assert e.service.get(e.approver, e.job.job_id, e.draft.revision_id,
                         sha256=e.draft.sha256) == e.original
    revisions = e.service.list_revisions(e.approver, e.job.job_id)
    assert {r.revision_id: r.state for r in revisions} == {
        e.draft.revision_id: "draft", receipt.revision_id: "approved"}
    idle(e, 2)


def test_replay_after_later_head_and_unavailable_catalog_is_detached(ready, monkeypatch):
    e = ready
    original, _ = approve(e)
    expected = original.model_dump(mode="json")
    later_draft(e, original)
    deprecate(e)
    # An archived/absent catalog lookup would fail. Replay must never consult it.
    def archived_catalog(*args, **kwargs):
        raise ApplicationError(404, "type_not_found", "Type not found.")

    monkeypatch.setattr(e.types, "get_version", archived_catalog)
    monkeypatch.setattr(e.types, "_binding_version", forbidden)
    monkeypatch.setattr(e.service.canonical, "open", forbidden)
    monkeypatch.setattr(e.bundle.files, "read", forbidden)
    monkeypatch.setattr(e.bundle.storage, "write_approval_reserved", forbidden)
    monkeypatch.setattr(policy, "uuid4", forbidden)
    monkeypatch.setattr(documents, "uuid4", forbidden)
    receipt, replayed = approve(e)
    assert replayed and receipt.model_dump(mode="json") == expected
    receipt.__dict__["sha256"] = "f" * 64
    assert approve(e)[0].model_dump(mode="json") == expected
    changed = copy.deepcopy(e.approval_body)
    changed["reviewed_fields"] = []
    failure(lambda: approve(e, changed), 409)
    idle(e, 3)


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("missing", sorted(APPROVAL_SCOPES))
def test_required_grants_gate_approval_and_replay_without_admin_exemption(ready, monkeypatch, replay, missing):
    e = ready
    if replay:
        approve(e)
    monkeypatch.setattr(e.service.canonical, "open", forbidden)
    principal = who(scopes=APPROVAL_SCOPES - {missing}, admin=True)
    failure(lambda: approve(e, principal=principal), 403, "insufficient_scope")
    idle(e, 1 + int(replay))


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("principal", [
    who("user:2", scopes=APPROVAL_SCOPES),
    who("user:3", scopes=APPROVAL_SCOPES, admin=True),
    who("service:" + "f" * 32, scopes=APPROVAL_SCOPES, admin=True),
    Principal(subject="legacy", user_id=None, scopes=frozenset(APPROVAL_SCOPES),
              auth_source="legacy", is_admin=True),
])
def test_other_owner_service_and_legacy_are_rejected(ready, monkeypatch, replay, principal):
    e = ready
    if replay:
        approve(e)
    monkeypatch.setattr(e.service.canonical, "open", forbidden)
    with pytest.raises(ApplicationError) as caught:
        approve(e, principal=principal)
    assert caught.value.status in {403, 404}
    assert caught.value.__context__ is None
    idle(e, 1 + int(replay))


@pytest.mark.parametrize("change", ["expired", "deleted", "status"])
def test_replay_rechecks_retention_and_completed_status(ready, monkeypatch, change):
    e = ready
    approve(e)
    if change == "deleted":
        JobApplication(e.jobs).delete_artifacts(e.owner, e.job.job_id)
    else:
        with e.jobs._lock:
            if change == "expired":
                e.job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            else:
                e.job.status = "failed"
            e.jobs._persist(e.job)
    monkeypatch.setattr(e.service.canonical, "open", forbidden)
    failure(lambda: approve(e), 409 if change == "status" else 410)
    assert rows(e, "metadata_pending") == [] and not e.jobs._artifact_leases


@pytest.mark.parametrize("change,status", [
    ("head", 409), ("hash", 409), ("lineage", 422),
    ("extra", 422), ("duplicate", 422), ("selection", 422),
    ("unreviewed", 409), ("mutated_model", 422),
])
def test_bad_request_cas_and_readiness_never_reserve(ready, monkeypatch, change, status):
    e, request = ready, copy.deepcopy(ready.approval_body)
    if change == "head":
        request["revision_id"] = request["expected_head"] = "f" * 32
    elif change == "hash":
        request["expected_sha256"] = "f" * 64
    elif change == "lineage":
        request["revision_id"] = "f" * 32
    elif change == "extra":
        request["actor_subject"] = "user:1"
    elif change == "duplicate":
        request["reviewed_fields"] *= 2
    elif change == "selection":
        request["reviewed_fields"][0]["segment_id"] = "f" * 32
    elif change == "unreviewed":
        request["reviewed_fields"] = []
    else:
        request = ApprovalInput.model_validate(request, strict=True)
        request.reviewed_fields[0].__dict__["field_name"] = True
    monkeypatch.setattr(e.bundle.index, "reserve_approval", forbidden)
    failure(lambda: approve(e, request), status)
    assert_original(e)
    idle(e, 1)


def test_missing_required_field_is_not_ready_without_reservation(env, monkeypatch):
    e = env
    draft, _ = save(e, body(e, segments=[segment(e, fields=[])]))
    request = dict(revision_id=draft.revision_id, expected_head=draft.revision_id,
                   expected_sha256=draft.sha256, reviewed_fields=[])
    monkeypatch.setattr(e.bundle.index, "reserve_approval", forbidden)
    failure(lambda: e.service.approve(who(scopes=APPROVAL_SCOPES), e.job.job_id,
                                      request, key=APPROVAL_KEY), 409, "metadata_not_ready")
    idle(e, 1)


def test_changed_literal_evidence_is_422_before_reservation(ready, monkeypatch):
    e = ready
    read = canonical.PreparedCanonicalText.read_page_text

    def changed_page(prepared, number):
        full = read(prepared, number)
        full["text"] = full["text"].replace("título", "TÍTULO")
        full["text_sha256"] = hashlib.sha256(full["text"].encode("utf-8")).hexdigest()
        return full

    monkeypatch.setattr(canonical.PreparedCanonicalText, "read_page_text", changed_page)
    monkeypatch.setattr(e.bundle.index, "reserve_approval", forbidden)
    failure(lambda: approve(e), 422, "evidence_invalid")
    assert_original(e)
    idle(e, 1)


@pytest.mark.parametrize("field,value", [
    ("job_id", "f" * 32), ("document_id", "f" * 32),
    ("artifact_version", 2), ("text_version", 2),
    ("sha256", "f" * 64), ("input_pdf_sha256", "f" * 64),
    ("output_pdf_sha256", "f" * 64), ("page_count", 1),
    ("parent_job_id", "f" * 32),
])
def test_every_manifest_identity_is_checked(ready, monkeypatch, field, value):
    e = ready
    getter = canonical.PreparedCanonicalText.manifest.fget

    def corrupted(prepared):
        result = getter(prepared)
        result[field] = value
        return result

    monkeypatch.setattr(canonical.PreparedCanonicalText, "manifest", property(corrupted))
    monkeypatch.setattr(e.bundle.index, "reserve_approval", forbidden)
    failure(lambda: approve(e), 503, "metadata_unavailable")
    assert_original(e)
    idle(e, 1)


@pytest.mark.parametrize("change", ["bytes", "header", "index"])
def test_head_integrity_and_common_index_headers_fail_closed(ready, monkeypatch, change):
    e = ready
    if change == "bytes":
        path = e.job.working_dir / "results" / "metadata" / (e.draft.revision_id + ".json")
        path.write_bytes(b"{}")
    elif change == "header":
        read = e.bundle.files.read
        def changed_read(*args, **kwargs):
            return read(*args, **kwargs).model_copy(update={"parent_job_id": "f" * 32})
        monkeypatch.setattr(e.bundle.files, "read", changed_read)
    else:
        revision = e.bundle.index._revision
        def changed_revision(*args, **kwargs):
            result = revision(*args, **kwargs)
            return replace(result, created_at=result.created_at - timedelta(seconds=1))
        monkeypatch.setattr(e.bundle.index, "_revision", changed_revision)
    monkeypatch.setattr(e.bundle.index, "reserve_approval", forbidden)
    failure(lambda: approve(e), 503)
    idle(e, 1)


@pytest.mark.parametrize("stage", ["verified_file", "reservation"])
def test_new_head_during_preparation_conflicts_without_overwrite(ready, monkeypatch, stage):
    e, later = ready, []
    if stage == "verified_file":
        read = e.bundle.files.read
        def racing_read(*args, **kwargs):
            result = read(*args, **kwargs)
            monkeypatch.setattr(e.bundle.files, "read", read)
            later.append(save(e, body(e, e.draft), key="document-approve-racing-save-0001")[0])
            return result
        monkeypatch.setattr(e.bundle.files, "read", racing_read)
    else:
        reserve = e.bundle.index.reserve_approval
        def racing_reserve(*args, **kwargs):
            later.append(save(e, body(e, e.draft), key="document-approve-racing-save-0001")[0])
            return reserve(*args, **kwargs)
        monkeypatch.setattr(e.bundle.index, "reserve_approval", racing_reserve)
    failure(lambda: approve(e), 409, "metadata_revision_conflict")
    assert len(later) == 1 and persisted(e, later[0]).state == "draft"
    assert_original(e)
    idle(e, 2)


def test_concurrent_duplicate_has_one_approved_file_and_eventual_replay(ready, monkeypatch):
    e, barrier, lock, outcomes = ready, Barrier(2, timeout=5), Lock(), []
    reserve = e.bundle.index.reserve_approval

    def racing_reserve(*args, **kwargs):
        barrier.wait()
        return reserve(*args, **kwargs)

    def worker():
        try:
            result = approve(e)
        except BaseException as error:
            result = error
        with lock:
            outcomes.append(result)

    with monkeypatch.context() as patch:
        patch.setattr(e.bundle.index, "reserve_approval", racing_reserve)
        threads = [Thread(target=worker, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
    successes = [value for value in outcomes if isinstance(value, tuple)]
    errors = [value for value in outcomes if isinstance(value, BaseException)]
    assert len(successes) + len(errors) == 2
    assert sum(not result[1] for result in successes) == 1
    assert all(isinstance(error, ApplicationError) and error.status == 409 for error in errors)
    receipt, replayed = approve(e)
    assert replayed and all(result[0] == receipt for result in successes)
    assert persisted(e, receipt).state == "approved"
    assert_original(e)
    idle(e, 2)


def test_receipt_race_at_reservation_skips_writer(ready, monkeypatch):
    e = ready
    reserve = e.bundle.index.reserve_approval
    write = e.bundle.storage.write_approval_reserved
    winner = []

    def racing_reserve(*args, **kwargs):
        with monkeypatch.context() as patch:
            patch.setattr(e.bundle.index, "reserve_approval", reserve)
            winner.append(approve(e)[0])
        result = reserve(*args, **kwargs)
        assert result.receipt is not None and result.pending is None
        monkeypatch.setattr(e.bundle.storage, "write_approval_reserved", forbidden)
        return result

    monkeypatch.setattr(e.bundle.index, "reserve_approval", racing_reserve)
    receipt, replayed = approve(e)
    assert replayed and winner == [receipt]
    monkeypatch.setattr(e.bundle.storage, "write_approval_reserved", write)
    idle(e, 2)


@pytest.mark.parametrize("change,status,code", [
    ("ttl", 410, "artifacts_expired"),
    ("grant", 403, "insufficient_scope"),
    ("corrupt_head", 503, "metadata_unavailable"),
])
def test_changes_after_publication_are_rejected_and_cleaned(ready, monkeypatch, change, status, code):
    e = ready
    read, head = e.bundle.files.read, e.bundle.index._head
    changed = []

    def changed_read(*args, **kwargs):
        result = read(*args, **kwargs)
        if result.state == "approved" and not changed:
            changed.append(True)
            assert e.jobs._artifact_leases == {e.job.job_id: 1}
            if change == "ttl":
                with e.jobs._lock:
                    e.job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                    e.jobs._persist(e.job)
            elif change == "grant":
                object.__setattr__(e.approver, "scopes", frozenset(APPROVAL_SCOPES - {"metadata:approve"}))
            else:
                def different_head(*head_args, **head_kwargs):
                    monkeypatch.setattr(e.bundle.index, "_head", head)
                    return replace(head(*head_args, **head_kwargs), revision_id="f" * 32)
                monkeypatch.setattr(e.bundle.index, "_head", different_head)
        return result

    original_scopes = e.approver.scopes
    monkeypatch.setattr(e.bundle.files, "read", changed_read)
    try:
        failure(lambda: approve(e), status, code)
    finally:
        if change == "grant":
            object.__setattr__(e.approver, "scopes", original_scopes)
    assert changed == [True]
    assert_original(e)
    idle(e, 1)
    assert not list((e.job.working_dir / "results" / "metadata").glob(".*.tmp"))


@pytest.mark.parametrize("field", ["size_bytes", "parent_revision_id", "created_at", "state"])
def test_final_callback_checks_exact_captured_revision(ready, monkeypatch, field):
    e = ready
    write, revision = e.bundle.storage.write_approval_reserved, e.bundle.index._revision
    callbacks = []

    def checked_write(principal, job, reservation, receipt, canonical_bytes, *, check_bindings_locked):
        def changed_revision(*args, **kwargs):
            current = revision(*args, **kwargs)
            value = {"size_bytes": current.size_bytes + 1,
                     "parent_revision_id": "f" * 32,
                     "created_at": current.created_at - timedelta(seconds=1),
                     "state": "approved"}[field]
            return replace(current, **{field: value})

        def checked_callback(conn):
            assert conn is e.jobs._conn and conn.in_transaction
            assert e.jobs._lock.locked()
            assert e.jobs._artifact_leases == {e.job.job_id: 1}
            callbacks.append(True)
            with monkeypatch.context() as patch:
                patch.setattr(e.bundle.index, "_revision", changed_revision)
                check_bindings_locked(conn)

        return write(principal, job, reservation, receipt, canonical_bytes,
                     check_bindings_locked=checked_callback)

    monkeypatch.setattr(e.bundle.storage, "write_approval_reserved", checked_write)
    failure(lambda: approve(e), 503, "metadata_unavailable")
    assert callbacks == [True]
    assert_original(e)
    idle(e, 1)


def test_expiry_immediately_after_reservation_releases_handoff(ready, monkeypatch):
    e = ready
    reserve = e.bundle.index.reserve_approval

    def expire(*args, **kwargs):
        result = reserve(*args, **kwargs)
        assert result.pending is not None
        with e.jobs._lock:
            e.job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            e.jobs._persist(e.job)
        return result

    monkeypatch.setattr(e.bundle.index, "reserve_approval", expire)
    failure(lambda: approve(e), 410, "artifacts_expired")
    assert_original(e)
    idle(e, 1)


@pytest.mark.parametrize("control", [False, True])
def test_canonical_application_errors_and_control_identity_survive_policy(ready, monkeypatch, control):
    e = ready
    error = KeyboardInterrupt("synthetic control") if control else ApplicationError(
        503, "result_integrity_error", "Stored result failed validation.")

    def failed_source(*args, **kwargs):
        raise error

    monkeypatch.setattr(MetadataCanonicalSource, "__call__", failed_source)
    monkeypatch.setattr(e.bundle.index, "reserve_approval", forbidden)
    if control:
        with pytest.raises(KeyboardInterrupt) as caught:
            approve(e)
        assert caught.value is error
    else:
        result = failure(lambda: approve(e), 503, "result_integrity_error")
        assert result.message == error.message
    assert_original(e)
    idle(e, 1)


def test_shared_identity_and_health_fail_before_canonical_work(ready, monkeypatch):
    e, jobs = ready, ready.types.jobs
    monkeypatch.setattr(e.service.canonical, "open", forbidden)
    e.types.jobs = object()
    try:
        failure(lambda: approve(e), 503, "metadata_unavailable")
    finally:
        e.types.jobs = jobs

    def unhealthy(self):
        assert self is e.bundle
        assert not e.jobs._lock.locked()
        raise ApplicationError(503, "metadata_unavailable", "Metadata is unavailable.")

    monkeypatch.setattr(type(e.bundle), "ensure_healthy", unhealthy)
    failure(lambda: approve(e), 503, "metadata_unavailable")
    idle(e, 1)


def test_context_exit_failure_returns_no_receipt_and_durable_retry_replays(ready, monkeypatch):
    e = ready
    real_open = e.service.canonical.open

    @contextmanager
    def failing_exit(*args, **kwargs):
        with real_open(*args, **kwargs) as prepared:
            yield prepared
        raise ApplicationError(503, "storage_unavailable", "Synthetic context exit failure.")

    with monkeypatch.context() as patch:
        patch.setattr(e.service.canonical, "open", failing_exit)
        failure(lambda: approve(e), 503, "storage_unavailable")
    idle(e, 2)
    receipt, replayed = approve(e)
    assert replayed and persisted(e, receipt).state == "approved"
    assert_original(e)
    idle(e, 2)
