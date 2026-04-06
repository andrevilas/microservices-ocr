from __future__ import annotations

from pathlib import Path


def preprocess_image(image_path: Path) -> Path:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return image_path

    image = Image.open(image_path).convert("L")
    image = ImageOps.autocontrast(image)
    image = image.point(lambda pixel: 255 if pixel > 160 else 0)
    output_path = image_path.with_name(f"{image_path.stem}-processed.png")
    image.save(output_path)
    return output_path
