"""Pure catalog DTOs and closed HTTP/MCP inputs. Imports only Pydantic, typing, datetime and document_models:
never application, services, storage or runtime, so MCP stdio can import it without opening any store."""
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.document_models import ContextCreate, TypeDraft

MAX_REVISION = 2**31 - 1
_DTO = ConfigDict(extra="forbid", frozen=True)
_INPUT = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
Revision = Annotated[int, Field(strict=True, ge=1, le=MAX_REVISION)]


class ContextReceipt(BaseModel):
    """Creation receipt only; it is not the current state of the context."""
    model_config = _DTO
    context_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    revision: int = Field(default=1, ge=1, le=1)
    created_at: datetime


class ContextSummary(BaseModel):
    model_config = _DTO
    context_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    name: str
    revision: int = Field(ge=1, le=MAX_REVISION)
    created_at: datetime
    updated_at: datetime


class ContextRecord(BaseModel):
    model_config = _DTO
    context_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    revision: int = Field(ge=1, le=MAX_REVISION)
    created_at: datetime
    updated_at: datetime
    body: ContextCreate


class ContextPage(BaseModel):
    model_config = _DTO
    items: list[ContextSummary]
    next_cursor: str | None = None


class TypeReceipt(BaseModel):
    """Creation receipt only; it is not the current state of the type."""
    model_config = _DTO
    type_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    context_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    revision: int = Field(default=1, ge=1, le=1)
    created_at: datetime


class TypeSummary(BaseModel):
    model_config = _DTO
    state: Literal["draft"] = "draft"
    type_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    context_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    name: str
    revision: int = Field(ge=1, le=MAX_REVISION)
    created_at: datetime
    updated_at: datetime


class TypeRecord(BaseModel):
    model_config = _DTO
    state: Literal["draft"] = "draft"
    type_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    context_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    revision: int = Field(ge=1, le=MAX_REVISION)
    created_at: datetime
    updated_at: datetime
    body: TypeDraft


class TypePage(BaseModel):
    model_config = _DTO
    items: list[TypeSummary]
    next_cursor: str | None = None


class TypeVersionSummary(BaseModel):
    """Stable consumer reference: type_id + version + schema_sha256 (the hash covers only the canonical envelope)."""
    model_config = _DTO
    type_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    context_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    version: int = Field(ge=1)
    draft_revision: int = Field(ge=1, le=MAX_REVISION)
    context_revision: int = Field(ge=1, le=MAX_REVISION)
    type_name: str
    context_name: str
    schema_version: str
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class VersionLifecycle(BaseModel):
    model_config = ConfigDict(**_DTO, hide_input_in_errors=True)
    state: Literal["published", "deprecated"]
    revision: Revision
    changed_at: datetime

    @field_validator("changed_at")
    @classmethod
    def utc_changed_at(cls, value: datetime) -> datetime:
        if value.utcoffset() is None or value.utcoffset() != timedelta(0):
            raise ValueError("changed_at must be timezone-aware UTC.")
        return value.astimezone(timezone.utc)


class TypeVersionViewSummary(TypeVersionSummary):
    lifecycle: VersionLifecycle


class TypeVersionRecord(TypeVersionViewSummary):
    # Python name avoids shadowing BaseModel.schema; the wire name stays "schema" (serialize with by_alias=True).
    schema_document: dict = Field(alias="schema")  # decoded from the stored canonical bytes, never recomputed


class TypeVersionPage(BaseModel):
    model_config = _DTO
    items: list[TypeVersionViewSummary]
    next_cursor: int | None = None


# -- closed inputs (synthetic examples only; no owner/reviewer fields)
_CONTEXT_EXAMPLE = {
    "name": "Contratos de exemplo",
    "description": "Contexto sintético.",
    "common_fields": [{"name": "numero", "type": "identifier", "description": "Número do documento."}],
}
_TYPE_EXAMPLE = {
    "name": "Contrato de serviço",
    "description": "Tipo sintético.",
    "specific_fields": [{"name": "valor_total", "type": "decimal", "description": "Valor total do contrato."}],
}


class ContextInput(ContextCreate):
    """Same contract as ContextCreate; exists only to carry OpenAPI examples."""
    model_config = ConfigDict(json_schema_extra={"examples": [_CONTEXT_EXAMPLE]})


class TypeInput(TypeDraft):
    """Same contract as TypeDraft; exists only to carry OpenAPI examples."""
    model_config = ConfigDict(json_schema_extra={"examples": [_TYPE_EXAMPLE]})


class ContextUpdateInput(BaseModel):
    model_config = ConfigDict(**_INPUT, json_schema_extra={"examples": [{"body": _CONTEXT_EXAMPLE, "expected_revision": 1}]})
    body: ContextCreate
    expected_revision: Revision


class TypeUpdateInput(BaseModel):
    model_config = ConfigDict(**_INPUT, json_schema_extra={"examples": [{"body": _TYPE_EXAMPLE, "expected_revision": 1}]})
    body: TypeDraft
    expected_revision: Revision


class TypePublishInput(BaseModel):
    model_config = ConfigDict(**_INPUT, json_schema_extra={"examples": [{"expected_revision": 1, "expected_context_revision": 1}]})
    expected_revision: Revision
    expected_context_revision: Revision
