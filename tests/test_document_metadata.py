"""Synthetic integration coverage for the internal document metadata save API."""
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import os
from threading import Barrier, Lock, Thread
from types import SimpleNamespace

import pytest

import app.services.canonical_text_service as canonical
from app.application import ApplicationError, JobApplication, Principal
from app.document_models import ContextCreate, FieldSpec, TypeDraft
from app.metadata_models import snapshot_bytes
from app.results import (
    FileDigest, JobReport, OutputValidation, QualityReport, digest_file,
    persist_pages, persist_report,
)
from app.services.context_service import ContextService
from app.services.document_service import DocumentMetadataService
from app.services.metadata_runtime import MetadataProvider
from app.services.storage_service import JobStore
from app.services.type_service import TypeService

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")
BASE = {"jobs:read", "artifacts:read", "metadata:read", "metadata:write"}
CATALOG = {"contexts:read", "contexts:write", "contexts:publish"}
QUOTE = "é 😀 título"
PAGES = ["x" * 21000 + QUOTE, "second synthetic page"]
KEY = "document-save-original-0001"


def who(subject="user:1", scopes=None, admin=False):
    return Principal(subject=subject,
                     user_id=int(subject[5:]) if subject.startswith("user:") else None,
                     scopes=frozenset(BASE | CATALOG | {"artifacts:delete"} if scopes is None else scopes),
                     auth_source="service" if subject.startswith("service:") else "local_jwt",
                     is_admin=admin)


def runtime(e):
    e.types = TypeService(e.jobs)
    e.provider = MetadataProvider(lambda: JobApplication(e.jobs))
    e.bundle = e.provider()
    e.service = DocumentMetadataService(e.bundle, e.types)


@pytest.fixture
def env(tmp_path):
    e = SimpleNamespace(jobs=JobStore(tmp_path.absolute() / "jobs"), owner=who())
    try:
        runtime(e)
        context, _ = ContextService(e.jobs).create(
            e.owner, ContextCreate(name="Synthetic"), "context-document-save-0001")
        draft, _ = e.types.create(e.owner, context.context_id, TypeDraft(
            name="Synthetic document", specific_fields=[
                FieldSpec(name="title", type="string", required=True, description="Synthetic")
            ]), "type-document-save-0001")
        e.version, _ = e.types.publish(
            e.owner, draft.type_id, expected_revision=1, expected_context_revision=1,
            key="publish-document-save-0001")
        e.job = e.jobs.create("synthetic.pdf", b"%PDF-synthetic", owner_user_id=1)
        pages = persist_pages(e.job.working_dir, PAGES, "pdf_text")
        report = JobReport(
            job_id=e.job.job_id, generated_at=datetime.now(timezone.utc), duration_ms=1,
            input=digest_file(e.job.input_pdf_path), output=FileDigest(sha256="b" * 64, size_bytes=1),
            page_count=2, pages=pages,
            quality=QualityReport(label="HIGH", character_count=sum(map(len, PAGES)),
                                  valid_ratio=1.0, min_text=1, valid_ratio_threshold=0.5),
            stages=[], engines={}, fallback_attempted=False, used_fallback=False, warnings=[],
            output_validation=OutputValidation(requested_format="pdf_searchable",
                                              pdf_readable=True, page_count_matches=True))
        persist_report(e.job.working_dir, report)
        e.jobs.update(e.job.job_id, status="completed", progress_percent=100)
        e.evidence = dict(document_id=e.job.job_id, page=1,
                          text_sha256=hashlib.sha256(PAGES[0].encode()).hexdigest(),
                          start=21000, end=21000 + len(QUOTE), quote=QUOTE)
        yield e
    finally:
        acquired = e.jobs._lock.acquire(timeout=5)
        assert acquired, "save worker retained the shared lock"
        try:
            e.jobs._conn.close()
        finally:
            e.jobs._lock.release()


def segment(e, key="main", sid=None, start=1, end=1, fields=None):
    return dict(client_key=key, segment_id=sid, page_start=start, page_end=end,
                type_id=e.version.type_id, type_version=e.version.version,
                fields=copy.deepcopy([dict(field_name="title", raw=QUOTE, evidence=[e.evidence])]
                                     if fields is None else fields))


