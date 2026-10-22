"""Lossless content, canonical budgets, scoped cursors and source integrity."""

import base64
import copy
import hashlib
import json

import pytest
from pydantic import ValidationError

from app.application import ApplicationError
from app.document_content_models import MetadataStringChunk, MetadataValuePage
from app.document_models import FieldSpec, compose_schema
from app.document_transport_models import MAX_PROJECTION_BYTES
from app.metadata_models import EvidenceRef, MetadataSnapshot, SchemaSnapshot, _bounded_canonical
from app.services import document_content_projection as content
from app.services import document_projection as projection
from test_document_projection import OWNER, MARKER, digest, ident, make_snapshot


def canonical(value):
    return _bounded_canonical(value.model_dump(mode="json", by_alias=True), 1024 * 1024)


def checked(value):
    assert len(canonical(value)) <= MAX_PROJECTION_BYTES
    return value


def failure(operation, code, status=422):
    with pytest.raises(ApplicationError) as caught:
        operation()
    error = caught.value
    assert getattr(error, "status", getattr(error, "status_code", None)) == status
    assert error.code == code and error.message == projection._ERRORS[code][1]
    assert error.__cause__ is None and error.__context__ is None
    assert MARKER not in str(error) + repr(vars(error))


def changed(source, **updates):
    data = source.model_dump(mode="python", by_alias=True)
    data["segments"][0]["fields"][0].update(updates)
    return MetadataSnapshot.model_validate(data, strict=True)


def call(function, source, **kwargs):
    return function(OWNER, source, digest(source), ident(100), "field_0", **kwargs)


def scope(source, view="values"):
    return dict(owner=OWNER, job_id=source.job_id, revision_id=source.revision_id,
                sha256=digest(source), segment_id=ident(100), field_name="field_0", view=view)


def pages(function, source, **kwargs):
    after = None
    while True:
        page = checked(call(function, source, after=after, **kwargs))
        yield page
        after = page.next_cursor
        if after is None:
            break
        assert getattr(page, "values", getattr(page, "evidence", []))


@pytest.mark.parametrize("raw,normalized,kind,count", [
    (None, None, "null", 0), (False, 0, "boolean", 1),
    (0, False, "integer", 1), ("", "", "string", 1), ([], [], "list", 0),
    ([0, False, "", "x"], ["0", 0, False, True], "list", 4),
])
def test_lossless_kinds_and_scalar_or_list_pairs(raw, normalized, kind, count):
    source = changed(make_snapshot(values=[raw]), normalized=normalized)
    result = checked(call(content.value_page, source))
    assert result.raw_kind == kind and result.total_count == count
    assert result.next_cursor is None
    assert [pair.position for pair in result.values] == list(range(count))
    if count:
        raws = raw if type(raw) is list else [raw]
        normals = normalized if type(normalized) is list else [normalized]
        for index, pair in enumerate(result.values):
            assert pair.item_index == (index if kind == "list" else None)
            assert type(pair.raw.value) is type(raws[index])
            assert pair.raw.value == raws[index]
            assert type(pair.normalized.value) is type(normals[index])
            assert pair.normalized.value == normals[index]


@pytest.mark.parametrize("text,is_ref", [
    ("x" * 1000, False), ("漢" * 1000, False),
    ("😀" * 1000, False), ("x" * 1001, True),
    ("\x01" * 1000, True), (("漢😀\"\\\n" * 600), True),
])
def test_string_reference_thresholds_and_exact_codepoint_reconstruction(text, is_ref):
    source = make_snapshot(values=[text])
    pair = checked(call(content.value_page, source)).values[0]
    for part in ("raw", "normalized"):
        component = getattr(pair, part)
        assert (component.kind == "string_ref") is is_ref
        if is_ref:
            ref = component.reference
            assert ref.part == part and ref.item_index is None
            assert ref.characters == len(text)
            assert ref.utf8_bytes == len(text.encode("utf-8"))
            assert ref.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
        else:
            assert component.value == text
        offset, chunks = 0, []
        while True:
            chunk = checked(call(content.string_chunk, source, part=part, offset=offset))
            assert chunk.text == text[offset:chunk.end]
            assert len(chunk.text) <= 1000
            assert chunk.utf8_bytes == len(text.encode("utf-8"))
            assert chunk.content_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
            chunks.append(chunk.text)
            if chunk.next_offset is None:
                break
            assert chunk.next_offset > offset
            offset = chunk.next_offset
        assert "".join(chunks) == text
    terminal = call(content.string_chunk, source, part="raw", offset=len(text))
    assert terminal.text == "" and terminal.next_offset is None
    failure(lambda: call(content.string_chunk, source, part="raw", offset=len(text) + 1),
            "invalid_metadata_query")


