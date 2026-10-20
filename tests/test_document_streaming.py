"""POSIX integration coverage for the internal asynchronous download lifecycle."""
import asyncio
import hashlib
import os
from threading import Event, get_ident

import pytest

from app.application import ApplicationError
from app.services.canonical_text_service import CanonicalTextService, PreparedCanonicalText
from app.services.document_execution import DocumentExecution
from app.services.document_streaming import DocumentStreams, _Transfer
from app.metadata_models import snapshot_bytes
from test_document_metadata import BASE, env, persisted, save, who

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real MetadataFiles requires POSIX")


async def until(predicate, timeout=3):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "download worker did not reach expected state"
        await asyncio.sleep(0.001)


async def idle(e, streams):
    await until(lambda: streams.execution.active_count == 0
                and not e.jobs._artifact_leases and not streams._workers)
    assert not e.jobs._lock.locked()


def streams_for(e, execution=None):
    return DocumentStreams(e.service, e.service.canonical,
                           execution if execution is not None else DocumentExecution())


def held(e, streams):
    assert streams.execution.active_count == 1
    assert e.jobs._artifact_leases == {e.job.job_id: 1}
    assert not e.jobs._lock.locked()


def denied(action, code):
    with pytest.raises(ApplicationError) as caught:
        action()
    assert caught.value.code == code
    return caught.value


def test_metadata_exact_bytes_one_snapshot_no_catalog_and_last_send(env, monkeypatch):
    e = env
    receipt, _ = save(e)
    expected = snapshot_bytes(persisted(e, receipt))
    streams = streams_for(e)
    reads, read = [], e.bundle.files.read

    def checked_read(*args, **kwargs):
        held(e, streams)
        reads.append(True)
        return read(*args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("download consulted the catalog")

    monkeypatch.setattr(e.bundle.files, "read", checked_read)
    monkeypatch.setattr(e.types, "get_version", forbidden)

    async def scenario():
        async with streams.open_metadata(
                e.owner, e.job.job_id, receipt.revision_id, sha256=receipt.sha256) as download:
            assert download.filename == "metadata.json"
            assert download.media_type == "application/json"
            assert download.sha256 == receipt.sha256 == hashlib.sha256(expected).hexdigest()
            assert download.size_bytes == len(expected)
            for name in ("filename", "media_type", "sha256", "size_bytes"):
                with pytest.raises(AttributeError):
                    setattr(download, name, "changed")
            iterator, chunks = download.iter_bytes(), []
            while True:
                try:
                    chunk = await anext(iterator)
                except StopAsyncIteration:
                    break
                assert type(chunk) is bytes and 0 < len(chunk) <= 65536
                chunks.append(chunk)
                held(e, streams)
                # This models an adapter awaiting send, including the last send.
                await asyncio.sleep(0.01)
                held(e, streams)
            assert b"".join(chunks) == expected
            assert reads == [True]
            await idle(e, streams)
        with pytest.raises(RuntimeError):
            download.iter_bytes()

    asyncio.run(scenario())


def test_canonical_exact_bytes_backpressure_and_last_send(env, monkeypatch):
    e, streams = env, streams_for(env)
    with e.service.canonical.open(e.owner, e.job.job_id) as prepared:
        manifest = prepared.manifest
        expected = b"".join(prepared.iter_jsonl())
    second_pass, read = [], PreparedCanonicalText._read_checked

    def checked_read(prepared, info):
        held(e, streams)
        second_pass.append(info.page)
        return read(prepared, info)

    monkeypatch.setattr(PreparedCanonicalText, "_read_checked", checked_read)

    async def scenario():
        async with streams.open_canonical(e.owner, e.job.job_id) as download:
            assert download.filename == "pages.jsonl"
            assert download.media_type == "application/x-ndjson"
            assert download.size_bytes == manifest["size_bytes"] == len(expected)
            assert download.sha256 == manifest["sha256"] == hashlib.sha256(expected).hexdigest()
            iterator = download.iter_bytes()
            first = await anext(iterator)
            await asyncio.sleep(0.02)
            assert second_pass == [1]  # Manifest preparation has already read both pages.
            held(e, streams)
            last = await anext(iterator)
            assert second_pass == [1, 2]
            assert all(type(chunk) is bytes and len(chunk) <= 256 * 1024
                       for chunk in (first, last))
            await asyncio.sleep(0.02)
            held(e, streams)
            with pytest.raises(StopAsyncIteration):
                await anext(iterator)
            assert first + last == expected
            await idle(e, streams)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["no_iteration", "early_break", "consumer_error"])
