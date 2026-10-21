"""Package XMP: a fixed, declared ocrdoc extension schema projected from an approved snapshot.

The schema is static, so veraPDF sees the same declaration for every document;
domain fields travel as a bag of (segment, name, index, value) structures.
Parsing is hardened and strict: anything ambiguous is a ValueError.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from lxml import etree

RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
PDFAEXT = "http://www.aiim.org/pdfa/ns/extension/"
PDFASCHEMA = "http://www.aiim.org/pdfa/ns/schema#"
PDFAPROP = "http://www.aiim.org/pdfa/ns/property#"
PDFATYPE = "http://www.aiim.org/pdfa/ns/type#"
PDFAFIELD = "http://www.aiim.org/pdfa/ns/field#"
OCRDOC = "https://ocr.andre.goiania.br/ns/ocrdoc/1.0/"
OCRFIELD = "https://ocr.andre.goiania.br/ns/ocrfield/1.0/"

_NS = {"rdf": RDF, "pdfaExtension": PDFAEXT, "pdfaSchema": PDFASCHEMA, "pdfaProperty": PDFAPROP,
       "pdfaType": PDFATYPE, "pdfaField": PDFAFIELD, "ocrdoc": OCRDOC, "ocrfield": OCRFIELD}
PROPERTIES = (
    ("documentId", "Text", "Document identifier."),
    ("packageVersion", "Integer", "Package version for this document."),
    ("metadataRevision", "Text", "Approved metadata revision identifier."),
    ("metadataSha256", "Text", "SHA-256 of the approved metadata snapshot."),
    ("originalSha256", "Text", "SHA-256 of the original input PDF."),
    ("canonicalSha256", "Text", "SHA-256 of the canonical pages JSONL."),
    ("fields", "bag Field", "Approved domain fields projected to XMP."),
)
FIELD_TYPE = (
    ("segment", "Text", "Segment identifier."),
    ("name", "Text", "Field name in the published schema."),
    ("index", "Integer", "Zero-based position for multi-valued fields."),
    ("value", "Text", "Normalized approved value."),
)
_SCALARS = ("documentId", "packageVersion", "metadataRevision", "metadataSha256",
            "originalSha256", "canonicalSha256")
_INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_DECIMAL = re.compile(r"0|[1-9][0-9]{0,8}")


@dataclass(frozen=True, slots=True)
class XmpField:
    segment: str
    name: str
    index: int
    value: str


@dataclass(frozen=True, slots=True)
class XmpProjection:
    document_id: str
    package_version: int
    metadata_revision: str
    metadata_sha256: str
    original_sha256: str
    canonical_sha256: str
    fields: tuple[XmpField, ...]


def _parse(xmp: bytes):
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False,
                             remove_blank_text=False, load_dtd=False)
    return etree.fromstring(xmp, parser).getroottree()


def _text(parent, tag, value):
    if type(value) is not str or _INVALID_XML.search(value):
        raise ValueError("Invalid XMP text value.")
    element = etree.SubElement(parent, tag)
    element.text = value
    return element


def _resource(parent):
    return etree.SubElement(parent, f"{{{RDF}}}li", {f"{{{RDF}}}parseType": "Resource"})


def _uses_ocrdoc(tree):
    for element in tree.iter():
        if not isinstance(element.tag, str):
            continue
        if etree.QName(element).namespace in (OCRDOC, OCRFIELD):
            return True
        if any(etree.QName(name).namespace in (OCRDOC, OCRFIELD) for name in element.attrib):
            return True
    return False


def _schema_items(tree):
    return tree.xpath("//rdf:Description/pdfaExtension:schemas/rdf:Bag/rdf:li", namespaces=_NS)


def _declared(item):
    return [e.text for e in item.xpath("pdfaSchema:namespaceURI", namespaces=_NS)]


def inject(xmp: bytes, projection: XmpProjection) -> bytes:
    """Return the packet with the ocrdoc schema declared and its values set."""
    tree = _parse(xmp)
    rdfs = tree.xpath("//rdf:RDF", namespaces=_NS)
    if len(rdfs) != 1:
        raise ValueError("Exactly one rdf:RDF element is required.")
    rdf = rdfs[0]
    if _uses_ocrdoc(tree) or any(OCRDOC in _declared(i) for i in _schema_items(tree)):
        raise ValueError("The ocrdoc schema is already present.")
    bags = rdf.xpath("rdf:Description/pdfaExtension:schemas/rdf:Bag", namespaces=_NS)
    if len(bags) > 1:
        raise ValueError("Ambiguous extension schema declarations.")
    if bags:
        bag = bags[0]
    else:
        holder = etree.SubElement(rdf, f"{{{RDF}}}Description", {f"{{{RDF}}}about": ""}, nsmap={
            k: _NS[k] for k in ("pdfaExtension", "pdfaSchema", "pdfaProperty", "pdfaType", "pdfaField")})
        bag = etree.SubElement(etree.SubElement(holder, f"{{{PDFAEXT}}}schemas"), f"{{{RDF}}}Bag")
    schema = _resource(bag)
    _text(schema, f"{{{PDFASCHEMA}}}schema", "OCR Recognizer document package")
    _text(schema, f"{{{PDFASCHEMA}}}namespaceURI", OCRDOC)
    _text(schema, f"{{{PDFASCHEMA}}}prefix", "ocrdoc")
    seq = etree.SubElement(etree.SubElement(schema, f"{{{PDFASCHEMA}}}property"), f"{{{RDF}}}Seq")
    for name, value_type, description in PROPERTIES:
        item = _resource(seq)
        _text(item, f"{{{PDFAPROP}}}name", name)
        _text(item, f"{{{PDFAPROP}}}valueType", value_type)
        _text(item, f"{{{PDFAPROP}}}category", "external")
        _text(item, f"{{{PDFAPROP}}}description", description)
    types = etree.SubElement(etree.SubElement(schema, f"{{{PDFASCHEMA}}}valueType"), f"{{{RDF}}}Seq")
    field_type = _resource(types)
    _text(field_type, f"{{{PDFATYPE}}}type", "Field")
    _text(field_type, f"{{{PDFATYPE}}}namespaceURI", OCRFIELD)
    _text(field_type, f"{{{PDFATYPE}}}prefix", "ocrfield")
    _text(field_type, f"{{{PDFATYPE}}}description", "Approved document field value.")
    fields = etree.SubElement(etree.SubElement(field_type, f"{{{PDFATYPE}}}field"), f"{{{RDF}}}Seq")
    for name, value_type, description in FIELD_TYPE:
        item = _resource(fields)
        _text(item, f"{{{PDFAFIELD}}}name", name)
        _text(item, f"{{{PDFAFIELD}}}valueType", value_type)
        _text(item, f"{{{PDFAFIELD}}}description", description)

    values = etree.SubElement(rdf, f"{{{RDF}}}Description", {f"{{{RDF}}}about": ""},
                              nsmap={"ocrdoc": OCRDOC, "ocrfield": OCRFIELD})
    if type(projection.package_version) is not int or projection.package_version < 1:
        raise ValueError("Invalid package version.")
    for name, value in zip(_SCALARS, (
            projection.document_id, str(projection.package_version), projection.metadata_revision,
            projection.metadata_sha256, projection.original_sha256, projection.canonical_sha256)):
        _text(values, f"{{{OCRDOC}}}{name}", value)
    field_bag = etree.SubElement(etree.SubElement(values, f"{{{OCRDOC}}}fields"), f"{{{RDF}}}Bag")
    for field in projection.fields:
        if type(field.index) is not int or field.index < 0:
            raise ValueError("Invalid field index.")
        item = _resource(field_bag)
        _text(item, f"{{{OCRFIELD}}}segment", field.segment)
        _text(item, f"{{{OCRFIELD}}}name", field.name)
        _text(item, f"{{{OCRFIELD}}}index", str(field.index))
        _text(item, f"{{{OCRFIELD}}}value", field.value)
    return etree.tostring(tree, encoding="utf-8", xml_declaration=False)


def _one(parent, path):
    found = parent.xpath(path, namespaces=_NS)
    if len(found) != 1 or len(found[0]) or found[0].attrib:
        raise ValueError(f"Expected exactly one simple {path}.")
    return found[0].text or ""


def _integer(text):
    if _DECIMAL.fullmatch(text) is None:
        raise ValueError("Non-canonical XMP integer.")
    return int(text)


def extract(xmp: bytes) -> XmpProjection:
    """Read back the ocrdoc values exactly as written; reject duplicates and ambiguity."""
    tree = _parse(xmp)
    holders = tree.xpath("//rdf:Description[ocrdoc:documentId]", namespaces=_NS)
    if len(holders) != 1 or len(tree.xpath("//ocrdoc:*", namespaces=_NS)) != len(_SCALARS) + 1:
        raise ValueError("Expected exactly one ocrdoc value set.")
    holder = holders[0]
    scalars = {name: _one(holder, f"ocrdoc:{name}") for name in _SCALARS}
    bags = holder.xpath("ocrdoc:fields/rdf:Bag", namespaces=_NS)
    if len(bags) != 1 or len(holder.xpath("ocrdoc:fields/*", namespaces=_NS)) != 1:
        raise ValueError("Expected exactly one ocrdoc:fields bag.")
    fields = []
    for item in bags[0]:
        if (item.tag != f"{{{RDF}}}li" or item.get(f"{{{RDF}}}parseType") != "Resource"
                or len(item) != len(FIELD_TYPE)):
            raise ValueError("Invalid ocrdoc field structure.")
        parts = {name: _one(item, f"ocrfield:{name}") for name, _, _ in FIELD_TYPE}
        fields.append(XmpField(parts["segment"], parts["name"], _integer(parts["index"]), parts["value"]))
    return XmpProjection(
        scalars["documentId"], _integer(scalars["packageVersion"]), scalars["metadataRevision"],
        scalars["metadataSha256"], scalars["originalSha256"], scalars["canonicalSha256"], tuple(fields))


def declared_properties(xmp: bytes) -> dict[str, str]:
    """Return {name: valueType} declared for ocrdoc; exactly one declaration is required."""
    items = [item for item in _schema_items(_parse(xmp)) if _declared(item) == [OCRDOC]]
    if len(items) != 1:
        raise ValueError("The ocrdoc schema must be declared exactly once.")
    declared = {}
    for item in items[0].xpath("pdfaSchema:property/rdf:Seq/rdf:li", namespaces=_NS):
        name = _one(item, "pdfaProperty:name")
        if name in declared:
            raise ValueError("Duplicate property declaration.")
        declared[name] = _one(item, "pdfaProperty:valueType")
    return declared
