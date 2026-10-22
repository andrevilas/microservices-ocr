"""Pure, fixed HTTP mapping for bounded document MCP reads and writes.

Paths are relative to the API version prefix. This module performs no I/O,
reference fetching, pagination, retries, or client/registry registration.
"""
import json
import re

from mcp.server.mcpserver.exceptions import ToolError

from .metadata_models import MAX_REQUEST_BYTES, ApprovalInput, SaveMetadataInput


_ID = re.compile(r"[0-9a-f]{32}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,63}")
_KEY = re.compile(r"[A-Za-z0-9._:-]{8,128}")
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


_WRITES = {
    "save_document_metadata": ("PUT", _ROOT + "/metadata", SaveMetadataInput),
    "approve_document_metadata": (
        "POST", _ROOT + "/metadata/approve", ApprovalInput,
    ),
}


def document_body(body):
    """Encode a JSON dictionary once, within the shared request byte limit."""
    if body is None:
        return None
    error = "invalid_request: Invalid document body."
    try:
        if type(body) is not dict:
            raise ValueError
        encoder = json.JSONEncoder(
            ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        )
        result = bytearray()
        for chunk in encoder.iterencode(body):
            remaining = MAX_REQUEST_BYTES - len(result)
            # Check codepoint count before allocating encoded chunk bytes.
            if len(chunk) > remaining:
                error = "request_too_large: Document body exceeds its byte limit."
                break
            encoded = chunk.encode("utf-8", errors="strict")
            if len(encoded) > remaining:
                error = "request_too_large: Document body exceeds its byte limit."
                break
            result.extend(encoded)
        else:
            return bytes(result)
    except Exception:
        pass
    raise ToolError(error) from None


def _write_request(operation, args):
    method, path, model = _WRITES[operation]
    try:
        if type(args) not in (list, tuple) or len(args) != 3:
            raise ValueError
        job_id, body, key = args
        if not _matches(job_id, _ID) or not _matches(key, _KEY):
            raise ValueError
        if type(body) is not dict and not isinstance(body, model):
            raise ValueError
        # Validate original nested instances before any serializer can coerce.
        validated = model.model_validate(body, strict=True)
        detached = validated.model_dump(mode="python", by_alias=True, warnings="error")
        validated = model.model_validate(detached, strict=True)
        body = validated.model_dump(
            mode="json", by_alias=True, exclude_none=False, warnings="error",
        )
    except Exception:
        pass
    else:
        document_body(body)
        return method, path.format(job_id=job_id), {}, body, {"Idempotency-Key": key}
    raise ToolError("validation_error: Invalid document request.") from None


def document_request(operation, args):
    """Return (method, relative_path, query, body, headers) for one operation.

    args must be a positional list or tuple in document_mcp_tools signature
    order. Omitted trailing optional arguments receive the tool defaults.
    Only None query values are omitted; continuations remain caller-controlled.
    Errors are fixed messages and never include caller-supplied content.
    """
    if type(operation) is not str or operation not in (*_OPERATIONS, *_WRITES):
        raise ToolError("unknown_operation: Unsupported document operation.") from None
    if operation in _WRITES:
        return _write_request(operation, args)
    path, names, required, defaults, query_names = _OPERATIONS[operation]
    if type(args) not in (list, tuple) or not required <= len(args) <= len(names):
        raise ToolError("validation_error: Invalid document request.") from None
    values = tuple(args) + defaults[len(args) - required:]
    if not all(_valid(name, value, operation) for name, value in zip(names, values)):
        raise ToolError("validation_error: Invalid document request.") from None
    parameters = dict(zip(names, values))
    query = {name: parameters[name] for name in query_names if parameters[name] is not None}
    return "GET", path.format_map(parameters), query, None, None
