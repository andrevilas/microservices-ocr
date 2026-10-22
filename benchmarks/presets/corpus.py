"""Deterministic synthetic corpus for preset homologation (OCR-EV-01). Never reads real documents.

Each case is a PDF plus the text a reader should recover from it. Scans are rendered from known lines,
so readability can be re-measured after compression by OCR of the rendered output.
"""
from __future__ import annotations

import io
import math
import random
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
A4 = (595.28, 841.89)
A3 = (841.89, 1190.55)
LINES = ["DOCUMENTO SINTETICO - SEM VALIDADE", "Processo 014/2026 - Materiais eletricos",
         "Valor: R$ 12.345,67. Data: 16/10/2026.", "Imovel: lote 12, quadra 3. Matricula: 12345.",
         "Fornecedor ficticio ALFA. Quantidade: 125 unidades.", "Edital 7/2026, item 4: cabo flexivel 2,5 mm.",
         "Prazo de entrega: 30 dias corridos apos o pedido.", "Contrato 88/2026 assinado em 03/09/2026."]


@dataclass(frozen=True)
class Case:
    name: str
    description: str
    reference: list[str]  # text expected per page, in reading order
    scanned_pages: list[int]  # 1-based pages that are images


def _scan(size_pt, dpi, lines, *, font_px, seed, color=False, stamp=False, table=False, bilevel=False):
    """Render a page image like a flatbed scan: off-white paper, slight blur and sensor noise."""
    width, height = (round(v / 72 * dpi) for v in size_pt)
    rng = random.Random(seed)
    image = Image.new("RGB", (width, height), (246, 245, 240))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(FONT, font_px)
    margin, step = round(width * 0.07), round(font_px * 1.8)
    y = margin
    if table:
        cols, rows = 4, 14
        cell_w, cell_h = (width - 2 * margin) // cols, round(font_px * 2.2)
        for r in range(rows + 1):
            draw.line((margin, y + r * cell_h, margin + cols * cell_w, y + r * cell_h), fill=(30, 30, 30), width=2)
        for c in range(cols + 1):
            draw.line((margin + c * cell_w, y, margin + c * cell_w, y + rows * cell_h), fill=(30, 30, 30), width=2)
        for r, line in enumerate(lines):
            draw.text((margin + 12, y + r * cell_h + (cell_h - font_px) // 2), line, font=font, fill=(20, 20, 24))
    else:
        for line in lines:
            draw.text((margin, y), line, font=font, fill=(20, 20, 24))
            y += step
    if color:
        # Logo block, photo-like gradient and a blue pen stroke: lossy re-encoding shows here first.
        draw.rectangle((width - margin - 300, margin, width - margin, margin + 160), fill=(24, 78, 127))
        top = height - margin - 600
        for x in range(margin, width - margin, 4):
            shade = int(127 + 120 * math.sin(x / 90))
            draw.line((x, top, x, top + 400), fill=(shade, 180 - shade // 3, 90 + shade // 2), width=4)
        points = [(margin + i * 18, height - margin - 120 + int(40 * math.sin(i / 4))) for i in range(60)]
        draw.line(points, fill=(20, 40, 160), width=5)
    if stamp:
        cx, cy, r = width - margin - 260, height // 2, 220
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(190, 20, 30), width=10)
        draw.text((cx - 170, cy - 30), "CARIMBO FICTICIO", font=ImageFont.truetype(FONT, 46), fill=(190, 20, 30))
    image = image.filter(ImageFilter.GaussianBlur(0.6))
    pixels = image.load()
    for _ in range(width * height // 60):
        x, y = rng.randrange(width), rng.randrange(height)
        v = rng.randrange(-28, 28)
        p = pixels[x, y]
        pixels[x, y] = tuple(max(0, min(255, c + v)) for c in p)
    if bilevel:
        image = image.convert("L").point(lambda v: 255 if v > 150 else 0, mode="1")
    elif not color and not stamp:
        image = image.convert("L")
    return image


def _draw_image(pdf, image, size_pt, *, png=False):
    buffer = io.BytesIO()
    if png or image.mode == "1":
        image.save(buffer, format="PNG")
    else:
        image.save(buffer, format="JPEG", quality=92)
    buffer.seek(0)
    pdf.setPageSize(size_pt)
    pdf.drawImage(ImageReader(buffer), 0, 0, width=size_pt[0], height=size_pt[1])
    pdf.showPage()


def _draw_text(pdf, lines, size_pt):
    pdf.setPageSize(size_pt)
    pdf.setFont("Helvetica", 12)
    for n, line in enumerate(lines):
        pdf.drawString(50, size_pt[1] - 70 - 22 * n, line)
    pdf.showPage()


def build(root: Path) -> list[Case]:
    root.mkdir(parents=True, exist_ok=True)
    cases: list[Case] = []

    def make(name, description, pages):
        pdf = canvas.Canvas(str(root / f"{name}.pdf"), pageCompression=1)
        pdf.setTitle("synthetic")
        reference, scanned = [], []
        for number, (kind, lines, extra) in enumerate(pages, 1):
            size = extra.pop("size", A4)
            if kind == "text":
                _draw_text(pdf, lines, size)
            else:
                dpi = extra.pop("dpi", 300)
                png = extra.pop("png", False)
                font_px = extra.pop("font_px", round(dpi / 300 * 42))
                _draw_image(pdf, _scan(size, dpi, lines, font_px=font_px, seed=number * 7919 + len(name), **extra),
                            size, png=png)
                scanned.append(number)
            reference.append("\n".join(lines))
        pdf.save()
        cases.append(Case(name, description, reference, scanned))

    make("digital", "born-digital text, 2 pages", [("text", LINES, {}), ("text", LINES[::-1], {})])
    make("scan-300-gray", "grayscale scan, 300 dpi, JPEG", [("scan", LINES, {})])
    make("scan-150-lowres", "low-resolution grayscale scan, 150 dpi", [("scan", LINES, {"dpi": 150})])
    make("scan-small-print", "300 dpi scan with small (about 8 pt) print", [("scan", LINES * 3, {"font_px": 30})])
    make("scan-color-stamp", "color scan with logo, gradient, pen stroke and red stamp",
         [("scan", LINES, {"color": True, "stamp": True})])
    make("scan-table", "300 dpi scan of a ruled table", [("scan", [f"{i + 1:02d} | {LINES[i % 8][:34]}" for i in range(14)],
                                                         {"table": True, "font_px": 34})])
    make("scan-a3", "large A3 scan, 300 dpi", [("scan", LINES * 2, {"size": A3})])
    make("scan-bilevel", "bilevel (fax-like) 300 dpi PNG scan", [("scan", LINES, {"bilevel": True})])
    make("mixed", "born-digital page followed by a color scan", [("text", LINES, {}),
                                                                   ("scan", LINES[2:], {"color": True})])
    return cases
