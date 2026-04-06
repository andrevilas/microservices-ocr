from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "OCR Recognizer"
    app_env: str = "development"
    max_upload_size_mb: int = 80
    ocr_tmp_dir: Path = Path("/tmp/ocr-recognizer")
    quality_min_text: int = 40
    quality_valid_ratio_threshold: float = 0.7
    fallback_min_improvement_chars: int = 20
    final_output_type: str = "pdfa"
    final_pdf_optimize_level: int = 3
    final_pdfa_image_compression: str = "jpeg"
    final_pdf_jpeg_quality: int = 75

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
