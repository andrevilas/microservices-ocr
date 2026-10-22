"""Detect digital signatures in an input PDF (OCR-EV-01). Structure only; signatures are not verified.

Any OCR derivative rewrites the file, so a signature never survives into the output. What the product
guarantees instead is that a signed original stays byte-identical and travels with its derivative.
"""
from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader
from pypdf.generic import DictionaryObject

from app.results import InputSignatures

MAX_FIELDS = 10_000


def _resolve(value):
    return value.get_object() if hasattr(value, "get_object") else value


def inspect_signatures(path: Path) -> InputSignatures:
    """Count signed /Sig form fields and DocMDP certification. Unreadable structure → signed=None."""
    try:
        root = _resolve(PdfReader(str(path)).trailer["/Root"])
        form = _resolve(root.get("/AcroForm"))
        perms = _resolve(root.get("/Perms"))
        certified = isinstance(perms, DictionaryObject) and "/DocMDP" in perms
        fields = _resolve(form.get("/Fields")) if isinstance(form, DictionaryObject) else None
        count, seen, pending = 0, set(), [(item, None) for item in fields or []]
        while pending:
            reference, inherited = pending.pop()
            key = (reference.idnum, reference.generation) if hasattr(reference, "idnum") else id(reference)
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > MAX_FIELDS:
                return InputSignatures(signed=None)
            field = _resolve(reference)
            if not isinstance(field, DictionaryObject):
                continue
            kind = field.get("/FT", inherited)  # /FT is inheritable down the field tree
            if kind == "/Sig" and _resolve(field.get("/V")) is not None:
                count += 1
            pending.extend((kid, kind) for kid in _resolve(field.get("/Kids")) or [])
        return InputSignatures(signed=bool(count) or certified, signature_count=count, certified=certified)
    except Exception:  # Malformed structure: preservation must not depend on a parse that failed.
        return InputSignatures(signed=None)
