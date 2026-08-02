"""Stage A: plain httpx GET.

The cheap path. Roughly 200ms and no browser, versus seconds and a Chromium
process for Stage B. On a 5,000-URL batch that difference is hours, so Stage B
is only ever reached when Stage A left something genuinely missing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from ..config import Settings
from ..models import FetchStage
from ..utils.logging import get_logger
from ..utils.netguard import BlockedHost, request_hook

log = get_logger(__name__)


class FetchError(Exception):
    """A fetch that failed in a way the row must record. Never swallowed."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class FetchResult:
    url: str
    final_url: str
    status_code: int
    html: str
    stage: FetchStage
    elapsed_ms: int
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)

    @property
    def is_html(self) -> bool:
        ctype = self.headers.get("content-type", "").lower()
        return "html" in ctype or not ctype


def build_client(settings: Settings, **overrides: object) -> httpx.AsyncClient:
    """One place that knows how we present ourselves to a site.

    The SSRF guard is attached here as a request event hook rather than called
    at the top of `fetch_static`, because a redirect chain never passes back
    through the fetcher -- httpx issues each hop itself, and the hook is the
    only thing that sees all of them.
    """
    cfg = settings.config.fetch
    kwargs: dict[str, object] = {
        "http2": True,
        "follow_redirects": True,
        "max_redirects": cfg.max_redirects,
        "timeout": httpx.Timeout(cfg.timeout_s, connect=cfg.connect_timeout_s),
        "event_hooks": {"request": [request_hook(cfg.allow_private_hosts)]},
        "headers": {
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        },
    }
    kwargs.update(overrides)
    return httpx.AsyncClient(**kwargs)  # type: ignore[arg-type]


async def fetch_static(client: httpx.AsyncClient, url: str, settings: Settings) -> FetchResult:
    """Fetch one page. Raises FetchError with a named reason; never returns junk.

    Note what is absent: no retry on 403, no alternate User-Agent, no cookie
    games. If a site blocks us, that is an answer, and the row fails loudly.
    """
    cfg = settings.config.fetch
    started = time.perf_counter()

    try:
        response = await client.get(url)
    except BlockedHost as exc:
        # Raised from the event hook, so it can fire on any hop. Its message
        # already names the remedy.
        raise FetchError("blocked_address", str(exc)) from exc
    except httpx.TimeoutException as exc:
        raise FetchError("timeout", str(exc)) from exc
    except httpx.ConnectError as exc:
        raise FetchError("dns_or_connect_error", str(exc)) from exc
    except httpx.TooManyRedirects as exc:
        raise FetchError("too_many_redirects", str(exc)) from exc
    except httpx.HTTPError as exc:
        raise FetchError("http_error", str(exc)) from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if response.status_code >= 400:
        raise FetchError(f"http_{response.status_code}", str(response.url))

    content_type = response.headers.get("content-type", "").lower()
    if content_type and "html" not in content_type and "xml" not in content_type:
        raise FetchError("not_html", content_type)

    if len(response.content) > cfg.max_html_bytes:
        log.warning("Truncating oversized page (%d bytes) from %s", len(response.content), url)

    return FetchResult(
        url=url,
        final_url=str(response.url),
        status_code=response.status_code,
        html=response.text[: cfg.max_html_bytes],
        stage=FetchStage.STATIC,
        elapsed_ms=elapsed_ms,
        headers={k.lower(): v for k, v in response.headers.items()},
        cookies=dict(response.cookies),
    )
