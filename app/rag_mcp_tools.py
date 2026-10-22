"""Pure MCP registry and REST mapping for delivery operations."""
import re
from typing import Any, get_args
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from app.rag_models import (DeliveryId, DeliveryInput, DeliveryList, DeliveryOperations, DeliveryState,
                            DeliveryStatus, DestinationList, RecoveryLink)

RAG_OPERATIONS = frozenset({"list_rag_destinations", "submit_rag_delivery", "get_rag_delivery",
                            "get_rag_receipt", "cancel_rag_delivery", "get_rag_recovery",
                            "retry_rag_delivery", "list_rag_deliveries", "get_rag_operations"})


def rag_request(operation, args):
    if operation == "list_rag_destinations":
        return "GET", "/rag/destinations", None
    if operation == "get_rag_operations":
        return "GET", "/rag/operations", None
    if operation == "list_rag_deliveries":
        state, limit = args
        if state is not None and state not in get_args(DeliveryState):
            raise ToolError("validation_error: Invalid delivery state.")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolError("validation_error: Invalid limit.")
        query = "?limit=" + str(limit) + ("&state=" + state if state else "")
        return "GET", "/rag/deliveries" + query, None
    if operation == "submit_rag_delivery":
        # Defaults omitted: a plain delivery stays readable by servers before ocr-rag/2.
        return "POST", "/rag/deliveries", args[0].model_dump(mode="json", exclude_defaults=True)
    delivery_id = args[0]
    if not isinstance(delivery_id, str) or not re.fullmatch("[0-9a-f]{64}", delivery_id):
        raise ToolError("validation_error: Invalid delivery identifier.")
    suffix = {"get_rag_delivery": "", "get_rag_receipt": "/receipt", "retry_rag_delivery": "/retry",
              "cancel_rag_delivery": "/cancel", "get_rag_recovery": "/recovery"}[operation]
    return ("POST" if operation in ("cancel_rag_delivery", "retry_rag_delivery") else "GET",
            "/rag/deliveries/" + delivery_id + suffix, None)


def register_rag_tools(server, invoke):
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    @server.tool(annotations=read)
    async def ocr_list_rag_destinations(ctx: Context) -> DestinationList:
        """List configured destinations owned by the caller. No URLs or credentials are accepted or returned."""
        return await invoke(ctx, "list_rag_destinations")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
    async def ocr_submit_rag_delivery(ctx: Context, body: DeliveryInput) -> DeliveryStatus:
        """Queue an owned completed OCR result. Replay the same job and destination after uncertainty. Submission is not an indexed receipt. include_graph=true (also requires metadata:read) adds the latest or the given APPROVED metadata snapshot and its ocr-graph/1, limited to the destination's sensitivity ceiling (contract ocr-rag/2); a job delivered without the graph keeps that delivery (rag_delivery_conflict)."""
        return await invoke(ctx, "submit_rag_delivery", body)

    @server.tool(annotations=read)
    async def ocr_get_rag_delivery(ctx: Context, delivery_id: DeliveryId) -> DeliveryStatus:
        """Read delivery progress. Resolution requires operator action; it never silently deletes documents."""
        return await invoke(ctx, "get_rag_delivery", delivery_id)

    @server.tool(annotations=read, structured_output=True)
    async def ocr_get_rag_receipt(ctx: Context, delivery_id: DeliveryId) -> dict[str, Any]:
        """Read the last verified receipt. Pending indexing is not indexed completion or a WORM guarantee."""
        return await invoke(ctx, "get_rag_receipt", delivery_id)

    @server.tool(annotations=read)
    async def ocr_get_rag_recovery(ctx: Context, delivery_id: DeliveryId) -> RecoveryLink:
        """Get authenticated HTTP download path/hash for a retained recovery package. No document bytes in MCP. Download never releases hold."""
        return await invoke(ctx, "get_rag_recovery", delivery_id)

    @server.tool(annotations=read)
    async def ocr_list_rag_deliveries(ctx: Context, state: DeliveryState | None = None, limit: int = 50) -> DeliveryList:
        """List owned deliveries, newest first, optionally by state (e.g. resolution). Administrators see every owner."""
        return await invoke(ctx, "list_rag_deliveries", state, limit)

    @server.tool(annotations=read)
    async def ocr_get_rag_operations(ctx: Context) -> DeliveryOperations:
        """Administrator-only aggregate worker state: queue counts, deadlines, resolution and worker failure. No documents."""
        return await invoke(ctx, "get_rag_operations")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    async def ocr_retry_rag_delivery(ctx: Context, delivery_id: DeliveryId) -> DeliveryStatus:
        """Return a delivery in resolution to the retry queue with a fresh attempt budget. Requires jobs:submit and a retained package; deletes nothing."""
        return await invoke(ctx, "retry_rag_delivery", delivery_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False))
    async def ocr_cancel_rag_delivery(ctx: Context, delivery_id: DeliveryId) -> DeliveryStatus:
        """Explicitly abandon delivery and release its hold, allowing expired files to be deleted. Requires artifacts:delete. Cannot undo a remote delivery."""
        return await invoke(ctx, "cancel_rag_delivery", delivery_id)
