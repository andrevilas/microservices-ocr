"""Pure contract tests for the edition-1 context template catalog."""

import builtins
import hashlib
import importlib
import io
import json
import os
import socket
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

import app.context_templates as templates
from app.document_models import ContextCreate, FieldSpec, TypeDraft, compose_schema
from app.document_values import validate_values


# Independent expected data: do not obtain descriptions, defaults or hashes from the catalog.
def _expected_definitions():
    disclaimer = (
        "Ponto de partida editável para organização documental. "
        "Não comprova validade jurídica, autenticidade ou conformidade do documento."
    )

    def field(name, kind, description, *, restricted=False, many=False):
        return {
            "name": name,
            "type": kind,
            "description": description,
            "label": None,
            "required": False,
            "critical": False,
            "evidence_required": False,
            "sensitivity": "restricted" if restricted else "internal",
            "indexable": False,
            "cardinality": "many" if many else "one",
            "max_items": 20,
            "normalization": "type-default",
            "destination": "sidecar",
            "examples": [],
            "required_when": None,
            "enum": None,
        }

    rows = [
        (
            "generic", "Genérico", "document", "Documento",
            [
                field("titulo", "string", "Título informado no documento."),
                field("identificador_documento", "identifier", "Identificador registrado no documento, sem normalização destrutiva."),
                field("data_documento", "date", "Data explicitamente informada; não inferir pela data de processamento."),
            ],
            [
                field("emissor", "string", "Pessoa ou organização emissora declarada.", restricted=True),
                field("assunto", "string", "Assunto descrito pelo documento.", restricted=True),
            ],
        ),
        (
            "real-estate-deed", "Imobiliário", "deed", "Escritura de imóvel",
            [
                field("identificador_documento", "identifier", "Identificador do documento."),
                field("municipio", "string", "Município relacionado ao imóvel conforme texto."),
                field("unidade_federativa", "string", "Unidade federativa tal como informada; sem enum jurisdicional."),
            ],
            [
                field("data_lavratura", "date", "Data de lavratura explicitamente informada."),
                field("serventia", "string", "Nome da serventia indicado no documento."),
                field("livro", "identifier", "Referência de livro; preservar caracteres e zeros."),
                field("folhas", "identifier", "Referência de folhas; pode conter intervalo, não presumir inteiro."),
                field("matricula_imovel", "identifier", "Referência de matrícula.", restricted=True),
                field("endereco_imovel", "string", "Endereço declarado.", restricted=True),
                field("partes", "string", "Nomes/identificações textuais das partes, sem presumir papéis.", restricted=True, many=True),
                field("valor_declarado", "decimal", "Quantia expressamente declarada, sem conversão.", restricted=True),
                field("moeda", "string", "Moeda explicitamente informada para a quantia, sem assumir BRL."),
            ],
        ),
        (
            "procurement-notice", "Licitações", "notice", "Edital",
            [
                field("numero_processo", "identifier", "Número do processo, preservando zeros e separadores."),
                field("entidade_responsavel", "string", "Entidade responsável declarada no documento."),
            ],
            [
                field("numero_edital", "identifier", "Identificação do edital."),
                field("modalidade", "string", "Modalidade tal como informada; não normalizar para taxonomia legal fixa."),
                field("objeto", "string", "Descrição do objeto no documento."),
                field("data_publicacao", "date", "Data de publicação indicada."),
                field("data_abertura", "date", "Data de abertura indicada; este campo não representa horário/fuso nem agenda operacional."),
                field("valor_estimado", "decimal", "Quantia estimada expressamente declarada, sem conversão."),
                field("moeda", "string", "Moeda explicitamente informada; não assumir BRL."),
                field("criterio_julgamento", "string", "Critério de julgamento informado, sem conclusão de regularidade."),
            ],
        ),
    ]
    return [
        {
            "template_id": identifier,
            "template_version": 1,
            "context": {"name": name, "description": disclaimer, "common_fields": common},
            "types": [{
                "type_key": key,
                "body": {"name": type_name, "description": "", "specific_fields": specific},
            }],
        }
        for identifier, name, key, type_name, common, specific in rows
    ]


