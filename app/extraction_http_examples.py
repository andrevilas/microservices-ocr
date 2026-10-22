"""Synthetic, model-validated OpenAPI examples for extraction runs; never real documents."""
from datetime import datetime, timezone

from app.extraction_models import (
    ExtractionCandidate, ExtractionList, ExtractionReceipt, ExtractionUsage, ExtractionView, FieldProposal, Rejection,
)
from app.metadata_models import EvidenceRef

_JOB, _RUN, _TYPE, _CONTEXT = "1" * 32, "2" * 32, "5" * 32, "6" * 32
_AT = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
_DONE = datetime(2026, 10, 21, 12, 1, tzinfo=timezone.utc)


def _ref(page, start, quote):
    return EvidenceRef(document_id=_JOB, page=page, text_sha256="d" * 64, start=start, end=start + len(quote),
                       quote=quote)


def _view() -> ExtractionView:
    fields = [
        FieldProposal(field_name="numero", status="needs_review", raw="123/2026", normalized="123/2026",
                      evidence=[_ref(1, 0, "CONTRATO Nº 123/2026")], methods=["llm"]),
        FieldProposal(field_name="data_assinatura", status="conflict", candidates=[
            ExtractionCandidate(raw="2026-03-15", normalized="2026-03-15", evidence=[_ref(1, 58, "Data: 15/03/2026")],
                                methods=["llm"]),
            ExtractionCandidate(raw="2026-03-16", normalized="2026-03-16", evidence=[_ref(2, 31, "em 16/03/2026")],
                                methods=["llm"])], methods=["llm"]),
        FieldProposal(field_name="objeto", status="not_found"),
    ]
    return ExtractionView(
        run_id=_RUN, job_id=_JOB, kind="extract", state="completed", outcome="completed", context_id=_CONTEXT,
        type_id=_TYPE, type_version=1, schema_sha256="f" * 64, canonical_sha256="d" * 64, page_start=1, page_end=2,
        model="qwen3:14b", prompt_version="ocr-extract/1", created_at=_AT, finished_at=_DONE,
        usage=ExtractionUsage(calls=1, prompt_tokens=1840, completion_tokens=212, latency_ms=9400), fields=fields,
        rejections=[Rejection(field_name="objeto", reason="quote_not_found", count=1)])


def extraction_view_example() -> dict:
    return _view().model_dump(mode="json")


def extraction_receipt_example(replayed=False) -> dict:
    view = _view() if replayed else _view().model_copy(update={
        "state": "queued", "outcome": None, "finished_at": None, "fields": None, "rejections": [],
        "usage": ExtractionUsage(calls=0, prompt_tokens=0, completion_tokens=0, latency_ms=0)})
    return ExtractionReceipt(run=view, replayed=replayed).model_dump(mode="json")


def extraction_list_example() -> dict:
    return ExtractionList(items=[_view()]).model_dump(mode="json")
