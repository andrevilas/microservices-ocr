"""Closed, immutable processing contracts. No client-supplied commands or paths."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Language = Literal["eng", "por"]
Mode = Literal["preserve_existing_text", "extract_text"]
OutputFormat = Literal["pdf_searchable", "pdfa"]
Compression = Literal["none", "economic", "balanced", "preservation"]
Fallback = Literal["disabled", "if_low_quality"]
# Bumped whenever the OCRmyPDF arguments behind a compression preset change.
COMPRESSION_PRESET_VERSION = "2026-10-16.1"
# Offered for new jobs only after benchmarks/presets homologated them for this preset version
# (docs/preset-homologation.md). economic failed readability on color scans and was withdrawn; it stays
# in Compression so reports and options written while it was offered remain readable.
HOMOLOGATED_COMPRESSION: tuple[Compression, ...] = ("none", "balanced", "preservation")
WITHDRAWN_COMPRESSION: tuple[Compression, ...] = ("economic",)


class ProcessingOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    languages: tuple[Language, ...] | None = Field(default=None, max_length=2)
    processing_mode: Mode = "preserve_existing_text"
    output_format: OutputFormat | None = None
    compression_profile: Compression | None = None
    fallback_policy: Fallback | None = None

    @field_validator("languages")
    @classmethod
    def canonical_languages(cls, value):
        if value is not None and len(set(value)) != len(value):
            raise ValueError("Duplicate languages.")
        return tuple(sorted(value)) if value is not None else None

    def resolved(self) -> "ResolvedOptions":
        extract = self.processing_mode == "extract_text"
        return ResolvedOptions(
            languages=self.languages if self.languages is not None else (() if extract else ("eng",)),
            processing_mode=self.processing_mode,
            output_format=self.output_format or ("pdf_searchable" if extract else "pdfa"),
            compression_profile=self.compression_profile or ("none" if extract else "balanced"),
            fallback_policy=self.fallback_policy or "disabled",
        )


class ResolvedOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    languages: tuple[Language, ...]
    processing_mode: Mode
    output_format: OutputFormat
    compression_profile: Compression
    fallback_policy: Fallback


class EffectiveOptions(ResolvedOptions):
    engine_jobs: int = Field(ge=1, le=8)
    subprocess_timeout_seconds: int = Field(ge=1)
    quality_min_text: int = Field(ge=0)
    quality_valid_ratio_threshold: float = Field(ge=0, le=1)
    fallback_character_tolerance: int = Field(ge=0)
    # None for jobs created before presets were versioned.
    compression_preset_version: str | None = None
    pdfa_validation_timeout_seconds: int | None = Field(default=None, ge=1)


class EngineCapability(BaseModel):
    available: bool
    version: str | None = None
    reasons: list[str] = Field(default_factory=list)


class Capabilities(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    engines: dict[str, EngineCapability]
    languages: list[Language]
    processing_modes: list[Mode]
    output_formats: list[OutputFormat]
    compression_profiles: list[Compression]
    fallback_policies: list[Fallback]
    fallback_language_sets: list[list[Language]]
    defaults: ResolvedOptions
    compression_preset_version: str = COMPRESSION_PRESET_VERSION
    # No preset is offered as experimental any more; kept so existing clients still find the field.
    experimental_compression_profiles: list[Compression] = Field(default_factory=list)
    withdrawn_compression_profiles: list[Compression] = Field(default_factory=lambda: list(WITHDRAWN_COMPRESSION))
    pdfa_profile: Literal["2b"] = "2b"
    limits: dict[str, int]
    probe_cache_seconds: int = 60
    warnings: list[str]
