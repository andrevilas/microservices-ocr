"""Actual ASGI sends over the real bounded document streaming runtime."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import os
from threading import Event
from types import SimpleNamespace

import pytest
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from app.application import ApplicationError
from app.document_http_streaming import DocumentDownloadResponse
from app.metadata_models import snapshot_bytes
from app.services.canonical_text_service import PreparedCanonicalText
from app.services.document_execution import DocumentExecution
from test_document_metadata import BASE, env, persisted, save, who
from test_document_streaming import held, idle, streams_for, until

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")
PRIVATE = "PRIVATE /synthetic/storage/path"


def scope(version="2.4"):
    return {"type": "http", "method": "GET", "path": "/", "headers": [],
            "asgi": {"version": "3.0", "spec_version": version}}


async def quiet_receive():
    await asyncio.Event().wait()


def body_bytes(messages):
    return b"".join(m.get("body", b"") for m in messages)


def assert_private_absent(messages):
    assert PRIVATE not in repr(messages)


def busy(execution, principal):
    with pytest.raises(ApplicationError) as caught:
        execution.acquire(principal)
    assert caught.value.code == "document_operations_busy"


@pytest.mark.parametrize("kind", ["metadata", "canonical"])
@pytest.mark.parametrize("version", ["2.4", "2.3"])
def test_exact_download_and_capacity_through_last_actual_send(env, kind, version):
    e, streams = env, streams_for(env)
    if kind == "metadata":
        receipt, _ = save(e)
        expected = snapshot_bytes(persisted(e, receipt))
        factory = lambda: streams.open_metadata(
            e.owner, e.job.job_id, receipt.revision_id, sha256=receipt.sha256)
        media, filename = "application/json", "metadata.json"
    else:
        with e.service.canonical.open(e.owner, e.job.job_id) as prepared:
            expected = b"".join(prepared.iter_jsonl())
        factory = lambda: streams.open_canonical(e.owner, e.job.job_id)
        media, filename = "application/x-ndjson", "pages.jsonl"
    response = DocumentDownloadResponse(factory)
    assert isinstance(response, Response)

    async def scenario():
        messages, sent = [], 0
        last, release = asyncio.Event(), asyncio.Event()

        async def send(message):
            nonlocal sent
            messages.append(dict(message))
            if message["type"] == "http.response.body" and message.get("body"):
                sent += len(message["body"])
                held(e, streams)
                await asyncio.sleep(0)
                held(e, streams)
                if sent == len(expected):
                    last.set()
                    await release.wait()
                    held(e, streams)

        task = asyncio.create_task(response(scope(version), quiet_receive, send))
        try:
            await asyncio.wait_for(last.wait(), 2)
            held(e, streams)
            busy(streams.execution, e.owner)
            other = streams.execution.acquire(who("user:2"))
            try:
                assert streams.execution.active_count == 2
                busy(streams.execution, who("user:3"))
                held_count = e.jobs._artifact_leases.copy()
                assert held_count == {e.job.job_id: 1}
            finally:
                other.close()
            assert not task.done()
            release.set()
            await asyncio.wait_for(task, 2)
            assert body_bytes(messages) == expected
            headers = dict(messages[0]["headers"])
            assert headers[b"content-type"] == media.encode()
            assert headers[b"content-disposition"] == (
                f'attachment; filename="{filename}"'.encode())
            assert headers[b"content-length"] == str(len(expected)).encode()
            assert headers[b"x-content-sha256"] == hashlib.sha256(expected).hexdigest().encode()
            assert headers[b"cache-control"] == b"no-store"
            assert messages[-1] == {
                "type": "http.response.body", "body": b"", "more_body": False}
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await idle(e, streams)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["scope", "owner", "retention", "metadata", "canonical"])
def test_real_source_failures_before_headers_pass_through(env, failure):
    e, streams = env, streams_for(env)
    receipt, _ = save(e)
    principal = e.owner
    if failure == "scope":
        principal = who(scopes=BASE - {"metadata:read"})
    elif failure == "owner":
        principal = who("user:2")
    elif failure == "retention":
        with e.jobs._lock:
            e.job.artifacts_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            e.jobs._persist(e.job)
    elif failure == "metadata":
        (e.job.working_dir / "results" / "metadata" /
         (receipt.revision_id + ".json")).write_bytes(PRIVATE.encode())
    elif failure == "canonical":
        (e.job.working_dir / "results" / "page-000001.txt").write_bytes(b"\xff\xfe")

    async def scenario():
        messages, errors = [], []

        @asynccontextmanager
        async def factory():
            context = (streams.open_canonical(principal, e.job.job_id)
                       if failure == "canonical" else streams.open_metadata(
                           principal, e.job.job_id, receipt.revision_id,
                           sha256=receipt.sha256))
            try:
                async with context as download:
                    yield download
            except ApplicationError as error:
                errors.append(error)
                raise

        async def send(message):
            messages.append(message)

        try:
            with pytest.raises(ApplicationError) as caught:
                await DocumentDownloadResponse(factory)(scope(), quiet_receive, send)
            assert errors == [caught.value]
            assert caught.value is errors[0]
            assert caught.value.status == {
                "scope": 403, "owner": 404, "retention": 410,
                "metadata": 503, "canonical": 503}[failure]
            assert caught.value.message == "Document operation failed."
            assert messages == []
        finally:
            await idle(e, streams)

    asyncio.run(scenario())


def test_source_error_after_prefix_does_not_complete_or_emit_private_data(env, monkeypatch):
    e, streams = env, streams_for(env)
    read = PreparedCanonicalText._read_checked

    def fail_second(prepared, info):
        if info.page == 2:
            raise ApplicationError(503, "result_integrity_error", PRIVATE)
        return read(prepared, info)

    monkeypatch.setattr(PreparedCanonicalText, "_read_checked", fail_second)

    async def scenario():
        messages = []

        async def send(message):
            messages.append(dict(message))

        try:
            response = DocumentDownloadResponse(
                lambda: streams.open_canonical(e.owner, e.job.job_id))
            with pytest.raises(ApplicationError) as caught:
                await response(scope(), quiet_receive, send)
            assert caught.value.code == "result_integrity_error"
            assert body_bytes(messages)
            assert sum(m["type"] == "http.response.start" for m in messages) == 1
            assert all(m.get("more_body") is True for m in messages
                       if m["type"] == "http.response.body")
            assert_private_absent(messages)
            assert PRIVATE not in str(caught.value)
        finally:
            await idle(e, streams)

    asyncio.run(scenario())


@pytest.mark.parametrize("abandon", ["cancel", "oserror", "disconnect"])
def test_network_abandonment_closes_real_context(env, abandon):
    e, streams = env, streams_for(env)

    async def scenario():
        sending, fail, disconnected = asyncio.Event(), asyncio.Event(), asyncio.Event()
        receive_closed = asyncio.Event()
        messages = []

        async def send(message):
            messages.append(dict(message))
            if message.get("body"):
                sending.set()
                await fail.wait()
                raise OSError(PRIVATE)

        async def receive():
            try:
                await disconnected.wait()
                return {"type": "http.disconnect"}
            finally:
                receive_closed.set()

        response = DocumentDownloadResponse(
            lambda: streams.open_canonical(e.owner, e.job.job_id))
        task = asyncio.create_task(response(
            scope("2.3" if abandon == "disconnect" else "2.4"), receive, send))
        try:
            await asyncio.wait_for(sending.wait(), 2)
            held(e, streams)
            if abandon == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif abandon == "oserror":
                fail.set()
                with pytest.raises(ClientDisconnect):
                    await task
            else:
                disconnected.set()
                await asyncio.wait_for(task, 2)
                assert receive_closed.is_set()
            await idle(e, streams)
            assert_private_absent(messages)
            assert all(m.get("more_body") is True for m in messages
                       if m["type"] == "http.response.body")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await idle(e, streams)

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["preparation", "after_prefix"])
@pytest.mark.parametrize("abandon", ["cancel", "timeout"])
def test_blocked_actual_io_keeps_capacity_after_caller_abandons(
        env, monkeypatch, stage, abandon):
    e = env
    streams = streams_for(e, DocumentExecution(timeout_seconds=0.2))
    entered, release = Event(), Event()
    receipt, _ = save(e)
    if stage == "preparation":
        read = e.bundle.files.read

        def blocked(*args, **kwargs):
            entered.set()
            assert release.wait(5), "test did not release metadata I/O"
            return read(*args, **kwargs)

        monkeypatch.setattr(e.bundle.files, "read", blocked)
        factory = lambda: streams.open_metadata(
            e.owner, e.job.job_id, receipt.revision_id, sha256=receipt.sha256)
    else:
        read = PreparedCanonicalText._read_checked

        def blocked(prepared, info):
            if info.page == 2:
                entered.set()
                assert release.wait(5), "test did not release canonical I/O"
            return read(prepared, info)

        monkeypatch.setattr(PreparedCanonicalText, "_read_checked", blocked)
        factory = lambda: streams.open_canonical(e.owner, e.job.job_id)

    async def scenario():
        messages = []

        async def send(message):
            messages.append(dict(message))

        task = asyncio.create_task(DocumentDownloadResponse(factory)(
            scope(), quiet_receive, send))
        try:
            await until(entered.is_set)
            held(e, streams)
            if abandon == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(ApplicationError) as caught:
                    await task
                assert caught.value.status == 408
                assert caught.value.code == "operation_timeout"
                assert PRIVATE not in caught.value.message
            held(e, streams)
            busy(streams.execution, e.owner)
            other = streams.execution.acquire(who("user:2"))
            try:
                assert streams.execution.active_count == 2
                busy(streams.execution, who("user:3"))
            finally:
                other.close()
            with pytest.raises(ApplicationError) as caught:
                await streams.execution.shutdown(timeout_seconds=0)
            assert caught.value.code == "document_operations_active"
            held(e, streams)
            if stage == "preparation":
                assert messages == []
            else:
                assert body_bytes(messages)
                assert all(m.get("more_body") is True for m in messages
                           if m["type"] == "http.response.body")
            assert_private_absent(messages)
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await idle(e, streams)

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_one_deadline_counts_preparation_and_all_awaited_sends(env, monkeypatch):
    e = env
    streams = streams_for(e, DocumentExecution(timeout_seconds=0.3))
    entered, release = Event(), Event()
    ensure_healthy = type(e.bundle).ensure_healthy

    def delayed_preparation(bundle):
        entered.set()
        assert release.wait(5), "test did not release preparation"
        return ensure_healthy(bundle)

    # MetadataServices is frozen: patch its class, not the instance.
    monkeypatch.setattr(type(e.bundle), "ensure_healthy", delayed_preparation)

    async def scenario():
        messages, timing = [], {}
        sends = 0
        loop = asyncio.get_running_loop()

        @asynccontextmanager
        async def factory():
            async with streams.open_canonical(e.owner, e.job.job_id) as download:
                timing["budget"] = download.remaining_seconds
                timing["opened"] = loop.time()
                yield download

        async def send(message):
            nonlocal sends
            messages.append(dict(message))
            if message.get("body"):
                sends += 1
                held(e, streams)
                if sends == 1:
                    await asyncio.sleep(timing["budget"] * 0.6)
                    held(e, streams)
                else:
                    await asyncio.Event().wait()

        started = loop.time()
        task = asyncio.create_task(DocumentDownloadResponse(factory)(scope(), quiet_receive, send))
        try:
            await until(entered.is_set)
            await asyncio.sleep(0.08)
            release.set()
            with pytest.raises(ApplicationError) as caught:
                await task
            finished = loop.time()
            assert caught.value.status == 408
            assert caught.value.code == "operation_timeout"
            assert caught.value.message == "Document operation timed out."
            assert sends == 2
            assert 0 < timing["budget"] < 0.26
            assert finished - timing["opened"] <= timing["budget"] + 0.06
            assert finished - started < 0.38
            assert all(m.get("more_body") is True for m in messages
                       if m["type"] == "http.response.body")
            assert sum(m["type"] == "http.response.start" for m in messages) == 1
            assert_private_absent(messages)
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await idle(e, streams)

    try:
        asyncio.run(scenario())
    finally:
        release.set()


@asynccontextmanager
async def isolated_download(remaining, closed):
    async def chunks():
        yield b"x"

    try:
        yield SimpleNamespace(remaining_seconds=remaining, iter_bytes=chunks,
                              media_type="application/json", filename="metadata.json",
                              size_bytes=1, sha256=hashlib.sha256(b"x").hexdigest())
    finally:
        closed.append(True)


def test_exhausted_deadline_sends_no_headers():
    async def scenario():
        messages, closed = [], []

        async def send(message):
            messages.append(message)

        response = DocumentDownloadResponse(lambda: isolated_download(0, closed))
        with pytest.raises(ApplicationError) as caught:
            await response(scope(), quiet_receive, send)
        assert caught.value.status == 408
        assert caught.value.code == "operation_timeout"
        assert caught.value.message == "Document operation timed out."
        assert messages == []
        assert closed == [True]

    asyncio.run(scenario())


@pytest.mark.parametrize("version", ["2.3", "2.4"])
@pytest.mark.parametrize("stage", ["headers", "body"])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit, asyncio.CancelledError, TimeoutError])
def test_send_control_exceptions_and_unrelated_timeout_keep_identity(error_type, stage, version):
    async def scenario():
        original = error_type(PRIVATE)
        closed, messages = [], []
        caller = asyncio.current_task()
        initial_cancelling = caller.cancelling()
        receive_entered, receive_closed = asyncio.Event(), asyncio.Event()

        async def receive():
            receive_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                receive_closed.set()

        async def send(message):
            messages.append(dict(message))
            if version == "2.3":
                await receive_entered.wait()
            if message["type"] == (
                    "http.response.start" if stage == "headers" else "http.response.body"):
                raise original

        response = DocumentDownloadResponse(lambda: isolated_download(1, closed))
        # Keep sends and control exceptions in this caller. The independent
        # bound fails promptly if a child swallows cancellation and hangs.
        async with asyncio.timeout(0.2) as bound:
            with pytest.raises(error_type) as caught:
                await response(scope(version), receive, send)
        assert not bound.expired()
        assert caught.value is original
        assert caller.cancelling() == initial_cancelling
        assert closed == [True]
        assert body_bytes(messages) == (b"" if stage == "headers" else b"x")
        assert all(m.get("more_body") is True for m in messages
                   if m["type"] == "http.response.body")
        assert receive_closed.is_set() == (version == "2.3")
        assert_private_absent(messages)

    asyncio.run(scenario())


@pytest.mark.parametrize("order", ["external_first", "disconnect_first"])
def test_legacy_disconnect_does_not_swallow_simultaneous_caller_cancellation(order):
    async def scenario():
        caller = asyncio.current_task()
        initial_cancelling = caller.cancelling()
        sending, receive_closed = asyncio.Event(), asyncio.Event()
        closed, messages = [], []

        async def receive():
            try:
                await sending.wait()
                if order == "external_first":
                    caller.cancel("external")
                else:
                    # Queue external cancellation before the caller resumes
                    # from the listener's own cancellation request.
                    asyncio.get_running_loop().call_soon(caller.cancel, "external")
                return {"type": "http.disconnect"}
            finally:
                receive_closed.set()

        async def send(message):
            messages.append(dict(message))
            if message.get("body"):
                sending.set()
                await asyncio.Event().wait()

        response = DocumentDownloadResponse(lambda: isolated_download(1, closed))
        try:
            async with asyncio.timeout(0.2) as bound:
                with pytest.raises(asyncio.CancelledError):
                    await response(scope("2.3"), receive, send)
            assert not bound.expired()
            assert caller.cancelling() == initial_cancelling + 1
            assert receive_closed.is_set()
            assert closed == [True]
            assert body_bytes(messages) == b"x"
            assert all(m.get("more_body") is True for m in messages
                       if m["type"] == "http.response.body")
        finally:
            # Remove the external request deliberately issued by this test.
            caller.uncancel()

    asyncio.run(scenario())
