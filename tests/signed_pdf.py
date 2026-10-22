"""Synthetic signed-PDF fixtures: the structure a signer leaves behind, with a placeholder signature value."""
import io

import pikepdf
from reportlab.pdfgen import canvas


def plain_pdf(text="Contrato sintetico assinado") -> bytes:
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer)
    c.drawString(72, 720, text)
    c.showPage()
    c.save()
    return buffer.getvalue()


def _signature(pdf):
    return pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Sig, Filter=pikepdf.Name("/Adobe.PPKLite"), SubFilter=pikepdf.Name("/adbe.pkcs7.detached"),
        ByteRange=pikepdf.Array([0, 100, 200, 300]), Contents=pikepdf.String(b"\x00" * 64)))


def signed_pdf(source: bytes | None = None, *, certified=False, inherited=False, value=True) -> bytes:
    """A form with one /Sig field. inherited: /FT on the parent field, /V on its widget kid."""
    with pikepdf.open(io.BytesIO(source or plain_pdf())) as pdf:
        signature = _signature(pdf) if value else None
        widget = pikepdf.Dictionary(Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget,
                                    Rect=pikepdf.Array([0, 0, 0, 0]), P=pdf.pages[0].obj)
        if inherited:
            if signature is not None:
                widget.V = signature
            kid = pdf.make_indirect(widget)
            field = pdf.make_indirect(pikepdf.Dictionary(FT=pikepdf.Name.Sig, T=pikepdf.String("Assinatura"),
                                                         Kids=pikepdf.Array([kid])))
            kid.Parent = field
            annotation = kid
        else:
            widget.FT, widget.T = pikepdf.Name.Sig, pikepdf.String("Assinatura")
            if signature is not None:
                widget.V = signature
            field = annotation = pdf.make_indirect(widget)
        pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([annotation]))
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([field]), SigFlags=3)
        if certified:
            pdf.Root.Perms = pikepdf.Dictionary(DocMDP=signature if signature is not None else _signature(pdf))
        out = io.BytesIO()
        pdf.save(out)
        return out.getvalue()


def cyclic_fields_pdf() -> bytes:
    with pikepdf.open(io.BytesIO(plain_pdf())) as pdf:
        field = pdf.make_indirect(pikepdf.Dictionary(T=pikepdf.String("loop")))
        field.Kids = pikepdf.Array([field])
        pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([field]))
        out = io.BytesIO()
        pdf.save(out)
        return out.getvalue()
