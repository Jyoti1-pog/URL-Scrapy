"""TIER 2b -- normalise. Runs in every mode, on every downloaded file.

Targets haat's own uploader limits, measured from its listing creator:
JPEG / PNG / WebP, up to 8 MB each. WebP is kept rather than converted, because
haat accepts it and re-encoding would be a lossy step for no benefit. Anything
outside those three formats becomes JPEG.

EXIF is stripped unconditionally. Phone photos carry GPS coordinates, and a
maker's home address is not something to publish with a product listing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from ..config import ImagesConfig
from ..utils.logging import get_logger

log = get_logger(__name__)

_EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}


@dataclass
class OptimisedFile:
    path: Path
    bytes: int
    width: int
    height: int
    image_format: str


class OptimiseError(Exception):
    """Named failure, never a silent skip."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _target_format(source_format: str, cfg: ImagesConfig) -> str:
    accepted = {f.upper() for f in cfg.accepted_formats}
    fmt = (source_format or "").upper()
    if fmt == "JPG":
        fmt = "JPEG"
    if fmt == "WEBP" and not cfg.keep_webp:
        return "JPEG"
    return fmt if fmt in accepted else "JPEG"


def _flatten_for_jpeg(image: Image.Image) -> Image.Image:
    """JPEG has no alpha channel; compositing onto white beats a black halo."""
    if image.mode in ("RGBA", "LA", "P"):
        converted = image.convert("RGBA")
        background = Image.new("RGB", converted.size, (255, 255, 255))
        background.paste(converted, mask=converted.split()[-1])
        return background
    return image.convert("RGB") if image.mode != "RGB" else image


def _save(image: Image.Image, path: Path, fmt: str, quality: int) -> int:
    options: dict[str, object] = {}
    if fmt == "JPEG":
        options = {"quality": quality, "progressive": True, "optimize": True}
    elif fmt == "WEBP":
        options = {"quality": quality, "method": 4}
    elif fmt == "PNG":
        options = {"optimize": True}
    # No exif= argument is passed, which is what strips it.
    image.save(path, format=fmt, **options)
    return path.stat().st_size


def optimise(
    source: Path, dest_dir: Path, order: int, cfg: ImagesConfig
) -> OptimisedFile:
    """Convert, strip, downscale and re-encode one file. Names it `01.jpg` etc.

    Raises OptimiseError with a named reason rather than returning None, so a
    caller cannot mistake failure for "nothing to do".
    """
    try:
        with Image.open(source) as opened:
            # Honour EXIF rotation BEFORE stripping EXIF, or portrait photos
            # come out sideways with no metadata left to explain why.
            image = ImageOps.exif_transpose(opened) or opened
            image.load()

            fmt = _target_format(opened.format or "", cfg)
            if fmt == "JPEG":
                image = _flatten_for_jpeg(image)

            # Downscale only. Upscaling invents detail that was never captured.
            longest = max(image.size)
            if longest > cfg.max_edge_px:
                scale = cfg.max_edge_px / longest
                new_size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
                image = image.resize(new_size, Image.Resampling.LANCZOS)

            dest_dir.mkdir(parents=True, exist_ok=True)
            path = dest_dir / f"{order:02d}{_EXTENSIONS.get(fmt, '.jpg')}"

            ceiling = cfg.max_file_mb * 1024 * 1024
            size = _save(image, path, fmt, cfg.jpeg_quality)

            if size > ceiling and fmt == "PNG":
                # PNG has no quality dial; JPEG is the only way down.
                path.unlink(missing_ok=True)
                fmt = "JPEG"
                image = _flatten_for_jpeg(image)
                path = dest_dir / f"{order:02d}.jpg"
                size = _save(image, path, fmt, cfg.jpeg_quality)

            for quality in cfg.jpeg_quality_steps:
                if size <= ceiling:
                    break
                size = _save(image, path, fmt, quality)

            if size > ceiling:
                path.unlink(missing_ok=True)
                raise OptimiseError("too_large_after_optimisation")

            return OptimisedFile(
                path=path,
                bytes=size,
                width=image.width,
                height=image.height,
                image_format=fmt,
            )
    except OptimiseError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise OptimiseError(f"optimise_failed:{type(exc).__name__}") from exc
