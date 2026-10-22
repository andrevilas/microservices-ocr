import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from app.application import ApplicationError, get_job_application
from app.config import settings
from app.mcp_server import build_mcp_server
from app.observability import install_logging
from app.routes.api import router
from app.routes.auth import auth_router
from app.routes.v1 import router as v1_router
from app.routes.service_identities import router as identity_router
from app.routes.uploads import router as upload_router
from app.routes.bulk import router as bulk_router
from app.routes.control import router as control_router
from app.routes.catalog import router as catalog_router
from app.routes.documents import router as document_router
from app.routes.packages import router as package_router
from app.routes.graph import router as graph_router
from app.routes.extractions import router as extraction_router
from app.routes.rag import router as rag_router
from app.services.catalog_runtime import CatalogProvider
from app.services.control_service import get_control_service
from app.services.document_runtime import DocumentProvider
from app.services.batch_service import get_batch_service
from app.services.export_service import get_export_service
from app.services.job_queue import get_job_queue_processor
from app.services.metadata_runtime import MetadataProvider, assert_metadata_health
from app.services.metadata_store import _error, _guard
from app.services.storage_service import JobStore
from app.services.runtime_lease import RuntimeLease
from app.services.rag_runtime import configured_runtime
from app.services.retention_worker import RetentionWorker
from app.services.user_store import get_user_store
from app.services.upload_service import get_upload_service
from app.http_contract import API_VERSION, ERRORS, V1Boundary, error_response, is_v1, install_openapi


