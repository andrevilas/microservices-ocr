"""Preset homologation benchmark (OCR-EV-01). Synthetic corpus only; run inside the production image:

  docker run --rm --network none --cpus 2 --memory 2g -v <repo>:/src:ro -v <out>:/out \
    -e PYTHONPATH=/src --entrypoint python <image> /src/benchmarks/presets/run.py /out

It mirrors the product pipeline: OCR to a searchable PDF first (fixed arguments), then each preset's
final conversion from app.services.ocrmypdf_service.preset_arguments, then veraPDF on the final bytes.
Acceptance criteria are fixed below (v1 before any result, v2 recorded as a revision); a preset is homologated only when
every case meets every criterion.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import resource
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import corpus  # noqa: E402

PRESETS = ("preservation", "none", "balanced", "economic")  # preservation first: the same-pipeline reference
LANGUAGES = "por+eng"
JOBS = "2"
# v1 was fixed before the first run. It compared every preset against the *input*, and the lossless
# preservation preset failed it exactly like the others: those metrics measure the Ghostscript PDF/A
# conversion every preset shares, not the preset. They stay as informational "pipeline_*" fields.
CRITERIA_V1 = {
    "text_layer_preserved": "extracted text identical to the pre-compression OCR text",
    "readability": "re-OCR CER at most 0.01 above re-OCR of the rendered input, per scanned page",
    "visual_fidelity": "PSNR against the input rendering at least 30 dB on every page",
}
# v2 isolates the preset: same thresholds, measured against the preservation output of the same pipeline,
# plus a direct lossless check for preservation itself.
CRITERIA = {
    "pdfa_passed": "veraPDF PDF/A-2b passed on the final bytes (validated hash == file hash)",
    "pages_preserved": "same page count and exactly the same page sizes as the input",
    "text_layer": "extracted text identical to the preservation output's (NFC, collapsed spaces)",
    "readability": "re-OCR CER at most 0.01 above re-OCR of the preservation rendering, per scanned page",
    "visual_fidelity": "PSNR of the 150 dpi rendering against the preservation rendering at least 30 dB",
    "lossless": "preservation only: every image decodes to exactly the input's pixels",
    "resources": "peak RSS of the conversion at most 1536 MiB",
    "economic_purpose": "economic only: smaller than balanced on every case with images (at most +1% without), "
                        "at least 15% smaller in median on scanned cases",
}
MAX_RSS_MB = 1536


def measured(cmd: list[str], timeout=600) -> tuple[subprocess.CompletedProcess, float, float]:
    """Run cmd in a fresh child so RUSAGE_CHILDREN reflects only this conversion and its descendants."""
    wrapper = ("import json,resource,subprocess,sys;r=subprocess.run(sys.argv[1:],capture_output=True,text=True);"
               "print(json.dumps({'rc':r.returncode,'err':r.stderr[-2000:],"
               "'rss':resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss}))")
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-c", wrapper, *cmd], capture_output=True, text=True, timeout=timeout)
    seconds = time.monotonic() - started
    info = json.loads(result.stdout.strip().splitlines()[-1])
    return subprocess.CompletedProcess(cmd, info["rc"], "", info["err"]), seconds, info["rss"] / 1024


def ocrmypdf(*args) -> list[str]:
    return [sys.executable, "-m", "ocrmypdf", *args]


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def distance(a, b) -> int:
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def cer(reference: str, text: str) -> float:
    reference, text = norm(reference), norm(text)
    return distance(reference, text) / max(1, len(reference))


def render(pdf: Path, page: int, dpi: int, target: Path, gray=True) -> Path:
    stem = target.with_suffix("")
    subprocess.run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", str(dpi), "-singlefile",
                    "-gray" if gray else "-png", *(["-png"] if gray else []), str(pdf), str(stem)],
                   check=True, capture_output=True, timeout=180)
    return stem.with_suffix(".png")


def psnr(a: Path, b: Path) -> float:
    from PIL import Image, ImageChops, ImageStat
    first, second = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    if first.size != second.size:
        return 0.0
    mse = sum(v * v for v in ImageStat.Stat(ImageChops.difference(first, second)).rms) / 3
    return 99.0 if mse == 0 else round(10 * math.log10(255 ** 2 / mse), 2)


def reocr(png: Path) -> str:
    result = subprocess.run(["tesseract", str(png), "-", "-l", LANGUAGES, "--psm", "6"],
                            capture_output=True, text=True, timeout=300)
    return result.stdout


def page_geometry(pdf: Path):
    from pypdf import PdfReader
    reader = PdfReader(str(pdf))
    return [(float(p.mediabox.width), float(p.mediabox.height)) for p in reader.pages], \
        [p.extract_text() or "" for p in reader.pages]


def image_pixels(pdf: Path) -> list[str]:
    """Digest of every decoded image, page order: equal lists mean the images survived losslessly."""
    from pypdf import PdfReader
    pixels = []
    for page in PdfReader(str(pdf)).pages:
        for image in page.images:
            decoded = image.image
            pixels.append(hashlib.sha256(decoded.mode.encode() + repr(decoded.size).encode()
                                         + decoded.tobytes()).hexdigest())
    return pixels


def main(out: Path) -> int:
    import os
    from app.services.ocrmypdf_service import preset_arguments
    from app.services.pdfa_validator import PROFILE_2B, build_validator
    validator = build_validator(os.environ["VERAPDF_EXECUTABLE"], 300)
    work = out / "work"
    shutil.rmtree(work, ignore_errors=True)
    cases = corpus.build(work / "corpus")
    rows, renders = [], work / "renders"
    renders.mkdir(parents=True)

    def readability(pdf, page, reference, name):
        return round(cer(reference, reocr(render(pdf, page, 300, renders / f"{name}-{page}-300.png"))), 4)

    def convert(case_name, source, reference, scanned):
        ocr = work / f"{case_name}.ocr.pdf"
        result, _, _ = measured(ocrmypdf("--skip-text", "--output-type", "pdf", "--optimize", "0",
                                         "--language", LANGUAGES, "--jobs", JOBS, str(source), str(ocr)))
        if result.returncode != 0:
            rows.append({"case": case_name, "stage": "ocr", "error": result.stderr[-600:]})
            return None
        geometry, ocr_text = page_geometry(ocr)
        input_pixels = image_pixels(source)
        inputs = {page: render(source, page, 150, renders / f"{case_name}-in-{page}-150.png", gray=False)
                  for page in range(1, len(geometry) + 1)}
        input_cer = {page: readability(source, page, reference[page - 1], f"{case_name}-in") for page in scanned}
        outputs, reference_pages, reference_text = {}, {}, None
        for preset in PRESETS:
            target = work / f"{case_name}-{preset}.pdf"
            args = preset_arguments(preset, "pdfa")
            result, seconds, rss = measured(ocrmypdf("--skip-text", "--language", LANGUAGES, "--jobs", JOBS,
                                                     *args, str(ocr), str(target)))
            row = {"case": case_name, "preset": preset, "arguments": args, "exit": result.returncode,
                   "seconds": round(seconds, 2), "peak_rss_mb": round(rss, 1),
                   "input_bytes": source.stat().st_size, "ocr_bytes": ocr.stat().st_size,
                   "has_images": bool(input_pixels)}
            if result.returncode != 0:
                row["error"] = result.stderr[-600:]
                rows.append(row)
                continue
            verdict = validator.validate(target, PROFILE_2B)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            out_geometry, out_text = page_geometry(target)
            texts = [norm(t) for t in out_text]
            if preset == "preservation":
                reference_text = texts
            pages = []
            for page in range(1, len(out_geometry) + 1):
                low = render(target, page, 150, renders / f"{case_name}-{preset}-{page}-150.png", gray=False)
                if preset == "preservation":
                    reference_pages[page] = low
                entry = {"page": page, "pipeline_psnr_db": psnr(inputs[page], low),
                         "psnr_db": psnr(reference_pages[page], low) if page in reference_pages else None}
                if page in scanned:
                    entry["pipeline_cer_input"] = input_cer[page]
                    entry["cer"] = readability(target, page, reference[page - 1], f"{case_name}-{preset}")
                pages.append(entry)
            row.update(output_bytes=target.stat().st_size, pages=pages, sha256=digest,
                       pdfa=verdict.status, pdfa_hash_matches=verdict.sha256 == digest,
                       pages_preserved=out_geometry == geometry,
                       pipeline_text_layer_preserved=texts == [norm(t) for t in ocr_text],
                       text_layer_matches_reference=texts == reference_text,
                       images_lossless=image_pixels(target) == input_pixels)
            rows.append(row)
            outputs[preset] = target
        return outputs

    for case in cases:
        outputs = convert(case.name, work / "corpus" / f"{case.name}.pdf", case.reference, case.scanned_pages)
        if case.name == "digital" and outputs and "balanced" in outputs:
            already = work / "corpus" / "already-pdfa.pdf"
            shutil.copyfile(outputs["balanced"], already)
            convert("already-pdfa", already, case.reference, [])

    verdicts = evaluate(rows)
    (out / "results.json").write_text(json.dumps({
        "criteria_v1_superseded": CRITERIA_V1, "criteria": CRITERIA,
        "cases": [{"name": c.name, "description": c.description, "scanned_pages": c.scanned_pages} for c in cases]
        + [{"name": "already-pdfa", "description": "balanced PDF/A output fed back as input", "scanned_pages": []}],
        "results": rows, "verdicts": verdicts}, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(verdicts, indent=1))
    return 0


def evaluate(rows: list[dict]) -> dict:
    verdicts = {}
    reference = {(r["case"], p["page"]): p.get("cer") for r in rows if r.get("preset") == "preservation"
                 for p in r.get("pages", [])}
    for preset in PRESETS:
        failures = [f"{r['case']}: ocr stage failed" for r in rows if r.get("stage") == "ocr"]
        mine = [r for r in rows if r.get("preset") == preset]
        for r in mine:
            if "error" in r:
                failures.append(f"{r['case']}: conversion failed")
                continue
            if r["pdfa"] != "passed" or not r["pdfa_hash_matches"]:
                failures.append(f"{r['case']}: pdfa_passed")
            if not r["pages_preserved"]:
                failures.append(f"{r['case']}: pages_preserved")
            if not r["text_layer_matches_reference"]:
                failures.append(f"{r['case']}: text_layer")
            if preset == "preservation" and not r["images_lossless"]:
                failures.append(f"{r['case']}: lossless")
            for p in r["pages"]:
                if p["psnr_db"] is None or p["psnr_db"] < 30:
                    failures.append(f"{r['case']} p{p['page']}: visual_fidelity {p['psnr_db']} dB")
                base = reference.get((r["case"], p["page"]))
                if "cer" in p and (base is None or p["cer"] > base + 0.01):
                    failures.append(f"{r['case']} p{p['page']}: readability {base}->{p['cer']}")
            if r["peak_rss_mb"] > MAX_RSS_MB:
                failures.append(f"{r['case']}: resources {r['peak_rss_mb']} MiB")
        if preset == "economic":
            balanced = {r["case"]: r for r in rows if r.get("preset") == "balanced" and "output_bytes" in r}
            savings = []
            for r in mine:
                b = balanced.get(r["case"])
                if b is None or "output_bytes" not in r:
                    continue
                allowed = b["output_bytes"] if r["has_images"] else b["output_bytes"] * 1.01
                if r["output_bytes"] > allowed:
                    failures.append(f"{r['case']}: economic_purpose {r['output_bytes']} > balanced {b['output_bytes']}")
                if r["has_images"]:
                    savings.append(1 - r["output_bytes"] / b["output_bytes"])
            savings.sort()
            median = savings[len(savings) // 2] if savings else 0
            if median < 0.15:
                failures.append(f"economic_purpose: median saving {median:.1%} on scanned cases")
        verdicts[preset] = {"homologated": not failures, "failures": failures}
    return verdicts


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
