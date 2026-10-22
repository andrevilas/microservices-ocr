"""Synthetic, model-validated OpenAPI examples for document packages; never real documents."""
from datetime import datetime, timezone

from app.package_models import (
    PackageArtifact, PackageLineage, PackageList, PackageManifest, PackageReceipt, PackageValidation,
    PackageView, PackageXmp, XmpExclusion,
)

_JOB, _PACKAGE, _REVISION, _SEGMENT = "1" * 32, "2" * 32, "3" * 32, "4" * 32
_AT = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
_EXPIRES = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def _view() -> PackageView:
    artifacts = [
        PackageArtifact(path="document.pdf", role="pdf", media_type="application/pdf", sha256="a" * 64,
                        size_bytes=48213),
        PackageArtifact(path="original.pdf", role="original", media_type="application/pdf", sha256="b" * 64,
                        size_bytes=30112),
        PackageArtifact(path="metadata.json", role="metadata", media_type="application/json", sha256="c" * 64,
                        size_bytes=2048),
        PackageArtifact(path="pages.jsonl", role="pages", media_type="application/x-ndjson", sha256="d" * 64,
                        size_bytes=1024),
        PackageArtifact(path="validation.json", role="validation", media_type="application/json",
                        sha256="e" * 64, size_bytes=310),
    ]
    validation = PackageValidation(status="passed", profile="2b", validator="veraPDF", validator_version="1.30.2",
                                   validated_sha256="a" * 64, validated_at=_AT, reason_codes=["compliant"])
    manifest = PackageManifest(
        package_id=_PACKAGE, package_version=1, created_at=_AT, expires_at=_EXPIRES,
        lineage=PackageLineage(
            job_id=_JOB, document_id=_JOB, metadata_revision_id=_REVISION, metadata_sha256="c" * 64,
            schema_sha256=["f" * 64], original_pdf_sha256="b" * 64, ocr_output_pdf_sha256="9" * 64,
            final_pdf_sha256="a" * 64, canonical_sha256="d" * 64),
        xmp=PackageXmp(projected_fields=2, projected_values=3,
                       excluded=[XmpExclusion(segment_id=_SEGMENT, field_name="cpf", reason="restricted")]),
        validation=validation, artifacts=artifacts)
    return PackageView(
        package_id=_PACKAGE, job_id=_JOB, package_version=1, state="ready", created_at=_AT, expires_at=_EXPIRES,
        metadata_revision_id=_REVISION, size_bytes=83104, sha256="8" * 64, manifest_sha256="7" * 64,
        download_path=f"/api/v1/jobs/{_JOB}/packages/{_PACKAGE}/content", manifest=manifest)


def package_view_example() -> dict:
    return _view().model_dump(mode="json")


def package_receipt_example(replayed=False) -> dict:
    return PackageReceipt(package=_view(), replayed=replayed).model_dump(mode="json")


def package_list_example() -> dict:
    return PackageList(items=[_view()]).model_dump(mode="json")
