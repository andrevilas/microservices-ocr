import hashlib
import json
from dataclasses import replace

import httpx2
import pytest

from app.application import ApplicationError
from app.results import page_path
from app.services.rag_delivery import Destination, prepare_delivery, send_delivery
from app.services.rag_delivery_contract import manifest_digest, parse_manifest
from test_results import OWNER, orchestrator, pdf
from app.services.storage_service import JobStore
from app.config import settings


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "final_output_type", "pdf")
    store = JobStore(root_dir=tmp_path)
    job = store.create("synthetic.pdf", pdf(["primeira pagina", "iluminacao publica municipal"]),
                       owner_user_id=1)
    orchestrator(store).process_job(job.job_id)
    destination = Destination("http://127.0.0.1:8092", "normas", OWNER.subject,
                              "normative_source", token="t" * 40)
    yield store, job, destination
    store._conn.close()


def receipt_for(payload):
    manifest = parse_manifest(json.dumps(json.loads(payload)["manifest"]).encode())
    return dict(contract_version="ocr-rag/1", delivery_id=manifest.delivery_id,
                destination_id=manifest.destination_id, manifest_sha256=manifest_digest(manifest),
                destination_document_id="d" * 32, destination_revision=manifest_digest(manifest),
                artifacts=[a.model_dump() for a in manifest.artifacts], storage_state="stored",
                indexing_state="indexed")


class Bytes(httpx2.SyncByteStream):
    def __init__(self, data):
        self.data = data

    def __iter__(self):
        yield self.data


def response(data, status=200, headers=None):
    return httpx2.Response(status, stream=Bytes(json.dumps(data).encode()),
                           headers=headers or {"content-type": "application/json"})


def test_prepare_and_send_preserves_bytes_and_does_not_purge(source):
    import base64
    store, job, dest = source
    prepared = prepare_delivery(store, OWNER, job.job_id, dest)
    assert prepare_delivery(store, OWNER, job.job_id, dest) == prepared
    body = json.loads(prepared.payload)
    assert base64.b64decode(body["artifacts"]["pdf"]) == job.output_pdf_path.read_bytes()
    pages = base64.b64decode(body["artifacts"]["pages"]).decode().splitlines()
    assert [json.loads(p)["page"] for p in pages] == [1, 2]
    assert "iluminacao" in json.loads(pages[1])["text"]
    calls = []
    def receive(request):
        calls.append(request)
        assert request.content == prepared.payload
        assert request.headers["authorization"] == "Bearer " + dest.token
        return response(receipt_for(request.content))
    result = send_delivery(prepared, dest, transport=httpx2.MockTransport(receive))
    assert result.indexed and len(calls) == 1
    assert job.output_pdf_path.is_file() and job.artifacts_removed_at is None
    assert dest.token not in repr(dest)
    assert store._artifact_leases == {}


def test_source_ownership_and_scopes(source):
    store, job, dest = source
    with pytest.raises(ApplicationError) as exc:
        prepare_delivery(store, replace(OWNER, subject="user:2", user_id=2), job.job_id,
                         replace(dest, owner="user:2"))
    assert exc.value.status == 404
    with pytest.raises(ApplicationError) as exc:
        prepare_delivery(store, replace(OWNER, scopes=frozenset({"jobs:read"})), job.job_id, dest)
    assert exc.value.status == 403
    with pytest.raises(ApplicationError) as exc:
        prepare_delivery(store, OWNER, job.job_id, replace(dest, owner="user:2"))
    assert exc.value.status == 403


@pytest.mark.parametrize("artifact", ["pdf", "page", "blank"])
def test_source_integrity(source, artifact):
    store, job, dest = source
    if artifact == "pdf":
        job.output_pdf_path.write_bytes(b"corrupt")
    elif artifact == "page":
        page_path(job.working_dir, 2).write_text("corrupt")
    else:
        other = store.create("blank.pdf", pdf(["first", ""]), owner_user_id=1)
        orchestrator(store).process_job(other.job_id)
        job = other
    with pytest.raises(ApplicationError) as exc:
        prepare_delivery(store, OWNER, job.job_id, dest)
    assert exc.value.code == ("rag_page_unsupported" if artifact == "blank" else "rag_source_integrity")
    assert store._artifact_leases == {}
    assert job.artifacts_removed_at is None


@pytest.mark.parametrize("status", [301, 307, 400, 401, 403, 409, 429, 500, 503])
def test_no_redirect_or_automatic_retry(source, status):
    store, job, dest = source
    prepared = prepare_delivery(store, OWNER, job.job_id, dest)
    calls = []
    def receive(request):
        calls.append(request)
        return response({}, status, {"location": "https://other.example"})
    with pytest.raises(ApplicationError) as exc:
        send_delivery(prepared, dest, transport=httpx2.MockTransport(receive))
    assert len(calls) == 1
    assert exc.value.code == ("rag_delivery_uncertain" if status in (429, 500, 503)
                              else "rag_delivery_rejected")
    assert job.output_pdf_path.is_file()