def _serialized(data):
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _expected_record(index=0):
    definition = _expected_definitions()[index]
    return {**definition, "template_sha256": hashlib.sha256(_serialized(definition)).hexdigest()}


def _receipt():
    return {
        "template_id": "generic",
        "template_version": 1,
        "template_sha256": _expected_record()["template_sha256"],
        "context_id": "a" * 32,
        "context_revision": 1,
        "types": [{"type_key": "document", "type_id": "b" * 32, "revision": 1}],
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


def _reverse_keys(value):
    if isinstance(value, dict):
        return {key: _reverse_keys(item) for key, item in reversed(list(value.items()))}
    if isinstance(value, list):
        return [_reverse_keys(item) for item in value]
    return value


def test_complete_catalog_and_independent_hashes():
    expected = _expected_definitions()
    result = templates.list_context_templates()
    assert set(result.model_dump()) == {"items"}
    assert len(result.items) == 3
    for index, (definition, record) in enumerate(zip(expected, result.items)):
        assert record.model_dump(mode="json") == _expected_record(index)
        assert record.template_version == 1
        assert len(record.types) == 1
        digest = hashlib.sha256(_serialized(definition)).hexdigest()
        assert record.template_sha256 == digest
        assert hashlib.sha256(_serialized(_reverse_keys(definition))).hexdigest() == digest
        reordered = _reverse_keys(definition)
        reordered["template_sha256"] = digest
        assert templates.ContextTemplateRecord.model_validate(reordered) == record
        assert templates.get_context_template(definition["template_id"]) == record
    assert len(_serialized(result.model_dump(mode="json"))) <= 64 * 1024
    assert templates.get_context_template("unknown") is None
    assert templates.get_context_template("Generic") is None
    assert templates.get_context_template(" generic") is None


def test_compositions_optionality_and_exact_field_defaults():
    for record in templates.list_context_templates().items:
        initial_type = record.types[0]
        fields = record.context.common_fields + initial_type.body.specific_fields
        composed = compose_schema(record.context.common_fields, initial_type.body.specific_fields)
        validate_values(fields, {})
        schema = json.loads(composed.canonical)
        assert len(schema["fields"]) == len(fields)
        assert len({field.name for field in fields}) == len(fields)
        assert composed.sha256 == hashlib.sha256(composed.canonical).hexdigest()
        assert composed.sha256 != record.template_sha256
        for field, resolved in zip(fields, schema["fields"]):
            assert not any((field.required, field.critical, field.evidence_required, field.indexable))
            assert field.description
            assert field.destination == "sidecar"
            assert field.normalization == "type-default"
            assert field.required_when is None and field.enum is None
            assert field.examples == []
            assert field.max_items == 20
            assert resolved["label"] == field.name
            assert field.cardinality == ("many" if field.name == "partes" else "one")


def test_mutable_returns_never_change_catalog_or_other_returns():
    first = templates.list_context_templates()
    second = templates.list_context_templates()
    fetched = templates.get_context_template("generic")
    assert fetched is not None
    assert first.items[0] is not second.items[0]
    assert first.items[0].context is not fetched.context
    first.items[0].context.common_fields[0].examples.append("Exemplo sintético")
    first.items[0].types[0].body.specific_fields.clear()
    first.items.pop()
    fetched.context.common_fields.clear()
    fetched.types.clear()
    assert second.model_dump(mode="json") == {"items": [_expected_record(i) for i in range(3)]}
    assert templates.list_context_templates() == second
    assert templates.get_context_template("generic").model_dump(mode="json") == _expected_record()


def test_inputs_and_copies_are_detached_and_revalidated():
    context = ContextCreate.model_validate(_expected_definitions()[0]["context"])
    body = TypeDraft.model_validate(_expected_definitions()[0]["types"][0]["body"])
    incoming = _expected_definitions()[0]
    incoming["context"] = context
    incoming["types"][0]["body"] = body
    definition = templates.ContextTemplateDefinition.model_validate(incoming)
    context.common_fields[0].examples.append("Exemplo sintético")
    body.specific_fields.clear()
    incoming["types"].clear()
    assert definition.model_dump(mode="json") == _expected_definitions()[0]
    for deep in (False, True):
        copied = definition.model_copy(deep=deep)
        copied.context.common_fields[0].examples.append("Outro exemplo sintético")
        copied.types.clear()
        assert definition.model_dump(mode="json") == _expected_definitions()[0]
    with pytest.raises(ValidationError):
        definition.model_copy(update={"template_version": True})
    with pytest.raises(ValidationError):
        definition.model_copy(update={"owner": "unexpected"})
    definition.types.clear()
    with pytest.raises(ValidationError):
        templates.ContextTemplateDefinition.model_validate(definition)
    with pytest.raises(ValidationError):
        definition.model_copy()


def test_mutated_nested_dtos_and_cross_scope_collisions_are_rejected():
    body = TypeDraft(name="Documento")
    body.specific_fields.append(FieldSpec.model_construct(name="invalid-name", type="string", description="Synthetic"))
    with pytest.raises(ValidationError):
        templates.TemplateTypeDefinition(type_key="document", body=body)
    invalid = _expected_definitions()[0]
    invalid["types"][0]["body"]["specific_fields"][0]["name"] = "titulo"
    with pytest.raises(ValidationError):
        templates.ContextTemplateDefinition.model_validate(invalid)
    invalid = _expected_definitions()[0]
    invalid["types"][0]["body"]["specific_fields"][0]["required_when"] = {
        "field": "missing", "equals": "synthetic"
    }
    with pytest.raises(ValidationError):
        templates.ContextTemplateDefinition.model_validate(invalid)


@pytest.mark.parametrize("bad", [True, False, "1", 1.0, 0, -1, None])
def test_strict_positive_versions(bad):
    definition = _expected_definitions()[0]
    definition["template_version"] = bad
    with pytest.raises(ValidationError):
        templates.ContextTemplateDefinition.model_validate(definition)
    with pytest.raises(ValidationError) as error:
        templates.TemplateCloneInput(expected_version=bad, expected_sha256="a" * 64)
    assert "input_value" not in str(error.value)
    receipt = _receipt()
    receipt["template_version"] = bad
    with pytest.raises(ValidationError):
        templates.TemplateCloneReceipt.model_validate(receipt)


@pytest.mark.parametrize("bad", ["A" * 64, "g" * 64, "a" * 63, "a" * 65, "a" * 64 + "\n", b"a" * 64, True, 123])
def test_hash_syntax_is_strict(bad):
    with pytest.raises(ValidationError):
        templates.TemplateCloneInput(expected_version=1, expected_sha256=bad)
    record = _expected_record()
    record["template_sha256"] = bad
    with pytest.raises(ValidationError):
        templates.ContextTemplateRecord.model_validate(record)
    receipt = _receipt()
    receipt["template_sha256"] = bad
    with pytest.raises(ValidationError):
        templates.TemplateCloneReceipt.model_validate(receipt)


def test_digest_covers_version_descriptions_defaults_and_field_order():
    for change in ("version", "description", "default", "order"):
        record = _expected_record()
        if change == "version":
            record["template_version"] = 2
        elif change == "description":
            record["context"]["description"] += " Alteração sintética."
        elif change == "default":
            record["context"]["common_fields"][0]["indexable"] = True
        else:
            record["context"]["common_fields"].reverse()
        with pytest.raises(ValidationError):
            templates.ContextTemplateRecord.model_validate(record)


@pytest.mark.parametrize("model_name,data", [
    ("TemplateTypeDefinition", _expected_definitions()[0]["types"][0]),
    ("ContextTemplateDefinition", _expected_definitions()[0]),
    ("ContextTemplateRecord", _expected_record()),
    ("ContextTemplateList", {"items": [_expected_record(i) for i in range(3)]}),
    ("TemplateCloneInput", {"expected_version": 1, "expected_sha256": "a" * 64}),
    ("TemplateClonedType", _receipt()["types"][0]),
    ("TemplateCloneReceipt", _receipt()),
])
def test_all_dtos_are_closed_and_frozen(model_name, data):
    model = getattr(templates, model_name)
    instance = model.model_validate(data)
    field = next(iter(model.model_fields))
    with pytest.raises(ValidationError):
        setattr(instance, field, getattr(instance, field))
    for extra in ("owner", "name", "fields", "publish", "state", "replayed", "next_cursor"):
        with pytest.raises(ValidationError):
            model.model_validate({**data, extra: "unexpected"})
    with pytest.raises(ValidationError):
        instance.model_copy(update={"unexpected": True})


@pytest.mark.parametrize("bad", ["Document", "doc-é", "document\n", "doc-name", "", True, 1])
def test_type_keys_use_existing_ascii_field_name_contract(bad):
    with pytest.raises(ValidationError):
        templates.TemplateTypeDefinition(type_key=bad, body=TypeDraft(name="Documento"))
    data = _receipt()["types"][0]
    data["type_key"] = bad
    with pytest.raises(ValidationError):
        templates.TemplateClonedType.model_validate(data)


@pytest.mark.parametrize("bad", ["Generic", "genérico", "generic\n", "generic_", "", True, 1])
def test_template_identifiers_are_strict_ascii(bad):
    data = _expected_definitions()[0]
    data["template_id"] = bad
    with pytest.raises(ValidationError):
        templates.ContextTemplateDefinition.model_validate(data)


def test_lists_are_strict_complete_and_ordered():
    records = [_expected_record(i) for i in range(3)]
    for invalid in (records[:2], records + records[:1], list(reversed(records)), [records[0]] * 3, tuple(records)):
        with pytest.raises(ValidationError):
            templates.ContextTemplateList(items=invalid)
    for invalid in ([], _expected_definitions()[0]["types"] * 2, tuple(_expected_definitions()[0]["types"])):
        data = _expected_definitions()[0]
        data["types"] = invalid
        with pytest.raises(ValidationError):
            templates.ContextTemplateDefinition.model_validate(data)


def test_receipt_is_a_snapshot_with_utc_and_initial_revisions():
    data = _receipt()
    receipt = templates.TemplateCloneReceipt.model_validate(data)
    data["types"].clear()
    assert receipt.model_dump() == _receipt()
    assert receipt.created_at.tzinfo is timezone.utc
    copied = receipt.model_copy()
    copied.types.clear()
    assert len(receipt.types) == 1
    for bad in (True, False, "1", 1.0, 0, 2):
        for target in ("context_revision", "revision"):
            invalid = _receipt()
            if target == "revision":
                invalid["types"][0][target] = bad
            else:
                invalid[target] = bad
            with pytest.raises(ValidationError):
                templates.TemplateCloneReceipt.model_validate(invalid)
    for bad in ("A" * 32, "g" * 32, "a" * 31, "a" * 33, "a" * 32 + "\n", b"a" * 32, True):
        for target in ("context_id", "type_id"):
            invalid = _receipt()
            if target == "type_id":
                invalid["types"][0][target] = bad
            else:
                invalid[target] = bad
            with pytest.raises(ValidationError):
                templates.TemplateCloneReceipt.model_validate(invalid)
    for bad in (datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1))), "2026-01-01T00:00:00Z"):
        invalid = _receipt()
        invalid["created_at"] = bad
        with pytest.raises(ValidationError):
            templates.TemplateCloneReceipt.model_validate(invalid)


def test_import_does_not_open_store_or_load_application(monkeypatch):
    original_import = builtins.__import__
    allowed_app_imports = {"app.context_templates", "app.document_models", "app.document_values"}

    def guarded_import(name, *args, **kwargs):
        if name.startswith("app.") and name not in allowed_app_imports:
            raise AssertionError("Application dependency imported by pure template module")
        if name.split(".")[0] in {"sqlite3", "socket"}:
            raise AssertionError("Store or network dependency imported by pure template module")
        return original_import(name, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("I/O attempted by pure template module")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", guarded_import)
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(io, "open", forbidden)
        patch.setattr(os, "open", forbidden)
        patch.setattr(sqlite3, "connect", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(socket, "create_connection", forbidden)
        imported = importlib.reload(templates)
        assert len(imported.list_context_templates().items) == 3
