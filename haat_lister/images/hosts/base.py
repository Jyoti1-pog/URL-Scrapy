"""The ImageHost contract.

Defined in Phase 7 so the image pipeline can be written -- and tested -- against
an interface before any adapter exists. `test_manifest_mode_never_calls_any_host`
asserts against this protocol, so it keeps working unchanged when Phase 8 adds
Cloudinary, ImgBB and Imgur.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, Field
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from ...utils.logging import get_logger

log = get_logger(__name__)


class HostedImage(BaseModel):
    url: str
    host_name: str
    # Persisted in the ledger: someone will want to clean these up later.
    delete_url: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class ImageHost(Protocol):
    """One third-party image host.

    `is_configured` is checked at STARTUP, not mid-run: an operator should learn
    that ImgBB has no API key before a 5,000-row batch, not after 300 of them.
    """

    name: str

    def is_configured(self) -> bool: ...

    async def upload(self, path: Path) -> HostedImage | None: ...


class RetryableUpload(Exception):
    """A failure worth trying again: 429, 5xx, or a transport hiccup."""


def parse_retry_after(response: httpx.Response) -> float | None:
    """`Retry-After` is either delta-seconds or an HTTP date. Honour both."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


async def post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    host_name: str,
    max_attempts: int,
    initial_s: float,
    max_s: float,
    **request_kwargs: Any,
) -> httpx.Response | None:
    """POST with exponential backoff and jitter, honouring `Retry-After`.

    Returns None when every attempt failed, so the caller can move to the next
    host in the chain rather than aborting the run. A 4xx that is not 429 is
    NOT retried -- bad credentials do not improve with waiting.
    """

    async def attempt_once() -> httpx.Response:
        try:
            response = await client.post(url, **request_kwargs)
        except httpx.HTTPError as exc:
            raise RetryableUpload(f"{host_name}: transport error: {exc}") from exc

        if response.status_code == 429 or response.status_code >= 500:
            if delay := parse_retry_after(response):
                log.info("%s asked us to wait %.1fs; honouring Retry-After", host_name, delay)
                await asyncio.sleep(delay)
            raise RetryableUpload(f"{host_name}: http_{response.status_code}")

        return response

    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential_jitter(initial=initial_s, max=max_s),
            retry=retry_if_exception_type(RetryableUpload),
            reraise=True,
        ):
            with attempt:
                return await attempt_once()
    except (RetryableUpload, RetryError) as exc:
        log.warning("%s exhausted %d attempts: %s", host_name, max_attempts, exc)
        return None

    return None
