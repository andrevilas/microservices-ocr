from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from app.application import ApplicationError, get_job_application
from app.config import settings
from app.mcp_server import build_mcp_server
from app.routes.api import router
from app.routes.auth import auth_router
from app.routes.v1 import router as v1_router
from app.routes.service_identities import router as identity_router
from app.services.job_queue import get_job_queue_processor
from app.services.runtime_lease import RuntimeLease
from app.services.user_store import get_user_store
from app.http_contract import API_VERSION, ERRORS, V1Boundary, error_response, is_v1, install_openapi


def create_app(service_provider=get_job_application) -> FastAPI:
    mcp_server, mcp_app = build_mcp_server(service_provider)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        lease = RuntimeLease(settings.ocr_tmp_dir)
        lease.acquire()
        application.state.runtime_lease = lease
        processor = None
        try:
            user_store = get_user_store()
            user_store.ensure_admin(name=settings.admin_name, email=settings.admin_email, password=settings.admin_password)
            processor = get_job_queue_processor()
            processor.start()
            async with mcp_server.session_manager.run():
                yield
        finally:
            # A timed-out worker keeps the lease until process exit.
            if processor is not None:
                processor.stop()
            lease.release()

    application = FastAPI(title=settings.app_name, version=API_VERSION, lifespan=lifespan,
                          description="OCR HTTP API. Versioned operations use Bearer user or individual service credentials; legacy routes retain separate contracts.")
    application.mount("/static", StaticFiles(directory="app/static"), name="static")
    application.include_router(router)
    application.include_router(auth_router)
    versioned_routes = []
    for versioned_router in (v1_router, identity_router):
        application.include_router(versioned_router)
        versioned_routes.extend(versioned_router.routes)
    if service_provider is not get_job_application:
        application.dependency_overrides[get_job_application] = service_provider
    application.mount("/", mcp_app)
    application.add_middleware(V1Boundary, routes=versioned_routes)

    @application.exception_handler(ApplicationError)
    async def application_error(_request, error: ApplicationError):
        if is_v1(_request.url.path):
            return error_response(_request, error.status, error.code, error.message,
                                  headers={"WWW-Authenticate": "Bearer"} if error.status == 401 else None)
        return JSONResponse({"error": {"code": error.code, "message": error.message}},
                            status_code=error.status,
                            headers={"WWW-Authenticate": "Bearer"} if error.status == 401 else None)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, error: RequestValidationError):
        if is_v1(request.url.path):
            details = [{"location": list(item["loc"]) if item["type"] != "extra_forbidden" else [],
                        "code": item["type"]} for item in error.errors()[:20]]
            return error_response(request, 422, "validation_error", "Invalid request parameters.", details=details)
        from fastapi.exception_handlers import request_validation_exception_handler
        return await request_validation_exception_handler(request, error)

    @application.exception_handler(HTTPException)
    async def http_error(request, error: HTTPException):
        if is_v1(request.url.path):
            code, message = ERRORS.get(error.status_code, ("http_error", "Request could not be completed."))
            return error_response(request, error.status_code, code, message, headers=error.headers)
        from fastapi.exception_handlers import http_exception_handler
        return await http_exception_handler(request, error)

    install_openapi(application, versioned_routes)
    return application


app = create_app()
