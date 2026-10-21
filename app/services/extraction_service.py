"""Assisted extraction runs (E3): queue, limits, persistence and application as a draft.

A request captures the selected canonical pages under the caller's identity and
queues one run; a single background worker talks to the configured model
runtime. Runs are proposals: they never change metadata. Applying one creates
an ordinary draft revision through DocumentMetadataService.save, so CAS,
idempotency and literal evidence checks are the same as for manual saves, and
approval still needs an explicit review by a user identity.

Persisted per run: identity, hashes, model, prompt version, token/time usage
and the checked proposals (values and short literal quotes). Prompts, model
answers and page text are never stored or logged.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from threading import Lock
import time
from uuid import uuid4

from app.application import ApplicationError, JobApplication, Principal
from app.config import settings
from app.document_models import FieldSpec
from app.extraction_models import (
    ApplyExtractionInput, CreateExtractionInput, ExtractionCandidate, ExtractionList, ExtractionReceipt,
    ExtractionUsage, ExtractionView, FieldProposal, Rejection, TypeProposal,
)
from app.metadata_models import EvidenceRef, SaveMetadataReceipt
from app.services.document_service import ExtractionApply
from app.services.extraction_engine import (
    CLASSIFY_PROMPT_VERSION, PROMPT_VERSION, ExtractionTooLarge, FieldCollector, InvalidModelOutput, Page,
    chunk_pages, classification_messages, extraction_messages, locate, parse_classification, parse_extraction,
)
from app.services.extraction_provider import AccessToken, ChatProvider, ProviderError

logger = logging.getLogger(__name__)
# Least privilege: reading runs needs metadata:read; queueing spends the model budget and
# reads the catalog; applying writes a draft (save itself checks contexts:read for new bindings).
READ_SCOPES = ("jobs:read", "artifacts:read", "metadata:read")
CREATE_SCOPES = READ_SCOPES + ("metadata:write", "contexts:read")
APPLY_SCOPES = READ_SCOPES + ("metadata:write",)
_DDL = """CREATE TABLE IF NOT EXISTS extraction_runs (
    run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, job_id TEXT NOT NULL, cache_key TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('extract','classify')),
    state TEXT NOT NULL CHECK (state IN ('queued','running','completed','failed')),
    outcome TEXT, context_id TEXT, type_id TEXT, type_version INTEGER, schema_sha256 TEXT,
    canonical_sha256 TEXT NOT NULL, page_start INTEGER NOT NULL, page_end INTEGER NOT NULL,
    model TEXT NOT NULL, prompt_version TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT,
    calls INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
    result TEXT, applied TEXT NOT NULL DEFAULT '[]')"""
_COLUMNS = ("run_id,job_id,kind,state,outcome,context_id,type_id,type_version,schema_sha256,canonical_sha256,"
            "page_start,page_end,model,prompt_version,created_at,finished_at,calls,prompt_tokens,"
            "completion_tokens,latency_ms,result,applied")
_TOKEN_CHARS = 3  # conservative chars-per-token estimate used only for the pre-call budget check


def _failed(status, code, message):
    return ApplicationError(status, code, message)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _csv(value: str) -> frozenset[str]:
    return frozenset(item.strip().lower() for item in value.split(",") if item.strip())


def _read_secret(path) -> str | None:
    if path is None:
        return None
    return path.read_text(encoding="utf-8").strip() or None


def settings_provider() -> ChatProvider | None:
    """Build the provider from server settings; None while extraction is disabled or unconfigured."""
    if not settings.extraction_enabled or not settings.extraction_base_url or not settings.extraction_model:
        return None
    token = None
    if settings.extraction_access_token_file is not None:
        data = json.loads(settings.extraction_access_token_file.read_text(encoding="utf-8"))
        token = AccessToken(str(data["client_id"]), str(data["client_secret"]))
    return ChatProvider(settings.extraction_base_url, settings.extraction_model,
                        _csv(settings.extraction_allowed_hosts), _csv(settings.extraction_access_hosts),
                        token, _read_secret(settings.extraction_api_key_file),
                        reasoning_effort=settings.extraction_reasoning_effort or None)


@dataclass(frozen=True)
class _Work:
    run_id: str
    kind: str
    pages: tuple[Page, ...]
    document_id: str
    specs: tuple[FieldSpec, ...] = ()
    candidates: tuple[dict, ...] = ()


class ExtractionService:
    """Create, list, read and apply extraction runs for the owner of a job."""

    def __init__(self, document, types, provider_factory=None, executor=None):
        self.document = document
        self.types = types
        self.jobs = document.bundle.jobs
        self.canonical = document.canonical
        self._provider_factory = provider_factory or settings_provider
        self._executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr-extraction")
        self._lock = Lock()
        self._table = False
        self._futures = {}

    # ── storage ──────────────────────────────────────────────────────────────
    def _conn(self):
        """Caller holds the JobStore lock; the table is created on first use, never by providers."""
        conn = self.jobs._get_conn()
        if not self._table:
            with conn:
                conn.execute(_DDL)
                conn.execute("CREATE INDEX IF NOT EXISTS extraction_runs_job ON extraction_runs(owner, job_id)")
                # Work lives only in memory: runs left by a previous process cannot resume.
                conn.execute("UPDATE extraction_runs SET state='failed', outcome='interrupted', finished_at=? "
                             "WHERE state IN ('queued','running')", (_now().isoformat(),))
            self._table = True
        return conn

    def _execute(self, sql, params=()):
        with self.jobs._lock:
            conn = self._conn()
            with conn:
                return conn.execute(sql, params)

    def _query(self, sql, params=()):
        with self.jobs._lock:
            return self._conn().execute(sql, params).fetchall()

    def _view(self, row) -> ExtractionView:
        record = dict(zip(_COLUMNS.split(","), row))
        result = json.loads(record["result"]) if record["result"] else {}
        return ExtractionView(
            run_id=record["run_id"], job_id=record["job_id"], kind=record["kind"], state=record["state"],
            outcome=record["outcome"], context_id=record["context_id"], type_id=record["type_id"],
            type_version=record["type_version"], schema_sha256=record["schema_sha256"],
            canonical_sha256=record["canonical_sha256"], page_start=record["page_start"],
            page_end=record["page_end"], model=record["model"], prompt_version=record["prompt_version"],
            created_at=datetime.fromisoformat(record["created_at"]),
            finished_at=datetime.fromisoformat(record["finished_at"]) if record["finished_at"] else None,
            usage=ExtractionUsage(calls=record["calls"], prompt_tokens=record["prompt_tokens"],
                                  completion_tokens=record["completion_tokens"], latency_ms=record["latency_ms"]),
            fields=[FieldProposal.model_validate(item) for item in result["fields"]] if "fields" in result else None,
            classification=(TypeProposal.model_validate(result["classification"])
                            if result.get("classification") else None),
            rejections=[Rejection.model_validate(item) for item in result.get("rejections", [])],
            applied_revision_ids=json.loads(record["applied"]))

    def _job(self, principal: Principal, job_id: str, scopes=READ_SCOPES):
        for scope in scopes:
            principal.require(scope)
        return JobApplication(self.jobs)._record(principal, job_id)

    # ── reads ────────────────────────────────────────────────────────────────
    def list(self, principal: Principal, job_id: str) -> ExtractionList:
        job = self._job(principal, job_id)
        rows = self._query(f"SELECT {_COLUMNS} FROM extraction_runs WHERE owner=? AND job_id=? ORDER BY created_at",
                           (job.effective_owner, job_id))
        return ExtractionList(items=[self._view(row) for row in rows])

    def get(self, principal: Principal, job_id: str, run_id: str) -> ExtractionView:
        job = self._job(principal, job_id)
        rows = self._query(f"SELECT {_COLUMNS} FROM extraction_runs WHERE owner=? AND job_id=? AND run_id=?",
                           (job.effective_owner, job_id, run_id))
        if not rows:
            raise _failed(404, "extraction_not_found", "Extraction run not found.")
        return self._view(rows[0])

    # ── create ───────────────────────────────────────────────────────────────
    def _target(self, principal, body: CreateExtractionInput):
        """Return (kind, specs, candidates, context_id, type_id, version, schema_sha256)."""
        if body.type_id is not None:
            if body.type_version is not None:
                version = self.types.get_version(principal, body.type_id, body.type_version)
            else:
                published = [v for v in self.types.list_versions(principal, body.type_id, limit=100).items
                             if v.lifecycle.state == "published"]
                if not published:
                    raise _failed(409, "type_version_deprecated", "The type has no published version.")
                version = self.types.get_version(principal, body.type_id, max(v.version for v in published))
            if version.lifecycle.state != "published":
                raise _failed(409, "type_version_deprecated", "The type version is deprecated.")
            specs = tuple(FieldSpec.model_validate(item, strict=True) for item in version.schema_document["fields"])
            return ("extract", specs, (), version.context_id, version.type_id, version.version,
                    version.schema_sha256)
        candidates = []
        for summary in self.types.list(principal, body.context_id, limit=100).items:
            published = [v for v in self.types.list_versions(principal, summary.type_id, limit=100).items
                         if v.lifecycle.state == "published"]
            if published:
                latest = max(published, key=lambda v: v.version)
                draft = self.types.get(principal, summary.type_id).body
                candidates.append({"type_id": summary.type_id, "version": latest.version, "name": summary.name,
                                   "description": getattr(draft, "description", "") or ""})
        if not candidates:
            raise _failed(409, "extraction_no_types", "The context has no published document type.")
        return "classify", (), tuple(candidates), body.context_id, None, None, None

    def create(self, principal: Principal, job_id: str, body: CreateExtractionInput, *, checkpoint=None
               ) -> ExtractionReceipt:
        job = self._job(principal, job_id, CREATE_SCOPES)
        owner = job.effective_owner
        provider = self._provider_factory()
        if provider is None:
            raise _failed(503, "extraction_disabled", "Assisted extraction is disabled on this server.")
        kind, specs, candidates, context_id, type_id, type_version, schema_sha256 = self._target(principal, body)
        with self.canonical.open(principal, job_id) as prepared:
            manifest = prepared.manifest
            count = manifest["page_count"]
            start, end = (body.page_start, body.page_end) if body.page_start is not None else (1, count)
            if end > count:
                raise _failed(422, "invalid_text_range", "The page range exceeds the document.")
            pages = []
            for number in range(start, end + 1):
                if checkpoint is not None:
                    checkpoint()
                page = prepared.read_page_text(number)
                pages.append(Page(number, page["text"], page["text_sha256"]))
        canonical_sha256 = manifest["sha256"]
        # The whole plan is known before queueing: too-large documents are refused, never truncated.
        try:
            chunk_pages(pages, settings.extraction_max_chunk_chars, settings.extraction_max_calls_per_run)
        except ExtractionTooLarge:
            raise _failed(413, "document_too_large",
                          "The selected text needs more model calls than allowed; select fewer pages.") from None
        prompt_version = PROMPT_VERSION if kind == "extract" else CLASSIFY_PROMPT_VERSION
        cache_key = hashlib.sha256(json.dumps({
            "owner": owner, "job": job_id, "kind": kind, "canonical": canonical_sha256, "schema": schema_sha256,
            "context": context_id if kind == "classify" else None, "candidates": list(candidates),
            "pages": [start, end], "model": provider.model, "prompt": prompt_version,
            "params": [settings.extraction_max_chunk_chars, settings.extraction_max_output_tokens, 0],
        }, sort_keys=True).encode()).hexdigest()
        with self._lock:
            rows = self._query(f"SELECT {_COLUMNS},cache_key FROM extraction_runs WHERE owner=? AND job_id=? "
                               "ORDER BY created_at", (owner, job_id))
            views = [self._view(row[:-1]) for row in rows]
            keyed = [view for view, row in zip(views, rows) if row[-1] == cache_key]
            active = [v for v in keyed if v.state in ("queued", "running")]
            if active:
                return ExtractionReceipt(run=active[-1], replayed=True)
            done = [v for v in keyed if v.state == "completed"]
            if done and not body.retry:
                return ExtractionReceipt(run=done[-1], replayed=True)
            if len(views) >= settings.extraction_max_runs_per_job:
                raise _failed(429, "extraction_limit_reached", "Extraction run limit reached for this job.")
            queued = self._query("SELECT count(*) FROM extraction_runs WHERE state IN ('queued','running')")[0][0]
            if queued >= settings.extraction_max_queued:
                raise _failed(429, "extraction_queue_full", "Too many extraction runs are waiting.")
            if self._tokens_today(owner) >= settings.extraction_owner_daily_tokens:
                raise _failed(429, "extraction_budget_exceeded", "The daily extraction token budget is exhausted.")
            run_id = uuid4().hex
            self._execute(
                "INSERT INTO extraction_runs (run_id,owner,job_id,cache_key,kind,state,context_id,type_id,"
                "type_version,schema_sha256,canonical_sha256,page_start,page_end,model,prompt_version,created_at) "
                "VALUES (?,?,?,?,?,'queued',?,?,?,?,?,?,?,?,?,?)",
                (run_id, owner, job_id, cache_key, kind, context_id, type_id, type_version, schema_sha256,
                 canonical_sha256, start, end, provider.model, prompt_version, _now().isoformat()))
            work = _Work(run_id, kind, tuple(pages), job_id, specs, candidates)
            self._futures[run_id] = self._executor.submit(self._process, work, provider, owner)
        return ExtractionReceipt(run=self.get(principal, job_id, run_id), replayed=False)

    def _tokens_today(self, owner) -> int:
        midnight = _now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        return self._query("SELECT coalesce(sum(prompt_tokens+completion_tokens),0) FROM extraction_runs "
                           "WHERE owner=? AND created_at>=?", (owner, midnight))[0][0]

    def wait(self, run_id: str, timeout: float | None = None) -> None:
        """Test and operator helper: block until the run's worker task finishes."""
        future = self._futures.get(run_id)
        if future is not None:
            future.result(timeout)

    # ── worker ───────────────────────────────────────────────────────────────
    def _process(self, work: _Work, provider: ChatProvider, owner: str) -> None:
        usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "latency_ms": 0}
        self._execute("UPDATE extraction_runs SET state='running' WHERE run_id=?", (work.run_id,))
        outcome, result = "completed", None
        try:
            result = self._run(work, provider, owner, usage)
            if work.kind == "extract" and all(f["status"] == "not_found" for f in result["fields"]):
                outcome = "abstained"
            if work.kind == "classify" and result.get("classification") is None:
                outcome = "abstained"
        except ProviderError as error:
            outcome = error.code
        except InvalidModelOutput:
            outcome = "invalid_output"
        except _Stop as stop:
            outcome = stop.code
        except Exception:
            outcome = "provider_error"
            logger.error("Extraction run failed unexpectedly", exc_info=False)
        state = "completed" if outcome in ("completed", "abstained") else "failed"
        self._execute(
            "UPDATE extraction_runs SET state=?, outcome=?, finished_at=?, calls=?, prompt_tokens=?, "
            "completion_tokens=?, latency_ms=?, result=? WHERE run_id=?",
            (state, outcome, _now().isoformat(), usage["calls"], usage["prompt_tokens"],
             usage["completion_tokens"], usage["latency_ms"],
             json.dumps(result, ensure_ascii=False) if result is not None and state == "completed" else None,
             work.run_id))
        logger.info("Extraction run %s finished: %s calls=%d tokens=%d", work.run_id, outcome, usage["calls"],
                    usage["prompt_tokens"] + usage["completion_tokens"])

    def _call(self, provider, messages, owner, usage, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            raise _Stop("run_timeout")
        estimate = sum(len(m["content"]) for m in messages) // _TOKEN_CHARS + settings.extraction_max_output_tokens
        if self._tokens_today(owner) + usage["prompt_tokens"] + usage["completion_tokens"] + estimate \
                > settings.extraction_owner_daily_tokens:
            raise _Stop("budget_exceeded")
        completion = provider.complete(messages, max_tokens=settings.extraction_max_output_tokens,
                                       timeout_seconds=min(settings.extraction_call_timeout_seconds, remaining))
        usage["calls"] += 1
        usage["prompt_tokens"] += completion.prompt_tokens
        usage["completion_tokens"] += completion.completion_tokens
        usage["latency_ms"] += completion.latency_ms
        return completion.content

    def _run(self, work: _Work, provider, owner, usage) -> dict:
        deadline = time.monotonic() + settings.extraction_run_timeout_seconds
        pages = {page.number: page for page in work.pages}
        chunks = chunk_pages(list(work.pages), settings.extraction_max_chunk_chars,
                             settings.extraction_max_calls_per_run)

        def ref(located, index=None):
            page = pages[located.page]
            return EvidenceRef(document_id=work.document_id, page=located.page, text_sha256=page.text_sha256,
                               start=located.start, end=located.end, quote=located.quote, item_index=index)

        if work.kind == "classify":
            by_id = {c["type_id"]: c for c in work.candidates}
            rejections = {}
            for chunk in chunks:
                answer = parse_classification(self._call(
                    provider, classification_messages(list(work.candidates), chunk), owner, usage, deadline))
                if answer.type_id is None:
                    continue
                if answer.type_id not in by_id:
                    rejections["unknown_type"] = rejections.get("unknown_type", 0) + 1
                    continue
                located = locate(answer.quote, answer.page, pages, [p.page for p in chunk])
                if located is None:
                    rejections["quote_not_found"] = rejections.get("quote_not_found", 0) + 1
                    continue
                chosen = by_id[answer.type_id]
                return {"classification": {"type_id": chosen["type_id"], "type_version": chosen["version"],
                                           "type_name": chosen["name"],
                                           "evidence": [ref(located).model_dump(mode="json")]},
                        "rejections": [{"reason": r, "count": n} for r, n in sorted(rejections.items())]}
            return {"classification": None,
                    "rejections": [{"reason": r, "count": n} for r, n in sorted(rejections.items())]}

        collector = FieldCollector(list(work.specs), pages)
        collector.add_deterministic()
        for chunk in chunks:
            content = self._call(provider, extraction_messages(list(work.specs), chunk), owner, usage, deadline)
            collector.add_model_output(parse_extraction(content), [piece.page for piece in chunk])
        fields = []
        for item in collector.results():
            fields.append(FieldProposal(
                field_name=item.field_name, status=item.status, raw=item.raw, normalized=item.normalized,
                evidence=[ref(loc, index) for loc, index in item.evidence],
                candidates=[ExtractionCandidate(raw=c.raw, normalized=c.normalized,
                                                evidence=[ref(loc) for loc in c.evidence],
                                                methods=sorted(set(c.method.split("+"))))
                            for c in item.candidates],
                methods=item.methods).model_dump(mode="json"))
        return {"fields": fields, "rejections": collector.rejection_list()}

    # ── apply ────────────────────────────────────────────────────────────────
    def apply(self, principal: Principal, job_id: str, run_id: str, body: ApplyExtractionInput, *, key,
              checkpoint=None) -> tuple[SaveMetadataReceipt, bool]:
        self._job(principal, job_id, APPLY_SCOPES)
        run = self.get(principal, job_id, run_id)
        if run.kind != "extract" or run.state != "completed" or run.fields is None:
            raise _failed(409, "extraction_not_applicable", "Only completed field extractions can be applied.")
        with self.canonical.open(principal, job_id) as prepared:
            if prepared.manifest["sha256"] != run.canonical_sha256:
                raise _failed(409, "extraction_source_changed", "The canonical text changed after this run.")
        head = None
        if body.expected_head is not None:
            with self.document.open_snapshot(principal, job_id, body.expected_head,
                                             sha256=body.expected_sha256, checkpoint=checkpoint) as snapshot:
                head = snapshot
        segments, target = [], None
        for index, segment in enumerate(head.segments if head is not None else ()):
            key_name = f"s{index}"
            same = ((segment.page_start, segment.page_end, segment.type_id, segment.type_version)
                    == (run.page_start, run.page_end, run.type_id, run.type_version))
            if same:
                target = key_name
            elif segment.page_start <= run.page_end and run.page_start <= segment.page_end:
                raise _failed(409, "extraction_segment_conflict",
                              "The run's pages overlap a segment of another type or range.")
            segments.append({"client_key": key_name, "segment_id": segment.segment_id,
                             "page_start": segment.page_start, "page_end": segment.page_end,
                             "type_id": segment.type_id, "type_version": segment.type_version, "fields": []})
        if target is None:
            target = "extracted"
            segments.append({"client_key": target, "segment_id": None, "page_start": run.page_start,
                             "page_end": run.page_end, "type_id": run.type_id, "type_version": run.type_version,
                             "fields": []})
            segments.sort(key=lambda s: s["page_start"])
        request = {"expected_head": body.expected_head, "expected_sha256": body.expected_sha256,
                   "segments": segments}
        receipt, replayed = self.document.save(
            principal, job_id, request, key=key, checkpoint=checkpoint,
            extraction=ExtractionApply(run_id, target, tuple(run.fields)))
        if not replayed:
            with self._lock:
                applied = json.loads(self._query("SELECT applied FROM extraction_runs WHERE run_id=?",
                                                 (run_id,))[0][0])
                self._execute("UPDATE extraction_runs SET applied=? WHERE run_id=?",
                              (json.dumps(applied + [receipt.revision_id]), run_id))
        return receipt, replayed


class _Stop(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code
