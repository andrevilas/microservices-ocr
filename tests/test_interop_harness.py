"""The AM-16 interop tools themselves: answer parsing and the secret-free result shape."""
import importlib.util
from pathlib import Path
import sys

import pytest

INTEROP = Path(__file__).resolve().parents[1] / "scripts" / "interop"
pytest.importorskip("mcp")


def load(name):
    sys.path.insert(0, str(INTEROP))
    try:
        spec = importlib.util.spec_from_file_location(name, INTEROP / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(INTEROP))


@pytest.mark.parametrize("text,expected", [
    ('{"status": "completed", "listed": 4}', {"status": "completed", "listed": 4}),
    ('Done.\n```json\n{"a": 1, "b": {"c": true}}\n```', {"a": 1, "b": {"c": True}}),
    ('first {"x": 1} then {"y": 2}', {"y": 2}),
    ("no json at all", None),
])
def test_host_answer_is_the_last_json_object(text, expected):
    assert load("hosts").last_json(text) == expected


def test_synthetic_pdf_carries_marker_on_every_page():
    from pypdf import PdfReader
    import io
    matrix = load("matrix")
    reader = PdfReader(io.BytesIO(matrix.synthetic_pdf(3)))
    assert len(reader.pages) == 3
    assert all(matrix.MARKER in page.extract_text() for page in reader.pages)


def test_recorder_keeps_details_but_no_tokens():
    matrix = load("matrix")
    recorder = matrix.Recorder()
    recorder.check("http", "whoami", True, status=200)
    recorder.check("http", "cancel", False, status="queued")
    assert recorder.rows == [{"client": "http", "check": "whoami", "ok": True, "status": 200},
                             {"client": "http", "check": "cancel", "ok": False, "status": "queued"}]
