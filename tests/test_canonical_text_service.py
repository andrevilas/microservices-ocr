"""Synthetic tests for the canonical pages.jsonl service (no transports, tmp_path only)."""
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

import app.services.canonical_text_service as canonical
from app.application import ApplicationError, JobApplication, Principal
from app.results import JobReport, QualityReport, OutputValidation, FileDigest, digest_file, persist_pages, persist_report
from app.services.canonical_text_service import CanonicalTextService
from app.services.storage_service import JobStore

SCOPES = frozenset({"jobs:read", "artifacts:read", "artifacts:delete"})
OWNER = Principal("user:1", 1, SCOPES)
OTHER = Principal("user:2", 2, SCOPES)
ADMIN = Principal("user:3", 3, SCOPES | {"admin"}, is_admin=True)
PAGES = ["Olá,\r\n\"mundo\"\t\x00", "", "é 😀 " + "x" * 25000]


@pytest.fixture
def store(tmp_path):
    jobs = JobStore(tmp_path / "jobs")
    yield jobs
    jobs._conn.close()


def make_job(store, pages=PAGES, status="completed", parent=None):
    job = store.create("synthetic.pdf", b"%PDF-synthetic", owner_user_id=1)
    info = persist_pages(job.working_dir, pages, "pdf_text")
    report = JobReport(
        job_id=job.job_id, generated_at=datetime.now(timezone.utc), duration_ms=1,
        input=digest_file(job.input_pdf_path), output=FileDigest(sha256="b" * 64, size_bytes=1),
        page_count=len(pages), pages=info,
        quality=QualityReport(label="HIGH", character_count=1, valid_ratio=1.0, min_text=1, valid_ratio_threshold=0.5),
        stages=[], engines={}, fallback_attempted=False, used_fallback=False, warnings=[],
        output_validation=OutputValidation(requested_format="pdf_searchable", pdf_readable=True, page_count_matches=True))
    persist_report(job.working_dir, report)
    with store._lock:
        job.parent_job_id = parent
        store._persist(job)
    store.update(job.job_id, status=status)
    return job, report


def expected_bytes(job, report, pages=PAGES):
    lines = []
    for number, text in enumerate(pages, 1):
        payload = text.encode("utf-8")
        record = {
            "schema_version": "ocr-canonical-page/1", "normalization": "identity-utf8-v1",
            "offset_unit": "unicode_code_points", "job_id": job.job_id, "document_id": job.job_id,
            "artifact_version": 1, "text_version": 1, "page": number, "source": "pdf_text", "text": text,
            "char_start": 0, "char_end": len(text), "text_sha256": hashlib.sha256(payload).hexdigest(),
            "text_utf8_bytes": len(payload), "input_pdf_sha256": report.input.sha256,
            "output_pdf_sha256": report.output.sha256}
        lines.append(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                                allow_nan=False).encode("utf-8") + b"\n")
    return b"".join(lines)


def collect(prepared):
    return list(prepared.iter_jsonl())


def code(action):
    with pytest.raises(ApplicationError) as error:
        action()
    return error.value.status, error.value.code


def test_exact_bytes_manifest_and_reopen(store):
    job, report = make_job(store)
    expected = expected_bytes(job, report)
    service = CanonicalTextService(store)
    manifests = []
    for _ in range(2):
        with service.open(OWNER, job.job_id) as prepared:
            assert b"".join(collect(prepared)) == expected
            manifests.append(prepared.manifest)
    assert manifests[0] == manifests[1]
    manifest = manifests[0]
    assert manifest["sha256"] == hashlib.sha256(expected).hexdigest()
    assert manifest["size_bytes"] == len(expected)
    assert manifest["total_characters"] == sum(map(len, PAGES))
    assert manifest["page_count"] == 3
    assert (manifest["document_id"], manifest["artifact_version"], manifest["text_version"]) == (job.job_id, 1, 1)
    assert "parent_job_id" not in manifest
    assert str(store.root_dir) not in json.dumps(manifest)
    assert [json.loads(line)["page"] for line in expected.splitlines()] == [1, 2, 3]
    manifest["page_count"] = 99  # copies: mutation cannot alter the prepared snapshot
    with service.open(OWNER, job.job_id) as prepared:
        assert prepared.manifest["page_count"] == 3
    assert not store._artifact_leases
    assert sorted(p.name for p in (job.working_dir / "results").iterdir()).count("pages.jsonl") == 0


