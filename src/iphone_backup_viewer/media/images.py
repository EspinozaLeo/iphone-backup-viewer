from __future__ import annotations

import logging
import threading
from pathlib import Path

from PIL import Image, ImageOps

log = logging.getLogger(__name__)

_heif_lock = threading.Lock()
_heif_registered = False


def _register_heif() -> None:
    """Teach Pillow to open HEIC/HEIF, once per process. Failing that, other formats still work."""
    global _heif_registered
    if _heif_registered:
        return
    with _heif_lock:
        if _heif_registered:
            return
        try:
            import pi_heif

            pi_heif.register_heif_opener()
        except Exception:
            log.warning("HEIC support is unavailable; .heic photos will show placeholders", exc_info=True)
        _heif_registered = True


def open_image(path: Path, max_size: int | None = None) -> Image.Image:
    """Decode JPEG/PNG/HEIC/etc. with EXIF orientation applied, RGB(A). Downscale to fit `max_size` if given."""
    _register_heif()
    with Image.open(path) as source:
        if max_size:
            # JPEG only (a no-op elsewhere): decode at 1/2, 1/4 or 1/8 scale straight from the DCT,
            # never smaller than the box. Square box, so it holds whatever the orientation.
            source.draft(None, (max_size, max_size))
        try:
            # pi-heif applies HEIF's own rotation and resets the EXIF tag to 1, so this never
            # rotates a HEIC twice.
            image = ImageOps.exif_transpose(source)
        except Exception as exc:  # a malformed EXIF block must not lose the picture
            log.debug("Ignoring bad EXIF orientation in %s: %s", path, exc)
            image = source.copy()
        image = _to_rgb_or_rgba(image)
    if max_size:
        image.thumbnail((max_size, max_size))
    return image


def _to_rgb_or_rgba(image: Image.Image) -> Image.Image:
    if image.mode in ("RGB", "RGBA"):
        return image
    if image.mode in ("LA", "PA", "RGBa", "La") or "transparency" in image.info:
        return image.convert("RGBA")
    return image.convert("RGB")