def test_context_exit_stops_prefetch_and_preserves_consumer_error(env, monkeypatch, mode):
    e, streams, reads = env, streams_for(env), []
    read = PreparedCanonicalText._read_checked

    def checked_read(prepared, info):
        reads.append(info.page)
        return read(prepared, info)

    monkeypatch.setattr(PreparedCanonicalText, "_read_checked", checked_read)
    consumer_error = RuntimeError("Synthetic consumer failure.")

    async def scenario():
        iterator = None
        try:
            async with streams.open_canonical(e.owner, e.job.job_id) as download:
                if mode == "no_iteration":
                    await until(lambda: reads == [1])
                else:
                    iterator = download.iter_bytes()
                    async for chunk in iterator:
                        assert chunk
                        held(e, streams)
                        if mode == "consumer_error":
                            raise consumer_error
                        break
        except RuntimeError as error:
            assert mode == "consumer_error" and error is consumer_error
        else:
            assert mode != "consumer_error"
        await idle(e, streams)
        assert reads == [1]
        if iterator is not None:
            await iterator.aclose()
        with pytest.raises(RuntimeError):
            download.iter_bytes()

    asyncio.run(scenario())


def test_single_use_and_iterator_created_before_context_exit(env):
    e, streams = env, streams_for(env)

    async def scenario():
        async with streams.open_canonical(e.owner, e.job.job_id) as download:
            iterator = download.iter_bytes()
            with pytest.raises(RuntimeError):
                download.iter_bytes()
        with pytest.raises(RuntimeError):
            await anext(iterator)
        await idle(e, streams)

    asyncio.run(scenario())


def test_disconnect_while_consumer_is_sending(env):
    e, streams = env, streams_for(env)

    async def scenario():
        sending = asyncio.Event()

        async def consumer():
            async with streams.open_canonical(e.owner, e.job.job_id) as download:
                iterator = download.iter_bytes()
                try:
                    await anext(iterator)
                    sending.set()
                    await asyncio.Event().wait()
                finally:
                    # Generator close must signal cancellation before its ack.
                    await iterator.aclose()

        task = asyncio.create_task(consumer())
        try:
            await asyncio.wait_for(sending.wait(), 2)
            held(e, streams)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await idle(e, streams)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["metadata", "canonical"])
@pytest.mark.parametrize("abandon", ["timeout", "cancel"])
def test_blocked_io_retains_permit_lease_and_shutdown_observes_it(
        env, monkeypatch, stage, abandon):
    e = env
    receipt, _ = save(e)
    execution = DocumentExecution(timeout_seconds=0.05)
    streams = streams_for(e, execution)
    started, release = Event(), Event()
    if stage == "metadata":
        original = e.bundle.files.read

        def blocked(*args, **kwargs):
            started.set()
            assert release.wait(5), "test failed to release metadata read"
            return original(*args, **kwargs)

        monkeypatch.setattr(e.bundle.files, "read", blocked)
    else:
        original = PreparedCanonicalText._read_checked

        def blocked(*args, **kwargs):
            started.set()
            assert release.wait(5), "test failed to release canonical second pass"
            return original(*args, **kwargs)

        monkeypatch.setattr(PreparedCanonicalText, "_read_checked", blocked)

    async def consumer():
        context = (streams.open_metadata(
            e.owner, e.job.job_id, receipt.revision_id, sha256=receipt.sha256)
            if stage == "metadata" else streams.open_canonical(e.owner, e.job.job_id))
        async with context as download:
            await anext(download.iter_bytes())

    async def scenario():
        task = asyncio.create_task(consumer())
        other = None
        try:
            await until(started.is_set)
            held(e, streams)
            if abandon == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(ApplicationError) as caught:
                    await task
                assert caught.value.code == "operation_timeout"
                assert caught.value.status == 408
            held(e, streams)
            assert denied(lambda: execution.acquire(e.owner), "document_operations_busy").status == 429
            other = execution.acquire(who("user:2"))
            assert execution.active_count == 2
            denied(lambda: execution.acquire(who("user:3")), "document_operations_busy")
            other.close()
            other = None
            with pytest.raises(ApplicationError) as caught:
                await execution.shutdown(timeout_seconds=0.01)
            assert caught.value.code == "document_operations_active"
            assert caught.value.status == 503
            held(e, streams)
        finally:
            if other is not None:
                other.close()
            release.set()  # Always unblock before asyncio.run shuts down its executor.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await idle(e, streams)
        await execution.shutdown(timeout_seconds=0.01)

    asyncio.run(scenario())