def test_lineage_only_from_job_and_identical_text_does_not_merge_documents(store):
    parent, _ = make_job(store)
    child, _ = make_job(store, parent=parent.job_id)
    twin, _ = make_job(store)
    service = CanonicalTextService(store)
    with service.open(OWNER, child.job_id) as prepared:
        assert prepared.manifest["parent_job_id"] == parent.job_id
        assert prepared.page_chunk(1)["document_id"] == child.job_id
    with service.open(OWNER, twin.job_id) as prepared:
        manifest = prepared.manifest
        assert manifest["document_id"] == twin.job_id != parent.job_id
        assert "parent_job_id" not in manifest
        first = json.loads(next(prepared.iter_jsonl()).splitlines()[0])
        assert (first["document_id"], first["artifact_version"], first["text_version"]) == (twin.job_id, 1, 1)


def test_long_text_unicode_and_reconstruction_by_slices(store):
    job, _ = make_job(store)
    service = CanonicalTextService(store)
    with service.open(OWNER, job.job_id) as prepared:
        rebuilt = {1: "", 2: "", 3: ""}
        page, offset = 1, 0
        seen = 0
        while page is not None:
            chunk = prepared.page_chunk(page, offset, 7)
            assert chunk["document_id"] == chunk["job_id"] == job.job_id
            assert (chunk["artifact_version"], chunk["text_version"]) == (1, 1)
            assert chunk["canonical_sha256"] == prepared.manifest["sha256"]
            assert chunk["end_offset"] == offset + len(chunk["text"])
            assert chunk["normalization"] == "identity-utf8-v1" and chunk["offset_unit"] == "unicode_code_points"
            rebuilt[chunk["page"]] += chunk["text"]
            page, offset = chunk["next_page"], chunk["next_offset"]
            seen += 1
        assert chunk["next_page"] is None and chunk["next_offset"] is None
        assert [rebuilt[n] for n in (1, 2, 3)] == PAGES
        assert len(rebuilt[3]) > 20000
        assert prepared.page_chunk(2)["next_page"] == 3 and prepared.page_chunk(2)["next_offset"] == 0
        emoji = prepared.page_chunk(3, 0, 5)
        assert emoji["text"] == "é 😀 " and len(emoji["text"]) == 5
        assert emoji["text_sha256"] == hashlib.sha256(PAGES[2].encode()).hexdigest()
        assert prepared.page_chunk(1, 0, 20000)["text"] == PAGES[0]


@pytest.mark.parametrize("args", [(0,), (4,), (True,), (1, -1), (1, 99), (1, True), (1, 0, 0), (1, 0, 20001),
                                  (1, 0, True), (1.0,), ("1",)])
def test_invalid_chunk_ranges(store, args):
    job, _ = make_job(store)
    with CanonicalTextService(store).open(OWNER, job.job_id) as prepared:
        assert code(lambda: prepared.page_chunk(*args)) == (422, "invalid_text_range")


def test_scopes_ownership_and_state(store):
    job, _ = make_job(store)
    service = CanonicalTextService(store)
    for missing in ("jobs:read", "artifacts:read"):
        principal = Principal("user:1", 1, SCOPES - {missing})
        assert code(lambda: service.open(principal, job.job_id).__enter__()) == (403, "insufficient_scope")
    assert code(lambda: service.open(OTHER, job.job_id).__enter__()) == (404, "job_not_found")
    assert code(lambda: service.open(ADMIN, job.job_id).__enter__()) == (404, "job_not_found")
    assert code(lambda: service.open(OWNER, "0" * 32).__enter__()) == (404, "job_not_found")
    for status in ("queued", "failed"):
        other, _ = make_job(store, status=status)
        assert code(lambda: service.open(OWNER, other.job_id).__enter__()) == (409, "result_not_ready")
    assert not store._artifact_leases


def test_missing_report_and_missing_or_corrupt_page_fail_before_any_yield(store):
    service = CanonicalTextService(store)
    no_report, _ = make_job(store)
    (no_report.working_dir / "results" / "report.json").unlink()
    assert code(lambda: service.open(OWNER, no_report.job_id).__enter__()) == (409, "result_unavailable")
    missing, _ = make_job(store)
    (missing.working_dir / "results" / "page-000002.txt").unlink()
    assert code(lambda: service.open(OWNER, missing.job_id).__enter__()) == (409, "result_unavailable")
    corrupt, _ = make_job(store)
    (corrupt.working_dir / "results" / "page-000001.txt").write_bytes(b"\xff\xfe")
    error = code(lambda: service.open(OWNER, corrupt.job_id).__enter__())
    assert error == (503, "result_integrity_error")
    assert not store._artifact_leases


