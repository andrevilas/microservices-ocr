"""Fixed mapping contracts, import purity, and real HTTP read integration."""
import asyncio
import subprocess
import sys

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app.document_canonical_models import CanonicalManifest, CanonicalPageChunk
from app.document_content_models import (
    MetadataEvidencePage, MetadataSchemaChunk, MetadataStringChunk, MetadataValuePage,
)
from app.document_mcp_http import document_request
from app.document_transport_models import MetadataFieldPage, MetadataRevisionPage, MetadataView
from test_document_http_basic import http
from test_document_metadata import PAGES, env, persisted, save

J, R, S, H, C = "a" * 32, "b" * 32, "c" * 32, "d" * 64, "e" * 64
ROOT = f"/jobs/{J}"
REV = ROOT + f"/metadata/revisions/{R}"
FIELDS = REV + f"/segments/{S}/fields"
# operation, required args, optional args, path, default query, explicit query, DTO
CASES = [
    ("get_document_canonical_manifest", [J], [], ROOT + "/canonical/manifest", {}, {}, CanonicalManifest),
    ("get_document_canonical_chunk", [J], [2, 7, 9], ROOT + "/canonical/pages",
     dict(page=1, offset=0, max_chars=1000), dict(page=2, offset=7, max_chars=9), CanonicalPageChunk),
    ("get_document_metadata", [J], [], ROOT + "/metadata", {}, {}, MetadataView),
    ("list_document_metadata_revisions", [J], [100, ""], ROOT + "/metadata/revisions",
     dict(limit=25), dict(limit=100, after=""), MetadataRevisionPage),
    ("get_document_metadata_revision", [J, R, H], [], REV,
     dict(sha256=H), dict(sha256=H), MetadataView),
    ("list_document_metadata_fields", [J, R, S, H], [50, "cursor"], FIELDS,
     dict(sha256=H, limit=20), dict(sha256=H, limit=50, after="cursor"), MetadataFieldPage),
    ("list_document_metadata_values", [J, R, S, "title", H], [1, "cursor"], FIELDS + "/title/values",
     dict(sha256=H, limit=20), dict(sha256=H, limit=1, after="cursor"), MetadataValuePage),
    ("list_document_metadata_evidence", [J, R, S, "title", H], [50, "cursor"], FIELDS + "/title/evidence",
     dict(sha256=H, limit=10), dict(sha256=H, limit=50, after="cursor"), MetadataEvidencePage),
    ("get_document_metadata_string", [J, R, S, "title", H, "raw"], [0, 0, 1], FIELDS + "/title/strings/raw",
     dict(sha256=H, offset=0, max_chars=1000), dict(sha256=H, item_index=0, offset=0, max_chars=1), MetadataStringChunk),
    ("get_document_metadata_schema", [J, R, C, H], [262144, 1], REV + f"/schemas/{C}",
     dict(sha256=H, offset=0, max_chars=1000), dict(sha256=H, offset=262144, max_chars=1), MetadataSchemaChunk),
]


@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
def test_mapping(case):
    op, required, optional, path, defaults, explicit, _ = case
    assert document_request(op, required) == ("GET", path, defaults, None, None)
    assert document_request(op, tuple(required + optional)) == ("GET", path, explicit, None, None)
    for count in range(len(optional) + 1):
        query = dict(defaults)
        names = list(explicit)
        optional_names = [name for name in names if name not in ("sha256",)]
        query.update(zip(optional_names[:count], optional[:count]))
        assert document_request(op, required + optional[:count])[2] == query


@pytest.mark.parametrize("case_index,index,bad", [
    (0, 0, value) for value in (None, 1, True, "A" * 32, "a" * 31, J + "\n",
                              "https://example.test", "/tmp/file", "../secret", "%2e%2e%2f")
] + [
    (4, 1, "../secret"), (5, 2, "%2fsecret"), (4, 2, "D" * 64),
    (8, 4, H + "\n"), (9, 2, "../schema"), (6, 3, "Title"), (6, 3, "a" * 65),
    (6, 3, "title/values"), (8, 5, "RAW"), (8, 5, "raw%2f.."),
    (1, 1, True), (1, 1, 0), (1, 1, 301), (1, 2, -1), (1, 2, 500001),
    (1, 3, 1001), (1, 3, 1.0), (3, 1, 101), (3, 1, False),
    (3, 2, "x" * 2049), (3, 2, False), (5, 4, 51), (6, 5, 0), (7, 5, 51),
    (8, 6, True), (8, 6, 100), (8, 7, 262145), (9, 4, 262145), (9, 5, 0),
])
def test_invalid_values(case_index, index, bad):
    op, required, optional, *_ = CASES[case_index]
    args = required + optional
    args[index] = bad
    with pytest.raises(ToolError) as caught:
        document_request(op, args)
    assert str(caught.value) == "validation_error: Invalid document request."


@pytest.mark.parametrize("args", [None, {}, J, [], [J, J]])
def test_wrong_argument_container_or_count(args):
    with pytest.raises(ToolError, match="^validation_error:"):
        document_request("get_document_metadata", args)


@pytest.mark.parametrize("op", [None, [], "GET", "get_document_canonical_page",
    "download_document_metadata", "get_document_metadata/content", "save_document_metadata",
    "approve_document_metadata", "https://example.test", "/jobs/../secret", "%2e%2e%2f"])
def test_unknown_operations(op):
    with pytest.raises(ToolError) as caught:
        document_request(op, [J])
    assert str(caught.value) == "unknown_operation: Unsupported document operation."


def test_fresh_import_is_pure():
    code = '''
import importlib.abc
import sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == p or fullname.startswith(p + '.')
               for p in ('app.services', 'app.config', 'app.main')):
            raise AssertionError('Forbidden import: ' + fullname)
sys.meta_path.insert(0, Guard())
from app.document_mcp_http import document_request
assert document_request('get_document_metadata', ['a' * 32])[0] == 'GET'
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_real_http_reads(http):
    e, app, client, services = http
    receipt, _ = save(e)
    snapshot = persisted(e, receipt)

    def schema_hashes(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "schema_sha256":
                    yield item
                else:
                    yield from schema_hashes(item)
        elif isinstance(value, list):
            for item in value:
                yield from schema_hashes(item)

    hashes = set(schema_hashes(snapshot.model_dump(mode="json")))
    assert len(hashes) == 1
    bindings = {J: e.job.job_id, R: receipt.revision_id,
                S: receipt.segment_ids["main"], H: receipt.sha256, C: hashes.pop()}

    async def read_all():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as session:
            for op, required, _, _, _, _, model in CASES:
                args = [bindings.get(value, value) for value in required]
                method, path, query, body, headers = document_request(op, args)
                assert body is headers is None
                response = await session.request(method, "/api/v1" + path, params=query)
                assert response.status_code == 200, response.text
                dto = model.model_validate_json(response.content)
                assert dto.job_id == e.job.job_id
                data = dto.model_dump(mode="json")
                if "revision_id" in data:
                    assert data["revision_id"] == receipt.revision_id and data["sha256"] == receipt.sha256
                if "segment_id" in data:
                    assert data["segment_id"] == receipt.segment_ids["main"]
                if op == "get_document_canonical_chunk":
                    assert dto.text == PAGES[0][:1000] and len(dto.text) == 1000
    asyncio.run(read_all())
    assert not e.jobs._artifact_leases
