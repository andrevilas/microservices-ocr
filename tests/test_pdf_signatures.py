"""Signed originals (OCR-EV-01): detection, report field and compatibility with reports written before it."""
from pathlib import Path

import pytest

from app.results import InputSignatures, JobReport
from app.services.pdf_signatures import inspect_signatures
from signed_pdf import cyclic_fields_pdf, plain_pdf, signed_pdf


def inspect(tmp_path, data):
    path = tmp_path / "input.pdf"
    path.write_bytes(data)
    return inspect_signatures(path)


def test_unsigned_pdf(tmp_path):
    assert inspect(tmp_path, plain_pdf()) == InputSignatures(signed=False, signature_count=0, certified=False)


def test_signed_field(tmp_path):
    assert inspect(tmp_path, signed_pdf()) == InputSignatures(signed=True, signature_count=1, certified=False)


def test_field_type_inherited_from_parent(tmp_path):
    assert inspect(tmp_path, signed_pdf(inherited=True)).signature_count == 1


def test_certification_counts_as_signed(tmp_path):
    result = inspect(tmp_path, signed_pdf(certified=True))
    assert result.signed and result.certified


def test_empty_signature_field_is_not_a_signature(tmp_path):
    assert inspect(tmp_path, signed_pdf(value=False)).signed is False


def test_cyclic_field_tree_terminates(tmp_path):
    assert inspect(tmp_path, cyclic_fields_pdf()).signed is False


@pytest.mark.parametrize("data", [b"%PDF-1.7\n garbage", b"not a pdf"])
def test_unreadable_structure_is_unknown(tmp_path, data):
    assert inspect(tmp_path, data).signed is None


LEGACY = sorted((Path(__file__).parent / "fixtures" / "legacy_reports").glob("report-*.json"))


def test_legacy_fixtures_cover_three_production_releases():
    assert {p.name.split("-")[1] for p in LEGACY} == {"f77349a", "596dfdd", "319a704"} and len(LEGACY) == 6


@pytest.mark.parametrize("path", LEGACY, ids=lambda p: p.stem)
def test_reports_written_by_earlier_releases_still_load(path):
    """Real report.json files produced by earlier production images (see fixtures/legacy_reports/generate.py)."""
    report = JobReport.model_validate_json(path.read_bytes())
    assert report.input_signatures is None
    assert report.output_validation.pdfa_conformance == "passed"
    assert report.output_validation.pdfa_validated_sha256 == report.output.sha256
    assert [page.page for page in report.pages] == list(range(1, report.page_count + 1))
    if path.stem.endswith("options"):
        # Written while economic was still offered: still readable whatever the current catalogue says.
        assert report.options_effective.compression_profile == "economic"
    # Round-trips without losing what the older release wrote.
    assert JobReport.model_validate_json(report.model_dump_json()) == report
