"""Versioned result artifacts; paths are internal and never supplied by clients."""
from __future__ import annotations

import hashlib
import os
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.models import QualityLabel
from app.options import EffectiveOptions, ProcessingOptions

MAX_PAGE_CHARS = 500_000
MAX_TOTAL_CHARS = 5_000_000
MAX_REPORT_BYTES = 1_048_576


class FileDigest(BaseModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class PageInfo(BaseModel):
    page: int = Field(ge=1)
    character_count: int = Field(ge=0, le=MAX_PAGE_CHARS)
    source: Literal["pdf_text", "ocrmypdf", "easyocr"]
    digest: FileDigest


class QualityReport(BaseModel):
    label: QualityLabel
    method: Literal["token-plausibility-v1"] = "token-plausibility-v1"
    character_count: int = Field(ge=0)
    valid_ratio: float = Field(ge=0, le=1)
    min_text: int
    valid_ratio_threshold: float
    is_accuracy_measure: Literal[False] = False


class StageReport(BaseModel):
    name: Literal["primary", "fallback", "build", "finalize"]
    duration_ms: int = Field(ge=0)


class OutputValidation(BaseModel):
    requested_format: str
    pdf_readable: bool
    page_count_matches: bool
    pdfa_conformance: Literal["not_validated", "passed", "failed", "indeterminate"] = "not_validated"
    pdfa_profile: str | None = None
    pdfa_validator: str | None = None
    pdfa_validator_version: str | None = None
    pdfa_validated_sha256: str | None = None
    pdfa_reason_codes: list[str] = Field(default_factory=list)
    pdfa_stats: dict[str, int] = Field(default_factory=dict)
    compression_preset_version: str | None = None
    compression_arguments: list[str] = Field(default_factory=list)
    compression_fallback: str | None = None


class JobReport(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    job_id: str
    generated_at: datetime
    duration_ms: int = Field(ge=0)
    input: FileDigest
    output: FileDigest
    page_count: int = Field(ge=1, le=300)
    pages: list[PageInfo] = Field(max_length=300)
    quality: QualityReport
    stages: list[StageReport]
    engines: dict[str, str | None]
    fallback_attempted: bool
    used_fallback: bool
    output_validation: OutputValidation
    warnings: list[str]
    options_requested: ProcessingOptions | None = None
    options_effective: EffectiveOptions | None = None


class TextChunk(BaseModel):
    job_id: str
    page: int
    page_count: int
    source: Literal["pdf_text", "ocrmypdf", "easyocr"]
    offset: int
    text: str
    page_character_count: int
    truncated: bool
    next_page: int | None
    next_offset: int | None


def package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def digest_file(path: Path) -> FileDigest:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        while chunk := stream.read(256 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return FileDigest(sha256=digest.hexdigest(), size_bytes=size)


def page_path(directory: Path, page: int) -> Path:
    return directory / "results" / f"page-{page:06d}.txt"


def atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def persist_pages(directory: Path, pages: list[str], source: str) -> list[PageInfo]:
    if not 1 <= len(pages) <= 300 or any(len(p) > MAX_PAGE_CHARS for p in pages) or sum(map(len, pages)) > MAX_TOTAL_CHARS:
        raise ValueError("Extracted text exceeds result limits.")
    (directory / "results").mkdir(exist_ok=True)
    info = []
    for index, text in enumerate(pages, 1):
        payload = text.encode("utf-8")
        atomic_write(page_path(directory, index), payload)
        info.append(PageInfo(page=index, character_count=len(text), source=source,
                             digest=FileDigest(sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))))
    return info


def persist_report(directory: Path, report: JobReport) -> None:
    payload = report.model_dump_json().encode("utf-8")
    if len(payload) > MAX_REPORT_BYTES:
        raise ValueError("Report exceeds result limits.")
    atomic_write(directory / "results" / "report.json", payload)


def read_report(directory: Path) -> JobReport:
    with (directory / "results" / "report.json").open("rb") as stream:
        payload = stream.read(MAX_REPORT_BYTES + 1)
    if len(payload) > MAX_REPORT_BYTES:
        raise ValueError("Invalid report size.")
    report = JobReport.model_validate_json(payload)
    if len(report.pages) != report.page_count or [p.page for p in report.pages] != list(range(1, report.page_count + 1)):
        raise ValueError("Invalid page manifest.")
    return report


def read_page(directory: Path, info: PageInfo) -> str:
    with page_path(directory, info.page).open("rb") as stream:
        payload = stream.read(MAX_PAGE_CHARS * 4 + 1)
    if len(payload) != info.digest.size_bytes or hashlib.sha256(payload).hexdigest() != info.digest.sha256:
        raise ValueError("Result integrity check failed.")
    text = payload.decode("utf-8")
    if len(text) != info.character_count:
        raise ValueError("Result character count mismatch.")
    return text