def test_list_references_address_part_and_exact_item_and_empty_string():
    source = changed(make_snapshot(values=[["漢" * 1500, "", False]]),
                     normalized=["Ω" * 1501, "", 0])
    page = call(content.value_page, source)
    assert page.values[0].raw.reference.item_index == 0
    assert page.values[0].normalized.reference.part == "normalized"
    result = call(content.string_chunk, source, part="normalized", item_index=0)
    assert result.text == "Ω" * 1000 and result.total_characters == 1501
    empty = call(content.string_chunk, source, part="raw", item_index=1)
    assert empty.text == "" and empty.end == 0 and empty.next_offset is None
    for kwargs in ({}, {"item_index": 2}, {"item_index": 3}):
        failure(lambda kwargs=kwargs: call(content.string_chunk, source, part="raw", **kwargs),
                "invalid_metadata_query")
    for raw in (None, False, 0, []):
        other = make_snapshot(values=[raw])
        failure(lambda: call(content.string_chunk, other, part="raw"), "invalid_metadata_query")
    scalar = make_snapshot(values=[""])
    failure(lambda: call(content.string_chunk, scalar, part="raw", item_index=0),
            "invalid_metadata_query")


def test_values_pack_by_bytes_and_reconstruct_all_positions():
    values = [f"{index:03d}" + "x" * 997 for index in range(100)]
    source = make_snapshot(values=[values])
    results = list(pages(content.value_page, source, limit=50))
    assert 1 < len(results[0].values) < 50
    pairs = [pair for page in results for pair in page.values]
    assert [pair.position for pair in pairs] == list(range(100))
    assert [pair.raw.value for pair in pairs] == values
    assert [pair.normalized.value for pair in pairs] == values
    token = projection.encode_cursor(99, **scope(source))
    assert call(content.value_page, source, after=token).values == []
    shorter = make_snapshot(values=[["a"]])
    token = projection.encode_cursor(1, **scope(shorter))
    failure(lambda: call(content.value_page, shorter, after=token), "invalid_metadata_cursor")


def test_all_128_evidence_refs_duplicates_quotes_and_item_indices_are_lossless():
    source = make_snapshot(values=[["a", "b"]])
    quote = "\x01" * 1000
    refs = [EvidenceRef(document_id=source.document_id, page=1, text_sha256="e" * 64,
                        start=0, end=len(quote), quote=quote, item_index=index % 2)
            for index in range(128)]
    source = changed(source, evidence=refs)
    results = list(pages(content.evidence_page, source, limit=50))
    assert 0 < len(results[0].evidence) < 10
    recovered = [ref for page in results for ref in page.evidence]
    assert recovered == refs and len(recovered) == 128
    assert recovered[0] == recovered[2] and recovered[0] is not refs[0]
    assert all(page.total_count == 128 for page in results)
    token = projection.encode_cursor(127, **scope(source, "evidence"))
    terminal = call(content.evidence_page, source, after=token)
    assert terminal.evidence == [] and terminal.next_cursor is None
    empty = call(content.evidence_page, make_snapshot(values=[None]))
    assert empty.total_count == 0 and empty.evidence == [] and empty.next_cursor is None


