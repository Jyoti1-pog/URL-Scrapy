"""How many ways to ask a site for a page, and in what order.

One attempt was not enough. A site can accept the TCP and TLS handshake and then
refuse at the HTTP layer -- a stream reset, a silent hang -- and the old fetcher
reported all of that as one word -- a bucket standing in for every transport
failure there is. Buckets hide bugs.

THE RUNGS

    A1  HTTP/2,   browser header set          the fast path, works on most sites
    A2  HTTP/1.1, same headers                some CDNs reset h2 and serve h1 fine
    A3  HTTP/1.1, fresh connection, one retry transient resets, stale pools
    B   a real browser                        when the site genuinely wants one

MEASURED BEFORE BUILT, and the measurement changed the design. Two hosts that
motivated this ladder (Myntra, Nykaa) do NOT resolve on A2: HTTP/1.1 is not
reset, it is black-holed, so a 0.7-second failure became a 21-second one. Nor
does a real headless Chromium get through -- it gets the same protocol error.
Both sit behind a bot manager that has decided about this client, and the honest
answer for them is `blocked_by_source`, not another rung.

That is why **each rung has its own short budget** rather than inheriting the
whole fetch timeout. A ladder whose rungs each wait 21 seconds turns a fast
failure into a 45-second one on exactly the sites it was meant to help. The
per-rung budget is the difference between a ladder and a punishment.

WHAT THIS IS NOT. Every rung identifies itself honestly and none of them
disguises anything: no TLS-signature mimicry, no proxy rotation, no captcha
solving, no cookie replay. A site that refuses a correctly-identified client
after every rung has given an answer, and the answer is recorded, not worked
around.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import StrEnum

import httpx

from ..config import Settings
from ..utils.logging import get_logger
from ..utils.netguard import BlockedHost, request_hook

log = get_logger(__name__)


class Rung(StrEnum):
    """Named so `diagnose` and `failed.csv` can say which one won."""

    A1 = "a1_http2"
    A2 = "a2_http11"
    A3 = "a3_http11_fresh"
    B = "b_browser"


# The A rungs, in order. Rung B is not here: it needs a browser rather than a
# client, so `pipeline` escalates to it rather than this module walking into it.
A_RUNGS: tuple[Rung, ...] = (Rung.A1, Rung.A2, Rung.A3)


class FailureKind(StrEnum):
    """What went wrong, at the level an operator can act on.

    Replaces the single catch-all. The distinction that matters most
    is between "the network did not work" and "the site said no": the first is
    worth retrying and escalating, the second is an answer.
    """

    TRANSPORT_RESET = "transport_reset"
    TLS_ERROR = "tls_error"
    DNS_ERROR = "dns_error"
    CONNECTION_REFUSED = "connection_refused"
    TIMEOUT_CONNECT = "timeout_connect"
    TIMEOUT_READ = "timeout_read"
    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    NOT_HTML = "not_html"
    BLOCKED_ADDRESS = "blocked_address"

    @property
    def is_transport(self) -> bool:
        """Worth another rung, and worth a browser.

        A transport failure means no HTTP response arrived at all. That is the
        single strongest signal that the browser is needed -- and it was
        precisely the case where the old fetcher gave up.
        """
        return self in (
            FailureKind.TRANSPORT_RESET,
            FailureKind.TLS_ERROR,
            FailureKind.TIMEOUT_CONNECT,
            FailureKind.TIMEOUT_READ,
        )

    @property
    def is_final(self) -> bool:
        """No rung will change this answer, so stop climbing.

        A name that does not resolve does not resolve over HTTP/1.1 either, and
        an address the SSRF guard refuses is a decision, not a failure. A port
        that refuses the TCP handshake refuses it for Chromium too -- climbing
        costs seventeen seconds to be told the same thing three more times.
        """
        return self in (
            FailureKind.DNS_ERROR,
            FailureKind.BLOCKED_ADDRESS,
            FailureKind.CONNECTION_REFUSED,
        )


@dataclass
class Attempt:
    """One rung, and what it got. Kept even when it failed -- especially then."""

    rung: Rung
    ok: bool
    elapsed_ms: int
    status: int | None = None
    kind: FailureKind | None = None
    detail: str = ""
    # The 4xx/5xx response itself, kept only so `Retry-After` can be read off
    # it. Not kept for successes: a body we are about to use has a home, and
    # holding a second reference to it across the climb is how a 5 MB page
    # outlives the row that fetched it.
    response: httpx.Response | None = None

    def __str__(self) -> str:
        outcome = str(self.status) if self.ok else (self.kind.value if self.kind else "failed")
        return f"{self.rung.value}:{outcome}:{self.elapsed_ms}ms"


@dataclass
class LadderOutcome:
    """The whole climb, whether or not it ended in a page."""

    attempts: list[Attempt] = field(default_factory=list)

    @property
    def rungs_tried(self) -> str:
        """For `failed.csv`, so a re-run can be reasoned about without re-running."""
        return " | ".join(str(a) for a in self.attempts)

    @property
    def winner(self) -> Rung | None:
        return next((a.rung for a in self.attempts if a.ok), None)

    @property
    def last_failure(self) -> Attempt | None:
        return next((a for a in reversed(self.attempts) if not a.ok), None)

    @property
    def last_response(self) -> httpx.Response | None:
        """The last refusal that came with headers, for `Retry-After`."""
        failure = self.last_failure
        return failure.response if failure else None

    @property
    def should_escalate_to_browser(self) -> bool:
        """§2.4. Any transport error escalates; a 4xx with no body escalates once.

        A genuine 404 does not: there is nothing to render. Neither does a DNS
        failure or an address the guard refused.
        """
        failure = self.last_failure
        if failure is None or failure.kind is None:
            return False
        if failure.kind.is_final:
            return False
        if failure.kind.is_transport:
            return True
        # A bare 403/429 is how a bot wall often presents. Worth one browser
        # attempt; anything else 4xx is an answer about the URL.
        return failure.kind is FailureKind.HTTP_4XX and failure.status in (403, 429)


# What name resolution failing looks like, per platform. Windows says one
# thing, glibc another, macOS a third, and httpcore passes the text through.
_DNS_MARKERS = (
    "getaddrinfo",  # Windows (WSAHOST_NOT_FOUND) and most wrappers
    "name or service not known",  # glibc, EAI_NONAME
    "temporary failure in name resolution",  # glibc, EAI_AGAIN
    "nodename nor servname",  # macOS
    "name resolution",
)


def classify(exc: Exception) -> tuple[FailureKind, str]:
    """An httpx exception, as something an operator can act on.

    `RemoteProtocolError` covers the h2 stream reset that started all this, and
    it is deliberately read as transport rather than as a generic HTTP error --
    that misclassification is what stopped the browser fallback from firing.
    """
    if isinstance(exc, BlockedHost):
        return FailureKind.BLOCKED_ADDRESS, str(exc)
    if isinstance(exc, httpx.ConnectTimeout):
        return FailureKind.TIMEOUT_CONNECT, str(exc)
    if isinstance(exc, httpx.ReadTimeout | httpx.PoolTimeout | httpx.WriteTimeout):
        return FailureKind.TIMEOUT_READ, str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return FailureKind.TIMEOUT_READ, str(exc)
    if isinstance(exc, httpx.TooManyRedirects):
        return FailureKind.TOO_MANY_REDIRECTS, str(exc)
    if isinstance(exc, httpx.RemoteProtocolError | httpx.LocalProtocolError):
        return FailureKind.TRANSPORT_RESET, str(exc)
    if isinstance(exc, httpx.ConnectError):
        text = str(exc).lower()
        # A ConnectError is three different things -- TLS refused, the name is
        # wrong, or the port said no -- and they send an operator to three
        # different places. DNS is matched POSITIVELY rather than used as the
        # fallback: it used to be the default, so `connection refused` printed
        # "that address does not resolve, check the link for a typo" about a
        # name that had resolved perfectly well.
        if any(marker in text for marker in ("ssl", "certificate", "tls", "handshake")):
            return FailureKind.TLS_ERROR, str(exc)
        if any(marker in text for marker in _DNS_MARKERS):
            return FailureKind.DNS_ERROR, str(exc)
        return FailureKind.CONNECTION_REFUSED, str(exc)
    if isinstance(exc, httpx.HTTPError):
        return FailureKind.TRANSPORT_RESET, str(exc)
    return FailureKind.TRANSPORT_RESET, str(exc)


def browser_headers(settings: Settings) -> dict[str, str]:
    """§2.3. A browser being honest about being a browser.

    The User-Agent still carries this tool's name and a contactable address, so
    a site operator who wants us to stop can find a human. The rest of the set
    is what any browser sends and what a lot of edges now require before they
    will serve a page at all -- omitting them is not honesty, it is just being
    unusual in a way that gets you refused.
    """
    cfg = settings.config.fetch
    headers = {
        "User-Agent": settings.user_agent,
        "Accept": cfg.accept_header,
        "Accept-Language": cfg.accept_language,
        "Accept-Encoding": "gzip, deflate, br",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    headers.update(cfg.extra_headers)
    return headers


def _client_for(
    rung: Rung, settings: Settings, allowance_s: float | None = None
) -> httpx.AsyncClient:
    """One client per rung. Separate instances on purpose: A3's whole point is a
    connection that has not been pooled.

    `allowance_s` is what the ROW has left (§5), which is never more than the
    rung's own budget and is often less. Clipping here rather than at the call
    site means the last rung of an already-slow row is the one that gets
    squeezed, instead of the row quietly overrunning its deadline.
    """
    cfg = settings.config.fetch
    budget = cfg.rung_timeout_s if allowance_s is None else min(cfg.rung_timeout_s, allowance_s)
    kwargs: dict[str, object] = {
        "http2": rung is Rung.A1,
        "follow_redirects": True,
        "max_redirects": cfg.max_redirects,
        "timeout": httpx.Timeout(budget, connect=min(cfg.connect_timeout_s, budget)),
        "event_hooks": {"request": [request_hook(cfg.allow_private_hosts)]},
        "headers": browser_headers(settings),
    }
    if rung is Rung.A3:
        # No keep-alive and an explicit close: the rung exists for a pooled
        # connection that has gone stale, so reusing the pool would defeat it.
        kwargs["limits"] = httpx.Limits(max_keepalive_connections=0)
        kwargs["headers"] = {**browser_headers(settings), "Connection": "close"}
    return httpx.AsyncClient(**kwargs)  # type: ignore[arg-type]


async def climb(
    url: str,
    settings: Settings,
    *,
    start_at: Rung | None = None,
    on_attempt: object = None,
    budget: object = None,
) -> tuple[httpx.Response | None, LadderOutcome]:
    """Walk the A rungs until one answers. Never raises for a bad page.

    `start_at` skips rungs a domain has already been shown to need -- see
    `store.fetch_profiles`. On a 200-URL catalogue from one shop that is the
    difference between 200 wasted HTTP/2 attempts and none.

    Returns (response, outcome). A response of None means every rung failed and
    `outcome.last_failure` says how; the caller decides whether to escalate.
    """
    cfg = settings.config.fetch
    outcome = LadderOutcome()

    rungs = list(A_RUNGS)
    if start_at is not None and start_at in rungs:
        rungs = rungs[rungs.index(start_at) :]

    for index, rung in enumerate(rungs):
        if index and cfg.rung_backoff_s:
            # Jittered, and only between rungs -- a retry that arrives in
            # lockstep with the last one is a second request, not a retry.
            pause = cfg.rung_backoff_s + random.uniform(0, cfg.rung_backoff_s)
            # Clipped, like everything else. An unclipped backoff is how the
            # row overran a 6-second budget by two seconds while every rung
            # inside it was being clipped correctly.
            if budget is not None and hasattr(budget, "clip"):
                pause = budget.clip(pause)
            await asyncio.sleep(pause)
            # §5. Deliberate waiting is `idle`, not `fetch`: it is the one part
            # of the row's time that we chose rather than waited on a shop for.
            if budget is not None and hasattr(budget, "waited"):
                budget.waited(pause)

        if budget is not None:
            remaining = getattr(budget, "remaining", None)
            if remaining is not None and remaining <= 0:
                # Out of time. Stopping is reported as the last failure rather
                # than as a new kind: the row ran out during a read, and calling
                # that anything other than a read timeout would be a second word
                # for one event.
                break

        started = time.perf_counter()
        try:
            allowance = getattr(budget, "remaining", None) if budget is not None else None
            async with _client_for(rung, settings, allowance) as client:
                response = await client.get(url)
        except Exception as exc:  # noqa: BLE001 -- classify() is exhaustive by design
            kind, detail = classify(exc)
            attempt = Attempt(
                rung=rung,
                ok=False,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
                kind=kind,
                detail=detail[:300],
            )
            outcome.attempts.append(attempt)
            log.debug("%s on %s: %s", rung.value, url, kind.value)
            if kind.is_final:
                # Nothing below this rung will answer differently.
                break
            continue

        elapsed = int((time.perf_counter() - started) * 1000)
        if response.status_code >= 400:
            kind = (
                FailureKind.HTTP_5XX if response.status_code >= 500 else FailureKind.HTTP_4XX
            )
            outcome.attempts.append(
                Attempt(
                    rung=rung,
                    ok=False,
                    elapsed_ms=elapsed,
                    status=response.status_code,
                    kind=kind,
                    detail=str(response.url),
                    # Kept so `Retry-After` can be read. `read()` first: the
                    # client closes at the end of this block and an unread
                    # streaming response cannot have its headers used after.
                    response=response,
                )
            )
            # A 404 is an answer about the URL and no rung improves it. A 403 or
            # 429 might be this client rather than this URL, so keep climbing.
            if response.status_code not in (403, 429, 500, 502, 503, 504):
                break
            continue

        outcome.attempts.append(
            Attempt(rung=rung, ok=True, elapsed_ms=elapsed, status=response.status_code)
        )
        return response, outcome

    return None, outcome
