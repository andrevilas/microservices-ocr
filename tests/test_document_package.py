"""Synthetic coverage for E4a document packages: XMP projection, fail-closed validation, integrity."""
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
import shutil
import subprocess
from types import SimpleNamespace
from zipfile import ZipFile

import pikepdf
import pytest
from reportlab.pdfgen import canvas

from app.application import ApplicationError
from app.document_models import ContextCreate, FieldSpec, TypeDraft
from app.package_models import CreatePackageInput, PackageManifest
from app.results import (
    JobReport, OutputValidation, QualityReport, digest_file, persist_pages, persist_report,
)
from app.services.context_service import ContextService
from app.services.package_service import DocumentPackageService, read_xmp
from app.services.package_xmp import extract
from app.services.pdfa_validator import PdfaValidationResult, VeraPdfValidator
from app.services.storage_service import JobStore
from test_document_metadata import runtime, who
from test_document_metadata_approval import APPROVAL_SCOPES

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")
PAGES = ["Contrato 123/2026 entre Prefeitura e Fornecedor Ação", "Página final com assinatura"]
READER = {"jobs:read", "artifacts:read", "metadata:read"}
VERAPDF = os.environ.get("VERAPDF_EXECUTABLE", "/opt/verapdf/verapdf")
REAL_TOOLS = os.path.isfile(VERAPDF) and shutil.which("ocrmypdf") is not None


def pdf_bytes(pages, xmp=False):
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer)
    for text in pages:
        c.drawString(72, 720, text)
        c.showPage()
    c.save()
    if not xmp:
        return buffer.getvalue()
    out = io.BytesIO()
    with pikepdf.open(io.BytesIO(buffer.getvalue())) as pdf:
        with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
            meta["dc:title"] = "Synthetic"
        pdf.save(out)
    return out.getvalue()


class Validator:
    """Scripted veraPDF stand-in: hashes the exact bytes it is given."""

    def __init__(self, status="passed", reasons=("compliant",), mutate=None):
        self.status, self.reasons, self.mutate, self.calls = status, reasons, mutate, []

    def __call__(self, timeout):
        self.timeout = timeout
        return self

    def validate(self, path, profile):
        self.calls.append((path, profile))
        digest = digest_file(path).sha256
        if self.mutate:
            self.mutate(path)
        return PdfaValidationResult(self.status, profile, digest, validator_version="1.30.2",
                                    reason_codes=tuple(self.reasons), stats={"failed_rules": 0})


def fields():
    return [
        FieldSpec(name="numero", type="string", required=True, description="Number", destination="xmp",
                  sensitivity="public"),
        FieldSpec(name="partes", type="string", cardinality="many", description="Parties", destination="xmp"),
        FieldSpec(name="cpf", type="string", description="Restricted", destination="xmp", sensitivity="restricted"),
        FieldSpec(name="vazio", type="string", description="Empty", destination="xmp"),
        FieldSpec(name="nota", type="string", description="Sidecar only"),
    ]


