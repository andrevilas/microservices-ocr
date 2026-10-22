"""C6 DTO and real canonical-runtime coverage, without transport adapters."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import os

import pytest
from pydantic import ValidationError

import app.services.canonical_text_service as canonical
import app.services.document_queries as queries_module
from app.application import ApplicationError, JobApplication
from app.document_canonical_models import CanonicalManifest, CanonicalPageChunk, CanonicalPageText
from app.results import MAX_PAGE_CHARS, MAX_TOTAL_CHARS, persist_pages, persist_report, read_report
from app.services.document_queries import DocumentQueries
from test_document_metadata import env, PAGES, QUOTE, who


CANONICAL_SCOPES = {"jobs:read", "artifacts:read"}
POSIX = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")
JID = "1" * 32
DIGEST = "a" * 64


def manifest_data():
    return dict(
        schema_version="ocr-canonical-pages/1", normalization="identity-utf8-v1",
        offset_unit="unicode_code_points", job_id=JID, document_id=JID,
        artifact_version=1, text_version=1, page_count=2, total_characters=100,
        size_bytes=1000, sha256=DIGEST, input_pdf_sha256="b" * 64,
        output_pdf_sha256="c" * 64)


def page_data(text="é 😀"):
    return dict(document_id=JID, artifact_version=1, text_version=1,
                canonical_sha256=DIGEST, page=1,
                text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(), text=text)


def chunk_data():
    text = "é 😀"
    return dict(
        job_id=JID, document_id=JID, artifact_version=1, text_version=1,
        page=1, page_count=2, text=text, offset=0, end_offset=len(text),
        page_character_count=100, text_sha256=DIGEST, canonical_sha256=DIGEST,
        normalization="identity-utf8-v1", offset_unit="unicode_code_points",
        truncated=True, next_page=1, next_offset=len(text))


@pytest.mark.parametrize("model,factory", [
    (CanonicalManifest, manifest_data), (CanonicalPageChunk, chunk_data),
    (CanonicalPageText, page_data),
])
def test_dtos_are_closed_frozen_and_require_every_source_field(model, factory):
    data = factory()
    value = model.model_validate(data, strict=True)
    assert value.model_dump(exclude_none=True) == data
    with pytest.raises(ValidationError):
        model.model_validate(dict(data, unexpected="private"), strict=True)
    with pytest.raises(ValidationError):
        value.document_id = "2" * 32
    for name in data:
        missing = dict(data)
        del missing[name]
        with pytest.raises(ValidationError):
            model.model_validate(missing, strict=True)
    for name, original in data.items():
        if type(original) is int:
            for wrong in (True, str(original), float(original)):
                with pytest.raises(ValidationError):
                    model.model_validate(dict(data, **{name: wrong}), strict=True)


@pytest.mark.parametrize("model,factory,field,wrong", [
    (CanonicalManifest, manifest_data, "job_id", "A" * 32),
    (CanonicalManifest, manifest_data, "sha256", "A" * 64),
    (CanonicalManifest, manifest_data, "input_pdf_sha256", "g" * 64),
    (CanonicalManifest, manifest_data, "output_pdf_sha256", "a" * 63),
    (CanonicalManifest, manifest_data, "schema_version", "wrong"),
    (CanonicalManifest, manifest_data, "normalization", "NFC"),
    (CanonicalManifest, manifest_data, "offset_unit", "bytes"),
    (CanonicalManifest, manifest_data, "document_id", "2" * 32),
    (CanonicalManifest, manifest_data, "parent_job_id", JID),
    (CanonicalManifest, manifest_data, "page_count", 301),
    (CanonicalManifest, manifest_data, "total_characters", MAX_TOTAL_CHARS + 1),
    (CanonicalManifest, manifest_data, "total_characters", -1),
    (CanonicalManifest, manifest_data, "size_bytes", 0),
    (CanonicalManifest, manifest_data, "size_bytes", canonical.MAX_JSONL_BYTES + 1),
    (CanonicalPageText, page_data, "text", "\ud800"),
    (CanonicalPageText, page_data, "text", b"text"),
    (CanonicalPageText, page_data, "text_sha256", DIGEST),
    (CanonicalPageText, page_data, "canonical_sha256", "a" * 63),
    (CanonicalPageText, page_data, "artifact_version", 2),
    (CanonicalPageText, page_data, "text_version", 0),
    (CanonicalPageText, page_data, "page", 0),
    (CanonicalPageChunk, chunk_data, "text", "\udfff"),
    (CanonicalPageChunk, chunk_data, "truncated", 1),
    (CanonicalPageChunk, chunk_data, "document_id", "2" * 32),
    (CanonicalPageChunk, chunk_data, "page", 3),
    (CanonicalPageChunk, chunk_data, "offset", -1),
    (CanonicalPageChunk, chunk_data, "end_offset", 1),
    (CanonicalPageChunk, chunk_data, "page_character_count", MAX_PAGE_CHARS + 1),
    (CanonicalPageChunk, chunk_data, "next_page", True),
    (CanonicalPageChunk, chunk_data, "next_offset", True),
    (CanonicalPageChunk, chunk_data, "next_page", 2),
    (CanonicalPageChunk, chunk_data, "next_offset", None),
    (CanonicalPageChunk, chunk_data, "truncated", False),
])
def test_dto_rejects_invalid_source(model, factory, field, wrong):
    with pytest.raises(ValidationError):
        model.model_validate(dict(factory(), **{field: wrong}), strict=True)


def test_dto_limits_unicode_empty_pages_and_continuations():
    data = manifest_data()
    data.update(total_characters=MAX_TOTAL_CHARS, size_bytes=canonical.MAX_JSONL_BYTES,
                parent_job_id="2" * 32, page_count=300)
    assert CanonicalManifest(**data).parent_job_id == "2" * 32
    assert CanonicalManifest(**manifest_data()).parent_job_id is None
    assert len(CanonicalPageText(**page_data("😀" * MAX_PAGE_CHARS)).text) == MAX_PAGE_CHARS
    with pytest.raises(ValidationError):
        CanonicalPageText(**page_data("x" * (MAX_PAGE_CHARS + 1)))
    data = chunk_data()
    data.update(text="😀" * canonical.MAX_CHUNK_CHARS,
                end_offset=canonical.MAX_CHUNK_CHARS,
                next_offset=canonical.MAX_CHUNK_CHARS,
                page_character_count=MAX_PAGE_CHARS)
    assert len(CanonicalPageChunk(**data).text) == canonical.MAX_CHUNK_CHARS
    data.update(text="x" * (canonical.MAX_CHUNK_CHARS + 1),
                end_offset=canonical.MAX_CHUNK_CHARS + 1,
                next_offset=canonical.MAX_CHUNK_CHARS + 1)
    with pytest.raises(ValidationError):
        CanonicalPageChunk(**data)
    data = chunk_data()
    data.update(text="", offset=MAX_PAGE_CHARS, end_offset=MAX_PAGE_CHARS,
                page_character_count=MAX_PAGE_CHARS, truncated=False,
                next_page=2, next_offset=0)
    assert CanonicalPageChunk(**data).next_page == 2
    data.update(page=2, offset=0, end_offset=0, page_character_count=0,
                next_page=None, next_offset=None,
                text_sha256=hashlib.sha256(b"").hexdigest())
    assert CanonicalPageChunk(**data).text == ""
    for changes in ({"next_page": 2}, {"next_offset": 0}, {"text_sha256": DIGEST}):
        with pytest.raises(ValidationError):
            CanonicalPageChunk(**dict(data, **changes))
    data = chunk_data()
    data.update(text="", end_offset=0, next_offset=0)
    with pytest.raises(ValidationError):
        CanonicalPageChunk(**data)
    data = chunk_data()
    data.update(page_character_count=len(data["text"]), truncated=False,
                next_page=2, next_offset=0,
                text_sha256=hashlib.sha256(data["text"].encode()).hexdigest())
    assert CanonicalPageChunk(**data).text == "é 😀"
    with pytest.raises(ValidationError):
        CanonicalPageChunk(**dict(data, text_sha256=DIGEST))
    with pytest.raises(ValidationError):
        CanonicalPageChunk(**dict(data, next_page=None, next_offset=None))


def reader():
    return who(scopes=CANONICAL_SCOPES)


def query(e, view, **kwargs):
    q = DocumentQueries(e.service)
    function = getattr(q, "get_canonical_" + view)
    if view == "page":
        kwargs.setdefault("page", 1)
    return function(reader(), e.job.job_id, **kwargs)


def failure(action, status, code):
    with pytest.raises(ApplicationError) as caught:
        action()
    error = caught.value
    assert (error.status, error.code) == (status, code)
    assert error.message == "Document metadata query failed."
    assert error.__cause__ is None and error.__context__ is None
    return error


def unlocked(e):
    acquired = e.jobs._lock.acquire(blocking=False)
    assert acquired, "canonical work acquired the shared nonreentrant SQL lock"
    e.jobs._lock.release()


def replace_pages(e, pages):
    report = read_report(e.job.working_dir)
    report.pages = persist_pages(e.job.working_dir, pages, "pdf_text")
    report.page_count = len(pages)
    report.quality.character_count = sum(map(len, pages))
    persist_report(e.job.working_dir, report)


@POSIX
def test_exact_typed_queries_without_metadata_or_catalog_access(env, monkeypatch):
    e = env
    q = DocumentQueries(e.service)
    with e.service.canonical.open(reader(), e.job.job_id) as prepared:
        expected_manifest = prepared.manifest
        expected_page = prepared.read_page_text(1)
        expected_chunks = {n: prepared.page_chunk(1, 0, n) for n in (1000, 8000, 20000)}

    def forbidden(*args, **kwargs):
        pytest.fail("canonical query consulted metadata or catalog")

    monkeypatch.setattr(type(e.bundle.files), "read", forbidden)
    monkeypatch.setattr(type(e.bundle.index), "get_head", forbidden)
    monkeypatch.setattr(type(e.types), "get_version", forbidden)
    monkeypatch.setattr(DocumentQueries, "_authorized", forbidden)
    manifest = q.get_canonical_manifest(reader(), e.job.job_id)
    assert type(manifest) is CanonicalManifest
    assert manifest.model_dump(exclude_none=True) == expected_manifest
    full = q.get_canonical_page(reader(), e.job.job_id, 1)
    assert type(full) is CanonicalPageText
    assert full.model_dump() == expected_page
    assert len(full.text) > 20000 and full.text == PAGES[0]
    assert full.text_sha256 == hashlib.sha256(full.text.encode("utf-8")).hexdigest()
    assert full.text.endswith(QUOTE)
    for n in (1000, 8000, 20000):
        kwargs = {} if n == 8000 else {"max_chars": n}
        chunk = q.get_canonical_chunk(reader(), e.job.job_id, **kwargs)
        assert type(chunk) is CanonicalPageChunk
        assert chunk.model_dump() == expected_chunks[n]
    dump = manifest.model_dump()
    dump["sha256"] = "0" * 64
    assert q.get_canonical_manifest(reader(), e.job.job_id).sha256 == expected_manifest["sha256"]
    with e.jobs._lock:
        assert e.jobs._conn.execute("SELECT count(*) FROM metadata_revisions").fetchone() == (0,)
        assert e.jobs._conn.execute("SELECT count(*) FROM metadata_pending").fetchone() == (0,)
    assert not list((e.job.working_dir / "results").glob("metadata/*.json"))
    assert not e.jobs._artifact_leases


@POSIX
def test_manifest_parent_is_taken_from_job_without_parent_access(env):
    e = env
    with e.jobs._lock:
        e.job.parent_job_id = "f" * 32
        e.jobs._persist(e.job)
    result = query(e, "manifest")
    assert result.parent_job_id == "f" * 32
    with e.service.canonical.open(reader(), e.job.job_id) as prepared:
        assert result.model_dump(exclude_none=True) == prepared.manifest


@POSIX
@pytest.mark.parametrize("view", ["manifest", "page", "chunk"])
@pytest.mark.parametrize("missing", sorted(CANONICAL_SCOPES))
def test_scopes_precede_query_syntax(env, monkeypatch, view, missing):
    e = env
    q = DocumentQueries(e.service)

    def forbidden(*args, **kwargs):
        pytest.fail("missing scope admitted canonical preparation")

    monkeypatch.setattr(canonical.CanonicalTextService, "open", forbidden)
    kwargs = {} if view == "manifest" else {"page": True}
    failure(lambda: getattr(q, "get_canonical_" + view)(
        who(scopes=CANONICAL_SCOPES - {missing}), "invalid", **kwargs),
        403, "insufficient_scope")
    assert not e.jobs._artifact_leases


@POSIX
@pytest.mark.parametrize("principal", [
    who("user:2", scopes=CANONICAL_SCOPES),
    who("user:3", scopes=CANONICAL_SCOPES, admin=True),
    who("service:" + "f" * 32, scopes=CANONICAL_SCOPES, admin=True),
])
@pytest.mark.parametrize("view", ["manifest", "page", "chunk"])
def test_existing_authority_rejects_foreign_owners(env, principal, view):
    q = DocumentQueries(env.service)
    kwargs = {"page": 1} if view == "page" else {}
    failure(lambda: getattr(q, "get_canonical_" + view)(
        principal, env.job.job_id, **kwargs), 404, "job_not_found")
    assert not env.jobs._artifact_leases


@POSIX
@pytest.mark.parametrize("view,kwargs", [
    ("page", {"page": value}) for value in (0, 301, True, "1", 1.0, None)
] + [
    ("chunk", {"page": value}) for value in (0, 301, True, "1", 1.0, None)
] + [
    ("chunk", {"offset": value}) for value in (-1, MAX_PAGE_CHARS + 1, True, "0", 0.0, None)
] + [
    ("chunk", {"max_chars": value}) for value in (0, 20001, True, "1", 1.0, None)
])
def test_query_syntax_is_strict_before_open(env, monkeypatch, view, kwargs):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid query opened canonical files")
    monkeypatch.setattr(canonical.CanonicalTextService, "open", forbidden)
    failure(lambda: query(env, view, **kwargs), 422, "invalid_text_range")
    assert not env.jobs._artifact_leases


@POSIX
@pytest.mark.parametrize("job_id", [True, 1, None, "../private", "A" * 32])
def test_job_id_is_validated_before_open(env, monkeypatch, job_id):
    q = DocumentQueries(env.service)
    def forbidden(*args, **kwargs):
        pytest.fail("invalid job ID opened canonical files")
    monkeypatch.setattr(canonical.CanonicalTextService, "open", forbidden)
    failure(lambda: q.get_canonical_manifest(reader(), job_id), 422, "invalid_metadata_query")


@POSIX
def test_code_point_reconstruction_and_document_relative_range_errors(env):
    e = env
    q = DocumentQueries(e.service)
    for page, expected in enumerate(PAGES, 1):
        pieces, offset = [], 0
        while True:
            chunk = q.get_canonical_chunk(reader(), e.job.job_id,
                                          page=page, offset=offset, max_chars=1000)
            pieces.append(chunk.text)
            assert chunk.end_offset - offset == len(chunk.text)
            if not chunk.truncated:
                break
            assert chunk.next_page == page and chunk.next_offset > offset
            offset = chunk.next_offset
        assert "".join(pieces) == expected
    tail = "".join(q.get_canonical_chunk(reader(), e.job.job_id,
                    page=1, offset=21000 + i, max_chars=1).text for i in range(len(QUOTE)))
    assert tail == QUOTE
    at_end = q.get_canonical_chunk(reader(), e.job.job_id, offset=len(PAGES[0]))
    assert at_end.text == "" and (at_end.next_page, at_end.next_offset) == (2, 0)
    for view in ("page", "chunk"):
        failure(lambda: query(e, view, page=3), 422, "invalid_text_range")
    failure(lambda: query(e, "chunk", offset=len(PAGES[0]) + 1), 422, "invalid_text_range")
    assert not e.jobs._artifact_leases


@POSIX
def test_maximum_offset_and_empty_final_page_are_valid(env):
    e = env
    replace_pages(e, ["x" * MAX_PAGE_CHARS, ""])
    assert len(query(e, "page").text) == MAX_PAGE_CHARS
    chunk = query(e, "chunk", offset=MAX_PAGE_CHARS, max_chars=1)
    assert chunk.text == "" and (chunk.next_page, chunk.next_offset) == (2, 0)
    empty = query(e, "chunk", page=2)
    assert empty.text == "" and not empty.truncated
    assert empty.next_page is None and empty.next_offset is None
    assert query(e, "page", page=2).text_sha256 == hashlib.sha256(b"").hexdigest()


@POSIX
@pytest.mark.parametrize("view", ["manifest", "page", "chunk"])
def test_io_and_dto_projection_hold_exactly_one_lease_without_sql_lock(env, monkeypatch, view):
    e, reads, projections = env, [], []
    real_read, real_project = canonical.read_page, queries_module._canonical_dto

    def checked_read(directory, info):
        unlocked(e)
        assert e.jobs._artifact_leases == {e.job.job_id: 1}
        reads.append(info.page)
        return real_read(directory, info)

    def checked_project(model, value):
        unlocked(e)
        assert e.jobs._artifact_leases == {e.job.job_id: 1}
        projections.append(model)
        return real_project(model, value)

    monkeypatch.setattr(canonical, "read_page", checked_read)
    monkeypatch.setattr(queries_module, "_canonical_dto", checked_project)
    query(e, view)
    assert reads == ([1, 2] if view == "manifest" else [1, 2, 1])
    assert projections == ([CanonicalManifest] if view == "manifest" else
                           [CanonicalManifest, CanonicalPageText if view == "page" else CanonicalPageChunk])
    assert not e.jobs._artifact_leases


SOURCE_FAULTS = [
    ("manifest", "missing"), ("manifest", "extra"), ("manifest", "wrong_job"),
    ("manifest", "bool"), ("manifest", "bad_hash"),
    ("page", "missing"), ("page", "extra"), ("page", "wrong_document"),
    ("page", "wrong_page"), ("page", "wrong_canonical"), ("page", "bad_text"),
    ("chunk", "missing"), ("chunk", "extra"), ("chunk", "wrong_document"),
    ("chunk", "wrong_page"), ("chunk", "wrong_canonical"),
    ("chunk", "wrong_count"), ("chunk", "wrong_offset"), ("chunk", "bool"),
]


@POSIX
@pytest.mark.parametrize("view,fault", SOURCE_FAULTS)
def test_invalid_source_and_cross_identity_results_are_sanitized(env, monkeypatch, view, fault):
    e = env

    def damage(data):
        data = dict(data)
        if fault == "missing":
            del data["artifact_version"]
        elif fault == "extra":
            data["private_path"] = str(e.job.working_dir)
        elif fault == "wrong_job":
            data.update(job_id="0" * 32, document_id="0" * 32)
        elif fault == "wrong_document":
            data["document_id"] = "0" * 32
            if view == "chunk":
                data["job_id"] = "0" * 32
        elif fault == "wrong_page":
            data["page"] = 2
            if view == "chunk":
                data["next_page"] = 2
        elif fault == "wrong_canonical":
            data["canonical_sha256"] = "0" * 64
        elif fault == "wrong_count":
            data["page_count"] = 3
        elif fault == "wrong_offset":
            data.update(offset=1, text=data["text"][1:])
        elif fault == "bool":
            data["artifact_version"] = True
        elif fault == "bad_hash":
            data["sha256"] = "private-invalid-hash"
        elif fault == "bad_text":
            data["text"] += "private-invalid-text"
        return data

    if view == "manifest":
        real = canonical.PreparedCanonicalText.manifest.fget
        monkeypatch.setattr(canonical.PreparedCanonicalText, "manifest",
                            property(lambda prepared: damage(real(prepared))))
    else:
        name = "read_page_text" if view == "page" else "page_chunk"
        real = getattr(canonical.PreparedCanonicalText, name)
        def changed(prepared, *args, **kwargs):
            return damage(real(prepared, *args, **kwargs))
        monkeypatch.setattr(canonical.PreparedCanonicalText, name, changed)
    error = failure(lambda: query(e, view), 503, "result_integrity_error")
    assert "private" not in str(error) and str(e.job.working_dir) not in str(error)
    assert not e.jobs._artifact_leases


@POSIX
def test_already_typed_sources_are_revalidated_and_detached(env, monkeypatch):
    e = env
    real = canonical.PreparedCanonicalText.manifest.fget
    with e.service.canonical.open(reader(), e.job.job_id) as prepared:
        original = CanonicalManifest(**real(prepared))
    monkeypatch.setattr(canonical.PreparedCanonicalText, "manifest", property(lambda prepared: original))
    result = query(e, "manifest")
    assert result == original and result is not original
    object.__setattr__(original, "artifact_version", True)
    failure(lambda: query(e, "manifest"), 503, "result_integrity_error")
    assert not e.jobs._artifact_leases


@POSIX
@pytest.mark.parametrize("view", ["manifest", "page", "chunk"])
def test_corrupt_persisted_page_has_existing_integrity_code(env, view):
    (env.job.working_dir / "results" / "page-000001.txt").write_bytes(b"\xff\xfe")
    failure(lambda: query(env, view), 503, "result_integrity_error")
    assert not env.jobs._artifact_leases


@POSIX
@pytest.mark.parametrize("status", ["queued", "failed"])
def test_noncompleted_result_preserves_source_conflict(env, status):
    env.jobs.update(env.job.job_id, status=status)
    failure(lambda: query(env, "manifest"), 409, "result_not_ready")
    assert not env.jobs._artifact_leases


@POSIX
def test_missing_result_preserves_source_unavailable(env):
    (env.job.working_dir / "results" / "report.json").unlink()
    failure(lambda: query(env, "manifest"), 409, "result_unavailable")
    assert not env.jobs._artifact_leases


@POSIX
@pytest.mark.parametrize("deleted", [False, True])
@pytest.mark.parametrize("view", ["manifest", "page", "chunk"])
def test_retention_denies_new_queries(env, deleted, view):
    e = env
    if deleted:
        JobApplication(e.jobs).delete_artifacts(e.owner, e.job.job_id)
    else:
        with e.jobs._lock:
            e.job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            e.jobs._persist(e.job)
    failure(lambda: query(e, view), 410, "artifacts_deleted" if deleted else "artifacts_expired")
    assert not e.jobs._artifact_leases


@POSIX
@pytest.mark.parametrize("deleted", [False, True])
def test_admitted_read_can_finish_after_retention_changes_but_new_query_is_denied(env, monkeypatch, deleted):
    e = env
    real = canonical.PreparedCanonicalText.read_page_text
    seen = []

    def change_retention(prepared, page):
        value = real(prepared, page)
        assert e.jobs._artifact_leases == {e.job.job_id: 1}
        if deleted:
            receipt = JobApplication(e.jobs).delete_artifacts(e.owner, e.job.job_id)
            assert receipt.retention.purge_pending
        else:
            with e.jobs._lock:
                e.job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                e.jobs._persist(e.job)
        failure(lambda: query(e, "manifest"), 410,
                "artifacts_deleted" if deleted else "artifacts_expired")
        assert e.jobs._artifact_leases == {e.job.job_id: 1}
        assert e.job.working_dir.exists()
        seen.append(True)
        return value

    monkeypatch.setattr(canonical.PreparedCanonicalText, "read_page_text", change_retention)
    assert query(e, "page").text == PAGES[0]
    assert seen == [True] and not e.jobs._artifact_leases
    if deleted:
        assert not e.job.working_dir.exists()


@POSIX
@pytest.mark.parametrize("view,last", [("manifest", 4), ("page", 5), ("chunk", 5)])
def test_checkpoint_positions_and_open_state(env, view, last):
    e, calls = env, []
    def checkpoint():
        unlocked(e)
        calls.append(dict(e.jobs._artifact_leases))
    query(e, view, checkpoint=checkpoint)
    assert calls == [{}, {}] + [{e.job.job_id: 1}] * (last - 2)
    assert not e.jobs._artifact_leases


@POSIX
@pytest.mark.parametrize("phase", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("control", ["timeout", "keyboard", "exit"])
def test_checkpoint_failures_preserve_controls_and_release_independently_tracked_lease(env, monkeypatch, phase, control):
    e, calls, leases = env, [], []
    real_lease = type(e.jobs).artifact_lease
    error = (ApplicationError(408, "query_cancelled", "private callback detail") if control == "timeout"
             else KeyboardInterrupt("private control") if control == "keyboard" else SystemExit(7))

    @contextmanager
    def tracked_lease(store, *args, **kwargs):
        with real_lease(store, *args, **kwargs):
            leases.append("enter")
            try:
                yield
            finally:
                leases.append("exit")

    def checkpoint():
        unlocked(e)
        calls.append(True)
        assert e.jobs._artifact_leases == ({} if len(calls) <= 2 else {e.job.job_id: 1})
        if len(calls) == phase:
            raise error

    monkeypatch.setattr(type(e.jobs), "artifact_lease", tracked_lease)
    if control == "timeout":
        caught = failure(lambda: query(e, "page", checkpoint=checkpoint), 408, "query_cancelled")
        assert caught is not error
    else:
        with pytest.raises(type(error)) as caught:
            query(e, "page", checkpoint=checkpoint)
        assert caught.value is error
    assert len(calls) == phase
    assert leases == ([] if phase <= 2 else ["enter", "exit"])
    assert not e.jobs._artifact_leases
    unlocked(e)
