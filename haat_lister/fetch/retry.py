"""§5 -- when it is worth asking again, and how long to wait first.

A retry budget is not evasion. That distinction is the whole of this module and
it comes down to one header: a tool that retries on its own schedule is pressing;
a tool that reads `Retry-After` and waits exactly that long is doing what it was
asked. So `Retry-After` is not a hint here, it is the instruction, and the only
thing we do with it is refuse to wait longer than the row has left.

WHAT IS RETRYABLE, and it is a short list on purpose:

    timeout_connect   the network did not work. It might next time.
    timeout_read      likewise.
    http_error_5xx    the shop broke. Their bug, probably transient.
    blocked_429       they asked us to slow down, and said when to return.

WHAT IS NOT, and each of these is a decision rather than a fault:

    robots_disallowed   retrying produces `robots_disallowed`, forever.
    bot_challenge       likewise, and trying harder is the thing we do not do.
    sign_in_required    we are not going to sign in.
    dns_failure         a name that does not resolve does not resolve on the
                        third attempt.
    not_a_product_page  a 404 is an answer about the URL.

A button that cannot change its own outcome should not exist, and a retry loop
that cannot change its own outcome is the same button pressed automatically.
"""

from __future__ import annotations

from email.utils import parsedate_to_datetime

import httpx

from ..images.reasons import NoImageReason, parse
from ..utils.logging import get_logger
from .budget import UrlBudget

log = get_logger(__name__)

# The closed vocabulary decides this, not a list kept here. §2 exists so that
# one table answers "what happened" everywhere, and a second table of "and is
# it worth retrying" would be the same drift in a new place.
RETRYABLE: frozenset[NoImageReason] = frozenset(r for r in NoImageReason if r.retryable)

# How long we will honour a `Retry-After` for before deciding the site has
# effectively said no. Sites answer 429 with anything from 1 second to an hour;
# an hour is not a wait, it is a refusal with a timestamp on it.
MAX_RETRY_AFTER_S = 120.0


def is_retryable(reason: str | NoImageReason | None) -> bool:
    """Never raises, and an unknown reason is NOT retryable.

    Deliberately the strict direction. Retrying something we do not understand
    doubles the requests to a site that just did something we do not understand.
    """
    parsed = parse(reason)
    return parsed is not None and parsed in RETRYABLE


def retry_after_seconds(response: httpx.Response | None) -> float | None:
    """The site's own instruction, in seconds. None when it did not give one.

    Both forms are real: `Retry-After: 30` and an HTTP date. The date form is
    parsed against the response's own `Date` header rather than our clock,
    because a client whose clock is four minutes fast would otherwise decide the
    wait had already elapsed and hammer straight back.
    """
    if response is None:
        return None
    raw = response.headers.get("retry-after", "").strip()
    if not raw:
        return None

    if raw.isdigit():
        return float(raw)

    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None

    now = None
    if date_header := response.headers.get("date"):
        try:
            now = parsedate_to_datetime(date_header)
        except (TypeError, ValueError):
            now = None
    if now is None:
        from datetime import UTC, datetime

        now = datetime.now(UTC)

    return max(0.0, (when - now).total_seconds())


def wait_for(
    reason: str | NoImageReason | None,
    attempt: int,
    budget: UrlBudget,
    response: httpx.Response | None = None,
    base_s: float = 1.0,
) -> float | None:
    """How long to wait before asking again, or None to stop.

    Returns None -- meaning "do not retry" -- when the reason is not retryable,
    when the row has no budget left, or when the site's own `Retry-After` is
    longer than we are prepared to sit still for. That last case is reported as
    the site's answer rather than as our impatience, which is what it is.
    """
    if not is_retryable(reason):
        return None

    stated = retry_after_seconds(response)
    if stated is not None:
        if stated > MAX_RETRY_AFTER_S:
            log.info(
                "Asked to wait %.0fs before retrying; that is a refusal with a timestamp on it, "
                "so the row ends here rather than holding the job open.",
                stated,
            )
            return None
        wait = stated
    else:
        # Exponential, and only when the site did not say. Doubling is not
        # politeness theatre: a shop that just 503'd is a shop under load, and
        # coming back at the same interval is the behaviour that keeps it there.
        wait = base_s * (2 ** max(0, attempt - 1))

    if wait >= budget.remaining:
        # Waiting would consume the row's whole remaining time and leave nothing
        # for the request itself, which is a slower way of failing.
        return None
    return wait
