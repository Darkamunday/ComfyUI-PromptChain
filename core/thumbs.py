import logging
from pathlib import Path

from PIL import Image

from .history_db import resolve_image_path, get_data_dir

MAX_WIDTH = 600
QUALITY = 85


def _get_thumbs_dir() -> Path:
    return get_data_dir() / "thumbs"


def get_or_create_thumbnail(image_hash: str) -> Path | None:
    thumbs_dir = _get_thumbs_dir()
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = thumbs_dir / f"{image_hash}.webp"

    if thumb_path.is_file():
        return thumb_path

    source = resolve_image_path(image_hash)
    if not source or not source.is_file():
        return None

    try:
        with Image.open(source) as img:
            if img.mode in ("RGBA", "LA", "P", "PA"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(img, mask=img.split()[-1] if "A" in img.mode else None)
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            if img.width > MAX_WIDTH:
                ratio = MAX_WIDTH / img.width
                img = img.resize((MAX_WIDTH, round(img.height * ratio)), Image.LANCZOS)

            img.save(thumb_path, format="WEBP", quality=QUALITY)
        return thumb_path
    except Exception:
        logging.exception("[PromptChain] thumbnail generation failed for %s", image_hash)
        return None
