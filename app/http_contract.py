"""HTTP v1 error boundary, correlation and public contract metadata."""
import logging
import sqlite3
from copy import deepcopy
from uuid import uuid4

from fastapi.routing import APIRoute
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.routing import Match

from app.application import ErrorResponse

logger = logging.getLogger(__name__)
API_VERSION = "1.1.0"


def is_v1(path: str) -> bool:
    return path == "/api/v1" or path.startswith("/api/v1/")


def error_response(request, status: int, code: str, message: str, *, details=None, headers=None):
    request_id = request.scope.get("state", {}).get("request_id") or uuid4().hex
    return JSONResponse({"error": {"code": code, "message": message, "details": details or []},
                         "request_id": request_id}, status_code=status,
                        headers={**(headers or {}), "X-Request-ID": request_id, "Cache-Control": "no-store"})


class V1Boundary:
    """Keep errors and headers consistent, including routing and unexpected failures.

    A server-generated ID is used; client-provided IDs are not trusted or logged.
    Once a streaming response starts, transport errors propagate normally.
    """
    def __init__(self, app, routes):
        self.app, self.routes = app, routes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not is_v1(scope["path"]):
            return await self.app(scope, receive, send)
        from starlette.requests import Request
        scope.setdefault("state", {})["request_id"] = uuid4().hex
        request = Request(scope)
        allowed, found = set(), False
        for route in self.routes:
            if not isinstance(route, APIRoute) or not is_v1(route.path):
                continue
            match, _ = route.matches(scope)
            if match == Match.FULL:
                found = True
                break
            if match == Match.PARTIAL:
                allowed.update(route.methods)
        if not found:
            response = (error_response(request, 405, "method_not_allowed", "Method not allowed.",
                                       headers={"Allow": ", ".join(sorted(allowed))}) if allowed else
                        error_response(request, 404, "route_not_found", "Route not found."))
            return await response(scope, receive, send)
        started, replaced = False, False

        async def contract_send(message):
            nonlocal started, replaced
            if replaced:
                return
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                if message["status"] >= 400 and not headers.get("content-type", "").startswith("application/json"):
                    # FileResponse emits plain-text Range errors directly.
                    code, text = ERRORS.get(message["status"], ("http_error", "Request could not be completed."))
                    preserved = {name: headers[name] for name in ("Content-Range", "Allow", "WWW-Authenticate", "Retry-After") if name in headers}
                    started, replaced = True, True
                    return await error_response(request, message["status"], code, text, headers=preserved)(scope, receive, send)
                headers["X-Request-ID"] = scope["state"]["request_id"]
                headers["Cache-Control"] = "no-store"
                started = True
            await send(message)

        try:
            await self.app(scope, receive, contract_send)
        except Exception as error:
            if started:
                raise
            # No exception text/body/path/credential in the public error or log.
            logger.error("HTTP request %s failed (%s)", scope["state"]["request_id"], type(error).__name__)
            if isinstance(error, (OSError, sqlite3.Error)):
                return await error_response(request, 503, "storage_unavailable", "Storage is unavailable.", headers={"Retry-After": "5"})(scope, receive, contract_send)
            await error_response(request, 500, "internal_error", "An internal error occurred.")(scope, receive, contract_send)


ERRORS = {
    400: ("invalid_request", "Invalid request."),
    401: ("authentication_required", "A valid Bearer token is required."),
    403: ("insufficient_scope", "Required permission is missing."),
    404: ("job_not_found", "Job not found."),
    405: ("method_not_allowed", "Method not allowed."),
    408: ("upload_timeout", "Upload deadline exceeded."),
    409: ("artifact_unavailable", "Artifact is not available."),
    410: ("upload_expired", "Upload has expired."),
    413: ("file_too_large", "Upload exceeds the accepted size."),
    415: ("unsupported_media_type", "Unsupported content type."),
    416: ("invalid_range", "Requested range is not satisfiable."),
    422: ("validation_error", "Invalid request parameters."),
    429: ("storage_quota_exceeded", "Input quota is exhausted."),
    500: ("internal_error", "An internal error occurred."),
    503: ("storage_unavailable", "Storage is unavailable."),
}


def error_responses(*statuses):
    return {status: {"model": ErrorResponse, "description": ERRORS[status][1],
                     "content": {"application/json": {"example": {
                         "error": {"code": ERRORS[status][0], "message": ERRORS[status][1], "details": []},
                         "request_id": "00000000000000000000000000000000"}}}}
            for status in statuses}


def operation(*scopes, admin=False):
    return {"x-required-scopes": list(scopes), "x-human-admin-required": admin}


def example(value, status=200):
    return {status: {"content": {"application/json": {"example": value}}}}


def install_openapi(application, routes):
    original = application.openapi

    def openapi():
        schema = original()
        schema["info"]["x-api-version"] = API_VERSION
        # FastAPI's OpenAPI encoder removes None from arbitrary example dicts.
        # Restore declared examples so nullable required fields remain valid JSON.
        for route in routes:
            for method in route.methods:
                responses = schema["paths"][route.path][method.lower()]["responses"]
                for status, response in route.responses.items():
                    for media, content in response.get("content", {}).items():
                        if "example" in content:
                            responses[str(status)]["content"][media]["example"] = deepcopy(content["example"])
        for path, methods in schema["paths"].items():
            if not is_v1(path):
                continue
            for method in methods.values():
                for response in method["responses"].values():
                    response.setdefault("headers", {}).update({
                        "X-Request-ID": {"description": "Server-generated correlation ID; client IDs are ignored.",
                                         "schema": {"type": "string", "pattern": "^[0-9a-f]{32}$"}},
                        "Cache-Control": {"description": "Authenticated responses are not cacheable.",
                                          "schema": {"type": "string", "const": "no-store"}},
                    })
        return schema

    application.openapi = openapi