@pytest.fixture
def env(tmp_path, request):
    real = getattr(request, "param", None) == "real"
    e = SimpleNamespace(jobs=JobStore(tmp_path.absolute() / "jobs"), owner=who())
    try:
        runtime(e)
        context, _ = ContextService(e.jobs).create(e.owner, ContextCreate(name="Contratos"), "context-package-0001")
        draft, _ = e.types.create(e.owner, context.context_id, TypeDraft(
            name="Contrato", specific_fields=fields()), "type-package-0001")
        e.version, _ = e.types.publish(e.owner, draft.type_id, expected_revision=1, expected_context_revision=1,
                                       key="publish-package-0001")
        original = pdf_bytes(PAGES)
        e.job = e.jobs.create("contrato.pdf", original, owner_user_id=1)
        output = e.job.working_dir / "final-searchable-pdfa.pdf"
        if real:
            subprocess.run(["ocrmypdf", "-q", "--output-type", "pdfa-2", "--force-ocr", "-l", "eng",
                            str(e.job.input_pdf_path), str(output)], check=True, timeout=300)
        else:
            output.write_bytes(pdf_bytes(PAGES, xmp=True))
        out = digest_file(output)
        pages = persist_pages(e.job.working_dir, PAGES, "pdf_text")
        persist_report(e.job.working_dir, JobReport(
            job_id=e.job.job_id, generated_at=datetime.now(timezone.utc), duration_ms=1,
            input=digest_file(e.job.input_pdf_path), output=out, page_count=2, pages=pages,
            quality=QualityReport(label="HIGH", character_count=sum(map(len, PAGES)), valid_ratio=1.0,
                                  min_text=1, valid_ratio_threshold=0.5),
            stages=[], engines={}, fallback_attempted=False, used_fallback=False, warnings=[],
            output_validation=OutputValidation(
                requested_format="pdfa", pdf_readable=True, page_count_matches=True,
                pdfa_conformance="passed", pdfa_profile="2b", pdfa_validator="veraPDF",
                pdfa_validated_sha256=out.sha256)))
        e.jobs.update(e.job.job_id, status="completed", progress_percent=100, output_pdf_path=output)
        evidence = dict(document_id=e.job.job_id, page=1, text_sha256=hashlib.sha256(PAGES[0].encode()).hexdigest(),
                        start=9, end=17, quote="123/2026")
        values = [dict(field_name="numero", raw="123/2026", evidence=[evidence]),
                  dict(field_name="partes", raw=["Prefeitura", "Fornecedor Ação"]),
                  dict(field_name="cpf", raw="000.000.000-00"),
                  dict(field_name="nota", raw="interna")]
        e.draft, _ = e.service.save(e.owner, e.job.job_id, dict(
            expected_head=None, expected_sha256=None, segments=[dict(
                client_key="main", segment_id=None, page_start=1, page_end=2, type_id=e.version.type_id,
                type_version=e.version.version, fields=values)]), key="package-draft-0001")
        draft = e.service.get(e.owner, e.job.job_id, e.draft.revision_id, sha256=e.draft.sha256)
        e.approver = who(scopes=APPROVAL_SCOPES)
        body = dict(revision_id=e.draft.revision_id, expected_head=e.draft.revision_id,
                    expected_sha256=e.draft.sha256,
                    reviewed_fields=[dict(segment_id=s.segment_id, field_name=f.field_name)
                                     for s in draft.segments for f in s.fields if f.raw is not None])
        e.approved, _ = e.service.approve(e.approver, e.job.job_id, body, key="package-approve-0001")
        e.reader = who(scopes=READER)
        e.validator = Validator() if not real else (lambda timeout: VeraPdfValidator([VERAPDF], timeout_seconds=timeout))
        e.packages = DocumentPackageService(e.service, validator_factory=e.validator)
        yield e
    finally:
        acquired = e.jobs._lock.acquire(timeout=5)
        assert acquired
        try:
            e.jobs._conn.close()
        finally:
            e.jobs._lock.release()


def create(e, **body):
    return e.packages.create(e.reader, e.job.job_id, CreatePackageInput(**body))


def failure(action, status, code):
    with pytest.raises(ApplicationError) as caught:
        action()
    assert (caught.value.status, caught.value.code) == (status, code), caught.value.message
    return caught.value


def unzip(e, view):
    with e.packages.open_download(e.reader, e.job.job_id, view.package_id) as (size, sha256, chunks):
        data = b"".join(chunks)
    assert len(data) == size and hashlib.sha256(data).hexdigest() == sha256
    zf = ZipFile(io.BytesIO(data))
    return {name: zf.read(name) for name in zf.namelist()}


def test_package_contents_hashes_and_lineage(env):
    e = env
    receipt = create(e)
    assert receipt.replayed is False
    view = receipt.package
    assert view.state == "ready" and view.package_version == 1 and view.metadata_revision_id == e.approved.revision_id
    files = unzip(e, view)
    assert sorted(files) == ["document.pdf", "manifest.json", "metadata.json", "original.pdf", "pages.jsonl",
                             "validation.json"]
    manifest = PackageManifest.model_validate_json(files["manifest.json"])
    assert hashlib.sha256(files["manifest.json"]).hexdigest() == view.manifest_sha256
    for artifact in manifest.artifacts:
        assert hashlib.sha256(files[artifact.path]).hexdigest() == artifact.sha256
        assert len(files[artifact.path]) == artifact.size_bytes
    lineage = manifest.lineage
    output = (e.job.working_dir / "final-searchable-pdfa.pdf").read_bytes()
    assert lineage.ocr_output_pdf_sha256 == hashlib.sha256(output).hexdigest()
    assert lineage.final_pdf_sha256 == hashlib.sha256(files["document.pdf"]).hexdigest()
    assert lineage.final_pdf_sha256 != lineage.ocr_output_pdf_sha256
    assert files["original.pdf"] == e.job.input_pdf_path.read_bytes()
    assert lineage.original_pdf_sha256 == hashlib.sha256(files["original.pdf"]).hexdigest()
    assert hashlib.sha256(files["metadata.json"]).hexdigest() == e.approved.sha256 == lineage.metadata_sha256
    assert lineage.previous_package_id is None
    # The final PDF hash appears only outside the PDF.
    assert lineage.final_pdf_sha256.encode() not in files["document.pdf"]
    # Validation covers the exact packaged bytes and ran after the XMP write.
    validation = json.loads(files["validation.json"])
    assert validation["validated_sha256"] == lineage.final_pdf_sha256 and validation["status"] == "passed"
    assert len(e.validator.calls) == 1 and e.validator.calls[0][1] == "2b"
    # Sources are never modified.
    report = json.loads((e.job.working_dir / "results" / "report.json").read_text())
    assert hashlib.sha256(output).hexdigest() == report["output"]["sha256"]
    assert e.job.input_pdf_path.read_bytes() == files["original.pdf"]
    assert not e.jobs._artifact_leases


