"""Cloudinary -- the primary host.

Chosen first because it has a real API, a usable free tier, and stable permanent
URLs. ImgBB is a light fallback; Imgur is last because its rate limits bite on
batches and its terms are unfriendly to commercial catalogue use.

Implemented against the REST endpoint with httpx rather than the `cloudinary`
SDK. The signed-upload signature is a dozen lines, and doing it here keeps the
dependency optional, keeps every host on one retry path, and makes the whole
thing testable with respx like everything else.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ...config import HostsConfig, Secrets
from ...utils.logging import get_logger
from .base import HostedImage, post_with_retry

log = get_logger(__name__)

API = "https://api.cloudinary.com/v1_1/{cloud_name}/image/upload"


class CloudinaryHost:
    name = "cloudinary"

    def __init__(self, client: httpx.AsyncClient, secrets: Secrets, cfg: HostsConfig) -> None:
        self._client = client
        self._cfg = cfg
        self._cloud_name, self._api_key, self._api_secret = _credentials(secrets)

    def is_configured(self) -> bool:
        return all((self._cloud_name, self._api_key, self._api_secret))

    def _signature(self, params: dict[str, str]) -> str:
        """Cloudinary signs the sorted params, then appends the secret."""
        payload = "&".join(f"{k}={params[k]}" for k in sorted(params))
        return hashlib.sha1(f"{payload}{self._api_secret}".encode()).hexdigest()

    async def upload(self, path: Path) -> HostedImage | None:
        if not self.is_configured():
            return None

        signed = {"timestamp": str(int(time.time()))}
        if self._cfg.cloudinary_folder:
            signed["folder"] = self._cfg.cloudinary_folder

        data = {**signed, "api_key": self._api_key, "signature": self._signature(signed)}

        with path.open("rb") as handle:
            response = await post_with_retry(
                self._client,
                API.format(cloud_name=self._cloud_name),
                host_name=self.name,
                max_attempts=self._cfg.max_attempts_per_host,
                initial_s=self._cfg.backoff_initial_s,
                max_s=self._cfg.backoff_max_s,
                data=data,
                files={"file": (path.name, handle, "application/octet-stream")},
            )

        if response is None:
            return None
        if response.status_code >= 400:
            log.warning("cloudinary refused upload: http_%s", response.status_code)
            return None

        try:
            payload = response.json()
        except ValueError:
            log.warning("cloudinary returned a non-JSON body")
            return None

        url = payload.get("secure_url") or payload.get("url")
        if not url:
            log.warning("cloudinary response carried no URL")
            return None

        return HostedImage(
            url=url,
            host_name=self.name,
            # Cloudinary deletes by public_id through the admin API, so the
            # ledger stores the identifier rather than a one-click URL.
            delete_url=payload.get("public_id"),
            raw=payload,
        )


def _credentials(secrets: Secrets) -> tuple[str, str, str]:
    """Accept either CLOUDINARY_URL or the three-part form; the URL wins."""
    if secrets.cloudinary_url:
        parsed = urlparse(secrets.cloudinary_url.get_secret_value())
        if parsed.scheme == "cloudinary" and parsed.hostname:
            return parsed.hostname, parsed.username or "", parsed.password or ""

    return (
        secrets.cloudinary_cloud_name,
        secrets.cloudinary_api_key.get_secret_value() if secrets.cloudinary_api_key else "",
        secrets.cloudinary_api_secret.get_secret_value() if secrets.cloudinary_api_secret else "",
    )
