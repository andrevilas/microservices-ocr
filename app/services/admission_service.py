"""Shared durable admission and queue handoff for HTTP and MCP."""
from app.application import ApplicationError


def submit_and_enqueue(service, queue, principal, upload_id, idempotency_key, options):
    try:
        receipt, replayed = service.submit(principal, upload_id, idempotency_key, options)
        job = service.jobs.get_snapshot(receipt.job_id)
        if job and job.status == "queued":
            try:
                queue.enqueue(job.job_id)
            except Exception:
                raise ApplicationError(503, "queue_unavailable",
                                       "Job is persisted; retry with the same key or recover the runtime queue.") from None
        return receipt, replayed
    except OSError:
        raise ApplicationError(503, "storage_unavailable",
                               "Admission storage is unavailable; retry with the same key.") from None