def test_xmp_projection_matches_approved_snapshot(env):
    e = env
    view = create(e).package
    files = unzip(e, view)
    with pikepdf.open(io.BytesIO(files["document.pdf"])) as pdf:
        packet = pdf.Root.Metadata.read_bytes()
        assert "/Filter" not in pdf.Root.Metadata
        with pdf.open_metadata() as meta:
            assert meta["dc:title"] == "Synthetic"  # Existing XMP is preserved.
    projection = extract(packet)
    assert projection.document_id == e.job.job_id and projection.package_version == 1
    assert projection.metadata_revision == e.approved.revision_id
    assert projection.metadata_sha256 == e.approved.sha256
    values = [(f.name, f.index, f.value) for f in projection.fields]
    assert values == [("numero", 0, "123/2026"), ("partes", 0, "Prefeitura"), ("partes", 1, "Fornecedor Ação")]
    assert b"000.000.000-00" not in files["document.pdf"] and b"interna" not in packet
    manifest = view.manifest
    assert {(x.field_name, x.reason) for x in manifest.xmp.excluded} == {("cpf", "restricted"), ("vazio", "no_value")}
    assert manifest.xmp.projected_fields == 2 and manifest.xmp.projected_values == 3


def test_idempotent_replay_and_versioning(env):
    e = env
    first = create(e).package
    again = create(e)
    assert again.replayed is True and again.package.package_id == first.package_id
    second = create(e, include_original=False).package
    assert second.package_version == 2 and second.manifest.lineage.previous_package_id == first.package_id
    assert "original.pdf" not in unzip(e, second)
    assert [p.package_id for p in e.packages.list(e.reader, e.job.job_id).items] == [first.package_id,
                                                                                      second.package_id]
    assert (e.job.working_dir / "packages" / first.package_id / "package.zip").is_file()  # Never overwritten.


@pytest.mark.parametrize("status,reasons", [("failed", ("non_compliant",)), ("indeterminate", ("validator_timeout",)),
                                            ("indeterminate", ("unsupported_platform",))])
def test_validation_failure_blocks_package(env, status, reasons):
    e = env
    e.validator.status, e.validator.reasons = status, reasons
    error = failure(lambda: create(e), 422, "package_pdfa_validation_failed")
    assert reasons[0] in error.message
    assert e.packages.list(e.reader, e.job.job_id).items == []
    assert [p.name for p in (e.job.working_dir / "packages").iterdir()] == []
    assert not e.jobs._artifact_leases


def test_unconfigured_validator_fails_closed(env):
    e = env
    e.packages = DocumentPackageService(e.service, validator_factory=lambda timeout: None)
    failure(lambda: create(e), 422, "package_pdfa_validation_failed")


def test_bytes_changed_during_validation_fail_closed(env):
    e = env
    e.validator.mutate = lambda path: path.write_bytes(path.read_bytes() + b"\n%tamper\n")
    failure(lambda: create(e), 422, "package_pdfa_validation_failed")
    assert e.packages.list(e.reader, e.job.job_id).items == []


