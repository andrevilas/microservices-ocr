FROM python:3.11-slim AS builder

ARG ENABLE_FALLBACK_OCR=false

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VENV_PATH=/opt/venv

WORKDIR /src

COPY pyproject.toml README.md /src/
COPY app /src/app

RUN python -m venv "$VENV_PATH" && \
    "$VENV_PATH/bin/pip" install --upgrade pip && \
    "$VENV_PATH/bin/pip" install .[ocr] && \
    if [ "$ENABLE_FALLBACK_OCR" = "true" ]; then \
      "$VENV_PATH/bin/pip" install \
        --index-url https://download.pytorch.org/whl/cpu \
        --extra-index-url https://pypi.org/simple \
        torch torchvision && \
      "$VENV_PATH/bin/pip" install .[ocr_fallback]; \
    fi

FROM python:3.11-slim

ARG ENABLE_FALLBACK_OCR=false

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OCR_TMP_DIR=/tmp/ocr-recognizer \
    MAX_UPLOAD_SIZE_MB=80 \
    ENABLE_FALLBACK_OCR=$ENABLE_FALLBACK_OCR

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    ghostscript \
    poppler-utils \
    pngquant \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY app /app/app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
