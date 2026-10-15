"""MCP application adapter. Documents are untrusted data; tools never accept filesystem paths."""
import sqlite3

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.application import ApplicationError, get_job_application
from app.mcp_tools import INSTRUCTIONS, OcrMCPServer, register_tools
from app.security import authenticate_bearer
from app.services.admission_service import submit_and_enqueue
from app.services.job_queue import get_job_queue_processor
from app.services.upload_service import get_upload_service
from app.services.batch_service import dispatch_batch, get_batch_service
from app.services.export_service import get_export_service
from app.services.control_service import get_control_service, retry_and_enqueue


class BearerAuth:
    """Authenticate every request, including discovery; do not bind auth to a session."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        if request.url.path != "/mcp":
            return await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        try:
            principal = authenticate_bearer(token if scheme.lower() == "bearer" else None)
        except ApplicationError as error:
            response = JSONResponse({"error": {"code": error.code, "message": error.message}},
                                    status_code=error.status, headers={"WWW-Authenticate": "Bearer"})
            return await response(scope, receive, send)
        scope.setdefault("state", {})["ocr_principal"] = principal
        await self.app(scope, receive, send)


def build_mcp_server(service_provider=get_job_application, upload_provider=get_upload_service,
                     queue_provider=get_job_queue_processor, batch_provider=get_batch_service,
                     export_provider=get_export_service, control_provider=get_control_service):
    server = OcrMCPServer("OCR Recognizer", version="0.5.0", instructions=INSTRUCTIONS)

    async def invoke(ctx: Context, operation: str, *args):
        request = ctx.request_context.request
        principal = request.state.ocr_principal if request else None
        if principal is None:
            raise ToolError("authentication_required: A valid Bearer token is required.")

        def execute():
            if operation in ("cancel", "cancel_many"):
                return getattr(control_provider(), operation)(principal, *args)
            if operation == "retry":
                return retry_and_enqueue(control_provider(), queue_provider(), principal, *args)[0]
            if operation == "create_batch":
                return dispatch_batch(batch_provider(), queue_provider(), principal, *args)[0]
            if operation in ("get_batch", "list_batches"):
                return getattr(batch_provider(), "get" if operation == "get_batch" else "list")(principal, *args)
            if operation == "create_export":
                return export_provider().create(principal, *args)[0]
            if operation == "get_export":
                return export_provider().get(principal, *args)
            if operation == "prepare_upload":
                return upload_provider().reserve(principal, *args)
            if operation == "get_upload":
                return upload_provider().get(principal, *args)
            if operation == "submit_job":
                return submit_and_enqueue(upload_provider(), queue_provider(), principal, *args)[0]
            return getattr(service_provider(), operation)(principal, *args)

        try:
            # Shield admission/queue handoff from request cancellation. The job's
            # persisted lifecycle is independent of the MCP connection.
            return await run_in_threadpool(execute)
        except ApplicationError as error:
            raise ToolError(f"{error.code}: {error.message}") from None
        except (OSError, sqlite3.Error):
            raise ToolError("storage_unavailable: Storage is unavailable; retry submissions with the same key.") from None
        except Exception:
            raise ToolError("internal_error: Operation could not be completed; retry submissions with the same key.") from None

    register_tools(server, invoke)

    # Explicit loopback allowlist includes both default and non-default ports.
    security = TransportSecuritySettings(
        allowed_hosts=["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"],
        allowed_origins=["http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*", "http://[::1]", "http://[::1]:*"],
    )
    transport = server.streamable_http_app(json_response=True, stateless_http=True,
                                            max_request_body_size=65536, transport_security=security)
    return server, BearerAuth(transport)
