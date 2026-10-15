"""AM-10 state races, owned process trees, immutable retries and real MCP clients."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Event
import time

import httpx2
import pytest
from app.application import ApplicationError, Principal
from app.config import settings
from app.control_models import RetryInput
from app.options import ProcessingOptions
from app.services.cancellation import JobCancelled, cancellation_scope, run_process
from app.services.control_service import ControlService, get_control_service
from app.services.job_queue import JobQueueProcessor
from app.services.ocr_orchestrator import OcrOrchestrator
from app.services.storage_service import JobStore
from app.services.upload_service import UploadService
from test_upload_admission import system, bearer, pdf, upload, submit
from test_http_contract import validate_response
from test_mcp_vertical import runtime, connected, call, complete

OWNER = Principal("user:1", 1, frozenset({"jobs:read", "jobs:submit", "jobs:cancel", "artifacts:read"}))
EXTRACT = ProcessingOptions(processing_mode="extract_text")


@pytest.fixture
def control(system):
    system.control = ControlService(system.uploads)
    system.app.dependency_overrides[get_control_service] = lambda: system.control
    return system


def new_job(control, status="queued", owner=1):
    job = control.jobs.create("original.pdf", pdf(), owner_user_id=owner)
    if status != "queued":
        control.jobs.update(job.job_id, status=status)
    return job


def cancel(control, job_id, token=None):
    return control.client.post(f"/api/v1/jobs/{job_id}/cancel", headers=bearer(token or control.tokens[0]))


def retry(control, job_id, body=None, key="retry-key-0001", token=None):
    return control.client.post(f"/api/v1/jobs/{job_id}/retry", json=body if body is not None else {"options": {"processing_mode": "extract_text"}},
                               headers={**bearer(token or control.tokens[0]), "Idempotency-Key": key})


def wait_for(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "Timed out waiting for synthetic worker"
        time.sleep(0.02)


def alive(pid):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = wintypes.DWORD()
        try:
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and stat.read_text().split()[2] == "Z")


@pytest.mark.parametrize("status,expected", [("queued", "canceled"), ("processing", "cancel_requested"),
                                           ("cancel_requested", "cancel_requested"), ("canceled", "canceled"),
                                           ("completed", "completed"), ("failed", "failed")])
def test_cancel_states_idempotent_and_owner_isolated(control, status, expected):
    job = new_job(control, status)
    first = cancel(control, job.job_id)
    assert first.status_code == 200, first.text
    validate_response(control.app, first, "/api/v1/jobs/{job_id}/cancel", "post")
    assert first.json()["status"] == expected
    assert cancel(control, job.job_id).json() == first.json()
    assert cancel(control, job.job_id, control.tokens[1]).status_code == 404
    assert cancel(control, "f" * 32).status_code == 404
    if expected in ("canceled", "completed", "failed"):
        assert control.jobs.claim(job.job_id) is None


def test_cancel_batch_explicit_per_item_and_scope(control):
    jobs = [new_job(control), new_job(control, "processing"), new_job(control, "completed"), new_job(control, owner=2)]
    ids = [j.job_id for j in jobs] + ["f" * 32]
    response = control.client.post("/api/v1/jobs/cancel-batch", json={"job_ids": ids}, headers=bearer(control.tokens[0]))
    assert response.status_code == 200
    validate_response(control.app, response, "/api/v1/jobs/cancel-batch", "post")
    assert [i["outcome"] for i in response.json()["items"]] == ["canceled", "requested", "already_terminal", "not_found", "not_found"]
    assert jobs[-1].status == "queued"
    for invalid in ([], [ids[0]] * 2, [f"{n:032x}" for n in range(26)]):
        assert control.client.post("/api/v1/jobs/cancel-batch", json={"job_ids": invalid}, headers=bearer(control.tokens[0])).status_code == 422
    # Existing submit/read service has no cancel scope.
    assert cancel(control, ids[0], control.tokens[2]).status_code == 403
    assert control.client.post("/api/v1/jobs/cancel-batch", json={"job_ids": ids}, headers=bearer(control.tokens[2])).status_code == 403


def test_claim_cancel_race_never_revives_terminal_job(control):
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(40):
            job = new_job(control)
            gate = Barrier(2)
            def claim():
                gate.wait()
                return control.jobs.claim(job.job_id)
            def cancel_now():
                gate.wait()
                return control.control.cancel(OWNER, job.job_id)
            a, b = pool.submit(claim), pool.submit(cancel_now)
            claimed, canceled = a.result(), b.result()
            assert (claimed is None and canceled.status == "canceled") or (claimed is not None and canceled.status == "cancel_requested")
            if claimed:
                with pytest.raises(JobCancelled):
                    control.jobs.worker_update(job.job_id, status="completed")
                control.jobs.finish_cancellation(job.job_id)
            assert control.jobs.get(job.job_id).status == "canceled"
            assert OcrOrchestrator(control.jobs).process_job(job.job_id) is None


def test_finish_cancel_race_reports_real_winner(control):
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(40):
            job = new_job(control)
            control.jobs.claim(job.job_id)
            gate = Barrier(2)
            def finish():
                gate.wait()
                try:
                    return control.jobs.worker_update(job.job_id, status="completed").status
                except JobCancelled:
                    return "interrupted"
            def cancel_now():
                gate.wait()
                return control.control.cancel(OWNER, job.job_id)
            a, b = pool.submit(finish), pool.submit(cancel_now)
            completed, canceled = a.result(), b.result()
            assert (completed == "completed" and canceled.outcome == "already_terminal") or (completed == "interrupted" and canceled.status == "cancel_requested")


def test_cancel_between_stages_prevents_result_publication(control):
    job = new_job(control)
    entered, release = Event(), Event()
    class Primary:
        def process_pages(self, *args, **kwargs):
            entered.set()
            assert release.wait(10)
            return ["Synthetic text"]
    processor = OcrOrchestrator(control.jobs, primary_ocr=Primary())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(processor.process_job, job.job_id)
        assert entered.wait(10)
        assert cancel(control, job.job_id).json()["status"] == "cancel_requested"
        assert control.jobs.get(job.job_id).status != "canceled"
        release.set()
        assert future.result(timeout=10) is None
    assert job.status == "canceled" and job.output_pdf_path is None
    assert control.client.get(f"/api/v1/jobs/{job.job_id}/artifact", headers=bearer(control.tokens[0])).json()["available"] is False
    assert job.input_pdf_path.is_file()


def test_cancel_persist_failure_does_not_change_cache(control, monkeypatch):
    job = new_job(control)
    def fail(*args, **kwargs):
        raise OSError("synthetic failure")
    monkeypatch.setattr(control.jobs, "_persist", fail)
    assert cancel(control, job.job_id).status_code == 503
    assert job.status == "queued"
    assert control.jobs._conn.execute("SELECT status FROM jobs WHERE job_id=?", (job.job_id,)).fetchone()[0] == "queued"


@pytest.mark.parametrize("reason", ["cancel", "timeout"])
def test_managed_process_tree_terminates_parent_and_child(tmp_path, reason):
    marker = tmp_path / "pids.json"
    code = ("import os,sys,subprocess,time,json;from pathlib import Path;"
            "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
            f"Path({str(marker)!r}).write_text(json.dumps([os.getpid(),child.pid]));time.sleep(60)")
    signal = Event()
    def run():
        with cancellation_scope(signal.is_set):
            run_process([sys.executable, "-c", code], timeout=1.5 if reason == "timeout" else 30)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        wait_for(marker.exists)
        pids = json.loads(marker.read_text())
        assert all(alive(pid) for pid in pids)
        if reason == "cancel":
            signal.set()
        with pytest.raises(JobCancelled if reason == "cancel" else subprocess.TimeoutExpired):
            future.result(timeout=20)
        wait_for(lambda: all(not alive(pid) for pid in pids))


def test_pre_canceled_context_does_not_launch_process(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Must not spawn")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(JobCancelled):
        with cancellation_scope(lambda: True):
            run_process([sys.executable, "-c", "pass"], timeout=1)


def test_pending_cancel_recovered_without_processing(control):
    job = new_job(control, "cancel_requested")
    other = new_job(control)
    control.control.cancel(OWNER, other.job_id)
    reopened = JobStore(control.jobs.root_dir)
    queue = JobQueueProcessor(reopened)
    queue.orchestrator.process_job = lambda _: pytest.fail("Canceled work must never be recovered into execution")
    try:
        queue.start()
        assert reopened.get(job.job_id).status == "cancel_requested"
        assert reopened.get(job.job_id).error_code == "cancellation_recovery_required"
        assert not queue._queued_ids
    finally:
        queue.stop()
        reopened._conn.close()


@pytest.mark.parametrize("state", ["completed", "failed", "canceled"])
def test_retry_creates_derivation_preserving_original(control, state):
    original = new_job(control, state)
    before = asdict(control.jobs.get_snapshot(original.job_id))
    digest = hashlib.sha256(original.input_pdf_path.read_bytes()).hexdigest()
    response = retry(control, original.job_id)
    assert response.status_code == 202, response.text
    validate_response(control.app, response, "/api/v1/jobs/{job_id}/retry", "post")
    receipt = response.json()
    derived = control.jobs.get(receipt["job_id"])
    assert derived.parent_job_id == original.job_id and derived.job_id != original.job_id
    assert derived.working_dir != original.working_dir
    assert hashlib.sha256(derived.input_pdf_path.read_bytes()).hexdigest() == digest
    assert asdict(control.jobs.get_snapshot(original.job_id)) == before
    assert OcrOrchestrator(control.jobs).process_job(derived.job_id)
    assert asdict(control.jobs.get_snapshot(original.job_id)) == before
    assert retry(control, original.job_id).json() == receipt
    assert control.client.get(f"/api/v1/jobs/{derived.job_id}", headers=bearer(control.tokens[0])).json()["parent_job_id"] == original.job_id
    reloaded = JobStore(control.jobs.root_dir)
    try:
        assert reloaded.get(derived.job_id).parent_job_id == original.job_id
    finally:
        reloaded._conn.close()


def test_retry_inherits_requested_options_and_replays_after_input_expiry(control):
    original = submit(control, upload(control)["upload_id"]).json()["job_id"]
    control.jobs.update(original, status="failed")
    first = retry(control, original, body={})
    assert first.status_code == 202
    assert control.jobs.get(first.json()["job_id"]).options_requested == control.jobs.get(original).options_requested
    control.jobs.cleanup(original)
    assert retry(control, original, body={}).json() == first.json()
    assert retry(control, original, body={}, key="new-key-0000").status_code == 410
    # Omitted options and explicit options are distinct requests.
    assert retry(control, original).status_code == 409


@pytest.mark.parametrize("kind,expected", [("queued",409), ("processing",409), ("cancel_requested",409),
                                         ("missing",404), ("foreign",404), ("expired",410), ("legacy",422), ("quota",429)])
def test_retry_rejections_do_not_admit_jobs(control, monkeypatch, kind, expected):
    job = new_job(control, kind if kind in ("queued","processing","cancel_requested") else "failed", owner=2 if kind == "foreign" else 1)
    if kind == "expired":
        control.jobs.cleanup(job.job_id)
    if kind == "quota":
        monkeypatch.setattr(settings, "job_owner_max_pending", 0)
    response = retry(control, "f"*32 if kind == "missing" else job.job_id, body={} if kind == "legacy" else None)
    assert response.status_code == expected, response.text
    assert len(control.jobs.snapshot()) == 1
    assert control.jobs._conn.execute("SELECT COUNT(*) FROM job_retries").fetchone()[0] == 0
    assert not control.queue.enqueued


def test_retry_concurrency_dispatch_failure_and_restart(control):
    original = new_job(control, "failed")
    body = RetryInput(options=EXTRACT)
    with ThreadPoolExecutor(max_workers=8) as pool:
        admissions = list(pool.map(lambda _: control.control.retry(OWNER, original.job_id, body, "concurrent-retry"), range(16)))
    assert len({x.job_id for x, _ in admissions}) == 1
    assert sum(not replay for _, replay in admissions) == 1
    assert len(control.jobs.snapshot()) == 2
    def unavailable(_):
        raise RuntimeError("queue down")
    control.queue.enqueue = unavailable
    assert retry(control, original.job_id, key="concurrent-retry").status_code == 503
    control.queue.enqueue = control.queue.enqueued.append
    replay = retry(control, original.job_id, key="concurrent-retry")
    assert replay.status_code == 202 and replay.headers["idempotency-replayed"] == "true"
    assert control.queue.enqueued == [admissions[0][0].job_id]
    reopened = JobStore(control.jobs.root_dir)
    try:
        service = ControlService(UploadService(reopened))
        assert service.retry(OWNER, original.job_id, body, "concurrent-retry")[0] == admissions[0][0]
    finally:
        reopened._conn.close()


def test_retry_persistence_failure_rolls_back_files(control, monkeypatch):
    original = new_job(control, "failed")
    def fail(*a, **kw):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(control.jobs, "_persist", fail)
    assert retry(control, original.job_id).status_code == 503
    assert len(control.jobs.snapshot()) == 1
    assert not list(control.uploads.admissions.iterdir())
    assert control.jobs._conn.execute("SELECT COUNT(*) FROM job_retries").fetchone()[0] == 0


@pytest.mark.parametrize("transport,mode", [("http","auto"), ("http","legacy"), ("stdio","auto"), ("stdio","legacy")])
def test_real_mcp_cancel_running_then_retry(runtime, transport, mode):
    runtime.start(control_pause=True)
    async def scenario():
        async with connected(runtime, transport, mode) as client:
            data = pdf()
            ready = await call(client, "ocr_prepare_upload", {"filename":"cancel.pdf","size_bytes":len(data)})
            async with httpx2.AsyncClient(base_url=runtime.origin, headers=bearer(runtime.tokens[0]), trust_env=False) as http:
                assert (await http.put(ready["content_url"], content=data, headers={"Content-Type":"application/pdf"})).status_code == 200
                receipt = await call(client, "ocr_submit_job", {"upload_id":ready["upload_id"],"idempotency_key":"cancel-runtime","options":{"processing_mode":"extract_text"}})
                job_id = receipt["job_id"]
                async with asyncio.timeout(15):
                    while not (runtime.root / "processing-control").exists():
                        await asyncio.sleep(0.05)
                pid = int((runtime.root / "processing-control").read_text())
                assert alive(pid)
                async with connected(runtime, transport, mode, runtime.tokens[1]) as other:
                    denied = await other.call_tool("ocr_cancel_job", {"job_id":job_id})
                    assert denied.is_error and "job_not_found" in denied.content[0].text
                canceled = await call(client, "ocr_cancel_job", {"job_id":job_id})
                assert canceled["status"] in ("cancel_requested","canceled")
                async with asyncio.timeout(15):
                    while (await call(client, "ocr_get_job", {"job_id":job_id}))["status"] != "canceled":
                        await asyncio.sleep(0.05)
                assert not alive(pid)
                assert (await http.get(f"/api/v1/jobs/{job_id}/artifact/content")).status_code == 409
                result = await call(client, "ocr_cancel_jobs", {"job_ids":[job_id,"f"*32]})
                assert [i["outcome"] for i in result["items"]] == ["canceled","not_found"]
                args = {"job_id":job_id,"idempotency_key":"derived-runtime"}
                derived = await call(client, "ocr_retry_job", args)
                assert derived["parent_job_id"] == job_id
                assert await call(client, "ocr_retry_job", args) == derived
                replay = await http.post(f"/api/v1/jobs/{job_id}/retry", json={}, headers={"Idempotency-Key":args["idempotency_key"]})
                assert replay.json() == derived
                await complete(client, derived["job_id"])
                original = await call(client, "ocr_get_job", {"job_id":job_id})
                assert original["status"] == "canceled"
                final = await call(client, "ocr_get_job", {"job_id":derived["job_id"]})
                assert final["parent_job_id"] == job_id
                assert (await http.get(final["download_url"])).status_code == 200
    asyncio.run(scenario())

@pytest.mark.parametrize("phase", ["raster", "inference", "success"])
def test_fallback_isolated_process_is_cancelable(control, monkeypatch, phase):
    from app.services.easyocr_service import FallbackOcrService
    modules = control.tmp / "synthetic-modules"
    modules.mkdir()
    marker = control.tmp / "fallback-pids.json"
    monkeypatch.setenv("PYTHONPATH", str(modules))
    monkeypatch.setenv("SYNTHETIC_PHASE", phase)
    monkeypatch.setenv("SYNTHETIC_MARKER", str(marker))
    (modules / "easyocr.py").write_text("""import os,time,json
