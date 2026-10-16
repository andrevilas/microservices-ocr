"""Lazy, cached pair of catalog services bound to the store of the current job application.

No I/O at import or construction. The provider never closes a store and never creates one: it only follows
whatever store the injected job-application provider returns."""
import threading
from dataclasses import dataclass

from fastapi import Request

from app.services.context_service import ContextService
from app.services.type_service import TypeService


@dataclass(frozen=True)
class CatalogServices:
    contexts: ContextService
    types: TypeService


class CatalogProvider:
    def __init__(self, job_application_provider):
        self._applications = job_application_provider
        self._lock = threading.Lock()  # own lock; never jobs._lock (not reentrant, taken by the constructors)
        self._store = None
        self._services: CatalogServices | None = None

    def __call__(self) -> CatalogServices:
        store = self._applications().store
        with self._lock:
            if self._services is None or self._store is not store:
                contexts = ContextService(store)  # creates document_contexts before types depend on it
                types = TypeService(store)
                self._services = CatalogServices(contexts, types)  # published only after both succeeded
                self._store = store
            return self._services


def get_catalog_services(request: Request) -> CatalogServices:
    return request.app.state.catalog_provider()