@pytest.mark.parametrize("case,code,status", [
    ("wrong_hash", "metadata_revision_conflict", 409),
    ("owner", "job_not_found", 404),
    ("scope", "insufficient_scope", 403),
    ("malformed", "invalid_metadata_input", 422),
    ("corrupt", "metadata_unavailable", 503),
])
def test_metadata_source_failures_precede_header_and_are_sanitized(
        env, case, code, status):
    e = env
    receipt, _ = save(e)
    streams = streams_for(e)
    principal, revision, digest = e.owner, receipt.revision_id, receipt.sha256
    if case == "wrong_hash":
        digest = "0" * 64
    elif case == "owner":
        principal = who("user:2")
    elif case == "scope":
        principal = who(scopes=BASE - {"metadata:read"})
    elif case == "malformed":
        revision = "invalid"
    else:
        path = e.job.working_dir / "results" / "metadata" / (revision + ".json")
        path.write_bytes(b"Synthetic corrupt private content")

    async def scenario():
        with pytest.raises(ApplicationError) as caught:
            async with streams.open_metadata(
                    principal, e.job.job_id, revision, sha256=digest):
                pytest.fail("source failure exposed a header")
        error = caught.value
        assert error.status == status
        # The job store's not-found code is owned by the existing admission service.
        if case != "owner":
            assert error.code == code
        assert error.message == "Document operation failed."
        assert error.__cause__ is None and error.__context__ is None
        await idle(e, streams)

    asyncio.run(scenario())


def test_canonical_corruption_fails_before_header(env):
    e, streams = env, streams_for(env)
    (e.job.working_dir / "results" / "page-000001.txt").write_bytes(b"\xff\xfe")

    async def scenario():
        with pytest.raises(ApplicationError) as caught:
            async with streams.open_canonical(e.owner, e.job.job_id):
                pytest.fail("corrupt preparation exposed a header")
        assert caught.value.code == "result_integrity_error"
        assert caught.value.message == "Document operation failed."
        await idle(e, streams)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["integrity", "unexpected"])
def test_producer_error_after_prefix_aborts_without_leaking(env, monkeypatch, failure):
    e, streams = env, streams_for(env)
    read = PreparedCanonicalText._read_checked

    def changed(prepared, info):
        if info.page == 2:
            if failure == "integrity":
                raise ApplicationError(503, "result_integrity_error", "PRIVATE /synthetic/path")
            raise ValueError("PRIVATE /synthetic/path")
        return read(prepared, info)

    monkeypatch.setattr(PreparedCanonicalText, "_read_checked", changed)

    async def scenario():
        async with streams.open_canonical(e.owner, e.job.job_id) as download:
            iterator = download.iter_bytes()
            assert await anext(iterator)
            held(e, streams)
            with pytest.raises(ApplicationError) as caught:
                await anext(iterator)
            assert caught.value.status == 503
            assert caught.value.code == ("result_integrity_error" if failure == "integrity"
                                         else "document_operations_unavailable")
            assert caught.value.message == ("Document operation failed." if failure == "integrity"
                                            else "Document operations are unavailable.")
            assert caught.value.__cause__ is None and caught.value.__context__ is None
        await idle(e, streams)

    asyncio.run(scenario())


@pytest.mark.parametrize("control_type", [KeyboardInterrupt, SystemExit])
def test_worker_control_exception_is_delivered_as_data_with_identity(
        env, monkeypatch, control_type):
    e = env
    receipt, _ = save(e)
    streams = streams_for(e)
    control = control_type("Synthetic worker control exception.")

    def interrupted(*args, **kwargs):
        raise control

    monkeypatch.setattr(e.bundle.files, "read", interrupted)

    async def scenario():
        # Catch in this coroutine, never in a background asyncio Task.
        with pytest.raises(control_type) as caught:
            async with streams.open_metadata(
                    e.owner, e.job.job_id, receipt.revision_id, sha256=receipt.sha256):
                pytest.fail("interrupted preparation exposed a header")
        assert caught.value is control
        await idle(e, streams)

    asyncio.run(scenario())


