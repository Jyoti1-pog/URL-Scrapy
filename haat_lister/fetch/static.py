"""Stage A: the HTTP fetch, now a ladder rather than a single attempt.

The cheap path. Roughly 200ms and no browser, versus seconds and a Chromium
process for Stage B. On a 5,000-URL batch that difference is hours, so Stage B
is only ever reached when Stage A left something genuinely missing.

`fetch_static` keeps its shape -- one URL in, one FetchResult out, FetchError
with a named reason otherwise -- but underneath it now walks `fetch/ladder.py`.
The error it raises carries the whole climb, so no single word ever has to stand
in for "something went wrong at the transport layer".
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx

from ..config import Settings
from ..models import FetchStage
from ..utils.logging import get_logger
from ..utils.netguard import request_hook
from ..utils.urls import host_of
from .budget import Spend, UrlBudget, budget_for
from .ladder import (
    FailureKind,
    LadderOutcome,
    Rung,
    browser_headers,
    classify,
    climb,
)
from .profiles import get_profile, is_refusing, record_refusal, remember_profile
from .retry import retry_after_seconds, wait_for

log = get_logger(__name__)


def _retry_word(kind: FailureKind, status: int | None) -> str:
    """The transport failure, in the shared vocabulary the retry policy reads.

    Translated here rather than in `retry.py` so that §2's table stays the one
    place that knows what a word means. A 503 is `http_error_5xx` to everybody
    who has to decide anything about it.
    """
    if status == 429:
        return "blocked_429"
    if status and 500 <= status < 600:
        return "http_error_5xx"
    if status and 400 <= status < 500:
        return "not_a_product_page"
    return {
        FailureKind.TIMEOUT_CONNECT: "timeout_connect",
        FailureKind.TIMEOUT_READ: "timeout_read",
        FailureKind.DNS_ERROR: "dns_failure",
        FailureKind.CONNECTION_REFUSED: "timeout_connect",
    }.get(kind, "")


# `TRANSPORT_RESET` is deliberately absent from that table, and it is the one
# omission worth explaining. A reset IS retryable in principle -- but the
# ladder is already its retry: three rungs, two of them specifically for the
# reset case, one of them on a fresh connection. Climbing the whole ladder a
# second time turns a 1.5-second failure into a 3-second one and doubles the
# requests to a host that has just reset us three times, which is exactly the
# grinding the per-rung budget was introduced to stop.
#
# The row-level retry exists for what the ladder CANNOT address: a site that
# said "come back in 30 seconds" and meant it.


class FetchError(Exception):
    """A fetch that failed in a way the row must record. Never swallowed.

    `outcome` carries every rung that was tried and what each one got, so
    `failed.csv` can say "h2 reset in 0.7s, h1.1 timed out at 8s, browser
    refused" instead of one word. That is the difference between a failure an
    operator can reason about and one they have to reproduce.
    """

    def __init__(
        self,
        reason: str,
        detail: str = "",
        outcome: LadderOutcome | None = None,
        budget: UrlBudget | None = None,
    ) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail
        self.outcome = outcome
        # §5. What the row spent before giving up. Carried on the exception
        # because the failure is exactly when somebody wants the number, and a
        # separate channel for it would be one the failing path forgets to use.
        self.budget = budget

    @property
    def spent(self) -> str:
        return self.budget.report() if self.budget else ""

    @property
    def rungs_tried(self) -> str:
        return self.outcome.rungs_tried if self.outcome else ""

    @property
    def should_escalate_to_browser(self) -> bool:
        return bool(self.outcome and self.outcome.should_escalate_to_browser)


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
    # Which rung answered, and the whole climb. Shown by `diagnose` so an
    # operator can see that a site needed HTTP/1.1 rather than guessing.
    rung: Rung | None = None
    rungs_tried: str = ""
    # The climb itself, not just its packed string. `diagnose` prints one line
    # per attempt, and re-parsing `rungs_tried` to do that would be a second
    # implementation of something the ladder already holds.
    outcome: LadderOutcome | None = None

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
        # The same honest browser set the ladder uses, so an image request and a
        # page request do not present as two different clients.
        "headers": browser_headers(settings),
    }
    kwargs.update(overrides)
    return httpx.AsyncClient(**kwargs)  # type: ignore[arg-type]


async def fetch_static(
    client: httpx.AsyncClient,
    url: str,
    settings: Settings,
    budget: UrlBudget | None = None,
) -> FetchResult:
    """Fetch one page. Raises FetchError with a named reason; never returns junk.

    `client` is accepted and deliberately not used for the climb: each rung
    needs its own protocol and connection settings, and A3's entire purpose is a
    connection that has not been pooled. The parameter stays because every
    caller already holds a client and because `build_client` remains the one
    place that decides how we present ourselves -- the ladder uses the same
    header set.

    Note what is still absent: no alternate User-Agent, no cookie games, no
    fingerprint work. The ladder tries other PROTOCOLS, not other identities. If
    a site refuses a correctly-identified client on every rung, that is an
    answer and the row fails loudly with the whole climb recorded.
    """
    cfg = settings.config.fetch
    budget = budget or budget_for(settings)

    if not cfg.ladder_enabled:
        return await _single_attempt(client, url, settings)

    if is_refusing(settings, url):
        # This host has refused the whole ladder repeatedly. Climbing again buys
        # the operator the same answer three rungs later.
        raise FetchError(
            FailureKind.TRANSPORT_RESET.value,
            f"{host_of(url)} refused every fetch rung on the previous attempts, so this URL was "
            f"not re-tried. Clear that with: haat-lister profiles --clear {host_of(url)}",
        )

    profile = get_profile(settings, url)

    # §5. The ladder, then -- only for a retryable reason and only inside the
    # row's remaining budget -- again. The two loops are separate on purpose:
    # a rung is a different WAY of asking, a retry is the same way at a later
    # time, and the site's `Retry-After` only has an opinion about the second.
    attempt = 0
    while True:
        budget.check()
        with budget.spending(Spend.FETCH):
            response, outcome = await climb(url, settings, start_at=profile, budget=budget)
        if response is not None:
            break

        failure = outcome.last_failure
        kind = failure.kind if failure and failure.kind else FailureKind.TRANSPORT_RESET
        reason = kind.value
        if failure and failure.status:
            # `http_404` stays readable, and `failed.csv` can still split the
            # status back out of it.
            reason = f"http_{failure.status}"

        attempt += 1
        pause = (
            wait_for(
                _retry_word(kind, failure.status if failure else None),
                attempt,
                budget,
                outcome.last_response,
                cfg.retry_base_s,
            )
            if attempt <= cfg.max_url_retries
            else None
        )
        if pause is None:
            record_refusal(settings, url)
            raise FetchError(reason, failure.detail if failure else "", outcome, budget)

        log.info("Retrying %s in %.1fs (%s, attempt %d)", url, pause, reason, attempt)
        await asyncio.sleep(pause)
        budget.waited(pause, requested=retry_after_seconds(outcome.last_response) is not None)

    content_type = response.headers.get("content-type", "").lower()
    if content_type and "html" not in content_type and "xml" not in content_type:
        raise FetchError(FailureKind.NOT_HTML.value, content_type, outcome)

    if len(response.content) > cfg.max_html_bytes:
        log.warning("Truncating oversized page (%d bytes) from %s", len(response.content), url)

    # Remember which rung answered, so the next URL on this host starts there.
    if (winner := outcome.winner) is not None:
        remember_profile(settings, url, winner)

    return FetchResult(
        url=url,
        final_url=str(response.url),
        status_code=response.status_code,
        html=response.text[: cfg.max_html_bytes],
        stage=FetchStage.STATIC,
        elapsed_ms=sum(a.elapsed_ms for a in outcome.attempts),
        headers={k.lower(): v for k, v in response.headers.items()},
        cookies=dict(response.cookies),
        rung=outcome.winner,
        rungs_tried=outcome.rungs_tried,
        outcome=outcome,
    )


async def _single_attempt(
    client: httpx.AsyncClient, url: str, settings: Settings
) -> FetchResult:
    """The pre-ladder path, kept for `fetch.ladder_enabled: false`.

    Worth keeping rather than deleting: an operator debugging a site that the
    ladder makes worse needs a way to see one clean attempt, and a config flag
    is a cheaper answer than a code change.
    """
    cfg = settings.config.fetch
    started = time.perf_counter()

    try:
        response = await client.get(url)
    except Exception as exc:  # noqa: BLE001 -- classify() covers the family
        kind, detail = classify(exc)
        raise FetchError(kind.value, detail) from exc

    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if response.status_code >= 400:
        raise FetchError(f"http_{response.status_code}", str(response.url))

    content_type = response.headers.get("content-type", "").lower()
    if content_type and "html" not in content_type and "xml" not in content_type:
        raise FetchError(FailureKind.NOT_HTML.value, content_type)

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
