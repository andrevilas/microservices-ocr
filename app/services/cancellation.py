"""Cooperative job cancellation and bounded termination of owned process trees."""
from contextlib import contextmanager
from contextvars import ContextVar
import os
import signal
import subprocess
import tempfile
import time

_checker = ContextVar("ocr_cancel_checker", default=None)


class JobCancelled(Exception):
    pass


class ProcessTerminationError(Exception):
    pass


@contextmanager
def cancellation_scope(checker):
    token = _checker.set(checker)
    try:
        checkpoint()
        yield
    finally:
        _checker.reset(token)


def active():
    return _checker.get() is not None


def checkpoint():
    checker = _checker.get()
    if checker is not None and checker():
        raise JobCancelled()


def terminate_tree(process):
    """Only the process/group created by this runner is targeted; never shell input."""
    if os.name == "nt":
        # /T covers OCRmyPDF/Poppler/Tesseract descendants; /F avoids console prompts.
        result = subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise ProcessTerminationError("Process tree termination failed.")
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        time.sleep(0.2)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        raise ProcessTerminationError("Process tree did not exit.") from None


def run_process(command, *, timeout, check=True):
    checkpoint()
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    # Keep stdout off the MCP stream; only bounded diagnostic tails enter memory.
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=output, **flags)
        deadline = time.monotonic() + timeout
        try:
            while True:
                checkpoint()
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    process.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    pass
        except BaseException:
            try:
                terminate_tree(process)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ProcessTerminationError("Process tree termination could not be confirmed.") from exc
            raise
        checkpoint()
        output.seek(0, os.SEEK_END)
        output.seek(max(0, output.tell() - 65536))
        tail = output.read().decode("utf-8", errors="replace")
        if process.returncode and check:
            raise subprocess.CalledProcessError(process.returncode, command, stderr=tail)
        return subprocess.CompletedProcess(command, process.returncode, stdout=tail, stderr="")
