"""Owner-scoped, atomic cloning of static context templates."""
import hashlib
import json
from contextlib import suppress
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from app.application import ApplicationError, Principal
from app.context_templates import (
    ContextTemplateList,
    ContextTemplateRecord,
    TemplateCloneInput,
    TemplateCloneReceipt,
    TemplateId,
    get_context_template,
    list_context_templates,
)
from app.services.batch_service import request_identity
from app.services.context_service import MAX_CONTEXTS_PER_OWNER
from app.services.storage_service import JobStore
from app.services.type_service import MAX_TYPES_PER_OWNER


_CONTEXT_DOMAIN = "context-template.clone.v1.context"
_TYPE_DOMAIN = "context-template.clone.v1.type"


def _unavailable():
    return ApplicationError(503, "storage_unavailable", "Context template storage is unavailable.")


def _require(principal, *scopes):
    if not set(scopes).issubset(principal.scopes):
        raise ApplicationError(403, "insufficient_scope", "Required context catalog scope is missing.")


def _creation_key(domain, clone_key_hash, context_id, *parts):
    # Actual NUL separators make these preimages inaccessible through public create keys.
    return hashlib.sha256("\0".join((domain, clone_key_hash, context_id, *parts)).encode()).hexdigest()


def _creation_request(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class ContextTemplateService:
    def __init__(self, jobs: JobStore):
        self.jobs = jobs
        try:
            with jobs._lock, jobs._get_conn() as conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS context_template_clones (
                    owner TEXT NOT NULL, key_hash TEXT NOT NULL, request_hash TEXT NOT NULL,
                    receipt TEXT NOT NULL, PRIMARY KEY(owner, key_hash))""")
        except Exception:
            raise _unavailable() from None

    def list(self, principal: Principal) -> ContextTemplateList:
        _require(principal, "contexts:read")
        return list_context_templates()

    def clone(self, principal: Principal, template_id: str,
              body: TemplateCloneInput, key: str) -> tuple[TemplateCloneReceipt, bool]:
        _require(principal, "contexts:read", "contexts:write")
        try:
            template_id = TypeAdapter(TemplateId).validate_python(template_id, strict=True)
            body = TemplateCloneInput.model_validate(
                body.model_dump(mode="python") if isinstance(body, TemplateCloneInput) else body,
                strict=True,
            )
        except ValidationError:
            raise ApplicationError(422, "validation_error", "Invalid context template clone input.") from None
        key_hash, request_hash = request_identity(key, {
            "operation": "context-template.clone.v1",
            "template_id": template_id,
            "body": body.model_dump(mode="json"),
        })
        owner = principal.subject
        with self.jobs._lock:
            conn = None
            try:
                conn = self.jobs._get_conn()
                conn.execute("BEGIN IMMEDIATE")
                previous = conn.execute(
                    "SELECT request_hash, receipt FROM context_template_clones WHERE owner=? AND key_hash=?",
                    (owner, key_hash),
                ).fetchone()
                if previous is not None:
                    if previous[0] != request_hash:
                        raise ApplicationError(409, "idempotency_conflict", "The key was already used for another request.")
                    receipt = self._replay(conn, owner, key_hash, template_id, body, previous[1])
                    result = receipt, True
                else:
                    receipt = self._clone(conn, owner, key_hash, request_hash, template_id, body)
                    result = receipt, False
                conn.commit()
                return result
            except BaseException as error:
                if conn is not None:
                    with suppress(Exception):
                        conn.rollback()
                if isinstance(error, ApplicationError):
                    raise
                if isinstance(error, Exception):
                    raise _unavailable() from None
                raise

    @staticmethod
    def _replay(conn, owner, key_hash, template_id, body, raw_receipt):
        # JSON validation preserves the DTO's strict UTC datetime roundtrip.
        receipt = TemplateCloneReceipt.model_validate_json(raw_receipt)
        if (receipt.template_id != template_id
                or receipt.template_version != body.expected_version
                or receipt.template_sha256 != body.expected_sha256):
            raise ValueError("Invalid clone receipt identity")
        ids = [receipt.context_id, *(item.type_id for item in receipt.types)]
        keys = [item.type_key for item in receipt.types]
        if len(set(ids)) != len(ids) or len(set(keys)) != len(keys):
            raise ValueError("Invalid clone receipt identifiers")
        stamp = receipt.created_at.isoformat()
        context = conn.execute(
            "SELECT create_key_hash, created_at FROM document_contexts WHERE owner=? AND context_id=?",
            (owner, receipt.context_id),
        ).fetchone()
        expected_key = _creation_key(_CONTEXT_DOMAIN, key_hash, receipt.context_id)
        if context is None or context[0] != expected_key or context[1] != stamp:
            raise ValueError("Invalid cloned context")
        for item in receipt.types:
            typ = conn.execute(
                "SELECT context_id, create_key_hash, created_at FROM document_types WHERE owner=? AND type_id=?",
                (owner, item.type_id),
            ).fetchone()
            expected_key = _creation_key(
                _TYPE_DOMAIN, key_hash, receipt.context_id, item.type_id, item.type_key,
            )
            if (typ is None or typ[0] != receipt.context_id
                    or typ[1] != expected_key or typ[2] != stamp):
                raise ValueError("Invalid cloned type")
        return receipt

    @staticmethod
    def _clone(conn, owner, key_hash, request_hash, template_id, body):
        template = get_context_template(template_id)
        if template is None:
            raise ApplicationError(404, "context_template_not_found", "Context template not found.")
        template = ContextTemplateRecord.model_validate(template.model_dump(mode="python"), strict=True)
        if template.template_id != template_id:
            raise ValueError("Invalid template identity")
        if (template.template_version != body.expected_version
                or template.template_sha256 != body.expected_sha256):
            raise ApplicationError(409, "template_version_conflict", "The requested template version or digest differs.")

        contexts = conn.execute(
            "SELECT COUNT(*) FROM document_contexts WHERE owner=?", (owner,),
        ).fetchone()[0]
        if contexts + 1 > MAX_CONTEXTS_PER_OWNER:
            raise ApplicationError(429, "context_quota_exceeded", "Context quota exceeded.")
        types = conn.execute(
            "SELECT COUNT(*) FROM document_types WHERE owner=?", (owner,),
        ).fetchone()[0]
        if types + len(template.types) > MAX_TYPES_PER_OWNER:
            raise ApplicationError(429, "type_quota_exceeded", "Document type quota exceeded.")

        context_id = uuid4().hex
        cloned_types = [(item, uuid4().hex) for item in template.types]
        receipt = TemplateCloneReceipt.model_validate({
            "template_id": template.template_id,
            "template_version": template.template_version,
            "template_sha256": template.template_sha256,
            "context_id": context_id,
            "context_revision": 1,
            "types": [{"type_key": item.type_key, "type_id": type_id, "revision": 1}
                      for item, type_id in cloned_types],
            "created_at": datetime.now(timezone.utc),
        }, strict=True)
        raw_receipt = receipt.model_dump_json()
        stamp = receipt.created_at.isoformat()
        context_payload = template.context.model_dump(mode="json")
        context_row = (
            context_id, owner, json.dumps(context_payload, sort_keys=True), 1, stamp, stamp,
            _creation_key(_CONTEXT_DOMAIN, key_hash, context_id),
            _creation_request({"operation": _CONTEXT_DOMAIN, "context_id": context_id, "body": context_payload}),
        )
        type_rows = []
        for item, type_id in cloned_types:
            payload = item.body.model_dump(mode="json")
            type_rows.append((
                type_id, context_id, owner, json.dumps(payload, sort_keys=True), 1, stamp, stamp,
                _creation_key(_TYPE_DOMAIN, key_hash, context_id, type_id, item.type_key),
                _creation_request({"operation": _TYPE_DOMAIN, "context_id": context_id,
                                   "type_id": type_id, "type_key": item.type_key, "body": payload}),
            ))

        conn.execute("""INSERT INTO document_contexts (
            context_id, owner, payload, revision, created_at, updated_at, create_key_hash, create_request_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", context_row)
        for row in type_rows:
            conn.execute("""INSERT INTO document_types (
                type_id, context_id, owner, payload, revision, created_at, updated_at,
                create_key_hash, create_request_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", row)
        conn.execute(
            "INSERT INTO context_template_clones (owner, key_hash, request_hash, receipt) VALUES (?, ?, ?, ?)",
            (owner, key_hash, request_hash, raw_receipt),
        )
        return receipt
