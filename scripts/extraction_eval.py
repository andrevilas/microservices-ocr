"""Field-level evaluation of assisted extraction (E3) on the synthetic corpus.

Runs the same engine as the service (chunking, prompts, literal evidence and
grounding checks) against a configured local model runtime and reports, per
field and per split: precision, recall, correct abstentions, conflicts,
rejection reasons, latency and tokens. Only synthetic documents are used.

Usage:
  python scripts/extraction_eval.py --base-url http://127.0.0.1:11434/v1 --model qwen2.5:7b-instruct \
      --allowed-host 127.0.0.1 [--split eval] [--out report.json]
  Access route: add --access-host HOST --access-token-file FILE (JSON client_id/client_secret).
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.document_models import FieldSpec  # noqa: E402
from app.document_values import normalize_value  # noqa: E402
from app.services.extraction_engine import (  # noqa: E402
    FieldCollector, InvalidModelOutput, Page, chunk_pages, extraction_messages, parse_extraction,
)
from app.services.extraction_provider import AccessToken, ChatProvider, ProviderError  # noqa: E402

CORPUS = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "extraction_corpus.json"


def gold_value(spec: FieldSpec, value):
    if value is None or value == "__conflict__":
        return value
    if spec.cardinality == "many":
        return [normalize_value(spec, item) for item in value]
    return normalize_value(spec, value)


def same(spec: FieldSpec, proposed, gold) -> bool:
    """Strings compare case- and space-insensitively: the gold styling is arbitrary, the content is not."""
    def fold(value):
        return " ".join(value.casefold().split()) if type(value) is str else value
    if spec.type in ("string", "identifier"):
        if type(proposed) is list and type(gold) is list:
            return [fold(v) for v in proposed] == [fold(v) for v in gold]
        return fold(proposed) == fold(gold)
    return proposed == gold


def evaluate(provider, corpus, split, max_chunk_chars, max_calls, max_tokens, timeout):
    fields, documents = {}, []
    totals = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "latency_ms": 0, "failed_documents": 0}
    for doc in corpus["documents"]:
        if split != "all" and doc["split"] != split:
            continue
        specs = [FieldSpec.model_validate(item) for item in corpus["types"][doc["type"]]]
        pages = [Page(i + 1, text, hashlib.sha256(text.encode()).hexdigest()) for i, text in enumerate(doc["pages"])]
        collector = FieldCollector(specs, {p.number: p for p in pages})
        collector.add_deterministic()
        started, outcome = time.monotonic(), "completed"
        try:
            for chunk in chunk_pages(pages, max_chunk_chars, max_calls):
                completion = provider.complete(extraction_messages(specs, chunk), max_tokens=max_tokens,
                                               timeout_seconds=timeout)
                totals["calls"] += 1
                totals["prompt_tokens"] += completion.prompt_tokens
                totals["completion_tokens"] += completion.completion_tokens
                totals["latency_ms"] += completion.latency_ms
                collector.add_model_output(parse_extraction(completion.content), [p.page for p in chunk])
        except (ProviderError, InvalidModelOutput) as error:
            outcome = str(error)
            totals["failed_documents"] += 1
        results = {r.field_name: r for r in collector.results()} if outcome == "completed" else {}
        row = {"id": doc["id"], "outcome": outcome, "seconds": round(time.monotonic() - started, 1), "fields": {}}
        for spec in specs:
            gold = gold_value(spec, doc["gold"].get(spec.name))
            result = results.get(spec.name)
            status = result.status if result else "failed"
            proposed = result.normalized if result else None
            if gold == "__conflict__":
                verdict = "tp" if status == "conflict" else ("fp" if status == "needs_review" else "fn")
            elif gold is None:
                verdict = "tn" if status in ("not_found", "failed") else ("conflict" if status == "conflict" else "fp")
            elif status == "needs_review":
                verdict = "tp" if same(spec, proposed, gold) else "fp"
            elif status == "conflict":
                verdict = "conflict"
            else:
                verdict = "fn"
            stats = fields.setdefault(f"{doc['type']}.{spec.name}",
                                      {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "conflict": 0})
            stats[verdict] += 1
            row["fields"][spec.name] = {"verdict": verdict, "status": status, "proposed": proposed, "gold": gold}
        row["rejections"] = collector.rejection_list()
        documents.append(row)
    summary = {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "conflict": 0}
    for stats in fields.values():
        for key in summary:
            summary[key] += stats[key]
        stats["precision"] = round(stats["tp"] / (stats["tp"] + stats["fp"]), 3) if stats["tp"] + stats["fp"] else None
        stats["recall"] = (round(stats["tp"] / (stats["tp"] + stats["fn"] + stats["conflict"]), 3)
                           if stats["tp"] + stats["fn"] + stats["conflict"] else None)
    proposed = summary["tp"] + summary["fp"]
    expected = summary["tp"] + summary["fn"] + summary["conflict"]
    summary["precision"] = round(summary["tp"] / proposed, 3) if proposed else None
    summary["recall"] = round(summary["tp"] / expected, 3) if expected else None
    return {"split": split, "model": provider.model, "summary": summary, "usage": totals, "fields": fields,
            "documents": documents}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--allowed-host", action="append", default=[])
    parser.add_argument("--access-host", action="append", default=[])
    parser.add_argument("--access-token-file", type=Path)
    parser.add_argument("--split", choices=["dev", "eval", "all"], default="eval")
    parser.add_argument("--max-chunk-chars", type=int, default=12000)
    parser.add_argument("--max-calls", type=int, default=6)
    parser.add_argument("--max-tokens", type=int, default=1500)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    token = None
    if args.access_token_file:
        data = json.loads(args.access_token_file.read_text(encoding="utf-8"))
        token = AccessToken(data["client_id"], data["client_secret"])
    provider = ChatProvider(args.base_url, args.model, frozenset(h.lower() for h in args.allowed_host),
                            frozenset(h.lower() for h in args.access_host), token)
    report = evaluate(provider, json.loads(CORPUS.read_text(encoding="utf-8")), args.split, args.max_chunk_chars,
                      args.max_calls, args.max_tokens, args.timeout)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(json.dumps({"summary": report["summary"], "usage": report["usage"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
