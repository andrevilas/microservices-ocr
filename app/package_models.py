"""Contract models for local document packages (E4a): ocr-document-package/1.

The manifest lives outside the PDF and carries every hash; the PDF never
references its own final bytes. Two PDF hashes are deliberately distinct:
ocr_output_pdf_sha256 is the validated OCR result bound by the metadata
snapshot and pages.jsonl, final_pdf_sha256 is the packaged PDF after XMP.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

ID = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
CONTRACT_VERSION = "ocr-document-package/1"
XMP_SCHEMA = "https://ocr.andre.goiania.br/ns/ocrdoc/1.0/"

PackageFile = Literal["document.pdf", "original.pdf", "metadata.json", "pages.jsonl", "validation.json"]
_ROLES = {"document.pdf": "pdf", "original.pdf": "original", "metadata.json": "metadata",
          "pages.jsonl": "pages", "validation.json": "validation"}


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PackageArtifact(_Closed):
    path: PackageFile
    role: Literal["pdf", "original", "metadata", "pages", "validation"]
    media_type: Literal["application/pdf", "application/json", "application/x-ndjson"]
    sha256: SHA256
    size_bytes: int = Field(ge=0)


class PackageLineage(_Closed):
    job_id: ID
    document_id: ID
    parent_job_id: ID | None = None
    previous_package_id: ID | None = None
    metadata_revision_id: ID
    metadata_sha256: SHA256
    schema_sha256: list[SHA256] = Field(min_length=1, max_length=100)
    original_pdf_sha256: SHA256
    ocr_output_pdf_sha256: SHA256
    final_pdf_sha256: SHA256
    canonical_sha256: SHA256


class XmpExclusion(_Closed):
    segment_id: ID
    field_name: str = Field(min_length=1, max_length=200)
    reason: Literal["restricted", "no_value"]


class PackageXmp(_Closed):
    schema_namespace: Literal["https://ocr.andre.goiania.br/ns/ocrdoc/1.0/"] = XMP_SCHEMA
    projected_fields: int = Field(ge=0)
    projected_values: int = Field(ge=0)
    excluded: list[XmpExclusion] = Field(default_factory=list, max_length=10000)
    consistent: Literal[True] = True


class PackageValidation(_Closed):
    """validation.json: the veraPDF verdict over the exact packaged PDF bytes."""
    schema_version: Literal["ocr-package-validation/1"] = "ocr-package-validation/1"
    status: Literal["passed"]
    profile: Literal["2b"]
    validator: Literal["veraPDF"]
    validator_version: str | None = Field(default=None, max_length=100)
    validated_sha256: SHA256
    validated_at: datetime
    reason_codes: list[str] = Field(default_factory=list, max_length=50)
    stats: dict[str, int] = Field(default_factory=dict)


class PackageManifest(_Closed):
    contract_version: Literal["ocr-document-package/1"] = CONTRACT_VERSION
    package_id: ID
    package_version: int = Field(ge=1)
    created_at: datetime
    expires_at: datetime
    lineage: PackageLineage
    xmp: PackageXmp
    validation: PackageValidation
    artifacts: list[PackageArtifact] = Field(min_length=4, max_length=5)


class CreatePackageInput(_Closed):
    metadata_revision_id: ID | None = Field(
        default=None, description="Approved revision to package; the latest approved one when omitted.")
    include_original: bool = Field(default=True, description="Embed the original input PDF unchanged.")


class PackageView(_Closed):
    package_id: ID
    job_id: ID
    package_version: int = Field(ge=1)
    state: Literal["ready", "invalid", "expired"]
    created_at: datetime
    expires_at: datetime
    metadata_revision_id: ID
    size_bytes: int = Field(ge=0)
    sha256: SHA256 = Field(description="SHA-256 of package.zip.")
    manifest_sha256: SHA256
    reason_codes: list[str] = Field(default_factory=list, max_length=50)
    download_path: str | None = Field(default=None, description="Relative path; still requires the bearer identity.")
    manifest: PackageManifest


class PackageReceipt(_Closed):
    package: PackageView
    replayed: bool


class PackageList(_Closed):
    items: list[PackageView] = Field(max_length=200)


def artifact_role(path: str) -> str:
    return _ROLES[path]