def body(e, head=None, segments=None):
    return dict(expected_head=None if head is None else head.revision_id,
                expected_sha256=None if head is None else head.sha256,
                segments=[segment(e, sid=None if head is None else head.segment_ids["main"])]
                if segments is None else segments)


def save(e, request=None, key=KEY, principal=None):
    return e.service.save(e.owner if principal is None else principal, e.job.job_id,
                          body(e) if request is None else request, key=key)


def failure(action, status, code=None):
    with pytest.raises(ApplicationError) as caught:
        action()
    assert caught.value.status == status
    if code is not None:
        assert caught.value.code == code
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    return caught.value


def rows(e, table):
    assert table in {"metadata_revisions", "metadata_pending"}
    with e.jobs._lock:
        return e.jobs._conn.execute(
            "SELECT * FROM " + table + " WHERE job_id=?", (e.job.job_id,)).fetchall()


def idle(e, revisions):
    assert len(rows(e, "metadata_revisions")) == revisions
    assert rows(e, "metadata_pending") == []
    assert not e.bundle.index._active_writes and not e.jobs._artifact_leases
    files = list((e.job.working_dir / "results" / "metadata").glob("*.json"))
    assert len(files) == revisions


def persisted(e, receipt):
    data = (e.job.working_dir / "results" / "metadata" / (receipt.revision_id + ".json")).read_bytes()
    assert hashlib.sha256(data).hexdigest() == receipt.sha256
    model = e.bundle.files.read(e.job, revision_id=receipt.revision_id,
                                sha256=receipt.sha256, size_bytes=len(data))
    assert snapshot_bytes(model) == data
    return model


def deprecate(e):
    e.types.deprecate(e.owner, e.version.type_id, e.version.version,
                      expected_lifecycle_revision=1, key="deprecate-document-save-0001")


def test_manual_save_exact_snapshot_provenance_and_single_lease(env, monkeypatch):
    e, reads, commits, storage = env, [], [], []
    read, commit, write = canonical.read_page, e.bundle.index._commit_index_locked, e.bundle.storage.write_reserved

    def checked_read(directory, info):
        assert e.jobs._artifact_leases == {e.job.job_id: 1}
        reads.append(info.page)
        return read(directory, info)

    def checked_commit(*args, **kwargs):
        assert e.jobs._artifact_leases == {e.job.job_id: 1}
        commits.append(True)
        return commit(*args, **kwargs)

    def checked_write(*args, **kwargs):
        assert e.jobs._artifact_leases == {e.job.job_id: 1}
        result = write(*args, **kwargs)
        assert e.jobs._artifact_leases == {e.job.job_id: 1}
        storage.append(True)
        return result

    monkeypatch.setattr(canonical, "read_page", checked_read)
    monkeypatch.setattr(e.bundle.index, "_commit_index_locked", checked_commit)
    monkeypatch.setattr(e.bundle.storage, "write_reserved", checked_write)
    receipt, replayed = save(e)
    assert not replayed and set(receipt.segment_ids) == {"main"}
    assert reads == [1, 2, 1] and commits == storage == [True]
    idle(e, 1)
    model = persisted(e, receipt)
    assert model.state == "draft" and model.approval is None
    assert model.job_id == model.document_id == e.job.job_id
    assert model.segments[0].segment_id == receipt.segment_ids["main"]
    field = model.segments[0].fields[0]
    assert field.raw == QUOTE and field.status == "needs_review" and field.origin == "manual"
    assert field.evidence[0].start > 20000
    assert field.evidence[0].text_sha256 == e.evidence["text_sha256"]
    assert field.provenance == model.author
    assert model.author.actor_subject == e.owner.subject
    assert model.author.auth_source == "local_jwt" and model.author.execution_id is None
    assert model.author.recorded_at == model.created_at
    with e.jobs._lock:
        owner_row = e.jobs._conn.execute(
            "SELECT owner FROM metadata_revisions WHERE job_id=?", (e.job.job_id,)).fetchone()
    assert owner_row == (e.owner.subject,)


def test_incomplete_draft_and_only_new_segments_get_new_ids(env):
    e = env
    first, _ = save(e, body(e, segments=[segment(e, fields=[])]))
    assert persisted(e, first).state == "draft" and persisted(e, first).approval is None
    second, replayed = save(e, body(e, first, [
        segment(e, sid=first.segment_ids["main"]),
        segment(e, key="extra", start=2, end=2, fields=[]),
    ]), key="document-save-second-0001")
    assert not replayed and second.revision_id != first.revision_id
    assert second.segment_ids["main"] == first.segment_ids["main"]
    assert second.segment_ids["extra"] not in first.segment_ids.values()
    assert persisted(e, second).parent_revision_id == first.revision_id
    idle(e, 2)


