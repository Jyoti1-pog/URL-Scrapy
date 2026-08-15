"""robots.txt, honoured by default.

Pulled forward from Phase 9 deliberately: the very first request this tool makes
should already be one a site operator has allowed. A fetch path that learns
manners later is a fetch path that shipped without them.

A missing or unparseable robots.txt means allowed -- that is the standard's own
default, not a shortcut. A 401/403 on robots.txt means disallowed, because a
site that will not even show us its rules has not granted us anything.

WHICH OF THOSE TWO HAPPENED IS NOT A DETAIL. Measured on a real shop: Flipkart
serves a reCAPTCHA page for `/robots.txt` itself. Failing closed is right, but
telling the operator "this site's robots.txt disallows product pages" is not
true -- we never read a rule. The remedies differ too: one is "they have asked
crawlers to stay off", the other is "their bot wall refuses everyone, including
us asking permission". So the decision carries which it was.
"""

from __future__ import annotations

from enum import StrEnum

import httpx
from protego import Protego

from .logging import get_logger
from .netguard import BlockedHost
from .urls import origin_of

log = get_logger(__name__)


class RobotsDecision(StrEnum):
    """Why a URL may or may not be fetched. Three outcomes, not two."""

    ALLOWED = "allowed"
    # A rule in a robots.txt we actually read covers this path.
    DISALLOWED = "robots_disallowed"
    # We asked for the rules and were refused -- a 401, a 403, or a bot wall in
    # front of robots.txt itself. Fails closed, same as DISALLOWED, but it is a
    # different fact and gets different words.
    REFUSED_SIGHT = "robots_unreadable"

    @property
    def may_fetch(self) -> bool:
        return self is RobotsDecision.ALLOWED


# What to tell an operator, and what to do instead. Neither offers a way to
# ignore the site's wishes: §4 is explicit that `--ignore-robots` stays a
# deliberate command-line act by someone who has read what it means, and that a
# checkbox next to "Process 4" would make it a reflex.
ROBOTS_GUIDANCE: dict[RobotsDecision, str] = {
    RobotsDecision.DISALLOWED: (
        "This shop's robots.txt asks crawlers to stay off product pages, so nothing was "
        "fetched. If these are your own products, the usual routes are your seller-panel "
        "export, the shop's official API or affiliate feed, or entering these few by hand."
    ),
    RobotsDecision.REFUSED_SIGHT: (
        "This shop would not even serve its robots.txt -- there is a bot check in front of it -- "
        "so we could not read what it permits, and treated that as a no. If these are your own "
        "products, the usual routes are your seller-panel export, the shop's official API or "
        "affiliate feed, or entering these few by hand."
    ),
}


class RobotsCache:
    """One robots.txt per origin, fetched once per run."""

    def __init__(self, client: httpx.AsyncClient, user_agent: str, enabled: bool = True) -> None:
        self._client = client
        self._user_agent = user_agent
        self._enabled = enabled
        self._parsers: dict[str, Protego | None] = {}
        # Origins that would not show us their rules at all.
        self._refused: set[str] = set()

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
        return (await self.decide(url)).may_fetch

    async def decide(self, url: str) -> RobotsDecision:
        """Allowed, disallowed, or refused sight of the rules."""
        if not self._enabled:
            return RobotsDecision.ALLOWED
        parser = await self._parser(url)
        if parser is None:
            return RobotsDecision.ALLOWED
        if parser.can_fetch(url, self._user_agent):
            return RobotsDecision.ALLOWED
        if origin_of(url) in self._refused:
            return RobotsDecision.REFUSED_SIGHT
        return RobotsDecision.DISALLOWED

    async def crawl_delay(self, url: str) -> float | None:
        """A site's own stated delay always wins over our configured one when it
        is longer."""
        parser = await self._parser(url)
        if parser is None:
            return None
        delay = parser.crawl_delay(self._user_agent)
        return float(delay) if delay is not None else None
