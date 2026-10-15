from pathlib import Path
from pydantic import Field

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Fluxo OCR"
    app_env: str = "development"
    static_asset_version: str = "20260702-navbar-workspace"
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
    job_retention_seconds: int = 86400
    worker_shutdown_timeout_seconds: int = 30
    user_db_path: Path | None = None
    jwt_secret: str = "CHANGE-ME-IN-PRODUCTION-use-a-strong-random-secret"
    jwt_exp_minutes: int = 480
    admin_name: str = "Admin"
    admin_email: str = "admin@localhost"
    admin_password: str = "admin"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
