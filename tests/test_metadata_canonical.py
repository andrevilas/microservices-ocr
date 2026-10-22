"""Synthetic C4a integration tests; tmp_path only, no transports or real PDFs."""

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json

import pytest

import app.services.canonical_text_service as canonical
from app.application import ApplicationError, Principal
from app.document_models import FieldSpec, compose_schema
from app.metadata_evidence import CanonicalPageText, assess_snapshot
from app.metadata_models import (
    EvidenceRef,
    ManualFieldInput,
    MetadataSnapshot,
    ProvenanceRecord,
    SchemaSnapshot,
    SegmentRecord,
    snapshot_bytes,
)
from app.metadata_validation import normalize_manual_values
from app.results import (
    FileDigest,
    JobReport,
    OutputValidation,
    QualityReport,
    digest_file,
    persist_pages,
    persist_report,
)
from app.services.canonical_text_service import CanonicalTextService, PreparedCanonicalText
from app.services.metadata_canonical import MetadataCanonicalSource
from app.services.storage_service import JobStore


OWNER = Principal("user:1", 1, frozenset({"jobs:read", "artifacts:read"}))
LITERAL = "\r\n\x00e\u0301😀 literal"
TEXT = "x" * 25001 + LITERAL
PAGES = [TEXT, ""]


@pytest.fixture
def store(tmp_path):
    jobs = JobStore(tmp_path / "jobs")
    try:
        yield jobs
    finally:
        jobs._conn.close()


def make_job(store):
    job = store.create("synthetic.pdf", b"%PDF-synthetic", owner_user_id=1)
    pages = persist_pages(job.working_dir, PAGES, "pdf_text")
    report = JobReport(
        job_id=job.job_id,
        generated_at=datetime.now(timezone.utc),
        duration_ms=1,
        input=digest_file(job.input_pdf_path),
        output=FileDigest(sha256="b" * 64, size_bytes=1),
        page_count=len(pages),
        pages=pages,
        quality=QualityReport(
            label="HIGH", character_count=len(TEXT), valid_ratio=1.0,
            min_text=1, valid_ratio_threshold=0.5,
        ),
        stages=[], engines={}, fallback_attempted=False, used_fallback=False,
        warnings=[],
        output_validation=OutputValidation(
            requested_format="pdf_searchable", pdf_readable=True,
            page_count_matches=True,
        ),
    )
    persist_report(job.working_dir, report)
    store.update(job.job_id, status="completed")
    return job, report


def track_reads(monkeypatch):
    real = canonical.read_page
    calls = []

    def read(directory, info):
        calls.append(info.page)
        return real(directory, info)

    monkeypatch.setattr(canonical, "read_page", read)
    return calls


def evidence_snapshot(manifest, page):
    fields = [
        FieldSpec.model_validate({
            "name": name, "type": "string", "description": "Synthetic field",
            "evidence_required": True,
        }, strict=True)
        for name in ("first", "second")
    ]
    composed = compose_schema(fields, [])
    schema = SchemaSnapshot.model_validate({
        "schema_sha256": composed.sha256,
        "schema": json.loads(composed.canonical),
    }, strict=True)
    now = datetime.now(timezone.utc)
    author = ProvenanceRecord(
        actor_subject="user:1", auth_source="local_jwt", recorded_at=now,
    )
    proof = EvidenceRef(
        document_id=page.document_id,
        artifact_version=page.artifact_version,
        text_version=page.text_version,
        page=page.page,
        text_sha256=page.text_sha256,
        start=25001,
        end=len(TEXT),
        quote=LITERAL,
    )
    records = normalize_manual_values(
        fields,
        [ManualFieldInput(
            field_name=field.name, raw="interpretation", evidence=[proof, proof],
        ) for field in fields],
        author,
    )
    records = [value.model_copy(update={"status": "reviewed"}) for value in records]
    segment = SegmentRecord(
        segment_id="1" * 32, page_start=1, page_end=manifest["page_count"],
        context_id="2" * 32, type_id="3" * 32, type_version=1,
        schema_sha256=composed.sha256, fields=records,
    )
    return MetadataSnapshot(
        revision_id="4" * 32, parent_revision_id=None,
        job_id=manifest["job_id"], document_id=manifest["document_id"],
        artifact_version=manifest["artifact_version"],
        text_version=manifest["text_version"],
        canonical_sha256=manifest["sha256"],
        input_pdf_sha256=manifest["input_pdf_sha256"],
        output_pdf_sha256=manifest["output_pdf_sha256"],
        page_count=manifest["page_count"],
        created_at=now, author=author, schemas=[schema], segments=[segment],
        state="draft", approval=None,
    )


def test_full_page_identity_one_preparation_and_no_adapter_cache(store, monkeypatch):
    job, report = make_job(store)
    calls = track_reads(monkeypatch)
    with CanonicalTextService(store).open(OWNER, job.job_id) as prepared:
        assert calls == [1, 2]
        assert store._artifact_leases == {job.job_id: 1}
        source = MetadataCanonicalSource(prepared)
        assert calls == [1, 2]
        real_checked = prepared._read_checked
        checked_calls = []

        def checked(info):
            checked_calls.append(info.page)
            return real_checked(info)

        monkeypatch.setattr(prepared, "_read_checked", checked)
        full = prepared.read_page_text(1)
        assert checked_calls == [1]
        assert full == {
            "document_id": job.job_id, "artifact_version": 1, "text_version": 1,
            "canonical_sha256": prepared.manifest["sha256"], "page": 1,
            "text_sha256": report.pages[0].digest.sha256, "text": TEXT,
        }
        adapted = source(1)
        assert type(adapted) is CanonicalPageText
        assert asdict(adapted) == full
        assert source(1) == adapted
        empty = source(2)
        assert empty.text == ""
        assert empty.page == 2
        assert empty.text_sha256 == hashlib.sha256(b"").hexdigest()
        assert checked_calls == [1, 1, 1, 2]
        assert calls == [1, 2, 1, 1, 1, 2]
        assert str(job.working_dir) not in repr(full)
        assert str(job.working_dir) not in repr(adapted)
        assert store._artifact_leases == {job.job_id: 1}
    assert not store._artifact_leases