def test_page_changed_between_passes_is_detected_before_emitting_that_line(store):
    pages = ["a" * 300_000, "second", "third"]
    job, report = make_job(store, pages)
    expected = expected_bytes(job, report, pages)
    first_line = expected.splitlines(keepends=True)[0]
    with CanonicalTextService(store).open(OWNER, job.job_id) as prepared:
        stream = prepared.iter_jsonl()
        emitted = b"".join(next(stream) for _ in range((len(first_line) + 262143) // 262144))
        assert emitted == first_line
        (job.working_dir / "results" / "page-000002.txt").write_bytes(b"SECOND")
        assert code(lambda: next(stream)) == (503, "result_integrity_error")
    assert not store._artifact_leases


def test_line_differing_from_first_pass_index_is_rejected(store, monkeypatch):
    job, _ = make_job(store, ["abc", "def"])
    real, calls = canonical.read_page, []

    def read(directory, info):
        calls.append(info.page)
        text = real(directory, info)
        return "X" + text[1:] if len(calls) > 2 and info.page == 2 else text

    monkeypatch.setattr(canonical, "read_page", read)
    with CanonicalTextService(store).open(OWNER, job.job_id) as prepared:
        assert calls == [1, 2]
        stream = prepared.iter_jsonl()
        assert len(next(stream)) > 0
        assert code(lambda: next(stream)) == (503, "result_integrity_error")
    assert calls == [1, 2, 1, 2]


def test_limits_during_accumulation_and_no_bool_or_extra_files(store, monkeypatch):
    job, _ = make_job(store, ["abcdef", "ghijkl"])
    service = CanonicalTextService(store)
    monkeypatch.setattr(canonical, "MAX_TOTAL_CHARS", 8)
    assert code(lambda: service.open(OWNER, job.job_id).__enter__()) == (422, "canonical_text_limit_exceeded")
    monkeypatch.setattr(canonical, "MAX_TOTAL_CHARS", 5_000_000)
    monkeypatch.setattr(canonical, "MAX_JSONL_BYTES", 400)
    assert code(lambda: service.open(OWNER, job.job_id).__enter__()) == (422, "canonical_text_limit_exceeded")
    assert not store._artifact_leases
    assert sorted(p.name for p in (job.working_dir / "results").iterdir()) == [
        "page-000001.txt", "page-000002.txt", "report.json"]


def test_stream_chunks_bounded_and_two_passes(store, monkeypatch):
    pages = ["é" * 400_000]
    job, report = make_job(store, pages)
    real, calls = canonical.read_page, []
    monkeypatch.setattr(canonical, "read_page", lambda d, i: calls.append(i.page) or real(d, i))
    with CanonicalTextService(store).open(OWNER, job.job_id) as prepared:
        assert calls == [1]
        chunks = collect(prepared)
        assert calls == [1, 1]
    assert len(chunks) > 3 and all(0 < len(c) <= 256 * 1024 for c in chunks)
    assert b"".join(chunks) == expected_bytes(job, report, pages)


def test_lease_released_after_error_iteration_error_and_early_exit(store):
    job, _ = make_job(store)
    service = CanonicalTextService(store)
    with pytest.raises(KeyError):
        with service.open(OWNER, job.job_id):
            assert store._artifact_leases == {job.job_id: 1}
            raise KeyError("boom")
    assert not store._artifact_leases
    with service.open(OWNER, job.job_id) as prepared:
        stream = prepared.iter_jsonl()
        next(stream)
        (job.working_dir / "results" / "page-000003.txt").write_bytes(b"x")
        with pytest.raises(ApplicationError):
            list(stream)
        assert store._artifact_leases == {job.job_id: 1}
    assert not store._artifact_leases
    other, _ = make_job(store)
    with service.open(OWNER, other.job_id) as prepared:
        stream = prepared.iter_jsonl()
        next(stream)
    assert not store._artifact_leases
    with pytest.raises(RuntimeError):
        next(stream)
    with pytest.raises(RuntimeError):
        prepared.iter_jsonl()
    with pytest.raises(RuntimeError):
        prepared.page_chunk(1)
    with pytest.raises(RuntimeError):
        prepared.manifest


def test_deletion_while_open_keeps_files_until_exit_then_410(store):
    job, report = make_job(store)
    service = CanonicalTextService(store)
    with service.open(OWNER, job.job_id) as prepared:
        receipt = JobApplication(store).delete_artifacts(OWNER, job.job_id)
        assert receipt.retention.purge_pending
        assert job.working_dir.exists()
        assert b"".join(collect(prepared)) == expected_bytes(job, report)
        assert prepared.page_chunk(1)["page"] == 1
        assert code(lambda: service.open(OWNER, job.job_id).__enter__()) == (410, "artifacts_deleted")
    assert not store._artifact_leases
    assert not job.working_dir.exists()


def test_expired_job_new_open_is_410(store):
    job, _ = make_job(store)
    with store._lock:
        job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        store._persist(job)
    assert code(lambda: CanonicalTextService(store).open(OWNER, job.job_id).__enter__()) == (410, "artifacts_expired")
    assert not store._artifact_leases