def test_values_edit_after_deprecation_without_catalog_scope(env):
    e = env
    first, _ = save(e)
    deprecate(e)
    request = body(e, first)
    request["segments"][0]["fields"][0]["raw"] = "Edited synthetic title"
    second, replayed = save(e, request, key="document-save-edit-0001", principal=who(scopes=BASE))
    assert not replayed and second.segment_ids == first.segment_ids
    assert persisted(e, second).schemas == persisted(e, first).schemas
    assert persisted(e, second).segments[0].fields[0].raw == "Edited synthetic title"
    idle(e, 2)


@pytest.mark.parametrize("change", ["initial", "range", "new"])
def test_deprecated_new_or_changed_binding_is_denied(env, change):
    e = env
    head = None if change == "initial" else save(e)[0]
    deprecate(e)
    request = body(e, head)
    if change == "range":
        request["segments"][0]["page_end"] = 2
    elif change == "new":
        request["segments"][0]["segment_id"] = None
    failure(lambda: save(e, request, key="document-save-deprecated-0001"), 409, "type_version_deprecated")
    idle(e, int(head is not None))


def test_false_quote_never_reserves_and_corrupt_source_is_503(env, monkeypatch):
    e = env
    def forbidden(*args, **kwargs):
        pytest.fail("invalid evidence reached reservation")
    monkeypatch.setattr(e.bundle.index, "reserve", forbidden)
    request = body(e)
    request["segments"][0]["fields"][0]["evidence"][0]["quote"] = "?" * len(QUOTE)
    failure(lambda: save(e, request), 422, "invalid_metadata_input")
    idle(e, 0)
    (e.job.working_dir / "results" / "page-000001.txt").write_bytes(b"\xff\xfe")
    failure(lambda: save(e), 503, "result_integrity_error")
    idle(e, 0)


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("missing", sorted(BASE))
def test_each_required_scope_gates_write_and_replay(env, replay, missing):
    if replay:
        save(env)
    failure(lambda: save(env, principal=who(scopes=(BASE | CATALOG) - {missing})),
            403, "insufficient_scope")
    idle(env, int(replay))


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("principal", [who("user:2"), who("user:3", admin=True),
                                        who("service:" + "f" * 32, admin=True)])
def test_owner_admin_and_service_isolation(env, replay, principal):
    if replay:
        save(env)
    failure(lambda: save(env, principal=principal), 404)
    idle(env, int(replay))


def test_original_replay_survives_later_head_deprecation_and_restart(env, monkeypatch):
    e = env
    original_request = body(e)
    original, _ = save(e, original_request)
    expected = original.model_dump(mode="json")
    later, _ = save(e, body(e, original), key="document-save-later-0001")
    deprecate(e)
    def forbidden(*args, **kwargs):
        pytest.fail("replay consulted canonical text or catalog")
    for restart in (False, True):
        if restart:
            root, job_id = e.jobs.root_dir, e.job.job_id
            with e.jobs._lock:
                e.jobs._conn.close()
            e.jobs = JobStore(root)
            e.job = e.jobs.jobs[job_id]
            runtime(e)
        monkeypatch.setattr(e.service.canonical, "open", forbidden)
        monkeypatch.setattr(e.types, "get_version", forbidden)
        receipt, replayed = save(e, copy.deepcopy(original_request))
        assert replayed and receipt.model_dump(mode="json") == expected
        receipt.segment_ids.clear()
        assert save(e, original_request)[0].model_dump(mode="json") == expected
        changed = copy.deepcopy(original_request)
        changed["segments"][0]["fields"][0]["raw"] = "Different request"
        failure(lambda: save(e, changed), 409)
        failure(lambda: save(e, body(e, original), key="document-save-stale-0001"),
                409, "metadata_revision_conflict")
        assert persisted(e, later).parent_revision_id == original.revision_id
        idle(e, 2)


