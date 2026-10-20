"""Bound document MCP request bodies and final wire results without I/O."""
import json
from collections.abc import Mapping

from app.document_mcp_tools import DOCUMENT_OPERATIONS


MAX_DOCUMENT_MCP_REQUEST_BYTES = 512 * 1024
MAX_DOCUMENT_MCP_RESULT_BYTES = 65536
_DOCUMENT_WRITE_TOOLS = frozenset({
    "ocr_save_document_metadata", "ocr_approve_document_metadata",
})
_REQUEST_TOO_LARGE = "request_too_large: Document request body exceeds 524288 bytes."
_INVALID_REQUEST = "validation_error: Document request body could not be serialized."
_DOCUMENT_TOOLS = frozenset("ocr_" + operation for operation in DOCUMENT_OPERATIONS)
_TOO_LARGE = (
    "response_too_large: Document response exceeds the MCP limit; "
    "request a smaller page or chunk."
)
_INVALID = "invalid_api_response: Document response could not be serialized."


def _tool_error(ctx, message):
    result = {"content": [{"type": "text", "text": message}], "isError": True}
    if ctx.protocol_version == "2026-07-28":
        result["resultType"] = "complete"
    return result


class DocumentMCPInputGuard:
    """Bound raw write bodies before SDK dispatch and typed validation."""

    async def __call__(self, ctx, call_next):
        guarded = (
            ctx.method == "tools/call"
            and isinstance(ctx.params, Mapping)
            and isinstance(ctx.params.get("name"), str)
            and ctx.params["name"] in _DOCUMENT_WRITE_TOOLS
        )
        arguments = ctx.params.get("arguments") if guarded else None
        if isinstance(arguments, Mapping) and "body" in arguments:
            remaining = MAX_DOCUMENT_MCP_REQUEST_BYTES
            try:
                encoder = json.JSONEncoder(
                    ensure_ascii=False, allow_nan=False, separators=(",", ":"),
                )
                for chunk in encoder.iterencode(arguments["body"]):
                    # Reject oversized chunks before allocating their UTF-8 copy.
                    if len(chunk) > remaining:
                        return _tool_error(ctx, _REQUEST_TOO_LARGE)
                    encoded = chunk.encode("utf-8")
                    if len(encoded) > remaining:
                        return _tool_error(ctx, _REQUEST_TOO_LARGE)
                    remaining -= len(encoded)
            except (UnicodeError, TypeError, ValueError, RecursionError):
                return _tool_error(ctx, _INVALID_REQUEST)
        return await call_next(ctx)


class DocumentMCPOutputGuard:
    """Measure serialized results after all inner middleware has completed."""

    async def __call__(self, ctx, call_next):
        guarded = (
            ctx.method == "tools/call"
            and isinstance(ctx.params, Mapping)
            and isinstance(ctx.params.get("name"), str)
            and ctx.params["name"] in _DOCUMENT_TOOLS
        )
        result = await call_next(ctx)
        if not guarded:
            return result

        remaining = MAX_DOCUMENT_MCP_RESULT_BYTES
        try:
            if not isinstance(result, dict):
                raise TypeError("Expected a final result dictionary")
            encoder = json.JSONEncoder(
                ensure_ascii=False, allow_nan=False, separators=(",", ":"),
            )
            for chunk in encoder.iterencode(result):
                # UTF-8 cannot use fewer bytes than Unicode code points. Avoid
                # allocating an encoded copy of a chunk already over budget.
                if len(chunk) > remaining:
                    return _tool_error(ctx, _TOO_LARGE)
                encoded = chunk.encode("utf-8")
                if len(encoded) > remaining:
                    return _tool_error(ctx, _TOO_LARGE)
                remaining -= len(encoded)
        except (UnicodeError, TypeError, ValueError, RecursionError):
            return _tool_error(ctx, _INVALID)
        return result