def test_real_c2_evidence_uses_full_unicode_page_and_caches_references(store, monkeypatch):
    job, report = make_job(store)
    calls = track_reads(monkeypatch)
    with CanonicalTextService(store).open(OWNER, job.job_id) as prepared:
        manifest = prepared.manifest
        # Build evidence from independently known persisted fixture text, without
        # loading a page through the adapter before the assessment.
        page = CanonicalPageText(
            document_id=job.job_id, artifact_version=1, text_version=1,
            canonical_sha256=manifest["sha256"], page=1,
            text_sha256=report.pages[0].digest.sha256, text=TEXT,
        )
        snapshot = evidence_snapshot(manifest, page)
        before = snapshot_bytes(snapshot)
        proof = snapshot.segments[0].fields[0].evidence[0]
        assert proof.start > 20000
        assert proof.end == len(TEXT)
        assert TEXT[proof.start:proof.end] == LITERAL
        source = MetadataCanonicalSource(prepared)
        assert calls == [1, 2]
        result = assess_snapshot(snapshot, source)
        assert result.approvable
        assert result.blockers == []
        assert calls == [1, 2, 1]
        assert snapshot_bytes(snapshot) == before
        assert snapshot.state == "draft" and snapshot.approval is None
        assert store._artifact_leases == {job.job_id: 1}
    assert not store._artifact_leases


def test_invalid_pages_and_closed_context_fail_before_io(store, monkeypatch):
    job, _ = make_job(store)
    calls = track_reads(monkeypatch)
    invalid = (True, False, -1, 0, 3, 1.0, "1", None)
    with CanonicalTextService(store).open(OWNER, job.job_id) as prepared:
        source = MetadataCanonicalSource(prepared)
        checked_calls = []
        real_checked = prepared._read_checked

        def checked(info):
            checked_calls.append(info.page)
            return real_checked(info)

        monkeypatch.setattr(prepared, "_read_checked", checked)
        for operation in (prepared.read_page_text, source):
            for page in invalid:
                with pytest.raises(ApplicationError) as caught:
                    operation(page)
                assert (caught.value.status, caught.value.code) == (422, "invalid_text_range")
        assert calls == [1, 2]
        assert checked_calls == []
    assert not store._artifact_leases
    for operation in (prepared.read_page_text, source):
        for page in (1, 2, *invalid):
            with pytest.raises(RuntimeError):
                operation(page)
    with pytest.raises(RuntimeError):
        MetadataCanonicalSource(prepared)
    assert calls == [1, 2]
    assert checked_calls == []


def test_manifest_captured_once_and_defensively_copied(store, monkeypatch):
    job, _ = make_job(store)
    original_property = PreparedCanonicalText.manifest
    with CanonicalTextService(store).open(OWNER, job.job_id) as prepared:
        expected = prepared.manifest
        external = prepared.manifest
        external["document_id"] = "0" * 32
        external["sha256"] = "0" * 64
        assert prepared.manifest == expected
        returned = []

        def manifest(instance):
            value = original_property.fget(instance)
            returned.append(value)
            return value

        monkeypatch.setattr(PreparedCanonicalText, "manifest", property(manifest))
        source = MetadataCanonicalSource(prepared)
        assert len(returned) == 1
        returned[0].update({
            "document_id": "0" * 32, "artifact_version": 9,
            "text_version": 9, "sha256": "0" * 64,
        })
        for _ in range(2):
            page = source(1)
            assert page.document_id == job.job_id
            assert page.artifact_version == page.text_version == 1
            assert page.canonical_sha256 == expected["sha256"]
        assert len(returned) == 1
        assert original_property.fget(prepared) == expected


def test_lease_released_on_caller_exception(store):
    job, _ = make_job(store)
    with pytest.raises(LookupError):
        with CanonicalTextService(store).open(OWNER, job.job_id) as prepared:
            source = MetadataCanonicalSource(prepared)
            assert source(1).text == TEXT
            assert store._artifact_leases == {job.job_id: 1}
            raise LookupError("synthetic caller failure")
    assert not store._artifact_leases
    with pytest.raises(RuntimeError):
        source(1)


def test_changed_page_bytes_propagate_503_and_release_lease(store):
    job, _ = make_job(store)
    with pytest.raises(ApplicationError) as caught:
        with CanonicalTextService(store).open(OWNER, job.job_id) as prepared:
            source = MetadataCanonicalSource(prepared)
            (job.working_dir / "results" / "page-000001.txt").write_bytes(
                ("y" + TEXT[1:]).encode("utf-8")
            )
            source(1)
    assert (caught.value.status, caught.value.code) == (503, "result_integrity_error")
    assert str(job.working_dir) not in str(caught.value)
    assert not store._artifact_leases


def test_changed_report_digest_fails_preparation_with_503(store):
    job, report = make_job(store)
    report.pages[0].digest.sha256 = "0" * 64
    persist_report(job.working_dir, report)
    with pytest.raises(ApplicationError) as caught:
        with CanonicalTextService(store).open(OWNER, job.job_id):
            pytest.fail("corrupt report digest admitted")
    assert (caught.value.status, caught.value.code) == (503, "result_integrity_error")
    assert str(job.working_dir) not in str(caught.value)
    assert not store._artifact_leases