def test_large_single_enum_schema_reconstructs_exact_canonical_utf8_and_hash():
    composed = compose_schema([FieldSpec(
        name="field_0", type="enum", description="Synthetic",
        enum=[f"{index:02d}" + "漢" * 1300 for index in range(20)],
    )], [])
    assert len(composed.canonical) > 65536
    source = make_snapshot(values=[None])
    data = source.model_dump(mode="python", by_alias=True)
    data["schemas"] = [SchemaSnapshot(schema=json.loads(composed.canonical),
                                      schema_sha256=composed.sha256)]
    data["segments"][0]["schema_sha256"] = composed.sha256
    source = MetadataSnapshot.model_validate(data, strict=True)
    offset, chunks = 0, []
    while True:
        chunk = checked(content.schema_chunk(OWNER, source, digest(source), composed.sha256,
                                             offset=offset, max_chars=997))
        assert chunk.canonical_utf8_bytes == len(composed.canonical)
        assert chunk.schema_sha256 == composed.sha256
        assert chunk.text == composed.canonical.decode("utf-8")[offset:chunk.end]
        chunks.append(chunk.text)
        if chunk.next_offset is None:
            break
        assert chunk.next_offset > offset
        offset = chunk.next_offset
    raw = "".join(chunks).encode("utf-8")
    assert raw == composed.canonical and hashlib.sha256(raw).hexdigest() == composed.sha256
    assert json.loads(raw) == source.schemas[0].schema_document
    total = len(raw.decode("utf-8"))
    terminal = content.schema_chunk(OWNER, source, digest(source), composed.sha256, offset=total)
    assert terminal.text == "" and terminal.next_offset is None
    failure(lambda: content.schema_chunk(OWNER, source, digest(source), composed.sha256,
                                         offset=total + 1), "invalid_metadata_query")


@pytest.mark.parametrize("key,value", [
    ("limit", True), ("limit", 0), ("limit", 51), ("limit", "2"), ("limit", 1.0),
    ("field_name", "INVALID"), ("segment_id", True), ("owner", MARKER),
])
def test_page_queries_are_strict_before_source_access(key, value):
    args = dict(owner=OWNER, snapshot=object(), sha256="a" * 64,
                segment_id=ident(100), field_name="field_0", limit=20)
    args[key] = value
    for function in (content.value_page, content.evidence_page):
        failure(lambda function=function: function(**args), "invalid_metadata_query")


@pytest.mark.parametrize("key,value", [
    ("offset", True), ("offset", -1), ("offset", 262145), ("offset", 1.0),
    ("max_chars", 0), ("max_chars", 1001), ("max_chars", "1"),
    ("max_chars", False), ("part", "other"), ("item_index", True),
    ("item_index", -1), ("item_index", 100), ("item_index", 0.0),
])
def test_chunk_queries_are_strict_before_source_access(key, value):
    args = dict(owner=OWNER, snapshot=object(), sha256="a" * 64,
                segment_id=ident(100), field_name="field_0", part="raw")
    args[key] = value
    failure(lambda: content.string_chunk(**args), "invalid_metadata_query")
    if key in ("offset", "max_chars"):
        failure(lambda: content.schema_chunk(OWNER, object(), "a" * 64, "b" * 64,
                                             **{key: value}), "invalid_metadata_query")


def test_missing_resources_and_corrupt_original_instances_are_sanitized():
    source = make_snapshot(values=[MARKER])
    sha = digest(source)
    failure(lambda: content.value_page(OWNER, source, sha, ident(999), "field_0"),
            "metadata_segment_not_found", 404)
    failure(lambda: content.evidence_page(OWNER, source, sha, ident(100), "absent"),
            "metadata_field_not_found", 404)
    failure(lambda: content.string_chunk(OWNER, source, sha, ident(100), "absent", part="raw"),
            "metadata_field_not_found", 404)
    failure(lambda: content.schema_chunk(OWNER, source, sha, "f" * 64),
            "metadata_schema_not_found", 404)
    for function in (content.value_page, content.evidence_page):
        failure(lambda function=function: function(OWNER, source, "f" * 64, ident(100), "field_0"),
                "metadata_unavailable", 503)
    source.segments[0].fields[0].__dict__["raw"] = 0.0
    operations = [
        lambda: content.value_page(OWNER, source, sha, ident(100), "field_0"),
        lambda: content.evidence_page(OWNER, source, sha, ident(100), "field_0"),
        lambda: content.string_chunk(OWNER, source, sha, ident(100), "field_0", part="raw"),
        lambda: content.schema_chunk(OWNER, source, sha, source.schemas[0].schema_sha256),
    ]
    for operation in operations:
        failure(operation, "metadata_unavailable", 503)