def test_submission_failure_releases_capacity(env, monkeypatch):
    e, streams = env, streams_for(env)

    async def scenario():
        def rejected(*args, **kwargs):
            raise RuntimeError("PRIVATE executor failure")

        monkeypatch.setattr(asyncio.get_running_loop(), "run_in_executor", rejected)
        with pytest.raises(ApplicationError) as caught:
            async with streams.open_canonical(e.owner, e.job.job_id):
                pytest.fail("failed submission exposed a header")
        assert caught.value.code == "document_operations_unavailable"
        await idle(e, streams)

    asyncio.run(scenario())


def test_constructor_and_worker_reject_changed_shared_identities(env):
    e, execution = env, DocumentExecution()
    denied(lambda: DocumentStreams(e.service, CanonicalTextService(e.jobs), execution),
           "metadata_unavailable")
    streams = streams_for(e, execution)
    streams.canonical_service = CanonicalTextService(e.jobs)

    async def scenario():
        with pytest.raises(ApplicationError) as caught:
            async with streams.open_canonical(e.owner, e.job.job_id):
                pytest.fail("changed shared identity exposed a header")
        assert caught.value.code == "metadata_unavailable"
        await idle(e, streams)

    asyncio.run(scenario())


def terminal_barrier(streams, monkeypatch):
    """Hold the real worker after permit close but before its future completes."""
    entered, release, transfers = Event(), Event(), []
    forget = streams._forget

    def delayed_forget(transfer):
        transfers.append(transfer)
        entered.set()
        try:
            assert release.wait(5), "test failed to release terminal handoff"
        finally:
            forget(transfer)

    monkeypatch.setattr(streams, "_forget", delayed_forget)
    return entered, release, transfers


