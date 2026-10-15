from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

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

    application = FastAPI(title=settings.app_name, lifespan=lifespan)
    application.mount("/static", StaticFiles(directory="app/static"), name="static")
    application.include_router(router)
    application.include_router(auth_router)
    application.include_router(v1_router)
    application.include_router(identity_router)
    if service_provider is not get_job_application:
        application.dependency_overrides[get_job_application] = service_provider
    application.mount("/", mcp_app)

    @application.exception_handler(ApplicationError)
    async def application_error(_request, error: ApplicationError):
        return JSONResponse({"error": {"code": error.code, "message": error.message}},
                            status_code=error.status,
                            headers={"WWW-Authenticate": "Bearer"} if error.status == 401 else None)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, error: RequestValidationError):
        if request.url.path.startswith("/api/v1/"):
            return JSONResponse({"error": {"code": "validation_error", "message": "Invalid request parameters."}}, status_code=422)
        from fastapi.exception_handlers import request_validation_exception_handler
        return await request_validation_exception_handler(request, error)

    return application


app = create_app()
