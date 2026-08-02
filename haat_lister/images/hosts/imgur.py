"""Imgur -- last in the chain, deliberately.

Its rate limits bite on batches and its terms are unfriendly to commercial
catalogue use, so it is present for completeness rather than as a real
recommendation. If it is doing much work, something upstream is wrong.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from ...config import HostsConfig, Secrets
from ...utils.logging import get_logger
from .base import HostedImage, post_with_retry

log = get_logger(__name__)

API = "https://api.imgur.com/3/image"


class ImgurHost:
    name = "imgur"

    def __init__(self, client: httpx.AsyncClient, secrets: Secrets, cfg: HostsConfig) -> None:
        self._client = client
        self._cfg = cfg
        self._client_id = (
            secrets.imgur_client_id.get_secret_value() if secrets.imgur_client_id else ""
        )

    def is_configured(self) -> bool:
        return bool(self._client_id)

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
                headers={"Authorization": f"Client-ID {self._client_id}"},
                files={"image": (path.name, handle, "application/octet-stream")},
            )

        if response is None or response.status_code >= 400:
            if response is not None:
                log.warning("imgur refused upload: http_%s", response.status_code)
            return None

        try:
            payload = response.json()
        except ValueError:
            log.warning("imgur returned a non-JSON body")
            return None

        data = payload.get("data") or {}
        if not (url := data.get("link")):
            log.warning("imgur response carried no link")
            return None

        return HostedImage(
            url=url, host_name=self.name, delete_url=data.get("deletehash"), raw=payload
        )
