from __future__ import annotations

from pathlib import Path

from PIL import Image


def open_image(path: Path, max_size: int | None = None) -> Image.Image:
    """Decode JPEG/PNG/HEIC/etc. with EXIF orientation applied, RGB(A). Downscale to fit `max_size` if given."""
    raise NotImplementedError