@pytest.mark.parametrize("mutation", ["digest", "artifact", "id", "oversized", "compressed"])
def test_receipt_rejection(source, mutation):
    store, job, dest = source
    prepared = prepare_delivery(store, OWNER, job.job_id, dest)
    receipt = receipt_for(prepared.payload)
    if mutation == "digest":
        receipt["manifest_sha256"] = "0" * 64
    elif mutation == "artifact":
        receipt["artifacts"][0]["sha256"] = "0" * 64
    elif mutation == "id":
        receipt["delivery_id"] = "other"
    elif mutation == "oversized":
        receipt["extra"] = "x" * 65536
    def receive(_):
        return response(receipt, headers={"content-encoding": "gzip"} if mutation == "compressed" else None)
    with pytest.raises(ApplicationError) as exc:
        send_delivery(prepared, dest, transport=httpx2.MockTransport(receive))
    assert exc.value.code == "rag_receipt_invalid"


@pytest.mark.parametrize("state", ["pending", "failed"])
def test_stored_not_indexed(source, state):
    store, job, dest = source
    prepared = prepare_delivery(store, OWNER, job.job_id, dest)
    receipt = receipt_for(prepared.payload)
    receipt["indexing_state"] = state
    result = send_delivery(prepared, dest, transport=httpx2.MockTransport(lambda _: response(receipt)))
    assert not result.indexed
    assert job.output_pdf_path.is_file()


def test_timeout_is_uncertain_and_binding_is_checked(source):
    store, job, dest = source
    prepared = prepare_delivery(store, OWNER, job.job_id, dest)
    def timeout(_):
        raise httpx2.ReadTimeout("sensitive remote text")
    with pytest.raises(ApplicationError) as exc:
        send_delivery(prepared, dest, transport=httpx2.MockTransport(timeout))
    assert exc.value.code == "rag_delivery_uncertain"
    assert "sensitive" not in str(exc.value)
    with pytest.raises(ApplicationError) as exc:
        send_delivery(prepared, replace(dest, destination_id="other"), transport=httpx2.MockTransport(timeout))
    assert exc.value.code == "rag_package_invalid"


@pytest.mark.parametrize("url", ["http://remote.example", "https://u:p@host", "https://host/path",
                                 "https://host/?key=x", "file:///tmp", "https://host:bad",
                                 "https://host:0", "https://host:65536", "https://[", "https://host\n"])
def test_operator_url_validation(source, url):
    store, job, dest = source
    with pytest.raises(ApplicationError) as exc:
        prepare_delivery(store, OWNER, job.job_id, replace(dest, url=url))
    assert exc.value.code == "rag_configuration_invalid"


def test_real_receiver_handoff(source, tmp_path):
    """Optional integration: install the separately built receiver wheel first."""
    import os
    import socket
    import subprocess
    import sys
    import time
    receiver = pytest.importorskip("document_rag.store")
    store, job, dest = source
    database = str(tmp_path / "independent-rag.sqlite")
    remote_store = receiver.Store(database)
    token = remote_store.provision("synthetic", "normas", "normative_source", write=True)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    dest = replace(dest, url=f"http://127.0.0.1:{port}", token=token)
    # Empty cwd and no OCR PYTHONPATH for the receiver subprocess.
    env = {**os.environ, "DOCUMENT_RAG_DB": database, "PYTHONPATH": ""}
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "document_rag.api:from_env", "--factory",
         "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
        cwd=tmp_path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with httpx2.Client(base_url=dest.url, trust_env=False) as client:
            for _ in range(100):
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx2.ConnectError:
                    time.sleep(.05)
            else:
                pytest.fail("independent receiver did not start")
            prepared = prepare_delivery(store, OWNER, job.job_id, dest)
            first = send_delivery(prepared, dest)
            repeated = send_delivery(prepared, dest)
            assert first == repeated and first.indexed
            auth = {"Authorization": "Bearer " + token}
            result = client.post("/v1/search", headers=auth, json={
                "query": "iluminação", "destination": "normas", "kind": "normative_source"})
            assert result.status_code == 200
            citation = result.json()["hits"][0]["citation"]
            assert citation["page"] == 2
            assert citation["document_id"] == first.receipt.destination_document_id
            downloaded = client.get(f"/v1/documents/{citation['document_id']}/artifacts/pdf", headers=auth)
            assert downloaded.content == job.output_pdf_path.read_bytes()
            assert hashlib.sha256(downloaded.content).hexdigest() == citation["pdf_sha256"]
            remote_store.revoke(token)
            with pytest.raises(ApplicationError) as exc:
                send_delivery(prepared, dest)
            assert exc.value.code == "rag_delivery_rejected"
            assert job.output_pdf_path.is_file() and job.artifacts_removed_at is None
    finally:
        process.terminate()
        process.wait(timeout=10)
