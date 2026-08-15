"""Why a URL did not become a listing — one name per outcome, and what to do next.

THREE THINGS THIS MODULE IS.

1. **The closed vocabulary.** Nothing anywhere -- CLI, API, CSV, or UI -- may
   emit a reason that is not a member of `Reason`. Two names for one event means
   an operator cannot search their logs, cannot compare two screens, and cannot
   tell whether they are looking at one problem or two. That happened: the job
   page said one word while `diagnose` said `timeout_read` about the same
   request. That word is deleted rather than aliased, because it
   was a CATEGORY masquerading as a reason and the disagreement was the symptom.

2. **The refused / failed split.** These are not degrees of the same thing:

       refused   the site declined access, and the tool behaved correctly
       failed    we should have got content and did not

   The distinction is load-bearing rather than cosmetic. A refused row must
   never be offered for retry -- retrying `robots_disallowed` produces
   `robots_disallowed`, forever, and a button that cannot change its own outcome
   should not exist. It must never appear in `review.csv` either: there is
   nothing on that row for a human to decide.

3. **What to do next.** Every reason carries one sentence naming the operator's
   next action -- not a description of the failure, which they can already see.
   A diagnosis that ends without a route is a diagnosis that wastes the person
   reading it.

WHAT IS NOT HERE, deliberately: any suggestion that a refusal should be worked
around. Three of these reasons exist because a site declined automated access on
purpose. The answer to those is the operator's own seller export, not a way in.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Klass(StrEnum):
    """Which side of the line an outcome falls on.

    `REFUSED` and `FAILED` are counted separately, filed separately, and
    retried differently. Collapsing them into one "failed" number is what made
    a robots refusal look like a bug in the fetcher.
    """

    REFUSED = "refused"
    FAILED = "failed"

    @property
    def retryable(self) -> bool:
        """Only failures can be fixed by trying again. See `Reason.retryable`
        for the finer per-reason rule."""
        return self is Klass.FAILED


@dataclass(frozen=True)
class Reason:
    """One outcome: its wire name, its class, and the operator's next move."""

    label: str
    klass: Klass
    what_to_do: str
    # Whether trying the identical request again could plausibly change the
    # answer. False for every refusal, and for failures whose cause is stable --
    # a name that does not resolve will not resolve on the third attempt either.
    retryable: bool = False
    # Which ingestion route to offer alongside the sentence. The UI turns this
    # into a button; `""` means there is nothing better to suggest.
    route: str = ""

    def __str__(self) -> str:
        return self.label

    @property
    def refused(self) -> bool:
        return self.klass is Klass.REFUSED


# The routes an operator can actually take when a fetch will not work. Named
# here so a reason and the screen it points at cannot drift apart.
ROUTE_SELLER_EXPORT = "seller_export"
ROUTE_SAVED_PAGE = "saved_page"


class NoImageReason(StrEnum):
    """The wire vocabulary. Members are the strings that reach CSVs and JSON.

    A StrEnum rather than a plain class so that `str(reason)`, JSON
    serialisation and CSV writing all produce the label without anyone
    remembering to ask for it.
    """

    # -- refused: the site declined, and stopping was correct ---------------
    ROBOTS_DISALLOWED = "robots_disallowed"
    BLOCKED_403 = "blocked_403"
    BLOCKED_429 = "blocked_429"
    BOT_CHALLENGE = "bot_challenge"
    SIGN_IN_REQUIRED = "sign_in_required"

    # -- failed: we should have got content and did not ---------------------
    TIMEOUT_CONNECT = "timeout_connect"
    TIMEOUT_READ = "timeout_read"
    DNS_FAILURE = "dns_failure"
    HTTP_ERROR_5XX = "http_error_5xx"
    NOT_A_PRODUCT_PAGE = "not_a_product_page"
    NO_EXTRACTABLE_CONTENT = "no_extractable_content"
    NO_IMAGE_CANDIDATES = "no_image_candidates"
    ALL_CANDIDATES_REJECTED = "all_candidates_rejected"

    # -- THREE MEMBERS BEYOND §2.1's LIST ----------------------------------
    #
    # The spec's vocabulary covers everything that can go wrong getting a page,
    # and stops there -- but the pipeline continues past the page, and three of
    # its outcomes have no member in that list. Forcing them into the nearest
    # neighbour would recreate the exact defect v5 exists to fix: a bucket that
    # tells the operator nothing they can act on.
    #
    # "Every configured image host refused this photo" is not "every candidate
    # failed a predicate"; the first is a Cloudinary problem and the second is a
    # photo problem, and they send an operator to different places. So they get
    # their own names, and the same `what_to_do` discipline as the rest.
    HOST_UPLOAD_FAILED = "host_upload_failed"
    HOSTING_BLOCKED = "hosting_blocked_third_party"
    BROWSER_UNAVAILABLE = "browser_unavailable"

    @property
    def spec(self) -> Reason:
        return REASONS[self]

    @property
    def klass(self) -> Klass:
        return REASONS[self].klass

    @property
    def refused(self) -> bool:
        return REASONS[self].klass is Klass.REFUSED

    @property
    def retryable(self) -> bool:
        return REASONS[self].retryable

    @property
    def what_to_do(self) -> str:
        return REASONS[self].what_to_do

    @property
    def route(self) -> str:
        return REASONS[self].route


