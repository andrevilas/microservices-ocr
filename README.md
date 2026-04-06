# OCR Recognizer

MVP da sprint inicial para processamento OCR de PDFs com:

- API FastAPI para upload, status e download
- upload de PDFs de ate 80 MB
- pipeline com OCR primario e fallback opcional por baixa confianca
- retorno final em PDF/A com compactacao
- interface web simples
- armazenamento temporario em `/tmp`
- empacotamento Docker

## Execucao local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .[dev]
uvicorn app.main:app --reload
```

## Dependencias OCR opcionais

Para execucao OCR primario real, instale tambem:

```bash
pip install -e .[ocr]
```

Para habilitar o fallback com EasyOCR localmente:

```bash
pip install -e .[ocr_fallback]
```

Build Docker enxuto, apenas com OCR primario:

```bash
docker build -t ocr-recognizer:slim .
```

Build Docker com fallback EasyOCR habilitado:

```bash
docker build --build-arg ENABLE_FALLBACK_OCR=true -t ocr-recognizer:full .
```

Sem os engines externos disponiveis, a aplicacao continua executando em modo degradado para desenvolvimento e testes.
