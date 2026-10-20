"""Authorized synchronous metadata queries over the existing document runtime.

These methods perform no transport I/O and acquire no execution permit. A caller
using DocumentExecution must retain its existing permit around the whole call.
Checkpoint callbacks are trusted, nonblocking callbacks with no I/O or JobStore
lock acquisition. Snapshot projections run inside the document artifact lease.
"""
from functools import wraps
import re

from pydantic import TypeAdapter

from app.application import ApplicationError
from app.metadata_models import PersistentID, SHA256
from app.document_canonical_models import (
    CanonicalManifest, CanonicalPageChunk, CanonicalPageText,
)
from app.results import MAX_PAGE_CHARS
from app.services.canonical_text_service import MAX_CHUNK_CHARS
from app.services import document_content_projection as content_projection
from app.services import document_projection as projection
from app.services.document_service import DocumentMetadataService


_MESSAGE = "Document metadata query failed."
_CODE = re.compile(r"[a-z][a-z0-9_]{0,79}")
_ID = TypeAdapter(PersistentID)
_HASH = TypeAdapter(SHA256)


def _failure(status, code):
    return ApplicationError(status, code, _MESSAGE)


def _safe(function):
    @wraps(function)
    def guarded(*args, **kwargs):
        status, code = 503, "metadata_unavailable"
        try:
            return function(*args, **kwargs)
        except ApplicationError as error:
            candidate_status = getattr(error, "status", None)
            candidate_code = getattr(error, "code", None)
            if (type(candidate_status) is int and 400 <= candidate_status <= 599
                    and type(candidate_code) is str
                    and _CODE.fullmatch(candidate_code) is not None):
                status, code = candidate_status, candidate_code
        except Exception:
            pass
        # Outside exception handlers: discard both sensitive causes and contexts.
        raise _failure(status, code)
    return guarded


def _coordinate(adapter, value):
    try:
        return adapter.validate_python(value, strict=True)
    except Exception:
        pass
    raise _failure(422, "invalid_metadata_query")


def _coordinates(revision_id, sha256):
    return _coordinate(_ID, revision_id), _coordinate(_HASH, sha256)


def _tick(checkpoint):
    if checkpoint is not None:
        checkpoint()


def _job_call(function, principal, job_id):
    # Let the existing service establish authorization. Its routing-key syntax
    # error is translated only when that key actually fails query validation.
    try:
        result = function(principal, job_id)
    except ApplicationError:
        _coordinate(_ID, job_id)
        raise
    _coordinate(_ID, job_id)
    return result


def _canonical_integrity():
    return ApplicationError(
        503, "result_integrity_error", "Stored result failed validation.")


def _canonical_dto(model, value):
    try:
        validated = model.model_validate(value, strict=True)
        # Revalidate original instances, then detach even an already typed source.
        return model.model_validate(
            validated.model_dump(mode="python", warnings="error"), strict=True)
    except Exception:
        pass
    raise _canonical_integrity()


def _text_range_integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise _failure(422, "invalid_text_range")


