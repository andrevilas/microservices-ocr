"""Regenerate these fixtures: real report.json files written by earlier production images.

  docker run --rm --network none -v <this dir>:/gen --entrypoint python localhost/ocr-recognizer:sha-<tag>     /gen/generate.py /gen <tag>        # tags used: f77349a, 596dfdd, 319a704

Synthetic one-page PDF only; "legacy" is a job without options, "options" an economic PDF/A job.
"""
import io, json, sys, tempfile
from pathlib import Path
from reportlab.pdfgen import canvas
from app.services.storage_service import JobStore
from app.services.ocr_orchestrator import OcrOrchestrator
from app.options import ProcessingOptions, EffectiveOptions, COMPRESSION_PRESET_VERSION
from app.config import settings
buf = io.BytesIO(); c = canvas.Canvas(buf); c.drawString(72, 720, "Documento sintetico 014/2026 valor 12.345,67"); c.showPage(); c.save()
out = Path(sys.argv[1]); tag = sys.argv[2]
for mode in ("legacy", "options"):
    store = JobStore(Path(tempfile.mkdtemp()) / "jobs")
    job = store.create("synthetic.pdf", buf.getvalue(), owner_user_id=1)
    if mode == "options":
        requested = ProcessingOptions(compression_profile="economic")
        resolved = requested.resolved()
        effective = EffectiveOptions(**resolved.model_dump(), engine_jobs=1, subprocess_timeout_seconds=300,
            quality_min_text=1, quality_valid_ratio_threshold=0.5, fallback_character_tolerance=0,
            compression_preset_version=COMPRESSION_PRESET_VERSION, pdfa_validation_timeout_seconds=120)
        record = store.get(job.job_id); record.options_requested, record.options_effective = requested, effective; store._persist(record)
    OcrOrchestrator(store).process_job(job.job_id)
    data = (store.get(job.job_id).working_dir / "results" / "report.json").read_bytes()
    (out / f"report-{tag}-{mode}.json").write_bytes(data)
    print(tag, mode, json.loads(data)["output_validation"]["pdfa_conformance"])
