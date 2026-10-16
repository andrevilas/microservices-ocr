"""V1a foundation: pure catalog DTOs, lazy store-following provider and the bounded JSON reader (synthetic data)."""
import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError
from starlette.requests import Request

from app import catalog_models
from app.application import ApplicationError
from app.config import settings
from app.routes.uploads import json_input
from app.services import context_service, type_service
from app.services.catalog_runtime import CatalogProvider, CatalogServices, get_catalog_services

ID = "0" * 31 + "1"
NOW = "2026-10-16T00:00:00+00:00"
CONTEXT = {"name": "Contexto", "common_fields": [{"name": "numero", "type": "identifier", "description": "Numero."}]}
TYPE = {"name": "Tipo", "specific_fields": []}


# -- pure DTO module
def test_catalog_models_imports_without_storage_or_runtime():
    root = str(Path(__file__).resolve().parents[1])
    # -I ignores PYTHONPATH and cwd, so the candidate root is inserted explicitly before the import.
    code = ("import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import app.catalog_models\n"
            "bad = [m for m in ('app.application', 'app.services.storage_service', 'app.services.job_queue',\n"
            "                   'app.services.catalog_runtime', 'app.services.context_service', 'app.main')\n"
            "       if m in sys.modules]\n"
            "assert not bad, bad")
    result = subprocess.run([sys.executable, "-I", "-c", code, root], capture_output=True, text=True,
                            cwd=root, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ["ContextReceipt", "ContextSummary", "ContextRecord", "ContextPage"])
def test_context_dtos_are_reexported(name):
    assert getattr(context_service, name) is getattr(catalog_models, name)


@pytest.mark.parametrize("name", ["TypeReceipt", "TypeSummary", "TypeRecord", "TypePage", "TypeVersionSummary",
                                  "TypeVersionRecord", "TypeVersionPage"])
def test_type_dtos_are_reexported(name):
    assert getattr(type_service, name) is getattr(catalog_models, name)


def _version_record():
    return catalog_models.TypeVersionRecord(
        type_id=ID, context_id=ID, version=1, draft_revision=1, context_revision=1, type_name="T", context_name="C",
        schema_version="1", schema_sha256="a" * 64, created_at=NOW, schema={"fields": []})


def test_schema_alias_is_the_only_wire_name():
    record = _version_record()
    assert record.schema_document == {"fields": []}
    dumped = record.model_dump(mode="json", by_alias=True)
    assert "schema" in dumped and "schema_document" not in dumped
    properties = catalog_models.TypeVersionRecord.model_json_schema(by_alias=True)["properties"]
    assert "schema" in properties and "schema_document" not in properties


@pytest.mark.parametrize("model,payload", [
    (catalog_models.ContextUpdateInput, {"body": CONTEXT, "expected_revision": 1}),
    (catalog_models.TypeUpdateInput, {"body": TYPE, "expected_revision": 1}),
    (catalog_models.TypePublishInput, {"expected_revision": 1, "expected_context_revision": 2}),
])
def test_inputs_accept_valid_and_refuse_extras_and_loose_revisions(model, payload):
    assert model.model_validate(payload)
    with pytest.raises(ValidationError):
        model.model_validate({**payload, "owner": "someone"})
    revisions = [key for key in payload if key.endswith("revision")]
    for key in revisions:
        for bad in (True, "1", 1.0, 0, 2**31):
            with pytest.raises(ValidationError):
                model.model_validate({**payload, key: bad})
        assert model.model_validate({**payload, key: 2**31 - 1})


def test_input_subclasses_keep_contract_and_carry_examples():
    for subclass, base, payload in ((catalog_models.ContextInput, catalog_models.ContextCreate, CONTEXT),
                                    (catalog_models.TypeInput, catalog_models.TypeDraft, TYPE)):
        assert set(subclass.model_fields) == set(base.model_fields)
        assert subclass.model_config["extra"] == "forbid" and subclass.model_config["frozen"]
        assert subclass.model_config["hide_input_in_errors"]
        assert subclass.model_json_schema()["examples"]
        with pytest.raises(ValidationError):
            subclass.model_validate({**payload, "owner": "x"})
    for model in (catalog_models.ContextUpdateInput, catalog_models.TypeUpdateInput, catalog_models.TypePublishInput):
        assert model.model_json_schema()["examples"]


# -- provider
class FakeStore:
    """Real sqlite behind the two attributes the services use; no JobStore, no job queue."""

    def __init__(self, path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)

    def _get_conn(self):
        return self._conn


def _provider_for(holder):
    return CatalogProvider(lambda: SimpleNamespace(store=holder["store"]))


def test_provider_constructor_resolves_nothing():
    def explode():
        raise AssertionError("resolved in constructor")
    CatalogProvider(explode)