def test_cursors_bind_all_coordinates_and_old_view_hashes_remain_identical():
    source = make_snapshot(values=[[1, 2]])
    coordinates = scope(source)
    token = projection.encode_cursor(0, **coordinates)
    assert projection.decode_cursor(token, **coordinates) == 0
    assert projection.decode_cursor(None, **coordinates) is None
    for key, value in dict(owner="user:2", job_id=ident(9), revision_id=ident(9),
                           sha256="f" * 64, segment_id=ident(9),
                           field_name="field_1", view="evidence").items():
        failure(lambda key=key, value=value: projection.decode_cursor(
            token, **{**coordinates, key: value}), "invalid_metadata_cursor")
    for bad in (token + "=", "!", "A" * 2049, True):
        failure(lambda bad=bad: call(content.value_page, source, after=bad),
                "invalid_metadata_cursor")
    failure(lambda: projection.decode_cursor(None, **{**coordinates, "field_name": None}),
            "invalid_metadata_cursor")
    for view, position in (("revisions", ident(7)), ("fields", 0)):
        old = dict(owner=OWNER, job_id=source.job_id, view=view)
        extras = dict(revision_id=None, sha256=None, segment_id=None)
        if view == "fields":
            extras.update(revision_id=source.revision_id, sha256=digest(source), segment_id=ident(100))
        document = dict(owner_sha256=hashlib.sha256(OWNER.encode()).hexdigest(),
                        job_id=source.job_id, view=view, view_version=1, **extras)
        scope_hash = hashlib.sha256(_bounded_canonical(document, 1536)).hexdigest()
        raw = _bounded_canonical(dict(v=1, scope=scope_hash, position=position), 1536)
        expected = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        assert projection.encode_cursor(position, **old, **extras) == expected
        assert projection.decode_cursor(expected, **old, **extras) == position
        failure(lambda: projection.decode_cursor(None, **old, **extras, field_name="field_0"),
                "invalid_metadata_cursor")
    evidence_scope = scope(source, "evidence")
    assert projection.decode_cursor(projection.encode_cursor(127, **evidence_scope),
                                    **evidence_scope) == 127
    failure(lambda: projection.encode_cursor(128, **evidence_scope), "invalid_metadata_cursor")
    failure(lambda: projection.encode_cursor(100, **coordinates), "invalid_metadata_cursor")


def test_detachment_dto_coherence_and_control_exception_identity(monkeypatch):
    source = make_snapshot(values=[MARKER])
    first = call(content.value_page, source)
    second = call(content.value_page, source)
    first.values.clear()
    assert len(second.values) == len(source.segments[0].fields) == 1
    evidence = call(content.evidence_page, source)
    evidence.evidence.clear()
    assert len(source.segments[0].fields[0].evidence) == 1
    data = second.model_dump(mode="python")
    for update in ({"values": tuple(data["values"])}, {"total_count": True},
                   {"extra": MARKER}, {"raw_kind": "null"}):
        with pytest.raises(ValidationError):
            MetadataValuePage.model_validate({**data, **update}, strict=True)
    broken = copy.deepcopy(data)
    broken["values"][0]["raw"]["reference"] = {}
    with pytest.raises(ValidationError):
        MetadataValuePage.model_validate(broken, strict=True)
    chunk = call(content.string_chunk, source, part="raw")
    for update in ({"end": 0}, {"next_offset": 1}, {"offset": True}):
        with pytest.raises(ValidationError):
            MetadataStringChunk.model_validate({**chunk.model_dump(mode="python"), **update}, strict=True)
    for signal in (KeyboardInterrupt(), SystemExit(), GeneratorExit()):
        def interrupted(*args, signal=signal):
            raise signal
        with monkeypatch.context() as patch:
            patch.setattr(content, "_verified_snapshot", interrupted)
            with pytest.raises(type(signal)) as caught:
                call(content.value_page, source)
            assert caught.value is signal


def test_legal_oversize_item_returns_fixed_413(monkeypatch):
    # Exercise the shared packer's no-fitting-item path independently of DTO size assumptions.
    source = make_snapshot(values=["x"])
    with monkeypatch.context() as patch:
        patch.setattr(content, "_fits", lambda model: False)
        failure(lambda: call(content.value_page, source), "metadata_response_too_large", 413)
