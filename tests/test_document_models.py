import hashlib
import json

import pytest
from pydantic import ValidationError

from app.document_models import MAX_PAYLOAD_BYTES, ContextCreate, FieldSpec, TypeDraft, compose_schema


def f(name="campo", type="string", description="desc", **kw):
    return FieldSpec(name=name, type=type, description=description, **kw)


def test_roundtrip_json_with_arrays():
    raw = {
        "name": "Contrato",
        "description": "linha1\nlinha2\tok",
        "common_fields": [
            {"name": "numero", "type": "identifier", "description": "Número", "required": True},
            {"name": "status", "type": "enum", "description": "s", "enum": ["a", "b"]},
        ],
    }
    ctx = ContextCreate.model_validate_json(json.dumps(raw))
    again = ContextCreate.model_validate_json(ctx.model_dump_json(exclude_none=True))
    assert again == ctx
    assert ctx.common_fields[1].enum == ["a", "b"]
    assert ctx.common_fields[0].required is True and ctx.common_fields[0].critical is False


@pytest.mark.parametrize("extra", [{"script": "x"}, {"$ref": "#/x"}, {"regex": ".*"}, {"unknown": 1}])
def test_extra_properties_rejected(extra):
    with pytest.raises(ValidationError):
        FieldSpec(name="a", type="string", description="d", **extra)
    with pytest.raises(ValidationError):
        ContextCreate(name="n", **extra)
    with pytest.raises(ValidationError):
        TypeDraft(name="n", **extra)


@pytest.mark.parametrize("bad", [1, 0, "true", None])
def test_bool_not_coerced(bad):
    for key in ("required", "critical", "evidence_required"):
        with pytest.raises(ValidationError):
            f(**{key: bad})


def test_no_type_coercion():
    with pytest.raises(ValidationError):
        f(name=1)
    with pytest.raises(ValidationError):
        f(description=5)
    with pytest.raises(ValidationError):
        ContextCreate(name=5)
    with pytest.raises(ValidationError):
        ContextCreate(name="n", common_fields=(f(),))
    with pytest.raises(ValidationError):
        f(type="enum", enum="ab")
    for t in ("number", "float", "object"):
        with pytest.raises(ValidationError):
            f(type=t)


@pytest.mark.parametrize("name", ["", "A", "1a", "a-b", "a b", "á", "a\n", "a" * 65, "_a"])
def test_invalid_field_names(name):
    with pytest.raises(ValidationError):
        f(name=name)


def test_valid_field_name_limits():
    f(name="a" * 64)
    f(name="a_1")


def test_enum_rules():
    for bad in ([], ["a", "a"], [""], ["a" * 4001]):
        with pytest.raises(ValidationError):
            f(type="enum", enum=bad)
    with pytest.raises(ValidationError):
        f(type="enum")
    with pytest.raises(ValidationError):
        f(type="string", enum=["a"])
    with pytest.raises(ValidationError):
        f(type="enum", enum=[1])
    f(type="enum", enum=[str(i) for i in range(100)])
    with pytest.raises(ValidationError):
        f(type="enum", enum=[str(i) for i in range(101)])
    f(type="enum", enum=["a\tb\n"])


def test_catalog_name_policy():
    for bad in ["", " ", "   ", " x", "x ", "x\n", "a\x00b", "a\tb"]:
        with pytest.raises(ValidationError):
            ContextCreate(name=bad)
    with pytest.raises(ValidationError):
        ContextCreate(name="x" * 121)
    assert ContextCreate(name="x" * 120).name == "x" * 120
    assert ContextCreate(name="A  B").name == "A  B"  # inner text preserved verbatim
    assert ContextCreate(name="n").description == ""


def test_duplicates_within_group_and_across_groups():
    with pytest.raises(ValidationError):
        ContextCreate(name="n", common_fields=[f("a"), f("a", "integer")])
    with pytest.raises(ValidationError):
        TypeDraft(name="n", specific_fields=[f("a"), f("a")])
    with pytest.raises(ValueError):
        compose_schema([f("a")], [f("a", description="override")])


def test_field_count_limits():
    ContextCreate(name="n", common_fields=[f(f"c{i}") for i in range(100)])
    with pytest.raises(ValidationError):
        ContextCreate(name="n", common_fields=[f(f"c{i}") for i in range(101)])
    with pytest.raises(ValidationError):
        TypeDraft(name="n", specific_fields=[f(f"c{i}") for i in range(101)])
    with pytest.raises(ValueError):
        compose_schema([f(f"c{i}") for i in range(60)], [f(f"s{i}") for i in range(41)])
    compose_schema([f(f"c{i}") for i in range(50)], [f(f"s{i}") for i in range(50)])