class DocumentQueries:
    """Project authorized metadata without opening another runtime or permit."""

    @_safe
    def __init__(self, document: DocumentMetadataService):
        if not isinstance(document, DocumentMetadataService):
            raise _failure(503, "metadata_unavailable")
        document._check_shared()
        self.document = document

    def _ready(self, checkpoint):
        if checkpoint is not None and not callable(checkpoint):
            raise _failure(422, "invalid_metadata_query")
        _tick(checkpoint)
        self.document._check_shared()
        # Health checks acquire the shared nonreentrant lock themselves.
        self.document.bundle.ensure_healthy()

    def _authorized(self, principal, job_id, checkpoint):
        self._ready(checkpoint)
        head = _job_call(self.document.bundle.index.get_head, principal, job_id)
        # get_head is the authority even when no metadata has been saved yet.
        owner = principal.subject
        return owner, head

    def _read(self, principal, job_id, revision_id, sha256, checkpoint, project):
        _tick(checkpoint)
        with self.document.open_snapshot(
                principal, job_id, revision_id, sha256=sha256,
                checkpoint=checkpoint) as snapshot:
            _tick(checkpoint)
            result = project(snapshot)
            _tick(checkpoint)
            return result

    @_safe
    def get_current(self, principal, job_id, *, checkpoint=None):
        owner, head = self._authorized(principal, job_id, checkpoint)
        if head is None:
            raise _failure(404, "metadata_not_found")
        # Pin these coordinates once. Subsequent HEAD changes are irrelevant.
        revision_id, digest = head.revision_id, head.sha256
        return self._read(
            principal, job_id, revision_id, digest, checkpoint,
            lambda snapshot: projection.metadata_view(owner, snapshot, digest))

    @_safe
    def get_revision(self, principal, job_id, revision_id, *, sha256, checkpoint=None):
        owner, _ = self._authorized(principal, job_id, checkpoint)
        revision_id, digest = _coordinates(revision_id, sha256)
        return self._read(
            principal, job_id, revision_id, digest, checkpoint,
            lambda snapshot: projection.metadata_view(owner, snapshot, digest))

    @_safe
    def list_revisions(self, principal, job_id, *, limit=25, after=None, checkpoint=None):
        self._ready(checkpoint)
        revisions = _job_call(self.document.list_revisions, principal, job_id)
        _tick(checkpoint)
        result = projection.revision_page(
            principal.subject, job_id, revisions, limit=limit, after=after)
        _tick(checkpoint)
        return result

    def _page(self, principal, job_id, revision_id, segment_id, field_name, *,
              sha256, limit, after, checkpoint, view):
        owner, _ = self._authorized(principal, job_id, checkpoint)
        revision_id, digest = _coordinates(revision_id, sha256)
        data = dict(owner=owner, segment_id=segment_id, limit=limit)
        if view == "fields":
            query = projection._query(projection._FieldQuery, data)
            project = projection.field_page
            arguments = (query.segment_id,)
        else:
            data["field_name"] = field_name
            query = projection._query(content_projection._PageQuery, data)
            project = (content_projection.value_page if view == "values"
                       else content_projection.evidence_page)
            arguments = (query.segment_id, query.field_name)
        scope = dict(owner=owner, job_id=job_id, view=view,
                     revision_id=revision_id, sha256=digest,
                     segment_id=query.segment_id)
        if view != "fields":
            scope["field_name"] = query.field_name
        # Reject malformed and foreign-scope cursors before opening any artifact.
        projection.decode_cursor(after, **scope)
        return self._read(
            principal, job_id, revision_id, digest, checkpoint,
            lambda snapshot: project(owner, snapshot, digest, *arguments,
                                     limit=query.limit, after=after))

    @_safe
    def list_fields(self, principal, job_id, revision_id, segment_id, *,
                    sha256, limit=20, after=None, checkpoint=None):
        return self._page(
            principal, job_id, revision_id, segment_id, None,
            sha256=sha256, limit=limit, after=after, checkpoint=checkpoint,
            view="fields")

    @_safe
    def list_values(self, principal, job_id, revision_id, segment_id, field_name, *,
                    sha256, limit=20, after=None, checkpoint=None):
        return self._page(
            principal, job_id, revision_id, segment_id, field_name,
            sha256=sha256, limit=limit, after=after, checkpoint=checkpoint,
            view="values")

    @_safe
    def list_evidence(self, principal, job_id, revision_id, segment_id, field_name, *,
                      sha256, limit=10, after=None, checkpoint=None):
        return self._page(
            principal, job_id, revision_id, segment_id, field_name,
            sha256=sha256, limit=limit, after=after, checkpoint=checkpoint,
            view="evidence")

    @_safe
    def get_string(self, principal, job_id, revision_id, segment_id, field_name, *,
                   sha256, part, item_index=None, offset=0, max_chars=1000,
                   checkpoint=None):
        owner, _ = self._authorized(principal, job_id, checkpoint)
        revision_id, digest = _coordinates(revision_id, sha256)
        query = projection._query(content_projection._StringQuery, dict(
            owner=owner, segment_id=segment_id, field_name=field_name,
            part=part, item_index=item_index, offset=offset, max_chars=max_chars))
        return self._read(
            principal, job_id, revision_id, digest, checkpoint,
            lambda snapshot: content_projection.string_chunk(
                owner, snapshot, digest, query.segment_id, query.field_name,
                part=query.part, item_index=query.item_index,
                offset=query.offset, max_chars=query.max_chars))

    @_safe
    def get_schema(self, principal, job_id, revision_id, schema_sha256, *,
                   sha256, offset=0, max_chars=1000, checkpoint=None):
        owner, _ = self._authorized(principal, job_id, checkpoint)
        revision_id, digest = _coordinates(revision_id, sha256)
        query = projection._query(content_projection._SchemaQuery, dict(
            owner=owner, schema_sha256=schema_sha256,
            offset=offset, max_chars=max_chars))
        return self._read(
            principal, job_id, revision_id, digest, checkpoint,
            lambda snapshot: content_projection.schema_chunk(
                owner, snapshot, digest, query.schema_sha256,
                offset=query.offset, max_chars=query.max_chars))

    def _canonical_read(self, principal, job_id, *, view, page, offset,
                        max_chars, checkpoint):
        # Canonical access has no metadata or catalog scope requirement.
        principal.require("jobs:read")
        principal.require("artifacts:read")
        self._ready(checkpoint)
        job_id = _coordinate(_ID, job_id)
        if view != "manifest":
            _text_range_integer(page, 1, 300)
        if view == "chunk":
            _text_range_integer(offset, 0, MAX_PAGE_CHARS)
            _text_range_integer(max_chars, 1, MAX_CHUNK_CHARS)
        _tick(checkpoint)
        # The existing canonical service owns authorization and ONE lease.
        # Preparation reads all pages; cancellation is checked when it returns.
        with self.document.canonical.open(principal, job_id) as prepared:
            _tick(checkpoint)
            manifest = _canonical_dto(CanonicalManifest, prepared.manifest)
            if manifest.job_id != job_id:
                raise _canonical_integrity()
            if view == "manifest":
                result = manifest
            else:
                if page > manifest.page_count:
                    raise _failure(422, "invalid_text_range")
                if view == "page":
                    source = prepared.read_page_text(page)
                    _tick(checkpoint)
                    result = _canonical_dto(CanonicalPageText, source)
                else:
                    source = prepared.page_chunk(page, offset, max_chars)
                    _tick(checkpoint)
                    result = _canonical_dto(CanonicalPageChunk, source)
                if (result.document_id != job_id or result.page != page
                        or result.canonical_sha256 != manifest.sha256):
                    raise _canonical_integrity()
                if view == "chunk" and (
                        result.page_count != manifest.page_count
                        or result.offset != offset or len(result.text) > max_chars):
                    raise _canonical_integrity()
            # DTO validation and identity checks remain inside the artifact lease.
            _tick(checkpoint)
            return result

    @_safe
    def get_canonical_manifest(self, principal, job_id, *, checkpoint=None):
        return self._canonical_read(
            principal, job_id, view="manifest", page=None, offset=None,
            max_chars=None, checkpoint=checkpoint)

    @_safe
    def get_canonical_page(self, principal, job_id, page, *, checkpoint=None):
        return self._canonical_read(
            principal, job_id, view="page", page=page, offset=None,
            max_chars=None, checkpoint=checkpoint)

    @_safe
    def get_canonical_chunk(self, principal, job_id, *, page=1, offset=0,
                            max_chars=8000, checkpoint=None):
        return self._canonical_read(
            principal, job_id, view="chunk", page=page, offset=offset,
            max_chars=max_chars, checkpoint=checkpoint)
