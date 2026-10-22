"""Local document packages (E4a): PDF/A + XMP projection, validated after the last write.

Build order is fixed: bind sources by hash, write XMP, read it back, validate the
exact final bytes with veraPDF, hash, then assemble. Nothing writes to the PDF
after validation; any verdict other than ``passed`` aborts the package. Packages
are versioned per job, never overwritten, and live inside the job's working
directory so artifact retention removes them with the job.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from threading import Lock
from uuid import uuid4
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from lxml import etree
import pikepdf

from app.application import ApplicationError, JobApplication, Principal
from app.config import settings
from app.metadata_models import MetadataSnapshot, snapshot_bytes
from app.package_models import (
    PackageArtifact, PackageLineage, PackageList, PackageManifest, PackageReceipt,
    PackageValidation, PackageView, PackageXmp, XmpExclusion, CreatePackageInput, artifact_role,
)
from app.retention_models import retention_info
from app.results import digest_file
from app.services.pdf_signatures import inspect_signatures
from app.services.package_xmp import FIELD_TYPE, PROPERTIES, XmpField, XmpProjection, declared_properties, extract, inject
from app.services.pdfa_validator import PROFILE_2B, REASON_NOT_CONFIGURED, build_validator, unavailable_result

MIB = 1024 * 1024
VALIDATION_TIMEOUT_SECONDS = 90
MAX_XMP_BYTES = 4 * MIB
CHUNK = 256 * 1024
_READ_SCOPES = ("jobs:read", "artifacts:read", "metadata:read")
_MEDIA = {"document.pdf": "application/pdf", "original.pdf": "application/pdf",
          "metadata.json": "application/json", "pages.jsonl": "application/x-ndjson",
          "validation.json": "application/json"}
_DDL = """CREATE TABLE IF NOT EXISTS document_packages (
    package_id TEXT PRIMARY KEY, owner TEXT NOT NULL, job_id TEXT NOT NULL,
    package_version INTEGER NOT NULL, metadata_revision_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('ready','invalid')),
    created_at TEXT NOT NULL, expires_at TEXT NOT NULL, size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL, manifest_sha256 TEXT NOT NULL, reason_codes TEXT NOT NULL,
    manifest TEXT NOT NULL, UNIQUE(job_id, package_version))"""


def _failed(status, code, message):
    return ApplicationError(status, code, message)


def _canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _xmp_text(value) -> str:
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if type(value) is str:
        return value
    raise ValueError("Unsupported metadata scalar.")


def project(snapshot: MetadataSnapshot, package_version: int, canonical_sha256: str,
            metadata_sha256: str) -> tuple[XmpProjection, list[XmpExclusion], int]:
    """Project fields whose published spec says destination=xmp; restricted ones never enter the PDF."""
    specs = {schema.schema_sha256: schema.schema_document["fields"] for schema in snapshot.schemas}
    fields, excluded, projected = [], [], 0
    for segment in snapshot.segments:
        records = {record.field_name: record for record in segment.fields}
        # Published schema order keeps the projection deterministic; every
        # xmp-destined field is either projected or listed as excluded.
        for spec in specs[segment.schema_sha256]:
            if spec.get("destination", "sidecar") != "xmp":
                continue
            name, record = spec["name"], records.get(spec["name"])
            if spec.get("sensitivity", "internal") == "restricted":
                excluded.append(XmpExclusion(segment_id=segment.segment_id, field_name=name, reason="restricted"))
                continue
            if record is None or record.normalized is None:
                excluded.append(XmpExclusion(segment_id=segment.segment_id, field_name=name, reason="no_value"))
                continue
            values = record.normalized if isinstance(record.normalized, list) else [record.normalized]
            projected += 1
            fields.extend(XmpField(segment.segment_id, name, index, _xmp_text(value))
                          for index, value in enumerate(values))
    return (XmpProjection(snapshot.document_id, package_version, snapshot.revision_id, metadata_sha256,
                          snapshot.input_pdf_sha256, canonical_sha256, tuple(fields)),
            excluded, projected)


def write_xmp(source: Path, target: Path, projection: XmpProjection) -> None:
    """Copy source to target with the projection added to its existing XMP packet."""
    with pikepdf.open(source) as pdf:
        metadata = pdf.Root.get("/Metadata")
        if not isinstance(metadata, pikepdf.Stream):
            raise _failed(422, "package_xmp_missing", "The PDF/A result has no XMP metadata stream.")
        try:
            packet = inject(_bounded(metadata.read_bytes()), projection)
        except (ValueError, etree.LxmlError):
            raise _failed(422, "package_xmp_invalid", "The PDF/A result has unusable XMP metadata.") from None
        pdf.Root.Metadata = pikepdf.Stream(pdf, packet, Type=pikepdf.Name.Metadata, Subtype=pikepdf.Name.XML)
        pdf.save(target, fix_metadata_version=False)


def read_xmp(path: Path) -> bytes:
    with pikepdf.open(path) as pdf:
        metadata = pdf.Root.get("/Metadata")
        if not isinstance(metadata, pikepdf.Stream) or "/Filter" in metadata:
            raise _failed(422, "package_xmp_invalid", "Packaged XMP stream is missing or filtered.")
        return _bounded(metadata.read_bytes())


def _bounded(packet: bytes) -> bytes:
    if len(packet) > MAX_XMP_BYTES:
        raise _failed(422, "package_xmp_invalid", "XMP metadata exceeds the size limit.")
    return packet


def _settings_validator(timeout_seconds):
    command = str(settings.verapdf_executable) if settings.verapdf_executable else None
    return build_validator(command, timeout_seconds)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class DocumentPackageService:
    """Create, list and read versioned packages for the owner of a job."""

    def __init__(self, document, validator_factory=None):
        self.document = document
        self.jobs = document.bundle.jobs
        self.canonical = document.canonical
        self._validator_factory = validator_factory or _settings_validator
        self._building: set[str] = set()
        self._lock = Lock()
        self._table = False

    def _conn(self):
        """Caller holds the JobStore lock; the table is created on first use, never by providers."""
        conn = self.jobs._get_conn()
        if not self._table:
            with conn:
                conn.execute(_DDL)
            self._table = True
        return conn

    # ── reads ────────────────────────────────────────────────────────────────
    def _job(self, principal: Principal, job_id: str):
        for scope in _READ_SCOPES:
            principal.require(scope)
        return JobApplication(self.jobs)._record(principal, job_id)

    def _rows(self, owner, job_id):
        with self.jobs._lock:
            return self._conn().execute(
                "SELECT package_id,package_version,metadata_revision_id,state,created_at,expires_at,size_bytes,"
                "sha256,manifest_sha256,reason_codes,manifest FROM document_packages "
                "WHERE owner=? AND job_id=? ORDER BY package_version", (owner, job_id)).fetchall()

    def _view(self, job, row) -> PackageView:
        (package_id, version, revision_id, state, created_at, expires_at, size, sha256,
         manifest_sha256, reasons, manifest) = row
        expires = datetime.fromisoformat(expires_at)
        retained = retention_info(job).state == "retained"
        if state == "ready" and (expires <= datetime.now(timezone.utc) or not retained):
            state = "expired"
        return PackageView(
            package_id=package_id, job_id=job.job_id, package_version=version, state=state,
            created_at=datetime.fromisoformat(created_at), expires_at=expires, metadata_revision_id=revision_id,
            size_bytes=size, sha256=sha256, manifest_sha256=manifest_sha256, reason_codes=json.loads(reasons),
            download_path=(f"/api/v1/jobs/{job.job_id}/packages/{package_id}/content"
                           if state == "ready" else None),
            manifest=PackageManifest.model_validate_json(manifest))

    def list(self, principal: Principal, job_id: str) -> PackageList:
        job = self._job(principal, job_id)
        return PackageList(items=[self._view(job, row) for row in self._rows(job.effective_owner, job_id)])

    def get(self, principal: Principal, job_id: str, package_id: str) -> PackageView:
        job = self._job(principal, job_id)
        for row in self._rows(job.effective_owner, job_id):
            if row[0] == package_id:
                return self._view(job, row)
        raise _failed(404, "package_not_found", "Package not found.")

    def _invalidate(self, package_id, reason):
        with self.jobs._lock:
            conn = self._conn()
            with conn:
                conn.execute("UPDATE document_packages SET state='invalid', reason_codes=? WHERE package_id=?",
                             (json.dumps([reason]), package_id))

    @contextmanager
    def open_download(self, principal: Principal, job_id: str, package_id: str):
        """Yield (size, sha256, chunks) after re-hashing the stored ZIP under an artifact lease."""
        job = self._job(principal, job_id)
        with self.jobs.artifact_lease(job_id, job.effective_owner) as leased:
            view = self.get(principal, job_id, package_id)
            if view.state == "expired":
                raise _failed(410, "package_expired", "Package expired or its job artifacts were removed.")
            if view.state != "ready":
                raise _failed(409, "package_invalid", "Package failed an integrity check and cannot be served.")
            path = leased.working_dir / "packages" / package_id / "package.zip"
            try:
                actual = digest_file(path)
            except OSError:
                actual = None
            if actual is None or actual.sha256 != view.sha256 or actual.size_bytes != view.size_bytes:
                self._invalidate(package_id, "package_bytes_changed")
                raise _failed(409, "package_invalid", "Package failed an integrity check and cannot be served.")

            def chunks():
                with path.open("rb") as source:
                    while chunk := source.read(CHUNK):
                        yield chunk
            yield view.size_bytes, view.sha256, chunks()

    # ── create ───────────────────────────────────────────────────────────────
    def create(self, principal: Principal, job_id: str, body: CreatePackageInput, *, checkpoint=None) -> PackageReceipt:
        job = self._job(principal, job_id)
        owner = job.effective_owner
        if (not body.include_original and job.input_pdf_path.exists()
                and inspect_signatures(job.input_pdf_path).signed is not False):
            # The derivative cannot carry the signature: the signed bytes must travel unchanged with it.
            raise _failed(409, "signed_original_required",
                          "The original PDF is digitally signed (or could not be inspected) and must be included.")
        revisions = self.document.list_revisions(principal, job_id)
        approved = [r for r in revisions if r.state == "approved"]
        if body.metadata_revision_id is not None:
            approved = [r for r in approved if r.revision_id == body.metadata_revision_id]
            if not approved:
                raise _failed(409, "metadata_not_approved", "The requested revision is not an approved snapshot.")
        if not approved:
            raise _failed(409, "metadata_not_approved", "The document has no approved metadata snapshot.")
        revision = max(approved, key=lambda r: r.created_at)
        rows = self._rows(owner, job_id)
        for row in rows:
            view = self._view(job, row)
            if (view.metadata_revision_id == revision.revision_id and view.state == "ready"
                    and (body.include_original == any(a.path == "original.pdf" for a in view.manifest.artifacts))):
                return PackageReceipt(package=view, replayed=True)
        if len(rows) >= settings.package_max_per_job:
            raise _failed(429, "package_limit_reached", "Package version limit reached for this job.")
        with self._lock:
            if job_id in self._building:
                raise _failed(409, "package_build_in_progress", "Another package is being built for this job.")
            self._building.add(job_id)
        try:
            return self._build(principal, job_id, owner, revision, body, rows, checkpoint)
        finally:
            with self._lock:
                self._building.discard(job_id)

    @staticmethod
    def _checkpoint(checkpoint):
        if checkpoint is not None:
            checkpoint()

    def _build(self, principal, job_id, owner, revision, body, rows, checkpoint):
        package_id = uuid4().hex
        with self.document.open_snapshot(principal, job_id, revision.revision_id, sha256=revision.sha256) as snapshot, \
                self.canonical.open(principal, job_id) as prepared:
            job, report = JobApplication(self.jobs)._result(principal, job_id)
            validation = report.output_validation
            if validation is None or validation.pdfa_conformance != "passed" or job.output_pdf_path is None:
                raise _failed(409, "package_requires_pdfa", "Packages require a completed, validated PDF/A result.")
            output, original = digest_file(job.output_pdf_path), digest_file(job.input_pdf_path)
            canonical = prepared.manifest
            if not (output.sha256 == report.output.sha256 == snapshot.output_pdf_sha256
                    == validation.pdfa_validated_sha256
                    and original.sha256 == report.input.sha256 == snapshot.input_pdf_sha256
                    and canonical["sha256"] == snapshot.canonical_sha256):
                raise _failed(409, "package_source_mismatch", "Package sources do not match the approved snapshot.")
            packages = job.working_dir / "packages"
            staging = packages / f".staging-{package_id}"
            staging.mkdir(parents=True)
            try:
                result = self._assemble(staging, job, report, snapshot, revision, prepared, canonical, output,
                                        original, package_id, rows, body, checkpoint)
                self._checkpoint(checkpoint)
                return self._publish(staging, packages / package_id, owner, job, result)
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    def _assemble(self, staging, job, report, snapshot, revision, prepared, canonical, output, original,
                  package_id, rows, body, checkpoint):
        version = 1 + max((row[1] for row in rows), default=0)
        previous = rows[-1][0] if rows else None
        metadata = snapshot_bytes(snapshot)
        if hashlib.sha256(metadata).hexdigest() != revision.sha256:
            raise _failed(409, "package_source_mismatch", "Metadata snapshot bytes changed.")
        (staging / "metadata.json").write_bytes(metadata)
        digest = hashlib.sha256()
        with (staging / "pages.jsonl").open("wb") as target:
            for chunk in prepared.iter_jsonl():
                digest.update(chunk)
                target.write(chunk)
        if digest.hexdigest() != canonical["sha256"]:
            raise _failed(409, "package_source_mismatch", "Canonical text changed while packaging.")
        if body.include_original:
            shutil.copyfile(job.input_pdf_path, staging / "original.pdf")
        self._checkpoint(checkpoint)

        projection, excluded, projected = project(snapshot, version, canonical["sha256"], revision.sha256)
        pdf = staging / "document.pdf"
        write_xmp(job.output_pdf_path, pdf, projection)  # The last write to the PDF.
        packet = read_xmp(pdf)
        try:
            consistent = (extract(packet) == projection
                          and declared_properties(packet) == {n: t for n, t, _ in PROPERTIES})
        except (ValueError, etree.LxmlError):
            consistent = False
        if not consistent:
            raise _failed(422, "package_xmp_inconsistent", "Written XMP does not match the approved snapshot.")
        self._checkpoint(checkpoint)

        # Bounded below the shared document execution deadline (120 s).
        timeout = min(settings.verapdf_timeout_seconds, VALIDATION_TIMEOUT_SECONDS)
        validator = self._validator_factory(timeout)
        before = digest_file(pdf)
        verdict = (validator.validate(pdf, PROFILE_2B) if validator is not None
                   else unavailable_result(REASON_NOT_CONFIGURED, before.sha256))
        final = digest_file(pdf)
        if verdict.status != "passed" or verdict.sha256 != final.sha256 or before.sha256 != final.sha256:
            reasons = ",".join(verdict.reason_codes) or "unknown"
            raise _failed(422, "package_pdfa_validation_failed",
                          f"Packaged PDF/A validation {verdict.status}: {reasons}.")
        now = datetime.now(timezone.utc)
        validation = PackageValidation(
            status="passed", profile=PROFILE_2B, validator="veraPDF", validator_version=verdict.validator_version,
            validated_sha256=final.sha256, validated_at=now, reason_codes=list(verdict.reason_codes),
            stats={k: v for k, v in dict(verdict.stats).items() if type(v) is int})
        (staging / "validation.json").write_bytes(_canonical_json(validation.model_dump(mode="json")))

        artifacts = []
        for name in ("document.pdf", "original.pdf", "metadata.json", "pages.jsonl", "validation.json"):
            path = staging / name
            if path.exists():
                d = digest_file(path)
                artifacts.append(PackageArtifact(path=name, role=artifact_role(name), media_type=_MEDIA[name],
                                                 sha256=d.sha256, size_bytes=d.size_bytes))
        if artifacts[0].sha256 != final.sha256:
            raise _failed(409, "package_source_mismatch", "Packaged PDF changed after validation.")
        expires = now + timedelta(seconds=settings.package_ttl_seconds)
        if job.artifacts_expires_at is not None:
            expires = min(expires, job.artifacts_expires_at)
        manifest = PackageManifest(
            package_id=package_id, package_version=version, created_at=now, expires_at=expires,
            lineage=PackageLineage(
                job_id=job.job_id, document_id=snapshot.document_id, parent_job_id=job.parent_job_id,
                previous_package_id=previous, metadata_revision_id=revision.revision_id,
                metadata_sha256=revision.sha256, schema_sha256=[s.schema_sha256 for s in snapshot.schemas],
                original_pdf_sha256=original.sha256, ocr_output_pdf_sha256=output.sha256,
                final_pdf_sha256=final.sha256, canonical_sha256=canonical["sha256"]),
            xmp=PackageXmp(projected_fields=projected, projected_values=len(projection.fields), excluded=excluded),
            validation=validation, artifacts=artifacts)
        manifest_bytes = _canonical_json(manifest.model_dump(mode="json"))
        (staging / "manifest.json").write_bytes(manifest_bytes)

        archive = staging / "package.zip"
        stamp = now.timetuple()[:6]
        with ZipFile(archive, "w", ZIP_STORED) as zf:
            for name in [a.path for a in artifacts] + ["manifest.json"]:
                info = ZipInfo(name, date_time=stamp)
                info.compress_type = ZIP_STORED
                with (staging / name).open("rb") as source, zf.open(info, "w", force_zip64=True) as sink:
                    shutil.copyfileobj(source, sink, CHUNK)
        packed = digest_file(archive)
        if packed.size_bytes > settings.package_max_total_mb * MIB:
            raise _failed(413, "package_too_large", "Package exceeds the configured size limit.")
        if digest_file(pdf).sha256 != final.sha256:
            raise _failed(409, "package_source_mismatch", "Packaged PDF changed after validation.")
        return manifest, hashlib.sha256(manifest_bytes).hexdigest(), packed

    def _publish(self, staging, target, owner, job, result):
        manifest, manifest_sha256, packed = result
        keep = {"package.zip", "manifest.json"}
        for path in staging.iterdir():
            if path.name not in keep:
                path.unlink()
        with self.jobs._lock:
            conn = self._conn()
            with conn:  # Commits on success; any failure below rolls the row back.
                self._insert(conn, staging, target, owner, job, manifest, manifest_sha256, packed)
        view = self._view(job, (manifest.package_id, manifest.package_version, manifest.lineage.metadata_revision_id,
                                "ready", _iso(manifest.created_at), _iso(manifest.expires_at), packed.size_bytes,
                                packed.sha256, manifest_sha256, "[]", manifest.model_dump_json()))
        return PackageReceipt(package=view, replayed=False)

    @staticmethod
    def _insert(conn, staging, target, owner, job, manifest, manifest_sha256, packed):
        latest = conn.execute("SELECT COALESCE(MAX(package_version),0) FROM document_packages WHERE job_id=?",
                              (job.job_id,)).fetchone()[0]
        if latest + 1 != manifest.package_version:
            raise _failed(409, "package_version_conflict", "A concurrent package was published; retry.")
        conn.execute("INSERT INTO document_packages VALUES (?,?,?,?,?,'ready',?,?,?,?,?,?,?)", (
            manifest.package_id, owner, job.job_id, manifest.package_version,
            manifest.lineage.metadata_revision_id, _iso(manifest.created_at), _iso(manifest.expires_at),
            packed.size_bytes, packed.sha256, manifest_sha256, "[]", manifest.model_dump_json()))
        # Still inside the transaction: a failed rename rolls the row back.
        os.replace(staging, target)
