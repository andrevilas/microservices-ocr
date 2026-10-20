"""Async MCP dispatch through the existing shared document execution runtime."""
from app.application import ApplicationError
from app.document_mcp_tools import DOCUMENT_OPERATIONS
from app.services.document_runtime import DocumentServices


_CANONICAL = frozenset({
    "get_document_canonical_manifest", "get_document_canonical_chunk",
})


def _unavailable():
    return ApplicationError(
        503, "metadata_unavailable", "Stored metadata is unavailable.")


def _dispatch(services, principal, operation, args, checkpoint):
    """Run synchronously under the caller's single execution permit."""
    queries = services.queries
    if operation == "get_document_canonical_manifest":
        job_id, = args
        return queries.get_canonical_manifest(
            principal, job_id, checkpoint=checkpoint)
    if operation == "get_document_canonical_chunk":
        job_id, page, offset, max_chars = args
        # The HTTP query supports larger chunks; MCP never does.
        if type(max_chars) is not int or not 1 <= max_chars <= 1000:
            raise ApplicationError(
                422, "invalid_metadata_query", "Invalid document metadata query.")
        return queries.get_canonical_chunk(
            principal, job_id, page=page, offset=offset,
            max_chars=max_chars, checkpoint=checkpoint)
    if operation == "get_document_metadata":
        job_id, = args
        return queries.get_current(principal, job_id, checkpoint=checkpoint)
    if operation == "list_document_metadata_revisions":
        job_id, limit, after = args
        return queries.list_revisions(
            principal, job_id, limit=limit, after=after, checkpoint=checkpoint)
    if operation == "get_document_metadata_revision":
        job_id, revision_id, sha256 = args
        return queries.get_revision(
            principal, job_id, revision_id, sha256=sha256, checkpoint=checkpoint)
    if operation == "list_document_metadata_fields":
        job_id, revision_id, segment_id, sha256, limit, after = args
        return queries.list_fields(
            principal, job_id, revision_id, segment_id, sha256=sha256,
            limit=limit, after=after, checkpoint=checkpoint)
    if operation == "list_document_metadata_values":
        job_id, revision_id, segment_id, field_name, sha256, limit, after = args
        return queries.list_values(
            principal, job_id, revision_id, segment_id, field_name,
            sha256=sha256, limit=limit, after=after, checkpoint=checkpoint)
    if operation == "list_document_metadata_evidence":
        job_id, revision_id, segment_id, field_name, sha256, limit, after = args
        return queries.list_evidence(
            principal, job_id, revision_id, segment_id, field_name,
            sha256=sha256, limit=limit, after=after, checkpoint=checkpoint)
    if operation == "get_document_metadata_string":
        (job_id, revision_id, segment_id, field_name, sha256, part,
         item_index, offset, max_chars) = args
        return queries.get_string(
            principal, job_id, revision_id, segment_id, field_name,
            sha256=sha256, part=part, item_index=item_index,
            offset=offset, max_chars=max_chars, checkpoint=checkpoint)
    if operation == "get_document_metadata_schema":
        job_id, revision_id, schema_sha256, sha256, offset, max_chars = args
        return queries.get_schema(
            principal, job_id, revision_id, schema_sha256, sha256=sha256,
            offset=offset, max_chars=max_chars, checkpoint=checkpoint)
    if operation == "save_document_metadata":
        job_id, body, key = args
        return services.document.save(
            principal, job_id, body, key=key, checkpoint=checkpoint)[0]
    if operation == "approve_document_metadata":
        job_id, body, key = args
        return services.document.approve(
            principal, job_id, body, key=key, checkpoint=checkpoint)[0]
    raise ApplicationError(422, "unknown_operation", "Unknown document operation.")


async def execute_document(provider, principal, operation, args):
    """Authorize before resolving services, then acquire exactly one shared permit.

    The transport supplies the trusted principal and registry-ordered arguments.
    Service exceptions and cancellation propagate without translation or retry.
    """
    if not isinstance(operation, str) or operation not in DOCUMENT_OPERATIONS:
        raise ApplicationError(
            422, "unknown_operation", "Unknown document operation.")
    principal.require("jobs:read")
    principal.require("artifacts:read")
    if operation not in _CANONICAL:
        principal.require("metadata:read")
    if operation == "save_document_metadata":
        principal.require("metadata:write")
    if operation == "approve_document_metadata":
        principal.require("metadata:approve")
    if not callable(provider):
        raise _unavailable()
    services = provider()
    if not isinstance(services, DocumentServices):
        raise _unavailable()
    return await services.execution.run(
        principal, lambda control: _dispatch(
            services, principal, operation, args, control.checkpoint))