@pytest.mark.parametrize("stage,kind", [
    ("eof", None),
    *[(stage, kind) for stage in ("before_header", "after_prefix")
      for kind in ("application", "unknown", "keyboard", "system")],
])
def test_terminal_handoff_waits_for_actual_outcome(env, monkeypatch, stage, kind):
    e, streams = env, streams_for(env)
    entered, release, transfers = terminal_barrier(streams, monkeypatch)
    gate, waiting, observed = Event(), Event(), Event()
    failure = None
    if kind == "application":
        failure = ApplicationError(
            403 if stage == "before_header" else 503,
            "insufficient_scope" if stage == "before_header" else "result_integrity_error",
            "PRIVATE synthetic source failure")
    elif kind is not None:
        failure = {"unknown": ValueError, "keyboard": KeyboardInterrupt,
                   "system": SystemExit}[kind]("PRIVATE synthetic source failure")

    if stage == "before_header":
        gate.set()

        def fail_preparation(_bundle):
            raise failure

        monkeypatch.setattr(type(e.bundle), "ensure_healthy", fail_preparation)
    elif stage == "after_prefix":
        read = PreparedCanonicalText._read_checked

        def fail_second_page(prepared, info):
            if info.page == 2:
                raise failure
            return read(prepared, info)

        monkeypatch.setattr(PreparedCanonicalText, "_read_checked", fail_second_page)

    receive = _Transfer.receive

    async def gated_receive(transfer):
        if gate.is_set():
            await until(entered.is_set)
            assert transfer is transfers[0]
            assert not transfer.future.done()
            waiting.set()
        return await receive(transfer)

    monkeypatch.setattr(_Transfer, "receive", gated_receive)

    async def consume():
        async with streams.open_canonical(e.owner, e.job.job_id) as download:
            assert stage != "before_header", "source failure exposed a header"
            iterator = download.iter_bytes()
            assert await anext(iterator)
            if stage == "eof":
                assert await anext(iterator)
            gate.set()
            if stage == "eof":
                with pytest.raises(StopAsyncIteration):
                    await anext(iterator)
            else:
                await anext(iterator)

    async def complete_handoff():
        await until(waiting.is_set)
        transfer = transfers[0]
        assert streams.execution.active_count == 0
        assert not e.jobs._artifact_leases
        assert transfer in streams._workers
        assert not transfer.future.done()
        observed.set()
        release.set()

    async def scenario():
        observer = asyncio.create_task(complete_handoff())
        try:
            if failure is None:
                await consume()
            else:
                expected = (ApplicationError if isinstance(failure, Exception)
                            else type(failure))
                # Control exceptions are caught in the awaiting caller, never
                # allowed to escape a detached asyncio Task.
                with pytest.raises(expected) as caught:
                    await consume()
                if isinstance(failure, Exception):
                    error = caught.value
                    assert error.status == (failure.status if kind == "application" else 503)
                    assert error.code == (failure.code if kind == "application"
                                          else "document_operations_unavailable")
                    assert error.message == ("Document operation failed." if kind == "application"
                                             else "Document operations are unavailable.")
                    assert error.__cause__ is None and error.__context__ is None
                else:
                    assert caught.value is failure
            assert observed.is_set(), "terminal phase was mistaken for worker completion"
            await observer
            assert transfers[0].future.result().error is failure
        finally:
            release.set()
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
            await idle(e, streams)

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["entry", "notification"])
def test_terminal_marker_is_rechecked_when_receive_checkpoint_raises(
        env, monkeypatch, path):
    e, streams = env, streams_for(env)
    entered, release, transfers = terminal_barrier(streams, monkeypatch)
    started, finish_source, waiting, observed = Event(), Event(), Event(), Event()
    failure = ApplicationError(422, "invalid_metadata_input", "PRIVATE source failure")

    def fail_preparation(_bundle):
        started.set()
        assert finish_source.wait(5), "test failed to release source preparation"
        raise failure

    monkeypatch.setattr(type(e.bundle), "ensure_healthy", fail_preparation)
    receive = _Transfer.receive

    async def racing_receive(transfer):
        await until(started.is_set)
        checkpoint = transfer.permit.control.checkpoint
        caller_thread, calls = get_ident(), 0

        def racing_checkpoint():
            nonlocal calls
            if get_ident() == caller_thread:
                calls += 1
                if calls == (1 if path == "entry" else 2):
                    # Advance the worker between receive's phase observation
                    # and the checkpoint's observation of closed-permit control.
                    finish_source.set()
                    assert entered.wait(3), "worker did not release its permit"
                    assert not transfer.future.done()
                    waiting.set()
            return checkpoint()

        monkeypatch.setattr(transfer.permit.control, "checkpoint", racing_checkpoint)
        if path == "notification":
            transfer._ready.set()  # A stale notification reaches the second checkpoint.
        return await receive(transfer)

    monkeypatch.setattr(_Transfer, "receive", racing_receive)

    async def complete_handoff():
        await until(waiting.is_set)
        assert not transfers[0].future.done()
        observed.set()
        release.set()

    async def scenario():
        observer = asyncio.create_task(complete_handoff())
        try:
            with pytest.raises(ApplicationError) as caught:
                async with streams.open_canonical(e.owner, e.job.job_id):
                    pytest.fail("source failure exposed a header")
            assert caught.value.status == 422
            assert caught.value.code == "invalid_metadata_input"
            assert caught.value.message == "Document operation failed."
            assert caught.value.__cause__ is None and caught.value.__context__ is None
            assert observed.is_set()
            await observer
            assert transfers[0].future.result().error is failure
        finally:
            finish_source.set()
            release.set()
            observer.cancel()
            await asyncio.gather(observer, return_exceptions=True)
            await idle(e, streams)

    asyncio.run(scenario())


@pytest.mark.parametrize("abandon", ["timeout", "cancel"])
def test_terminal_handoff_remains_deadline_bounded_and_cancellable(
        env, monkeypatch, abandon):
    e = env
    streams = streams_for(e, DocumentExecution(timeout_seconds=1))
    entered, release, transfers = terminal_barrier(streams, monkeypatch)

    def fail_preparation(_bundle):
        raise ApplicationError(403, "insufficient_scope", "PRIVATE source failure")

    monkeypatch.setattr(type(e.bundle), "ensure_healthy", fail_preparation)
    receive = _Transfer.receive

    async def gated_receive(transfer):
        await until(entered.is_set)
        assert not transfer.future.done()
        if abandon == "cancel":
            asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
        return await receive(transfer)

    monkeypatch.setattr(_Transfer, "receive", gated_receive)

    async def scenario():
        try:
            expected = asyncio.CancelledError if abandon == "cancel" else ApplicationError
            with pytest.raises(expected) as caught:
                async with streams.open_canonical(e.owner, e.job.job_id):
                    pytest.fail("unfinished handoff exposed a header")
            if abandon == "timeout":
                assert caught.value.status == 408
                assert caught.value.code == "operation_timeout"
            transfer = transfers[0]
            assert not transfer.future.done() and not transfer.future.cancelled()
            assert transfer in streams._workers
            assert streams.execution.active_count == 0
            assert not e.jobs._artifact_leases
        finally:
            release.set()
            await idle(e, streams)

    asyncio.run(scenario())
