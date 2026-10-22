"""Static context templates and closed DTOs. Pure: no application or I/O dependencies."""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictStr,
    TypeAdapter,
    ValidationInfo,
    model_validator,
)

from app.document_models import ContextCreate, FieldName, FieldSpec, TypeDraft, compose_schema
from app.document_values import validate_values


_IDS = ("generic", "real-estate-deed", "procurement-notice")
_DISCLAIMER = (
    "Ponto de partida editável para organização documental. "
    "Não comprova validade jurídica, autenticidade ou conformidade do documento."
)


def _canonical(data) -> bytes:
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _detached(value):
    """Dump nested models before validation; never trust instance identity or unchecked copies."""
    if isinstance(value, BaseModel):
        try:
            value = value.model_dump(mode="python", warnings="error")
        except Exception:
            raise ValueError("Invalid template data.") from None
    if isinstance(value, dict):
        return {key: _detached(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_detached(item) for item in value]
    return value


def _template_id(value: str) -> str:
    if re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", value, flags=re.ASCII) is None:
        raise ValueError("Invalid stable template identifier.")
    return value


def _sha256(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value, flags=re.ASCII) is None:
        raise ValueError("Expected a lowercase SHA-256 digest.")
    return value


def _object_id(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{32}", value, flags=re.ASCII) is None:
        raise ValueError("Expected a lowercase 32-character hexadecimal identifier.")
    return value


def _revision(value):
    # Literal[1] alone can accept True through Python's equality rules.
    if type(value) is not int or value != 1:
        raise ValueError("Revision must be the integer 1.")
    return value


_JSON_DATETIME = TypeAdapter(datetime)


def _json_datetime(value, info: ValidationInfo):
    """Restore strict JSON datetime parsing after the input snapshot validator."""
    if info.mode == "json" and isinstance(value, str):
        return _JSON_DATETIME.validate_json(json.dumps(value), strict=True)
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("created_at must be an aware UTC datetime.")
    return value.astimezone(timezone.utc)


TemplateId = Annotated[
    StrictStr,
    Field(min_length=1, max_length=64, json_schema_extra={
        "pattern": r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
        # JSON Schema patterns search; $ alone can admit a final newline.
        "not": {"pattern": r"[^a-z0-9-]"},
    }),
    AfterValidator(_template_id),
]
Sha256 = Annotated[
    StrictStr,
    Field(min_length=64, max_length=64, json_schema_extra={
        "pattern": r"^[0-9a-f]{64}$",
    }),
    AfterValidator(_sha256),
]
ObjectId = Annotated[
    StrictStr,
    Field(min_length=32, max_length=32, json_schema_extra={
        "pattern": r"^[0-9a-f]{32}$",
    }),
    AfterValidator(_object_id),
]
Version = Annotated[int, Field(strict=True, ge=1)]
InitialRevision = Annotated[Literal[1], BeforeValidator(_revision)]
UTCDateTime = Annotated[
    datetime, BeforeValidator(_json_datetime), AfterValidator(_utc)
]


class _TemplateDTO(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        hide_input_in_errors=True,
        revalidate_instances="always",
        validate_default=True,
    )

    @model_validator(mode="before")
    @classmethod
    def _snapshot_input(cls, value):
        return _detached(value)

    def model_copy(self, *, update=None, deep=False):
        """Both copy modes return an independent, fully revalidated snapshot."""
        data = _detached(self)
        if update is not None:
            data.update(update)
        return type(self).model_validate(data)


class TemplateTypeDefinition(_TemplateDTO):
    type_key: FieldName
    body: TypeDraft


class ContextTemplateDefinition(_TemplateDTO):
    template_id: TemplateId
    template_version: Version
    context: ContextCreate
    types: Annotated[list[TemplateTypeDefinition], Field(min_length=1, max_length=1)]

    @model_validator(mode="after")
    def _validate_schema(self):
        for item in self.types:
            compose_schema(self.context.common_fields, item.body.specific_fields)
            validate_values(self.context.common_fields + item.body.specific_fields, {})
        return self


def _definition_bytes(definition: ContextTemplateDefinition) -> bytes:
    data = definition.model_dump(mode="json", warnings="error")
    data.pop("template_sha256", None)
    return _canonical(data)


class ContextTemplateRecord(ContextTemplateDefinition):
    template_sha256: Sha256

    @model_validator(mode="after")
    def _validate_digest(self):
        if self.template_sha256 != hashlib.sha256(_definition_bytes(self)).hexdigest():
            raise ValueError("Template digest does not match its definition.")
        return self


class ContextTemplateList(_TemplateDTO):
    items: Annotated[list[ContextTemplateRecord], Field(min_length=3, max_length=3)]

    @model_validator(mode="after")
    def _validate_catalog(self):
        if tuple(item.template_id for item in self.items) != _IDS:
            raise ValueError("Expected the complete catalog in its fixed order.")
        if len(_canonical(self.model_dump(mode="json", warnings="error"))) > 64 * 1024:
            raise ValueError("Serialized template list exceeds 64 KiB.")
        return self


class TemplateCloneInput(_TemplateDTO):
    expected_version: Version
    expected_sha256: Sha256


class TemplateClonedType(_TemplateDTO):
    type_key: FieldName
    type_id: ObjectId
    revision: InitialRevision


class TemplateCloneReceipt(_TemplateDTO):
    template_id: TemplateId
    template_version: Version
    template_sha256: Sha256
    context_id: ObjectId
    context_revision: InitialRevision
    types: Annotated[list[TemplateClonedType], Field(min_length=1, max_length=1)]
    created_at: UTCDateTime


def _build_definitions() -> tuple[bytes, ...]:
    """Build edition 1 once, retaining only immutable canonical definition bytes."""
    def field(name, kind, description, **options):
        return FieldSpec(name=name, type=kind, description=description, **options)

    definitions = (
        ContextTemplateDefinition(
            template_id="generic",
            template_version=1,
            context=ContextCreate(
                name="Genérico",
                description=_DISCLAIMER,
                common_fields=[
                    field("titulo", "string", "Título informado no documento."),
                    field("identificador_documento", "identifier", "Identificador registrado no documento, sem normalização destrutiva."),
                    field("data_documento", "date", "Data explicitamente informada; não inferir pela data de processamento."),
                ],
            ),
            types=[TemplateTypeDefinition(
                type_key="document",
                body=TypeDraft(name="Documento", specific_fields=[
                    field("emissor", "string", "Pessoa ou organização emissora declarada.", sensitivity="restricted"),
                    field("assunto", "string", "Assunto descrito pelo documento.", sensitivity="restricted"),
                ]),
            )],
        ),
        ContextTemplateDefinition(
            template_id="real-estate-deed",
            template_version=1,
            context=ContextCreate(
                name="Imobiliário",
                description=_DISCLAIMER,
                common_fields=[
                    field("identificador_documento", "identifier", "Identificador do documento."),
                    field("municipio", "string", "Município relacionado ao imóvel conforme texto."),
                    field("unidade_federativa", "string", "Unidade federativa tal como informada; sem enum jurisdicional."),
                ],
            ),
            types=[TemplateTypeDefinition(
                type_key="deed",
                body=TypeDraft(name="Escritura de imóvel", specific_fields=[
                    field("data_lavratura", "date", "Data de lavratura explicitamente informada."),
                    field("serventia", "string", "Nome da serventia indicado no documento."),
                    field("livro", "identifier", "Referência de livro; preservar caracteres e zeros."),
                    field("folhas", "identifier", "Referência de folhas; pode conter intervalo, não presumir inteiro."),
                    field("matricula_imovel", "identifier", "Referência de matrícula.", sensitivity="restricted"),
                    field("endereco_imovel", "string", "Endereço declarado.", sensitivity="restricted"),
                    field("partes", "string", "Nomes/identificações textuais das partes, sem presumir papéis.", sensitivity="restricted", cardinality="many", max_items=20),
                    field("valor_declarado", "decimal", "Quantia expressamente declarada, sem conversão.", sensitivity="restricted"),
                    field("moeda", "string", "Moeda explicitamente informada para a quantia, sem assumir BRL."),
                ]),
            )],
        ),
        ContextTemplateDefinition(
            template_id="procurement-notice",
            template_version=1,
            context=ContextCreate(
                name="Licitações",
                description=_DISCLAIMER,
                common_fields=[
                    field("numero_processo", "identifier", "Número do processo, preservando zeros e separadores."),
                    field("entidade_responsavel", "string", "Entidade responsável declarada no documento."),
                ],
            ),
            types=[TemplateTypeDefinition(
                type_key="notice",
                body=TypeDraft(name="Edital", specific_fields=[
                    field("numero_edital", "identifier", "Identificação do edital."),
                    field("modalidade", "string", "Modalidade tal como informada; não normalizar para taxonomia legal fixa."),
                    field("objeto", "string", "Descrição do objeto no documento."),
                    field("data_publicacao", "date", "Data de publicação indicada."),
                    field("data_abertura", "date", "Data de abertura indicada; este campo não representa horário/fuso nem agenda operacional."),
                    field("valor_estimado", "decimal", "Quantia estimada expressamente declarada, sem conversão."),
                    field("moeda", "string", "Moeda explicitamente informada; não assumir BRL."),
                    field("criterio_julgamento", "string", "Critério de julgamento informado, sem conclusão de regularidade."),
                ]),
            )],
        ),
    )
    return tuple(_definition_bytes(definition) for definition in definitions)


_CANONICAL_DEFINITIONS = _build_definitions()


def _record(canonical: bytes) -> ContextTemplateRecord:
    data = json.loads(canonical)
    data["template_sha256"] = hashlib.sha256(canonical).hexdigest()
    return ContextTemplateRecord.model_validate(data)


def list_context_templates() -> ContextTemplateList:
    """Return the complete, ordered catalog as an independent validated snapshot."""
    return ContextTemplateList(items=[_record(data) for data in _CANONICAL_DEFINITIONS])


def get_context_template(template_id: str) -> ContextTemplateRecord | None:
    """Return an independent snapshot for an exact ID, or None."""
    for identifier, data in zip(_IDS, _CANONICAL_DEFINITIONS):
        if template_id == identifier:
            return _record(data)
    return None