def test_mutation_after_publication_invalidates(env):
    e = env
    view = create(e).package
    archive = e.job.working_dir / "packages" / view.package_id / "package.zip"
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 0x01
    archive.write_bytes(bytes(data))
    failure(lambda: unzip(e, view), 409, "package_invalid")
    current = e.packages.get(e.reader, e.job.job_id, view.package_id)
    assert current.state == "invalid" and current.download_path is None
    assert current.reason_codes == ["package_bytes_changed"]
    # Restoring the bytes does not silently re-validate the package.
    data[len(data) // 2] ^= 0x01
    archive.write_bytes(bytes(data))
    failure(lambda: unzip(e, view), 409, "package_invalid")
    # A new package is a new version, not an overwrite.
    assert create(e).package.package_version == 2


def test_removed_package_file_invalidates(env):
    e = env
    view = create(e).package
    (e.job.working_dir / "packages" / view.package_id / "package.zip").unlink()
    failure(lambda: unzip(e, view), 409, "package_invalid")


def test_source_mismatch_is_rejected(env):
    e = env
    output = e.job.working_dir / "final-searchable-pdfa.pdf"
    output.write_bytes(output.read_bytes() + b"\n%changed\n")
    failure(lambda: create(e), 409, "package_source_mismatch")
    assert e.validator.calls == []


def test_requires_approved_snapshot(env):
    e = env
    failure(lambda: create(e, metadata_revision_id=e.draft.revision_id), 409, "metadata_not_approved")
    failure(lambda: create(e, metadata_revision_id="f" * 32), 409, "metadata_not_approved")


def test_expiration_is_explicit_and_bounded_by_job_retention(env):
    e = env
    view = create(e).package
    job = e.jobs.get_snapshot(e.job.job_id)
    assert view.expires_at <= job.artifacts_expires_at
    with e.jobs._lock, e.jobs._get_conn():
        e.jobs._get_conn().execute("UPDATE document_packages SET expires_at=? WHERE package_id=?",
                                   ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), view.package_id))
    current = e.packages.get(e.reader, e.job.job_id, view.package_id)
    assert current.state == "expired" and current.download_path is None
    failure(lambda: unzip(e, view), 410, "package_expired")


@pytest.mark.parametrize("principal", [who("user:2", scopes=READER), who("user:2", scopes=READER, admin=True)])
def test_other_owner_cannot_see_or_create(env, principal):
    e = env
    view = create(e).package
    failure(lambda: e.packages.get(principal, e.job.job_id, view.package_id), 404, "job_not_found")
    failure(lambda: e.packages.create(principal, e.job.job_id, CreatePackageInput()), 404, "job_not_found")


@pytest.mark.parametrize("missing", sorted(READER))
def test_every_read_scope_is_required(env, missing):
    e = env
    with pytest.raises(ApplicationError) as caught:
        e.packages.create(who(scopes=READER - {missing}), e.job.job_id, CreatePackageInput())
    assert caught.value.status == 403


def test_non_pdfa_result_is_rejected(env):
    e = env
    report_path = e.job.working_dir / "results" / "report.json"
    report = json.loads(report_path.read_text())
    report["output_validation"]["pdfa_conformance"] = "not_validated"
    report_path.write_text(json.dumps(report))
    failure(lambda: create(e), 409, "package_requires_pdfa")


@pytest.mark.skipif(not REAL_TOOLS, reason="Requires OCRmyPDF and veraPDF")
@pytest.mark.parametrize("env", ["real"], indirect=True)
def test_real_verapdf_accepts_projected_xmp_after_last_write(env):
    e = env
    view = create(e).package
    files = unzip(e, view)
    assert view.manifest.validation.status == "passed"
    assert view.manifest.validation.validator_version
    assert view.manifest.validation.validated_sha256 == hashlib.sha256(files["document.pdf"]).hexdigest()
    assert [(f.name, f.value) for f in extract(read_xmp_bytes(files["document.pdf"])).fields][0] == ("numero", "123/2026")


@pytest.mark.skipif(not REAL_TOOLS, reason="Requires OCRmyPDF and veraPDF")
@pytest.mark.parametrize("env", ["real"], indirect=True)
def test_real_verapdf_rejects_undeclared_xmp(env, monkeypatch):
    e = env
    import app.services.package_service as service
    original = service.inject

    def undeclared(packet, projection):
        return original(packet, projection).replace(
            b"</rdf:RDF>", b'<rdf:Description rdf:about="" xmlns:zz="urn:zz:"><zz:x>1</zz:x></rdf:Description></rdf:RDF>')
    monkeypatch.setattr(service, "inject", undeclared)
    error = failure(lambda: create(e), 422, "package_pdfa_validation_failed")
    assert "failed" in error.message
    assert e.packages.list(e.reader, e.job.job_id).items == []


def read_xmp_bytes(data):
    with pikepdf.open(io.BytesIO(data)) as pdf:
        return pdf.Root.Metadata.read_bytes()
