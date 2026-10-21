from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    rag_config_file: Path | None = None
    app_name: str = "Fluxo OCR"
    app_env: str = "development"
    static_asset_version: str = "20261016-contexts-u2a-i"
    browser_public_origin: str | None = None

    max_upload_size_mb: int = Field(default=80, ge=1)
    upload_ttl_seconds: int = Field(default=3600, ge=60)
    upload_storage_limit_mb: int = Field(default=2048, ge=1)
    upload_owner_limit_mb: int = Field(default=512, ge=1)
    upload_min_free_mb: int = Field(default=256, ge=0)
    upload_concurrency: int = Field(default=2, ge=1, le=16)
    upload_owner_max_reservations: int = Field(default=32, ge=1)
    upload_timeout_seconds: int = Field(default=300, ge=1)
    upload_max_pages: int = Field(default=300, ge=1)
    upload_max_page_pixels: int = Field(default=50000000, ge=1)
    upload_validation_memory_mb: int = Field(default=256, ge=64)
    upload_validation_timeout_seconds: int = Field(default=15, ge=1)
    job_owner_max_pending: int = Field(default=20, ge=1)
    job_worker_concurrency: int = 2
    ocr_tmp_dir: Path = Path("/tmp/ocr-recognizer")
    ocr_subprocess_timeout_seconds: int = Field(default=300, ge=1)
    ocr_engine_jobs: int = Field(default=1, ge=1, le=8)
    max_batch_files: int = 25
    batch_max_total_mb: int = Field(default=256, ge=1, le=2048)
    export_max_total_mb: int = Field(default=256, ge=1, le=2048)
    export_storage_limit_mb: int = Field(default=2048, ge=1)
    export_owner_limit_mb: int = Field(default=512, ge=1)
    export_ttl_seconds: int = Field(default=3600, ge=60)
    export_owner_max_pending: int = Field(default=4, ge=1, le=32)
    export_max_pending: int = Field(default=32, ge=1, le=256)
    api_key: str | None = None
    upload_rate_limit_per_minute: int = 0
    quality_min_text: int = Field(default=40, ge=0)
    quality_valid_ratio_threshold: float = Field(default=0.7, ge=0, le=1)
    fallback_min_improvement_chars: int = 20
    fallback_character_tolerance: int = Field(default=20, ge=0)
    final_output_type: str = "pdfa"
    final_pdf_optimize_level: int = 3
    final_pdfa_image_compression: str = "jpeg"
    final_pdf_jpeg_quality: int = 75
    # Server-only absolute path of the veraPDF executable; never taken from requests.
    verapdf_executable: Path | None = None
    verapdf_timeout_seconds: int = Field(default=120, ge=1, le=3600)
    job_retention_seconds: int = Field(default=86400, ge=1)
    # Document packages (E4a) never outlive their job artifacts.
    package_ttl_seconds: int = Field(default=86400, ge=60)
    package_max_total_mb: int = Field(default=512, ge=1, le=4096)
    package_max_per_job: int = Field(default=20, ge=1, le=200)
    # Assisted extraction (E3). Off by default; the endpoint must be the owner's own
    # model runtime (decisions 2026-10-21): a private-network host, or a hostname
    # behind the owner's Cloudflare Tunnel + Access with a service token.
    extraction_enabled: bool = False
    extraction_base_url: str | None = None
    extraction_model: str | None = None
    extraction_allowed_hosts: str = ""
    extraction_access_hosts: str = ""
    # JSON {"client_id": ..., "client_secret": ...}; server-only file, never in env or argv.
    extraction_access_token_file: Path | None = None
    extraction_api_key_file: Path | None = None
    extraction_call_timeout_seconds: int = Field(default=120, ge=5, le=900)
    extraction_run_timeout_seconds: int = Field(default=600, ge=10, le=3600)
    extraction_max_chunk_chars: int = Field(default=12000, ge=1000, le=200000)
    extraction_max_calls_per_run: int = Field(default=6, ge=1, le=50)
    extraction_max_output_tokens: int = Field(default=1500, ge=64, le=16384)
    extraction_owner_daily_tokens: int = Field(default=400000, ge=1000)
    extraction_max_runs_per_job: int = Field(default=20, ge=1, le=200)
    extraction_max_queued: int = Field(default=8, ge=1, le=100)
    retention_interval_seconds: int = Field(default=60, ge=1, le=3600)
    worker_shutdown_timeout_seconds: int = 30
    user_db_path: Path | None = None
    jwt_secret: str = "CHANGE-ME-IN-PRODUCTION-use-a-strong-random-secret"
    jwt_exp_minutes: int = 480
    admin_name: str = "Admin"
    admin_email: str = "admin@localhost"
    admin_password: str = "admin"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("browser_public_origin")
    @classmethod
    def validate_browser_public_origin(cls, value):
        if value is None:
            return None
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment or "?" in value or "#" in value
                or "*" in value or any(c.isspace() for c in value)
                or parsed.port == 0):
            raise ValueError("BROWSER_PUBLIC_ORIGIN must be one HTTPS origin without credentials, path, query or fragment")
        return f"https://{parsed.netloc.lower()}".removesuffix(":443")


settings = Settings()
