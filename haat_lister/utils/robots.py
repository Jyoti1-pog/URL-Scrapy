"""robots.txt, honoured by default.

Pulled forward from Phase 9 deliberately: the very first request this tool makes
should already be one a site operator has allowed. A fetch path that learns
manners later is a fetch path that shipped without them.

A missing or unparseable robots.txt means allowed -- that is the standard's own
default, not a shortcut. A 401/403 on robots.txt means disallowed, because a
site that will not even show us its rules has not granted us anything.
"""

from __future__ import annotations

import httpx
from protego import Protego

from .logging import get_logger
from .netguard import BlockedHost
from .urls import origin_of

log = get_logger(__name__)


class RobotsCache:
    """One robots.txt per origin, fetched once per run."""

    def __init__(self, client: httpx.AsyncClient, user_agent: str, enabled: bool = True) -> None:
        self._client = client
        self._user_agent = user_agent
        self._enabled = enabled
        self._parsers: dict[str, Protego | None] = {}

    async def _parser(self, url: str) -> Protego | None:
        origin = origin_of(url)
        if origin in self._parsers:
            return self._parsers[origin]

        parser: Protego | None = None
        try:
            response = await self._client.get(
                f"{origin}/robots.txt",
                headers={"User-Agent": self._user_agent},
                follow_redirects=True,
            )
            if response.status_code in (401, 403):
                # Deliberately restrictive: we were refused sight of the rules.
                parser = Protego.parse("User-agent: *\nDisallow: /")
            elif response.status_code == 200:
                parser = Protego.parse(response.text)
        except BlockedHost as exc:
            # The SSRF guard refused this origin. That is not a robots decision,
            # and raising here turned a preflight into a 500. The URL itself
            # will be refused at fetch time and the row will say so properly.
            log.debug("robots.txt not fetched for %s: %s", origin, exc.reason)
        except httpx.HTTPError as exc:
            # Unreachable robots.txt is not consent withheld, it is a network
            # problem. Standard behaviour is to proceed.
            log.debug("robots.txt unreachable for %s: %s", origin, exc)

        self._parsers[origin] = parser
        return parser

    async def allowed(self, url: str) -> bool:
        if not self._enabled:
            return True
        parser = await self._parser(url)
        if parser is None:
            return True
        return bool(parser.can_fetch(url, self._user_agent))

    async def crawl_delay(self, url: str) -> float | None:
        """A site's own stated delay always wins over our configured one when it
        is longer."""
        parser = await self._parser(url)
        if parser is None:
            return None
        delay = parser.crawl_delay(self._user_agent)
        return float(delay) if delay is not None else None