def create_app(service_provider=get_job_application, *, request_authenticator=None, transport_security=None) -> FastAPI:
    install_logging()
    runtime_store = None
    runtime_lease = None

    def new_document_provider():
        return DocumentProvider(
            lambda: application.state.metadata_provider(),
            lambda: application.state.catalog_provider())

    @_guard
    def job_application():
        try:
            resolved = application.dependency_overrides.get(get_job_application, service_provider)()
            if runtime_store is not None and resolved.store is not runtime_store:
                raise _error("metadata_unavailable")
            return resolved
        except Exception:
            if runtime_store is not None:
                raise _error("metadata_unavailable") from None
            raise

    @_guard
    def startup_store(lease):
        try:
            jobs = job_application().store
            if not isinstance(jobs, JobStore):
                raise _error("metadata_unavailable")
            root = jobs.root_dir
            handle = lease._file
            if (not isinstance(root, Path) or not root.is_absolute()
                    or '..' in root.parts
                    or lease.path != root / '.ocr-runtime.lock'
                    or handle is None or handle.closed is not False):
                raise _error("metadata_unavailable")
            assert_metadata_health(jobs)
            return jobs
        except Exception:
            raise _error("metadata_unavailable") from None

    # Resolve the same injectable instances for both transports, without creating
    # storage or starting workers at import/discovery time.
    def uploads():
        return application.dependency_overrides.get(get_upload_service, get_upload_service)()

    def queue():
        return application.dependency_overrides.get(get_job_queue_processor, get_job_queue_processor)()

    def batches():
        return application.dependency_overrides.get(get_batch_service, get_batch_service)()

    def exports():
        return application.dependency_overrides.get(get_export_service, get_export_service)()

    def controls():
        return application.dependency_overrides.get(get_control_service, get_control_service)()

    def rag_service():
        service = application.state.rag_service
        if service is not None and service.outbox.store is not job_application().store:
            raise ApplicationError(503, "rag_unavailable", "RAG store binding is unavailable.")
        return service

    mcp_server, mcp_app = build_mcp_server(job_application, uploads, queue, batches, exports, controls,
                                          request_authenticator=request_authenticator, transport_security=transport_security,
                                          # Same cached pair/store as the HTTP routes; only called after application exists.
                                          catalog_provider=lambda: application.state.catalog_provider(),
                                          document_provider=lambda: application.state.document_provider(),
                                          rag_provider=rag_service)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        nonlocal runtime_store, runtime_lease
        # A failed shutdown retains ownership even if the configured root changes.
        # Reject reentry before acquiring or publishing any replacement lease.
        if runtime_lease is not None:
            raise _error("metadata_unavailable") from None
        document_provider = application.state.document_provider
        lease = RuntimeLease(settings.ocr_tmp_dir)
        lease.acquire()
        runtime_lease = lease
        application.state.runtime_lease = lease
        processor = None
        export_processor = None
        retention_worker = None
        rag_runtime = None
        try:
            runtime_store = startup_store(lease)
            # Custom applications require all five backend overrides, each bound
            # to this same JobStore. Partial injection could invoke global
            # constructors that clean up a different store before validation.
            backend_getters = (get_upload_service, get_batch_service,
                               get_control_service, get_export_service,
                               get_job_queue_processor)
            custom_runtime = (service_provider is not get_job_application
                              or get_job_application in application.dependency_overrides)
            if custom_runtime and any(getter not in application.dependency_overrides
                                      for getter in backend_getters):
                raise _error("metadata_unavailable") from None

            metadata = application.state.metadata_provider()
            if metadata.jobs is not runtime_store:
                raise _error("metadata_unavailable") from None
            metadata.ensure_healthy()
            metadata.recovery.recover_startup(lease)
            metadata.ensure_healthy()

            @_guard
            def checked_backend(factory, *, store_attribute="jobs", shared_uploads=None):
                try:
                    metadata.ensure_healthy()
                    candidate = factory()
                    if getattr(candidate, store_attribute, None) is not runtime_store:
                        raise _error("metadata_unavailable")
                    if (shared_uploads is not None
                            and getattr(candidate, "uploads", shared_uploads) is not shared_uploads):
                        raise _error("metadata_unavailable")
                    metadata.ensure_healthy()
                    return candidate
                except Exception:
                    raise _error("metadata_unavailable") from None

            user_store = get_user_store()
            user_store.ensure_admin(name=settings.admin_name, email=settings.admin_email, password=settings.admin_password)
            upload_service = checked_backend(uploads)
            checked_backend(batches, shared_uploads=upload_service)
            checked_backend(controls, shared_uploads=upload_service)
            # Publish shutdown targets only after validating their store identity.
            export_processor = checked_backend(exports)
            processor = checked_backend(queue, store_attribute="job_store")
            rag_runtime = configured_runtime(runtime_store, settings.rag_config_file)
            application.state.rag_runtime = rag_runtime
            metadata.ensure_healthy()
            retention_worker = RetentionWorker(upload_service, export_processor, metadata=metadata)
            metadata.ensure_healthy()
            retention_worker.start()
            metadata.ensure_healthy()
            processor.start()
            metadata.ensure_healthy()
            export_processor.start()
            metadata.ensure_healthy()
            if rag_runtime is not None:
                rag_runtime.start()
                # Graph deliveries read approved snapshots through the same document services.
                rag_runtime.service.documents = lambda: application.state.document_provider()
                application.state.rag_service = rag_runtime.service
            async with mcp_server.session_manager.run():
                yield
        finally:
            # Drain the captured coordinator before stopping other writers. Any
            # failure or cancellation retains the exact coordinator, lease and
            # store pin; a later lifespan must not overwrite that ownership.
            await document_provider.shutdown()
            application.state.rag_service = None
            if rag_runtime is not None:
                from starlette.concurrency import run_in_threadpool
                await run_in_threadpool(rag_runtime.stop)
            if retention_worker is not None:
                retention_worker.stop()
            if export_processor is not None:
                export_processor.stop()
            if processor is not None:
                processor.stop()
            lease.release()
            runtime_store = None
            runtime_lease = None
            application.state.rag_runtime = None
            # Admission on the drained coordinator is permanently closed. Rotate
            # only after every stop and lease release succeeds, without resolving
            # metadata/catalog services or touching their stores.
            application.state.document_provider = new_document_provider()

    application = FastAPI(title=settings.app_name, version=API_VERSION, lifespan=lifespan,
                          description="OCR HTTP API. Versioned operations use Bearer user or individual service credentials; legacy routes retain separate contracts.")
    application.mount("/static", StaticFiles(directory="app/static"), name="static")
    application.include_router(router)
    application.include_router(auth_router)
    # Both providers and MCP resolve the current override through one callback.
    # Construction is lazy; the active lifespan pins only the actual JobStore.
    application.state.catalog_provider = CatalogProvider(job_application)
    application.state.metadata_provider = MetadataProvider(job_application)
    application.state.document_provider = new_document_provider()
    application.state.rag_service = None
    application.state.rag_provider = rag_service
    versioned_routes = []
    for versioned_router in (v1_router, identity_router, upload_router, bulk_router, control_router, catalog_router, document_router, package_router, graph_router, extraction_router, rag_router):
        application.include_router(versioned_router)
        versioned_routes.extend(versioned_router.routes)
    if service_provider is not get_job_application:
        application.dependency_overrides[get_job_application] = service_provider
    if request_authenticator is not None:
        from app.security import require_principal
        application.dependency_overrides[require_principal] = request_authenticator
    application.mount("/", mcp_app)
    application.add_middleware(V1Boundary, routes=versioned_routes)

    @application.exception_handler(ApplicationError)
    async def application_error(_request, error: ApplicationError):
        if is_v1(_request.url.path):
            return error_response(_request, error.status, error.code, error.message,
                                  headers={"WWW-Authenticate": "Bearer"} if error.status == 401 else
                                  {"Retry-After": str(error.retry_after or 60)} if error.status == 429 else
                                  {"Retry-After": "5"} if error.status == 503 else None)
        return JSONResponse({"error": {"code": error.code, "message": error.message}},
                            status_code=error.status,
                            headers={"WWW-Authenticate": "Bearer"} if error.status == 401 else
                            {"Retry-After": str(error.retry_after)} if error.retry_after else None)

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



def _served_app():
    # OCR_REMOTE_CONFIG_FILE adds the OAuth MCP hostname to this same process (OCR-AM-13).
    if os.environ.get("OCR_REMOTE_CONFIG_FILE"):
        from app.remote import create_served_app
        return create_served_app()
    return create_app()


app = _served_app()
