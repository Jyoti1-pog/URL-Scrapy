"""ImgBB -- the light fallback.

Simple key-in-the-query upload, and it hands back a working delete URL, which
is the friendliest cleanup story of the three.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from ...config import HostsConfig, Secrets
from ...utils.logging import get_logger
from .base import HostedImage, post_with_retry

log = get_logger(__name__)

API = "https://api.imgbb.com/1/upload"


class ImgbbHost:
    name = "imgbb"

    def __init__(self, client: httpx.AsyncClient, secrets: Secrets, cfg: HostsConfig) -> None:
        self._client = client
        self._cfg = cfg
        self._key = secrets.imgbb_api_key.get_secret_value() if secrets.imgbb_api_key else ""

    def is_configured(self) -> bool:
        return bool(self._key)

    async def upload(self, path: Path) -> HostedImage | None:
        if not self.is_configured():
            return None

        with path.open("rb") as handle:
            response = await post_with_retry(
                self._client,
                API,
                host_name=self.name,
                max_attempts=self._cfg.max_attempts_per_host,
                initial_s=self._cfg.backoff_initial_s,
                max_s=self._cfg.backoff_max_s,
                params={"key": self._key},
                files={"image": (path.name, handle, "application/octet-stream")},
            )

        if response is None or response.status_code >= 400:
            if response is not None:
                log.warning("imgbb refused upload: http_%s", response.status_code)
            return None

        try:
            payload = response.json()
        except ValueError:
            log.warning("imgbb returned a non-JSON body")
            return None

        data = payload.get("data") or {}
        url = data.get("url") or (data.get("image") or {}).get("url")
        if not url:
            log.warning("imgbb response carried no URL")
            return None

        return HostedImage(
            url=url, host_name=self.name, delete_url=data.get("delete_url"), raw=payload
        )
