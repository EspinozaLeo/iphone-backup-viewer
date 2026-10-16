"""One still frame from a video, upright, via PyAV. Pure Python: no Qt here."""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

# Mirrors extractors.photos.VIDEO_SUFFIXES; media/ sits beside extractors/, not above it.
VIDEO_SUFFIXES = frozenset({".mov", ".mp4", ".m4v", ".3gp"})

_SEEK_SECONDS = 1.0
# After a seek, decoding runs forward from the previous keyframe. Bound it so a file with one
# keyframe per minute costs a few hundred frames at most, not the whole clip.
_MAX_FRAMES_AFTER_SEEK = 240
_ROTATIONS = {
    90: Image.Transpose.ROTATE_90,  # counter-clockwise
    180: Image.Transpose.ROTATE_180,
    270: Image.Transpose.ROTATE_270,  # = 90° clockwise: an iPhone shot held upright (rotation -90)
}


def video_frame(path: Path, max_size: int) -> Image.Image:
    """RGB frame about 1 s in (first frame as a fallback), rotated upright, fit inside `max_size`.

    Raises (av.FFmpegError, ValueError, OSError, ...) if the file has no decodable video at all.
    """
    try:
        image = _frame(path, _SEEK_SECONDS, max_size)
    except Exception as exc:
        log.debug("Seeking in %s failed (%s); using its first frame", path, exc)
        image = None
    if image is None:
        image = _frame(path, 0.0, max_size)
    if image is None:
        raise ValueError(f"No decodable video frame in {path}")
    return image


def _frame(path: Path, seconds: float, max_size: int) -> Image.Image | None:
    import av  # imported lazily: a broken FFmpeg install must not take photo thumbnails down with it

    with av.open(str(path), mode="r") as container:
        if not container.streams.video:
            raise ValueError(f"No video stream in {path}")
        stream = container.streams.video[0]
        time_base = stream.time_base
        start = stream.start_time or 0

        target = seconds
        duration = _duration_seconds(container, stream)
        if duration is not None:
            target = min(target, duration / 2)  # short clips: the middle, never past the end
        if target > 0 and time_base:
            container.seek(start + int(target / time_base), stream=stream, backward=True, any_frame=False)
            wanted = float(start * time_base) + target
        else:
            wanted = None

        chosen = None
        for count, frame in enumerate(container.decode(stream)):
            chosen = frame
            if wanted is None or frame.time is None or frame.time >= wanted or count >= _MAX_FRAMES_AFTER_SEEK:
                break
        if chosen is None:
            return None
        return _upright_image(chosen, max_size)


def _duration_seconds(container, stream) -> float | None:
    if stream.duration and stream.time_base:
        return float(stream.duration * stream.time_base)
    if container.duration:
        import av

        return container.duration / av.time_base
    return None


def _upright_image(frame, max_size: int) -> Image.Image:
    width, height = frame.width, frame.height
    scale = min(1.0, max_size / max(width, height))
    # Scale in swscale before conversion: a 4K HEVC frame never becomes a full-size RGB copy.
    image = frame.to_image(width=max(1, round(width * scale)), height=max(1, round(height * scale)))

    # Display matrix rotation, counter-clockwise degrees in [-180, 180]. iPhones record sideways
    # and store -90 for a portrait clip; FFmpeg's own autorotate turns that into a clockwise turn.
    # Mirroring in the matrix (rare; not produced by the iPhone camera) is not applied.
    try:
        rotation = int(round((frame.rotation or 0) / 90.0)) * 90 % 360
    except Exception:
        rotation = 0
    transpose = _ROTATIONS.get(rotation)
    if transpose is not None:
        image = image.transpose(transpose)
    return image
