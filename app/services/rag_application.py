"""Shared HTTP/MCP application facade. Configuration is operator-owned."""
import hashlib
from app.application import ApplicationError
from app.rag_models import (DeliveryList, DeliveryOperations, DeliveryStatus, DestinationList,
                            DestinationView, RecoveryLink)


class RagApplication:
    def __init__(self, outbox, destinations, worker=None):
        self.outbox = outbox
        self.worker = worker
        self.destinations = dict(destinations)
        if len(self.destinations) > 100:
            raise ValueError("too many RAG destinations")
        for key, dest in self.destinations.items():
            dest.validate()
            DestinationView(key=key, destination_id=dest.destination_id,
                            source_kind=dest.source_kind, process_reference=dest.process_reference)

    def list_rag_destinations(self, principal):
        principal.require("jobs:read")
        return DestinationList(items=[DestinationView(
            key=key, destination_id=dest.destination_id, source_kind=dest.source_kind,
            process_reference=dest.process_reference)
            for key, dest in sorted(self.destinations.items()) if dest.owner == principal.subject])

    def submit_rag_delivery(self, principal, body):
        principal.require("jobs:read")
        principal.require("artifacts:read")
        dest = self.destinations.get(body.destination)
        if dest is None or dest.owner != principal.subject:
            raise ApplicationError(404, "rag_destination_not_found", "Destination not found.")
        delivery_id = self.outbox.enqueue(principal, body.job_id, dest)
        return self.get_rag_delivery(principal, delivery_id)

    def get_rag_delivery(self, principal, delivery_id):
        return DeliveryStatus(**self.outbox.status(principal, delivery_id))

    def get_rag_receipt(self, principal, delivery_id):
        return self.outbox.receipt(principal, delivery_id)

    def cancel_rag_delivery(self, principal, delivery_id):
        self.outbox.cancel(principal, delivery_id)
        return self.get_rag_delivery(principal, delivery_id)

    def retry_rag_delivery(self, principal, delivery_id):
        self.outbox.retry(principal, delivery_id)
        return self.get_rag_delivery(principal, delivery_id)

    def list_rag_deliveries(self, principal, state=None, limit=50):
        return DeliveryList(items=[DeliveryStatus(**row) for row in self.outbox.list(principal, state, limit)])

    def get_rag_operations(self, principal):
        if not (principal.kind == "user" and principal.is_admin):
            raise ApplicationError(403, "admin_required", "A human administrator identity is required.")
        snapshot = self.worker.snapshot() if self.worker is not None else None
        if snapshot is None:
            raise ApplicationError(503, "rag_unavailable", "RAG delivery worker is not configured.")
        thread = getattr(self.worker, "_thread", None)
        return DeliveryOperations(
            enabled=True, worker_running=bool(thread and thread.is_alive()),
            worker_failure=snapshot["worker_failure"], states=snapshot["states"], due=snapshot["due"],
            near_deadline=snapshot["near_deadline"], requires_resolution=snapshot["requires_resolution"],
            queued_payload_bytes=snapshot["queued_payload_bytes"])

    def get_rag_recovery(self, principal, delivery_id):
        raw = self.outbox.recovery_package(principal, delivery_id)
        return RecoveryLink(delivery_id=delivery_id,
                            content_url=f"/api/v1/rag/deliveries/{delivery_id}/recovery/content",
                            size_bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def require_rag(request):
    provider = getattr(request.app.state, "rag_provider", None)
    service = provider() if provider else getattr(request.app.state, "rag_service", None)
    if service is None:
        raise ApplicationError(503, "rag_unavailable", "RAG delivery is not enabled.")
    return service
