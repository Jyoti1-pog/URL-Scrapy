"""Third-party image hosts. Unreachable in `manifest` mode, by construction."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from ...config import HostsConfig, Secrets, Settings
from ...models import ImageMode
from .base import HostedImage, ImageHost
from .cloudinary import CloudinaryHost
from .imgbb import ImgbbHost
from .imgur import ImgurHost

__all__ = ["HostedImage", "ImageHost", "build_hosts"]

HostFactory = Callable[[httpx.AsyncClient, Secrets, HostsConfig], ImageHost]

# Chain order comes from config; this is only the name -> class lookup.
_REGISTRY: dict[str, HostFactory] = {
    "cloudinary": CloudinaryHost,
    "imgbb": ImgbbHost,
    "imgur": ImgurHost,
}


def build_hosts(
    settings: Settings, client: httpx.AsyncClient, mode: ImageMode
) -> tuple[list[ImageHost], list[str]]:
    """Build the configured chain, in order. Returns (usable, skipped).

    In a mode that needs no URL this returns nothing at all -- the strongest
    form of "manifest mode never contacts a host" is having no host object to
    contact.

    Hosts with missing credentials are dropped HERE, at startup, so an operator
    finds out before a 5,000-row batch rather than 300 rows into one.
    """
    if not mode.need_url:
        return [], []

    usable: list[ImageHost] = []
    skipped: list[str] = []

    for name in settings.config.hosts.chain:
        factory = _REGISTRY.get(name)
        if factory is None:
            skipped.append(f"{name} (unknown host)")
            continue
        host = factory(client, settings.secrets, settings.config.hosts)
        if host.is_configured():
            usable.append(host)
        else:
            skipped.append(f"{name} (no credentials)")

    return usable, skipped
