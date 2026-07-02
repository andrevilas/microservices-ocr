from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Fluxo OCR"
    app_env: str = "development"
    static_asset_version: str = "20260702-navbar-workspace"
    max_upload_size_mb: int = 80
    job_worker_concurrency: int = 2
    ocr_tmp_dir: Path = Path("/tmp/ocr-recognizer")
    ocr_subprocess_timeout_seconds: int = 300
    max_batch_files: int = 25
    api_key: str | None = None
    upload_rate_limit_per_minute: int = 0
    quality_min_text: int = 40
    quality_valid_ratio_threshold: float = 0.7
    fallback_min_improvement_chars: int = 20
    fallback_character_tolerance: int = 20
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
