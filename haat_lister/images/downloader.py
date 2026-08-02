"""TIER 2a -- download.

Runs when the mode needs a local file, or when Tier 1 failed while a URL is
needed. Never runs speculatively.

Note the asymmetry with Tier 1: a `hotlink_blocked` or `signed_or_expiring_url`
candidate usually downloads perfectly well *for us*, because we send the product
page as `Referer` and reuse the session cookies from the page fetch. Those URLs
fail Tier 1 because they would not work for a buyer looking at a haat listing --
which is a different question from whether we can fetch the bytes now.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, UnidentifiedImageError

from ..config import FetchConfig, ImagesConfig
from ..utils.logging import get_logger
from .validator import sniff_format

log = get_logger(__name__)

_EXTENSIONS = {
    "jpeg": ".jpg",
    "png": ".png",
    "webp": ".webp",
    "gif": ".gif",
    "avif": ".avif",
    "bmp": ".bmp",
}


@dataclass
class DownloadedFile:
    path: Path
    source_url: str
    bytes: int


async def download_candidate(
    client: httpx.AsyncClient,
    url: str,
    dest_dir: Path,
    index: int,
    images_cfg: ImagesConfig,
    fetch_cfg: FetchConfig,
    referer: str,
) -> DownloadedFile | None:
    """Stream one candidate to disk. Returns None on any failure, never raises.

    Corrupt bytes are deleted rather than left for the optimiser to trip over.
    """
    limit = images_cfg.max_download_mb * 1024 * 1024
    dest_dir.mkdir(parents=True, exist_ok=True)
    headers = {
        "Referer": referer,
        "User-Agent": fetch_cfg.download_user_agent,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }

    buffer = bytearray()
    try:
        async with client.stream("GET", url, headers=headers, follow_redirects=True) as response:
            if response.status_code >= 400:
                log.debug("Download %s -> http_%s", url, response.status_code)
                return None
            async for chunk in response.aiter_bytes(65536):
                buffer.extend(chunk)
                if len(buffer) > limit:
                    log.warning(
                        "Skipping %s: exceeds max_download_mb (%d MB)",
                        url,
                        images_cfg.max_download_mb,
                    )
                    return None
    except httpx.HTTPError as exc:
        log.debug("Download %s failed: %s", url, exc)
        return None

    if not buffer:
        return None

    fmt = sniff_format(bytes(buffer[:16]))
    if fmt is None:
        log.debug("Download %s: not an image (magic bytes)", url)
        return None

    path = dest_dir / f"{index:02d}{_EXTENSIONS.get(fmt, '.img')}"
    path.write_bytes(bytes(buffer))

    try:
        with Image.open(path) as image:
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        log.debug("Download %s: corrupt image, discarding (%s)", url, exc)
        path.unlink(missing_ok=True)
        return None

    return DownloadedFile(path=path, source_url=url, bytes=len(buffer))


async def download_candidates(
    client: httpx.AsyncClient,
    candidates: list[str],
    dest_dir: Path,
    images_cfg: ImagesConfig,
    fetch_cfg: FetchConfig,
    referer: str,
    limit: int | None = None,
) -> list[DownloadedFile]:
    """Highest-ranked candidates first, skipping any that fail.

    `limit` caps how many photos we keep; it defaults to haat's own ceiling.
    """
    wanted = limit or images_cfg.max_images_per_product
    files: list[DownloadedFile] = []

    for url in candidates:
        if len(files) >= wanted:
            break
        if downloaded := await download_candidate(
            client, url, dest_dir, len(files) + 1, images_cfg, fetch_cfg, referer
        ):
            files.append(downloaded)

    return files