_EXPORT = (
    "Use your seller-panel export instead — it has cleaner data than any fetch, "
    "and it is unambiguously yours."
)
_SAVED = (
    "Open the page in your own browser, save it (Ctrl+S, \"Webpage, complete\") "
    "and import the file — the extractor treats it exactly like a fetched page."
)


REASONS: dict[NoImageReason, Reason] = {
    # -- refused ------------------------------------------------------------
    NoImageReason.ROBOTS_DISALLOWED: Reason(
        label="robots_disallowed",
        klass=Klass.REFUSED,
        what_to_do=(
            "This shop's robots.txt asks crawlers to stay off product pages, so nothing was "
            f"fetched. {_EXPORT}"
        ),
        route=ROUTE_SELLER_EXPORT,
    ),
    NoImageReason.BLOCKED_403: Reason(
        label="blocked_403",
        klass=Klass.REFUSED,
        what_to_do=f"The shop refused this request outright. {_EXPORT}",
        route=ROUTE_SELLER_EXPORT,
    ),
    NoImageReason.BLOCKED_429: Reason(
        label="blocked_429",
        klass=Klass.REFUSED,
        # The one refusal worth retrying: it is a rate limit, not a decision
        # about us, and the site tells us when to come back.
        retryable=True,
        what_to_do=(
            "The shop asked us to slow down and kept asking. Try again later with a lower "
            "--concurrency, or use your seller-panel export."
        ),
        route=ROUTE_SELLER_EXPORT,
    ),
    NoImageReason.BOT_CHALLENGE: Reason(
        label="bot_challenge",
        klass=Klass.REFUSED,
        what_to_do=(
            "The shop served a bot check instead of the product page. This tool will not try "
            f"to defeat one. {_EXPORT}"
        ),
        route=ROUTE_SELLER_EXPORT,
    ),
    NoImageReason.SIGN_IN_REQUIRED: Reason(
        label="sign_in_required",
        klass=Klass.REFUSED,
        what_to_do=(
            "The page asked for a sign-in before showing the product. Export it from inside "
            f"your own account. {_SAVED}"
        ),
        route=ROUTE_SAVED_PAGE,
    ),
    # -- failed -------------------------------------------------------------
    NoImageReason.TIMEOUT_CONNECT: Reason(
        label="timeout_connect",
        klass=Klass.FAILED,
        retryable=True,
        what_to_do=(
            "The connection was never established. If the shop is up in your browser, this is "
            f"usually the network between you and it. {_SAVED}"
        ),
        route=ROUTE_SAVED_PAGE,
    ),
    NoImageReason.TIMEOUT_READ: Reason(
        label="timeout_read",
        klass=Klass.FAILED,
        retryable=True,
        what_to_do=(
            "The page never arrived over HTTP/2, HTTP/1.1, a fresh connection, or a real "
            f"browser. {_SAVED} Or use your seller-panel export."
        ),
        route=ROUTE_SAVED_PAGE,
    ),
    NoImageReason.DNS_FAILURE: Reason(
        label="dns_failure",
        klass=Klass.FAILED,
        what_to_do="That address does not resolve. Check the link for a typo.",
    ),
    NoImageReason.HTTP_ERROR_5XX: Reason(
        label="http_error_5xx",
        klass=Klass.FAILED,
        retryable=True,
        what_to_do="The shop's own server errored. Worth trying again in a few minutes.",
    ),
    NoImageReason.NOT_A_PRODUCT_PAGE: Reason(
        label="not_a_product_page",
        klass=Klass.FAILED,
        what_to_do=(
            "The page loaded but nothing on it says product — it may be a category page, a "
            "redirect, or a listing that has been taken down. Check the link."
        ),
    ),
    NoImageReason.NO_EXTRACTABLE_CONTENT: Reason(
        label="no_extractable_content",
        klass=Klass.FAILED,
        what_to_do=(
            "The product page loaded but neither a title nor a description could be read from "
            f"it. {_EXPORT}"
        ),
        route=ROUTE_SELLER_EXPORT,
    ),
    NoImageReason.NO_IMAGE_CANDIDATES: Reason(
        label="no_image_candidates",
        klass=Klass.FAILED,
        what_to_do=(
            "The page carries no image the extractor recognises — the photos are probably "
            f"assembled by JavaScript. {_SAVED}"
        ),
        route=ROUTE_SAVED_PAGE,
    ),
    NoImageReason.ALL_CANDIDATES_REJECTED: Reason(
        label="all_candidates_rejected",
        klass=Klass.FAILED,
        what_to_do=(
            "Photos were found, but every one failed a listability check — too small, "
            "unreachable, or blocked to anyone but the shop's own page. Add a photo by hand, "
            "or use your seller-panel export."
        ),
        route=ROUTE_SELLER_EXPORT,
    ),
    # -- past the page, see the note on the enum ----------------------------
    NoImageReason.HOST_UPLOAD_FAILED: Reason(
        label="host_upload_failed",
        klass=Klass.FAILED,
        retryable=True,
        what_to_do=(
            "The photo is fine; every configured image host refused to take it. Check the "
            "host credentials with `haat-lister config-check`, or switch to manifest mode and "
            "upload the files to haat yourself."
        ),
    ),
    NoImageReason.HOSTING_BLOCKED: Reason(
        label="hosting_blocked_third_party",
        # Refused, not failed: nothing went wrong, and no retry changes it. The
        # tool declined on the operator's behalf.
        klass=Klass.REFUSED,
        what_to_do=(
            "Provenance is third-party, so the photo was not re-uploaded anywhere — doing so "
            "would copy someone else's work on your behalf. If the photo is yours, re-run with "
            "--provenance own."
        ),
    ),
    NoImageReason.BROWSER_UNAVAILABLE: Reason(
        label="browser_unavailable",
        klass=Klass.FAILED,
        # Not retryable, and the distinction is the point of the flag: asking
        # again changes nothing until somebody installs Chromium. `retryable`
        # means "a later attempt could differ on its own", not "this is fixable"
        # -- everything is fixable, and a retry button that needs a prerequisite
        # is a button that lies about what pressing it does.
        retryable=False,
        what_to_do=(
            "This page needs a real browser and Playwright is not installed. Run: "
            "playwright install chromium"
        ),
    ),
}