def test_provider_same_store_same_pair_even_concurrently(tmp_path):
    holder = {"store": FakeStore(tmp_path / "a.db")}
    provider, results = _provider_for(holder), []
    barrier = threading.Barrier(8)

    def run():
        barrier.wait(timeout=10)
        results.append(provider())
    threads = [threading.Thread(target=run, daemon=True) for _ in range(8)]
    [t.start() for t in threads]
    [t.join(timeout=10) for t in threads]
    assert not any(t.is_alive() for t in threads), "provider threads did not finish"
    assert len(results) == 8 and all(r is results[0] for r in results)
    assert isinstance(results[0], CatalogServices)
    assert results[0].contexts.jobs is holder["store"] and results[0].types.jobs is holder["store"]


def test_provider_follows_store_changes_without_leaking(tmp_path):
    a, b = FakeStore(tmp_path / "a.db"), FakeStore(tmp_path / "b.db")
    holder = {"store": a}
    provider = _provider_for(holder)
    first = provider()
    holder["store"] = b
    second = provider()
    assert second is not first and second.contexts.jobs is b and second.types.jobs is b
    holder["store"] = a
    third = provider()
    assert third.contexts.jobs is a and third is not first  # one cached pair only; A is rebuilt, never reused from B


def test_provider_builds_contexts_before_types_and_retries_after_failure(tmp_path, monkeypatch):
    holder = {"store": FakeStore(tmp_path / "a.db")}
    provider, order, fail = _provider_for(holder), [], {"on": True}
    real_types = type_service.TypeService

    def contexts(store):
        order.append("contexts")
        return SimpleNamespace(jobs=store)

    def types(store):
        order.append("types")
        if fail["on"]:
            raise ApplicationError(503, "storage_unavailable", "Type storage is unavailable.")
        return real_types(store)
    monkeypatch.setattr("app.services.catalog_runtime.ContextService", contexts)
    monkeypatch.setattr("app.services.catalog_runtime.TypeService", types)
    with pytest.raises(ApplicationError):
        provider()
    assert order == ["contexts", "types"]
    assert provider._services is None  # failure was not cached
    fail["on"] = False
    pair = provider()
    assert order == ["contexts", "types", "contexts", "types"] and provider() is pair


def test_provider_is_usable_with_real_services_and_dependency_reads_app_state(tmp_path):
    holder = {"store": FakeStore(tmp_path / "a.db")}
    provider = _provider_for(holder)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(catalog_provider=provider)))
    assert get_catalog_services(request) is provider()
    tables = {r[0] for r in holder["store"]._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"document_contexts", "document_types", "document_type_versions"} <= tables


# -- bounded JSON reader
class Probe(BaseModel):
    value: str


def _request(chunks, content_type="application/json", delay=0.0):
    messages = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    messages = messages or [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        if delay:
            await asyncio.sleep(delay)
        return messages.pop(0) if messages else {"type": "http.disconnect"}
    headers = [(b"content-type", content_type.encode())] if content_type else []
    return Request({"type": "http", "method": "POST", "headers": headers, "query_string": b""}, receive)


def _read(request, model=Probe, **kwargs):
    return asyncio.run(json_input(request, model, **kwargs))


def _body(size):
    return json.dumps({"value": "x" * size}).encode()


def test_default_limit_is_64_kib_and_preserved():
    assert _read(_request([_body(60000)])).value == "x" * 60000
    with pytest.raises(ApplicationError) as error:
        _read(_request([_body(70000)]))
    assert error.value.status == 413 and error.value.code == "request_too_large"


def test_catalog_limit_accepts_valid_body_above_64_kib():
    data = _body(100000)
    assert len(data) > 65536
    assert _read(_request([data[:50000], data[50000:]]), max_bytes=2 * 1024 * 1024).value == "x" * 100000


def test_oversized_chunked_body_is_refused_before_parsing_and_not_accumulated():
    class Spy(BaseModel):
        @classmethod
        def model_validate_json(cls, *args, **kwargs):
            raise AssertionError("parsed before the size check")
    seen = []
    big = b"y" * 1024

    async def receive():
        seen.append(1)
        return {"type": "http.request", "body": big, "more_body": True}
    request = Request({"type": "http", "method": "POST", "headers": [(b"content-type", b"application/json")],
                       "query_string": b""}, receive)
    with pytest.raises(ApplicationError) as error:
        _read(request, Spy, max_bytes=4096)
    assert error.value.status == 413 and error.value.code == "request_too_large"
    assert len(seen) == 5  # stops at the first chunk that crosses the limit
    assert "64" not in error.value.message and "4096" not in error.value.message


def test_timeout_media_type_and_invalid_body_keep_their_statuses(monkeypatch):
    monkeypatch.setattr(settings, "upload_timeout_seconds", 0.05)
    with pytest.raises(ApplicationError) as error:
        _read(_request([_body(10)], delay=1.0))
    assert error.value.status == 408 and error.value.code == "upload_timeout"
    monkeypatch.undo()
    with pytest.raises(ApplicationError) as error:
        _read(_request([_body(10)], content_type="text/plain"))
    assert error.value.status == 415
    from fastapi.exceptions import RequestValidationError
    with pytest.raises(RequestValidationError):
        _read(_request([b'{"value": 1}']))
    with pytest.raises(RequestValidationError):
        _read(_request([b"not json"]))
