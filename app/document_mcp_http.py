"""Pure, fixed HTTP mapping for bounded document MCP reads.

Paths are relative to the API version prefix. This module performs no I/O,
reference fetching, pagination, retries, or client/registry registration.
"""
import re

from mcp.server.mcpserver.exceptions import ToolError


_ID = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,63}")
_ROOT = "/jobs/{job_id}"
_REVISION = _ROOT + "/metadata/revisions/{revision_id}"
_FIELD_ROOT = _REVISION + "/segments/{segment_id}/fields"

# Each entry fixes the path, positional argument order, required argument count,
# trailing defaults, and query keys. There is no dynamic operation dispatch.
_OPERATIONS = {
    "get_document_canonical_manifest": (
        _ROOT + "/canonical/manifest", ("job_id",), 1, (), (),
    ),
    "get_document_canonical_chunk": (
        _ROOT + "/canonical/pages",
        ("job_id", "page", "offset", "max_chars"), 1, (1, 0, 1000),
        ("page", "offset", "max_chars"),
    ),
    "get_document_metadata": (
        _ROOT + "/metadata", ("job_id",), 1, (), (),
    ),
    "list_document_metadata_revisions": (
        _ROOT + "/metadata/revisions", ("job_id", "limit", "after"),
        1, (25, None), ("limit", "after"),
    ),
    "get_document_metadata_revision": (
        _REVISION, ("job_id", "revision_id", "sha256"), 3, (), ("sha256",),
    ),
    "list_document_metadata_fields": (
        _FIELD_ROOT,
        ("job_id", "revision_id", "segment_id", "sha256", "limit", "after"),
        4, (20, None), ("sha256", "limit", "after"),
    ),
    "list_document_metadata_values": (
        _FIELD_ROOT + "/{field_name}/values",
        ("job_id", "revision_id", "segment_id", "field_name", "sha256", "limit", "after"),
        5, (20, None), ("sha256", "limit", "after"),
    ),
    "list_document_metadata_evidence": (
        _FIELD_ROOT + "/{field_name}/evidence",
        ("job_id", "revision_id", "segment_id", "field_name", "sha256", "limit", "after"),
        5, (10, None), ("sha256", "limit", "after"),
    ),
    "get_document_metadata_string": (
        _FIELD_ROOT + "/{field_name}/strings/{part}",
        ("job_id", "revision_id", "segment_id", "field_name", "sha256", "part",
         "item_index", "offset", "max_chars"),
        6, (None, 0, 1000), ("sha256", "item_index", "offset", "max_chars"),
    ),
    "get_document_metadata_schema": (
        _REVISION + "/schemas/{schema_sha256}",
        ("job_id", "revision_id", "schema_sha256", "sha256", "offset", "max_chars"),
        4, (0, 1000), ("sha256", "offset", "max_chars"),
    ),
}


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _matches(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def _valid(name, value, operation):
    if name in ("job_id", "revision_id", "segment_id"):
        return _matches(value, _ID)
    if name in ("sha256", "schema_sha256"):
        return _matches(value, _DIGEST)
    if name == "field_name":
        return _matches(value, _FIELD)
    if name == "part":
        return type(value) is str and value in ("raw", "normalized")
    if name == "after":
        return value is None or (type(value) is str and len(value) <= 2048)
    if name == "item_index":
        return value is None or _integer(value, 0, 99)
    if name == "page":
        return _integer(value, 1, 300)
    if name == "offset":
        high = 500000 if operation == "get_document_canonical_chunk" else 262144
        return _integer(value, 0, high)
    if name == "max_chars":
        return _integer(value, 1, 1000)
    if name == "limit":
        high = 100 if operation == "list_document_metadata_revisions" else 50
        return _integer(value, 1, high)
    return False


def document_request(operation, args):
    """Return (method, relative_path, query, body, headers) for one read.

    args must be a positional list or tuple in document_mcp_tools signature
    order. Omitted trailing optional arguments receive the tool defaults.
    Only None query values are omitted; continuations remain caller-controlled.
    Errors are fixed messages and never include caller-supplied content.
    """
    if type(operation) is not str or operation not in _OPERATIONS:
        raise ToolError("unknown_operation: Unsupported document operation.") from None
    path, names, required, defaults, query_names = _OPERATIONS[operation]
    if type(args) not in (list, tuple) or not required <= len(args) <= len(names):
        raise ToolError("validation_error: Invalid document request.") from None
    values = tuple(args) + defaults[len(args) - required:]
    if not all(_valid(name, value, operation) for name, value in zip(names, values)):
        raise ToolError("validation_error: Invalid document request.") from None
    parameters = dict(zip(names, values))
    query = {name: parameters[name] for name in query_names if parameters[name] is not None}
    return "GET", path.format_map(parameters), query, None, None