# Every member has a spec. Asserted at import rather than in a test, so a new
# member without a `what_to_do` cannot survive even one run.
assert set(REASONS) == set(NoImageReason), (
    f"reasons without a spec: {set(NoImageReason) - set(REASONS)}"
)

REFUSED: frozenset[NoImageReason] = frozenset(
    r for r in NoImageReason if REASONS[r].klass is Klass.REFUSED
)
FAILED: frozenset[NoImageReason] = frozenset(
    r for r in NoImageReason if REASONS[r].klass is Klass.FAILED
)
RETRYABLE: frozenset[NoImageReason] = frozenset(r for r in NoImageReason if REASONS[r].retryable)


def parse(value: str | NoImageReason | None) -> NoImageReason | None:
    """A stored string back into a member, or None if it is not one of ours.

    Never raises and never invents. An unknown string reaching here means some
    path is emitting a literal instead of a member, which a test looks for --
    swallowing it into a default would hide exactly that.
    """
    if value is None:
        return None
    if isinstance(value, NoImageReason):
        return value
    try:
        return NoImageReason(value)
    except ValueError:
        return None


def explain(value: str | NoImageReason | None) -> str:
    """The next action for a reason, or the raw string when it is not ours.

    Returned verbatim rather than replaced, so an unknown reason shows up in the
    report looking wrong instead of looking handled.
    """
    reason = parse(value)
    return reason.what_to_do if reason is not None else str(value or "")


def klass_of(value: str | NoImageReason | None) -> Klass:
    """Which column a row belongs in. Unknown reasons count as failures --
    calling something "refused" is a claim about the site, and we only make it
    when we actually saw a refusal."""
    reason = parse(value)
    return reason.klass if reason is not None else Klass.FAILED
