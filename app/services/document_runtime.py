"""Lazy internal document services sharing existing metadata/catalog runtimes."""
from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from fastapi import Request

from app.services.canonical_text_service import CanonicalTextService
from app.services.catalog_runtime import CatalogServices
from app.services.context_service import ContextService
from app.services.context_template_service import ContextTemplateService
from app.services.document_execution import DocumentExecution
from app.services.document_service import DocumentMetadataService
from app.services.metadata_runtime import MetadataServices
from app.services.metadata_store import _error, _guard, _require
from app.services.storage_service import JobStore
from app.services.type_service import TypeService


@dataclass(frozen=True)
class DocumentServices:
    document: DocumentMetadataService
    canonical: CanonicalTextService
    execution: DocumentExecution

    @property
    def jobs(self) -> JobStore:
        return self.document.bundle.jobs


class DocumentProvider:
    def __init__(self, metadata_provider, catalog_provider):
        self._metadata = metadata_provider
        self._catalog = catalog_provider
        self._lock = Lock()
        self._execution = DocumentExecution()
        # Retain both input bundles, including otherwise unused catalog services.
        # Identity keys therefore cannot be reused during this provider's life.
        self._cache = {}
        self._services: DocumentServices | None = None

    @staticmethod
    def _shared(metadata, catalog):
        _require(type(metadata) is MetadataServices
                 and type(catalog) is CatalogServices)
        jobs = metadata.jobs
        _require(isinstance(jobs, JobStore)
                 and type(catalog.contexts) is ContextService
                 and type(catalog.types) is TypeService
                 and type(catalog.templates) is ContextTemplateService
                 and catalog.contexts.jobs is jobs
                 and catalog.types.jobs is jobs
                 and catalog.templates.jobs is jobs)
        return jobs

    @_guard
    def __call__(self) -> DocumentServices:
        try:
            # Providers and health checks are always outside the JobStore lock.
            metadata = self._metadata()
            catalog = self._catalog()
            jobs = self._shared(metadata, catalog)
            metadata.ensure_healthy()
            with self._lock:
                key = (id(metadata), id(catalog))
                cached = self._cache.get(key)
                if cached is None:
                    document = DocumentMetadataService(metadata, catalog.types)
                    services = DocumentServices(
                        document, document.canonical, self._execution)
                else:
                    old_metadata, old_catalog, services = cached
                    _require(old_metadata is metadata and old_catalog is catalog)
                services.document._check_shared()
                _require(services.document.bundle is metadata
                         and services.document.types is catalog.types
                         and services.canonical is services.document.canonical
                         and services.canonical.store is jobs
                         and services.execution is self._execution)
                self._shared(metadata, catalog)
                metadata.ensure_healthy()
                # Publish only after construction and every identity/health check.
                if cached is None:
                    self._cache[key] = (metadata, catalog, services)
                self._services = services
                return services
        except Exception:
            raise _error("metadata_unavailable") from None


def get_document_services(request: Request) -> DocumentServices:
    return request.app.state.document_provider()