def test_compose_rejects_over_limit_before_snapshot(monkeypatch):
    import app.document_models as dm

    common, specific = [f(f"c{i}") for i in range(60)], [f(f"s{i}") for i in range(41)]
    assert len(common) + len(specific) == 101

    def boom(_fields):
        raise AssertionError("_snapshot must not run above the limit")

    monkeypatch.setattr(dm, "_snapshot", boom)
    with pytest.raises(ValueError, match="exceeds"):
        compose_schema(common, specific)
    with pytest.raises(ValueError, match="exceeds"):
        compose_schema([], [f(f"s{i}") for i in range(101)])
    with pytest.raises(ValueError):
        compose_schema("abc", [])
    with pytest.raises(ValueError):
        compose_schema([], b"abc")


def test_enum_none_roundtrip_and_type_enum_requires_list():
    s = f(type="string", enum=None)
    assert s.enum is None
    assert FieldSpec.model_validate(s.model_dump()) == s
    assert FieldSpec.model_validate_json(s.model_dump_json()) == s
    assert FieldSpec.model_validate(s.model_dump(mode="json", exclude_none=True)) == s
    compose_schema([s], [])
    with pytest.raises(ValidationError):
        f(type="enum", enum=None)
    with pytest.raises(ValidationError):
        FieldSpec.model_validate({"name": "a", "type": "enum", "description": "d", "enum": None})


def _big_enum_field(name, prefix="", count=100):
    return f(name, "enum", enum=[f"{prefix}{i:04d}".ljust(4000, "x") for i in range(count)])


def test_aggregate_bytes_rejected_though_items_valid():
    big = _big_enum_field("e")  # each item and the count are individually within limits
    with pytest.raises(ValidationError, match="exceeds"):
        TypeDraft(name="n", specific_fields=[big])
    with pytest.raises(ValidationError, match="exceeds"):
        ContextCreate(name="n", common_fields=[big])


def test_composition_bytes_limit():
    a, b = _big_enum_field("a", "a", 25), _big_enum_field("b", "b", 25)
    ContextCreate(name="n", common_fields=[a])
    TypeDraft(name="n", specific_fields=[b])
    with pytest.raises(ValueError, match="exceeds"):
        compose_schema([a], [b])


def test_unicode_accepted():
    fs = f(description="Ação 😀 日本語\n\tfim", type="enum", enum=["é", "😀"])
    ContextCreate(name="Contrato 😀", description="ç", common_fields=[fs])
    compose_schema([fs], [])


@pytest.mark.parametrize("bad", ["\ud800", "a\udfffb", "\x00", "\r", "\x7f", "\x85"])
def test_surrogates_and_controls_rejected_in_text(bad):
    with pytest.raises(ValidationError):
        f(description=bad)
    with pytest.raises(ValidationError):
        ContextCreate(name="n", description=bad)
    with pytest.raises(ValidationError):
        f(type="enum", enum=[bad])
    with pytest.raises(ValidationError):
        ContextCreate(name=bad + "x")


def test_error_does_not_leak_payload():
    secret = "SEGREDO\x00" * 3
    with pytest.raises(ValidationError) as e:
        f(description=secret)
    assert "SEGREDO" not in str(e.value)


def test_compose_deterministic_and_hash_covers_bytes():
    c, s = [f("a", required=True), f("b", "integer")], [f("z", "enum", enum=["x", "y"])]
    r1, r2 = compose_schema(c, s), compose_schema(list(c), list(s))
    assert r1 == r2
    assert isinstance(r1.canonical, bytes)
    assert r1.sha256 == hashlib.sha256(r1.canonical).hexdigest()
    env = json.loads(r1.canonical.decode("utf-8"))
    assert list(env) == sorted(env)
    assert env["schema_version"] == "1"
    assert [x["name"] for x in env["fields"]] == ["a", "b", "z"]
    assert env["fields"] == env["common_fields"] + env["specific_fields"]
    assert "sha256" not in env and "hash" not in env
    assert b" " not in r1.canonical.replace(b"desc", b"")
    assert len(r1.canonical) <= MAX_PAYLOAD_BYTES


def test_ensure_ascii_false_and_description_changes_hash():
    base = compose_schema([f("a", description="Ação")], [])
    assert "Ação".encode("utf-8") in base.canonical
    assert compose_schema([f("a", description="Acao")], []).sha256 != base.sha256


def test_snapshot_independent_of_later_mutation():
    fs = f("a", "enum", enum=["x", "y"])
    common = [fs]
    r = compose_schema(common, [])
    before = (r.canonical, r.sha256)
    fs.enum.append("z")  # frozen models do not freeze inner lists
    common.append(f("b"))
    assert (r.canonical, r.sha256) == before
    with pytest.raises(AttributeError):
        r.canonical = b""
    assert compose_schema(common, []).sha256 != before[1]


def test_compose_rejects_non_fieldspec_and_unvalidated_mutation():
    with pytest.raises(ValueError):
        compose_schema([{"name": "a"}], [])
    with pytest.raises(ValueError):
        compose_schema("abc", [])
    bad = FieldSpec.model_construct(name="A!", type="string", description="d")
    with pytest.raises(ValueError):
        compose_schema([bad], [])
