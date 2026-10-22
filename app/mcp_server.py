"""MCP application adapter. Documents are untrusted data; tools never accept filesystem paths."""
import sqlite3

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.application import ApplicationError, get_job_application
from app.catalog_mcp_tools import CATALOG_OPERATIONS
from app.mcp_tools import INSTRUCTIONS, MCP_VERSION, OcrMCPServer, register_tools
from app.document_mcp_tools import DOCUMENT_OPERATIONS
from app.document_mcp_runtime import execute_document
from app.rag_mcp_tools import RAG_OPERATIONS
from app.security import authenticate_bearer
from app.services.admission_service import submit_and_enqueue
from app.services.job_queue import get_job_queue_processor
from app.services.upload_service import get_upload_service
from app.services.batch_service import dispatch_batch, get_batch_service
from app.services.export_service import get_export_service
from app.services.control_service import get_control_service, retry_and_enqueue


def _retry_hint(operation):
    # PUT updates carry no key and are never replayed: the outcome must be read back first.
    if operation in ("update_context", "update_document_type"):
        return "read the current revision with the matching get tool before trying again"
    return "retry submissions with the same key"


def execute_catalog(services, principal, operation, args):
    """Explicit mapping to the keyword-aware catalog services; create/publish return only the receipt."""
    if operation == "list_context_templates":
        return services.templates.list(principal)
    if operation == "clone_context_template":
        template_id, body, key = args
        return services.templates.clone(principal, template_id, body, key)[0]
    contexts, types = services.contexts, services.types
    if operation == "list_contexts":
        limit, after = args
        return contexts.list(principal, limit=limit, after=after)
    if operation == "create_context":
        body, key = args
        return contexts.create(principal=principal, body=body, key=key)[0]
    if operation == "get_context":
        return contexts.get(principal, *args)
    if operation == "update_context":
        context_id, body = args
        return contexts.update(principal, context_id, body.body, expected_revision=body.expected_revision)
    if operation == "list_document_types":
        context_id, limit, after = args
        return types.list(principal, context_id, limit=limit, after=after)
    if operation == "create_document_type":
        context_id, body, key = args
        return types.create(principal=principal, context_id=context_id, body=body, key=key)[0]
    if operation == "get_document_type":
        return types.get(principal, *args)
    if operation == "update_document_type":
        type_id, body = args
        return types.update(principal, type_id, body.body, expected_revision=body.expected_revision)
    if operation == "publish_document_type":
        type_id, body, key = args
        return types.publish(principal, type_id, expected_revision=body.expected_revision,
                             expected_context_revision=body.expected_context_revision, key=key)[0]
    if operation == "deprecate_document_type_version":
        type_id, version, body, key = args
        return types.deprecate(principal, type_id, version,
                               expected_lifecycle_revision=body.expected_lifecycle_revision, key=key)[0]
    if operation == "list_document_type_versions":
        type_id, limit, after = args
        return types.list_versions(principal, type_id, limit=limit, after=after)
    if operation == "get_document_type_version":
        type_id, version = args
        return types.get_version(principal, type_id, version)
    raise ValueError(operation)


class BearerAuth:
    """Authenticate every request, including discovery; do not bind auth to a session."""
    def __init__(self, app, request_authenticator=None):
        self.app = app
        self.request_authenticator = request_authenticator

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        if request.url.path != "/mcp":
            return await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
        header = request.headers.get("authorization", "")
        scheme, _, token = header.partition(" ")
        try:
            principal = (await self.request_authenticator(request) if self.request_authenticator else
                         authenticate_bearer(token if scheme.lower() == "bearer" else None))
        except ApplicationError as error:
            response = JSONResponse({"error": {"code": error.code, "message": error.message}},
                                    status_code=error.status, headers={"WWW-Authenticate": "Bearer"})
            return await response(scope, receive, send)
        scope.setdefault("state", {})["ocr_principal"] = principal
        await self.app(scope, receive, send)


def build_mcp_server(service_provider=get_job_application, upload_provider=get_upload_service,
                     queue_provider=get_job_queue_processor, batch_provider=get_batch_service,
                     export_provider=get_export_service, control_provider=get_control_service,
                     *, request_authenticator=None, transport_security=None, catalog_provider=None,
                     document_provider=None, rag_provider=None):
    server = OcrMCPServer("OCR Recognizer", version=MCP_VERSION, instructions=INSTRUCTIONS)
    if catalog_provider is None:
        # Imported only here so the pure registry and stdio never load the runtime. Resolved lazily per call.
        from app.services.catalog_runtime import CatalogProvider
        catalog_provider = CatalogProvider(service_provider)

    async def invoke(ctx: Context, operation: str, *args):
        request = ctx.request_context.request
        principal = request.state.ocr_principal if request else None
        if principal is None:
            raise ToolError("authentication_required: A valid Bearer token is required.")

        def execute():
            if operation in RAG_OPERATIONS:
                service = rag_provider() if rag_provider else None
                if service is None:
                    raise ApplicationError(503, "rag_unavailable", "RAG delivery is not enabled.")
                return getattr(service, operation)(principal, *args)
            if operation in CATALOG_OPERATIONS or operation in ("deprecate_document_type_version", "list_context_templates", "clone_context_template"):
                return execute_catalog(catalog_provider(), principal, operation, args)
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
            if operation in DOCUMENT_OPERATIONS:
                return await execute_document(document_provider, principal, operation, args)
            # Shield admission/queue handoff from request cancellation. The job's
            # persisted lifecycle is independent of the MCP connection.
            return await run_in_threadpool(execute)
        except ApplicationError as error:
            raise ToolError(f"{error.code}: {error.message}") from None
        except (OSError, sqlite3.Error):
            raise ToolError(f"storage_unavailable: Storage is unavailable; {_retry_hint(operation)}.") from None
        except Exception:
            raise ToolError(f"internal_error: Operation could not be completed; {_retry_hint(operation)}.") from None

    register_tools(server, invoke)

    # Explicit loopback allowlist includes both default and non-default ports.
    security = transport_security or TransportSecuritySettings(
        allowed_hosts=["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "[::1]", "[::1]:*"],
        allowed_origins=["http://localhost", "http://localhost:*", "http://127.0.0.1", "http://127.0.0.1:*", "http://[::1]", "http://[::1]:*"],
    )
    transport = server.streamable_http_app(json_response=True, stateless_http=True,
                                            max_request_body_size=2 * 1024 * 1024, transport_security=security)
    return server, BearerAuth(transport, request_authenticator)