@pytest.mark.parametrize("deleted", [False, True])
def test_initial_key_cannot_replay_after_expiry_or_deletion(env, deleted):
    e = env
    save(e)
    if deleted:
        JobApplication(e.jobs).delete_artifacts(e.owner, e.job.job_id)
    else:
        with e.jobs._lock:
            e.job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            e.jobs._persist(e.job)
    failure(lambda: save(e), 410, "artifacts_deleted" if deleted else "artifacts_expired")
    assert rows(e, "metadata_pending") == [] and not e.jobs._artifact_leases


@pytest.mark.parametrize("stage", ["evidence", "publication"])
def test_expiry_during_save_cleans_pending_without_extending_ttl(env, monkeypatch, stage):
    e = env
    with e.jobs._lock:
        e.job.artifacts_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        e.jobs._persist(e.job)
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    def expire():
        with e.jobs._lock:
            e.job.artifacts_expires_at = expired
            e.jobs._persist(e.job)
    if stage == "evidence":
        real = canonical.PreparedCanonicalText.read_page_text
        def read(prepared, number):
            result = real(prepared, number)
            expire()
            return result
        monkeypatch.setattr(canonical.PreparedCanonicalText, "read_page_text", read)
    else:
        real = e.bundle.files.read
        def read(*args, **kwargs):
            result = real(*args, **kwargs)
            expire()
            return result
        monkeypatch.setattr(e.bundle.files, "read", read)
    failure(lambda: save(e), 410, "artifacts_expired")
    assert e.job.artifacts_expires_at == expired
    idle(e, 0)


def test_concurrent_same_head_commits_once(env, monkeypatch):
    e, barrier, lock, outcomes = env, Barrier(2, timeout=5), Lock(), []
    real = e.bundle.index.reserve
    def reserve(*args, **kwargs):
        barrier.wait()
        return real(*args, **kwargs)
    monkeypatch.setattr(e.bundle.index, "reserve", reserve)
    def worker(number):
        try:
            result = save(e, key="document-save-race-000" + str(number))
        except BaseException as error:
            result = error
        with lock:
            outcomes.append(result)
    threads = [Thread(target=worker, args=(n,), daemon=True) for n in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads), "save worker did not finish"
    successes = [value for value in outcomes if isinstance(value, tuple)]
    errors = [value for value in outcomes if isinstance(value, BaseException)]
    assert len(successes) == len(errors) == 1 and successes[0][1] is False
    assert isinstance(errors[0], ApplicationError) and errors[0].status == 409
    assert errors[0].code in {"metadata_revision_conflict", "metadata_write_pending"}
    persisted(e, successes[0][0])
    idle(e, 1)


def test_expiry_immediately_after_reserve_releases_own_handoff(env, monkeypatch):
    e = env
    reserve = e.bundle.index.reserve
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    reservations = []

    def expire_after_reserve(*args, **kwargs):
        result = reserve(*args, **kwargs)
        assert result.pending is not None
        reservations.append(result.pending)
        with e.jobs._lock:
            e.job.artifacts_expires_at = expired
            e.jobs._persist(e.job)
        return result

    monkeypatch.setattr(e.bundle.index, "reserve", expire_after_reserve)
    failure(lambda: save(e), 410, "artifacts_expired")
    assert len(reservations) == 1
    assert e.job.artifacts_expires_at == expired
    idle(e, 0)
    assert not list((e.job.working_dir / "results" / "metadata").glob(".*.tmp"))


def test_initial_transaction_failure_after_reserve_releases_own_handoff(env, monkeypatch):
    e = env
    index = e.bundle.index
    reserve, transaction = index.reserve, index._transaction
    failures = []
    message = "Synthetic initial handoff transaction failure."

    def fail_initial_transaction():
        monkeypatch.setattr(index, "_transaction", transaction)
        failures.append(True)
        raise ApplicationError(503, "metadata_unavailable", message)

    def arm_after_reserve(*args, **kwargs):
        result = reserve(*args, **kwargs)
        assert result.pending is not None
        monkeypatch.setattr(index, "reserve", reserve)
        monkeypatch.setattr(index, "_transaction", fail_initial_transaction)
        return result

    monkeypatch.setattr(index, "reserve", arm_after_reserve)
    error = failure(lambda: save(e), 503, "metadata_unavailable")
    assert error.message == message and failures == [True]
    idle(e, 0)
    assert not list((e.job.working_dir / "results" / "metadata").glob(".*.tmp"))
    with e.jobs._lock:
        assert e.jobs._conn.in_transaction is False
    receipt, replayed = save(e)
    assert not replayed
    persisted(e, receipt)
    idle(e, 1)


