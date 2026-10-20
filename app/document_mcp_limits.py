"""Bound final document MCP wire results without runtime dependencies or I/O."""
import json
from collections.abc import Mapping

from app.document_mcp_tools import DOCUMENT_OPERATIONS


MAX_DOCUMENT_MCP_RESULT_BYTES = 65536
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