from pathlib import Path
class Reader:
    def __init__(self,*a,**kw): pass
    def readtext(self,*a,**kw):
        if os.environ['SYNTHETIC_PHASE']=='inference':
            Path(os.environ['SYNTHETIC_MARKER']).write_text(json.dumps([os.getpid()]))
            time.sleep(60)
        return [(None,'synthetic fallback',None)]
""", encoding="utf-8")
    (modules / "pdf2image.py").write_text("""import os,sys,time,json,subprocess
from pathlib import Path
from PIL import Image
def convert_from_path(*a,**kw):
    if os.environ['SYNTHETIC_PHASE']=='raster':
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
        Path(os.environ['SYNTHETIC_MARKER']).write_text(json.dumps([os.getpid(),child.pid]))
        time.sleep(60)
    return [Image.new('RGB',(20,20),'white')]
""", encoding="utf-8")
    job = new_job(control)
    service = FallbackOcrService()
    signal = Event()
    def run():
        with cancellation_scope(signal.is_set):
            return service.process_pages(job.input_pdf_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        if phase == "success":
            assert future.result(timeout=15) == ["synthetic fallback"]
        else:
            wait_for(marker.exists)
            pids = json.loads(marker.read_text())
            assert all(alive(pid) for pid in pids)
            signal.set()
            with pytest.raises(JobCancelled):
                future.result(timeout=20)
            wait_for(lambda: all(not alive(pid) for pid in pids))
    assert service._reader is None  # EasyOCR model never instantiated in the parent.
    assert not (job.working_dir / "fallback-pages.json").exists()


def test_failed_termination_never_claims_canceled(control):
    from app.services.cancellation import ProcessTerminationError
    job = new_job(control)
    class Primary:
        def process_pages(self, *a, **kw):
            control.control.cancel(OWNER, job.job_id)
            raise ProcessTerminationError("synthetic termination failure")
    with pytest.raises(ProcessTerminationError):
        OcrOrchestrator(control.jobs, primary_ocr=Primary()).process_job(job.job_id)
    assert job.status == "cancel_requested"
    assert job.error_code == "process_termination_failed"
    assert job.output_pdf_path is None


def test_cancel_scope_does_not_grant_retry_and_retry_cannot_cross_owner(control):
    job = new_job(control, "failed")
    token = control.identities.issue_credential(control.services[0]["id"], "Cancel only", ["jobs:cancel"], 600, "user:1")[1]
    assert retry(control, job.job_id, token=token).status_code == 403
    assert retry(control, job.job_id, token=control.tokens[1]).status_code == 404


def test_cancel_requested_keeps_pending_quota(control, monkeypatch):
    new_job(control, "cancel_requested")
    original = new_job(control, "failed")
    monkeypatch.setattr(settings, "job_owner_max_pending", 1)
    assert retry(control, original.job_id).status_code == 429