def test_duplicate_handoff_preserves_active_writer_and_published_file(env, monkeypatch):
    e = env
    write, read = e.bundle.storage.write_reserved, e.bundle.files.read
    duplicates = []

    def checked_write(principal, job, reservation, receipt, canonical_bytes, *,
                      check_bindings_locked):
        def checked_read(*args, **kwargs):
            pending = rows(e, "metadata_pending")
            path = job.working_dir / "results" / "metadata" / (reservation.revision_id + ".json")
            published = path.read_bytes()
            assert published == canonical_bytes
            assert e.bundle.index._active_writes == {reservation.pending_id}
            assert e.jobs._artifact_leases == {job.job_id: 1}

            def forbidden(*args, **kwargs):
                pytest.fail("duplicate handoff attempted physical cleanup preparation")

            with monkeypatch.context() as patch:
                patch.setattr(e.bundle.files, "prepare", forbidden)
                failure(lambda: write(
                    principal, job, reservation, receipt, canonical_bytes,
                    check_bindings_locked=check_bindings_locked,
                ), 409, "metadata_write_pending")
            assert e.bundle.index._active_writes == {reservation.pending_id}
            assert rows(e, "metadata_pending") == pending
            assert e.jobs._artifact_leases == {job.job_id: 1}
            assert path.read_bytes() == published
            duplicates.append(True)
            return read(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(e.bundle.files, "read", checked_read)
            return write(principal, job, reservation, receipt, canonical_bytes,
                         check_bindings_locked=check_bindings_locked)

    monkeypatch.setattr(e.bundle.storage, "write_reserved", checked_write)
    receipt, replayed = save(e)
    assert not replayed and duplicates == [True]
    persisted(e, receipt)
    idle(e, 1)


@pytest.mark.parametrize("change", ["owner", "malformed"])
def test_initial_failure_cannot_cleanup_an_unmatched_handoff(env, monkeypatch, change):
    e = env
    index = e.bundle.index
    write, transaction = e.bundle.storage.write_reserved, index._transaction
    rejected = []
    message = "Synthetic initial transaction failure for an unmatched handoff."

    def checked_write(principal, job, reservation, receipt, canonical_bytes, *,
                      check_bindings_locked):
        pending = rows(e, "metadata_pending")
        bad = (replace(reservation, owner="user:2") if change == "owner" else
               replace(reservation, size_bytes=str(reservation.size_bytes)))

        def fail_initial_transaction():
            patch.setattr(index, "_transaction", transaction)
            raise ApplicationError(503, "metadata_unavailable", message)

        def forbidden(*args, **kwargs):
            pytest.fail("unmatched handoff attempted physical cleanup preparation")

        with monkeypatch.context() as patch:
            patch.setattr(index, "_transaction", fail_initial_transaction)
            patch.setattr(e.bundle.files, "prepare", forbidden)
            error = failure(lambda: write(
                principal, job, bad, receipt, canonical_bytes,
                check_bindings_locked=check_bindings_locked,
            ), 503, "metadata_unavailable")
        assert error.message == message
        assert rows(e, "metadata_pending") == pending
        assert not index._active_writes
        assert e.jobs._artifact_leases == {job.job_id: 1}
        assert not list((job.working_dir / "results" / "metadata").glob("*.json"))
        assert not list((job.working_dir / "results" / "metadata").glob(".*.tmp"))
        rejected.append(True)
        return write(principal, job, reservation, receipt, canonical_bytes,
                     check_bindings_locked=check_bindings_locked)

    monkeypatch.setattr(e.bundle.storage, "write_reserved", checked_write)
    receipt, replayed = save(e)
    assert not replayed and rejected == [True]
    persisted(e, receipt)
    idle(e, 1)


def test_constructor_and_mutated_shared_objects_reject_cross_store(env, tmp_path):
    e, other = env, JobStore(tmp_path.absolute() / "other")
    try:
        failure(lambda: DocumentMetadataService(e.bundle, TypeService(other)),
                503, "metadata_unavailable")
        original = e.service.canonical.store
        e.service.canonical.store = other
        try:
            failure(lambda: save(e), 503, "metadata_unavailable")
        finally:
            e.service.canonical.store = original
        idle(e, 0)
    finally:
        other._conn.close()
