"""Is this 200 actually the product page?

A captcha wall, a sign-in interstitial and a "no longer available" notice all
arrive with status 200 and parse cleanly into a record with a title, no price
and no images. The pipeline then reports "extracted successfully, no photo",
which is the most misleading thing it can say: the operator goes looking for a
photo problem when the truth is that we never saw the product at all.

This module answers that question and nothing else. It makes no network call,
changes no record, and decides no policy -- it looks at bytes we already have
and names what it sees. `diagnose` reports it; Phase 3 fails rows on it.

CONSERVATIVE ON PURPOSE. A false positive fails a good row, which is worse than
the silence we are fixing. So:

  * captcha markers are strong strings that do not occur on real product pages
  * the weaker signals -- sign-in, unavailable -- only count when the page ALSO
    shows no sign of being a product page

and there is no scoring, no threshold, and nothing tunable. A marker either
appears or it does not.

WHAT THIS IS NOT: a step towards getting past any of these. A page that blocks
us is an answer. See the module docstring of `netguard` for the same principle
applied to addresses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

from ..images.reasons import NoImageReason
from ..utils.logging import get_logger

log = get_logger(__name__)

# Strings that mean "you are talking to a bot check, not a shop". Each one is
# specific enough that a genuine product page containing it would be a page
# about captchas.
CAPTCHA_MARKERS: tuple[str, ...] = (
    "/errors/validatecaptcha",
    "enter the characters you see below",
    "type the characters you see in this image",
    "robot check",
    "cf-browser-verification",
    "cf_chl_opt",
    "checking your browser before accessing",
    "please verify you are a human",
    "captcha-delivery.com",
    "px-captcha",
    "unusual traffic from your computer network",
    "to discuss automated access to amazon data please contact",
    "why have i been blocked",
    "attention required! | cloudflare",
)

# Weaker: real shops do have login links. Only counted when nothing on the page
# looks like a product.
LOGIN_MARKERS: tuple[str, ...] = (
    "sign in to continue",
    "please log in to view",
    "you must be logged in",
    "log in to see prices",
    "members only",
    "create an account to view",
)

LOGIN_PATH_MARKERS: tuple[str, ...] = (
    "/ap/signin",
    "/login",
    "/signin",
    "/sign-in",
    "/account/login",
)

# Weaker still: "currently unavailable" appears on live listings next to a
# variant picker. Same rule -- only counted with no product signal.
UNAVAILABLE_MARKERS: tuple[str, ...] = (
    "currently unavailable",
    "no longer available",
    "this item is not available",
    "product not found",
    "page not found",
    "sorry, we couldn't find that page",
    "the page you requested does not exist",
    "this listing has been removed",
    "out of print",
)

# Signs of an actual shop page. Any one of these is enough.
_CART_MARKERS: tuple[str, ...] = (
    "add to cart",
    "add to bag",
    "add to basket",
    "buy now",
    "add to trolley",
    "proceed to checkout",
)

_PRICE = re.compile(r"(?:₹|rs\.?\s|inr\s|\$|€|£)\s?\d[\d,]*(?:\.\d{1,2})?", re.IGNORECASE)

# Body text below this is not a product page whatever it says; it is a stub.
# Generous, because a JS-only shop legitimately ships very little HTML -- that
# is Stage B's problem, not a block.
_THIN_BODY_CHARS = 400

# WHERE THE BUY BOX IS.
#
# Availability is a property of the buy box, not of the page. On a 2.3 MB
# Amazon page the words "currently unavailable" appear in recommendation
# carousels, other sellers' offers, and accessibility strings -- so a substring
# match against `body` flagged a product with a live buy box as unavailable, and
# contradicted the same report's own "something priced" finding two lines above.
#
# Matching is scoped to these containers first. Only when none of them exists
# does the check fall back to the whole body, and it says which it used.
_BUY_BOX_SELECTORS = (
    "#buybox",
    "#rightCol",
    "#desktop_buybox",
    "#addToCart_feature_div",
    "#availability",
    "[data-testid='buybox']",
    ".product-form",
    ".product__info",
    ".single_add_to_cart_button",
    "form[action*='cart']",
    "[itemprop='offers']",
)


def _buy_box_text(dom: HTMLParser) -> tuple[str, bool]:
    """The buy box's text and whether one was found.

    Returns the whole body when no container matches -- a shop we do not have a
    selector for should still get the check, just with the weaker scope, and the
    caller records which happened.
    """
    parts: list[str] = []
    for selector in _BUY_BOX_SELECTORS:
        for node in dom.css(selector):
            parts.append(node.text(separator=" ", strip=True) or "")
    if parts:
        return " ".join(parts).lower(), True
    body = dom.body
    return ((body.text(separator=" ", strip=True) if body is not None else "") or "").lower(), False


@dataclass
class PageShape:
    """What the response looks like, and the one word for it."""

    # None means "nothing wrong that this module can see". It does NOT mean
    # "definitely a product page" -- see `looks_like_product`.
    verdict: NoImageReason | None = None
    looks_like_product: bool = False
    captcha: bool = False
    login_wall: bool = False
    unavailable: bool = False
    thin: bool = False
    # True when an availability marker was found inside the buy box rather than
    # loose on the page. Only this one is a real signal about the product.
    unavailable_in_buy_box: bool = False
    buy_box_found: bool = False
    # Every marker that matched, as `where: what`, so a false positive can be
    # traced to the exact string that caused it without a debugger.
    evidence: list[str] = field(default_factory=list)
    product_signals: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        """True when the row should fail rather than be written half-extracted."""
        return self.verdict is not None


def _hits(haystack: str, markers: tuple[str, ...]) -> list[str]:
    return [marker for marker in markers if marker in haystack]


def _product_signals(html_lc: str, dom: HTMLParser, has_product_node: bool) -> list[str]:
    found: list[str] = []
    if has_product_node:
        found.append("a Product node in structured data")
    if 'property="og:type"' in html_lc and "product" in html_lc:
        for node in dom.css("meta[property='og:type']"):
            if "product" in (node.attributes.get("content") or "").lower():
                found.append("og:type=product")
                break
    if cart := _hits(html_lc, _CART_MARKERS):
        found.append(f"a purchase control ({cart[0]!r})")
    if _PRICE.search(html_lc):
        found.append("something priced")
    return found


def inspect(
    html: str,
    final_url: str,
    dom: HTMLParser | None = None,
    has_product_node: bool = False,
) -> PageShape:
    """Classify one fetched page. Pure; never raises.

    `has_product_node` comes from the structured-data pass the caller has
    already done. Passed in rather than re-parsed, because extruct is the
    single most expensive thing in the extraction and running it twice to
    answer a yes/no question would be silly.
    """
    dom = dom or HTMLParser(html)
    body = dom.body
    text = (body.text(separator=" ", strip=True) if body is not None else "") or ""
    html_lc = html.lower()
    url_lc = final_url.lower()

    shape = PageShape()
    shape.thin = len(text) < _THIN_BODY_CHARS
    shape.product_signals = _product_signals(html_lc, dom, has_product_node)
    shape.looks_like_product = bool(shape.product_signals)

    for marker in _hits(html_lc, CAPTCHA_MARKERS):
        shape.captcha = True
        shape.evidence.append(f"body: {marker!r}")

    login_body = _hits(html_lc, LOGIN_MARKERS)
    login_url = _hits(url_lc, LOGIN_PATH_MARKERS)
    if login_body or login_url:
        shape.login_wall = True
        shape.evidence += [f"body: {m!r}" for m in login_body]
        # A redirect INTO a sign-in path is the strong form of this signal: we
        # asked for a product and were sent somewhere else entirely.
        shape.evidence += [f"final url: {m!r}" for m in login_url]

    # Availability, scoped. Where the marker sits decides what it means, so the
    # evidence says `buy-box:` or `elsewhere on page:` and the next false
    # positive is obvious at a glance.
    buy_box, shape.buy_box_found = _buy_box_text(dom)
    in_box = _hits(buy_box, UNAVAILABLE_MARKERS)
    elsewhere = [m for m in _hits(html_lc, UNAVAILABLE_MARKERS) if m not in in_box]
    if in_box:
        shape.unavailable = True
        shape.unavailable_in_buy_box = True
        shape.evidence += [f"buy-box: {m!r}" for m in in_box]
    elif elsewhere:
        shape.unavailable = True
        shape.evidence += [f"elsewhere on page: {m!r}" for m in elsewhere]

    # --- the verdict, most certain first -----------------------------------
    #
    # Captcha needs no corroboration: those strings are not ambiguous. The other
    # two do, because every shop has a login link and plenty of live listings
    # say "currently unavailable" about one size.
    if shape.captcha:
        shape.verdict = NoImageReason.BOT_CHALLENGE
    elif shape.login_wall and not shape.looks_like_product:
        shape.verdict = NoImageReason.SIGN_IN_REQUIRED
    elif shape.unavailable_in_buy_box and not shape.looks_like_product:
        # Only a marker INSIDE the buy box can end a row, and only when nothing
        # else on the page says product. A marker elsewhere is ignored
        # entirely -- §3 is explicit that it is conditional, not additive.
        shape.verdict = NoImageReason.NOT_A_PRODUCT_PAGE

    _assert_consistent(shape)
    return shape


def _assert_consistent(shape: PageShape) -> None:
    """Two findings that disagree are worth noticing, not averaging.

    "something priced" plus "currently unavailable" inside one report is the
    contradiction that exposed the unscoped match. It is logged rather than
    raised: the verdict logic above already resolves it correctly, and a warning
    is what makes the NEXT such pair visible before it becomes a bug.
    """
    if shape.verdict is NoImageReason.NOT_A_PRODUCT_PAGE and shape.product_signals:
        log.warning(
            "page-shape contradiction: called unavailable while also showing %s",
            ", ".join(shape.product_signals),
        )
    if shape.captcha and shape.product_signals:
        log.debug(
            "page-shape: bot-check markers on a page that also looks like a product (%s); "
            "the captcha markers win, as they are unambiguous",
            ", ".join(shape.product_signals),
        )
