"""Phase 3: a blocked page must never present as "extracted fine, no photo".

That conflation is the defect. A captcha wall arrives with status 200, parses
into a record with a title and no images, and the row is written out looking
like an ordinary product with a missing photo -- so the operator goes hunting
for an image problem that does not exist.

Two halves are tested here:

  1. the shape check itself, including the false positives it must NOT produce
  2. the pipeline acting on it, and never reaching for a browser to get past it

Plus §4.6's never-silent rule, asserted as a property over every path that can
end a row without a photo rather than one test per path.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from haat_lister.config import Settings
from haat_lister.fetch.shape import inspect
from haat_lister.fetch.static import build_client
from haat_lister.images.pipeline import _candidate_reason, _no_image
from haat_lister.images.reasons import REASONS, NoImageReason
from haat_lister.models import ImageMethod, ImageResult, Provenance, RowStatus
from haat_lister.pipeline import process_url

# What Amazon actually serves instead of a product page.
ROBOT_CHECK = """<html><head><title>Amazon.com</title></head><body>
<form action="/errors/validateCaptcha">
<h4>Enter the characters you see below</h4>
<p>Sorry, we just need to make sure you're not a robot.</p>
<img src="https://images-na.ssl-images-amazon.com/captcha/abc/Captcha_xyz.jpg">
</form>
<p>To discuss automated access to Amazon data please contact api-services@amazon.com</p>
</body></html>"""

CLOUDFLARE = """<html><head><title>Attention Required! | Cloudflare</title></head>
<body><div class="cf-browser-verification">Checking your browser before accessing
handloom.example. This process is automatic.</div></body></html>"""

SIGN_IN = """<html><head><title>Sign in</title></head><body>
<h1>Sign in to continue</h1><form><input type="password"></form>
</body></html>"""

GONE = """<html><head><title>Not found</title></head><body>
<h1>Sorry, we couldn't find that page</h1>
<p>This listing has been removed.</p></body></html>"""

# A real product page that happens to say some of the same words.
REAL_PRODUCT = """<html><head><title>Indigo Cotton Stole</title>
<meta property="og:type" content="product">
<meta property="og:image" content="https://shop.example/img/stole.jpg"></head>
<body><h1>Indigo Cotton Stole</h1>
<p>₹2,400</p>
<button>Add to cart</button>
<p>Size M is currently unavailable.</p>
<p>Members only: sign in to continue to wholesale pricing.</p>
<a href="/account/login">Sign in</a>
</body></html>"""


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("html", "url", "expected"),
    [
        (ROBOT_CHECK, "https://www.amazon.in/dp/B0FT", NoImageReason.BOT_CHALLENGE),
        (CLOUDFLARE, "https://handloom.example/p/1", NoImageReason.BOT_CHALLENGE),
        (SIGN_IN, "https://shop.example/ap/signin", NoImageReason.SIGN_IN_REQUIRED),
        (GONE, "https://shop.example/p/9", NoImageReason.NOT_A_PRODUCT_PAGE),
    ],
)
def test_a_page_that_is_not_the_product_is_named(html: str, url: str, expected: str) -> None:
    shape = inspect(html, url)
    assert shape.verdict == expected
    assert shape.evidence, "a verdict with no evidence cannot be argued with"


def test_a_real_product_page_survives_its_own_vocabulary() -> None:
    """The false positive that matters.

    A live listing says "currently unavailable" about one size and carries a
    sign-in link in its header. Failing that row would be far worse than the
    silence being fixed -- so the weak signals only count when nothing on the
    page looks like a product at all.
    """
    shape = inspect(REAL_PRODUCT, "https://shop.example/products/stole", has_product_node=False)

    assert shape.verdict is None
    assert shape.looks_like_product
    assert shape.unavailable and shape.login_wall, "the words are there; the verdict is not"
    assert "a purchase control ('add to cart')" in shape.product_signals


def test_a_javascript_only_shop_is_thin_but_not_blocked() -> None:
    """Stage B's problem, not a block. Failing it here would break every
    single-page-app storefront."""
    shape = inspect('<html><body><div id="root"></div></body></html>', "https://shop.example/p/1")
    assert shape.thin
    assert shape.verdict is None


def test_captcha_needs_no_corroboration_but_the_others_do() -> None:
    """Those strings do not appear on real product pages; "sign in" does."""
    with_product = ROBOT_CHECK + "<button>Add to cart</button><p>₹999</p>"
    assert inspect(with_product, "https://x.example/p").verdict is NoImageReason.BOT_CHALLENGE


# --------------------------------------------------------------------------
# The pipeline acting on it
# --------------------------------------------------------------------------


@respx.mock
async def test_captcha_page_reports_blocked_not_no_photo(settings: Settings) -> None:
    """§8 test 5, and the headline of this phase."""
    url = "https://www.amazon.in/dp/B0FTFMNYBV"
    respx.get(url).mock(
        return_value=httpx.Response(200, html=ROBOT_CHECK, headers={"content-type": "text/html"})
    )

    async with build_client(settings) as client:
        record = await process_url(url, Provenance.OWN, settings, client)

    assert record.status is RowStatus.FAILED
    assert record.failure_reason == NoImageReason.BOT_CHALLENGE.value
    assert record.image.none_reason is NoImageReason.BOT_CHALLENGE
    # And the operator is told what to do instead of being told to fight it.
    joined = " ".join(record.notes)
    assert "seller-panel export" in joined
    assert "will not try to defeat" in joined


@respx.mock
async def test_a_blocked_page_never_reaches_for_a_browser(settings: Settings) -> None:
    """Retrying a bot check with a real browser is escalation, and §4.3 forbids
    escalation. The row fails loudly instead."""
    url = "https://www.amazon.in/dp/B0FTFMNYBV"
    respx.get(url).mock(
        return_value=httpx.Response(200, html=ROBOT_CHECK, headers={"content-type": "text/html"})
    )

    launched = False

    class Tripwire:
        started = False

        async def fetch(self, _: str) -> None:
            nonlocal launched
            launched = True
            raise AssertionError("a browser was launched to get past a bot check")

    async with build_client(settings) as client:
        record = await process_url(
            url, Provenance.OWN, settings, client, renderer=Tripwire()  # type: ignore[arg-type]
        )

    assert not launched
    assert record.status is RowStatus.FAILED


@respx.mock
async def test_a_good_page_is_untouched_by_the_check(settings: Settings) -> None:
    url = "https://shop.example/products/stole"
    respx.get(url).mock(
        return_value=httpx.Response(200, html=REAL_PRODUCT, headers={"content-type": "text/html"})
    )

    async with build_client(settings) as client:
        record = await process_url(url, Provenance.OWN, settings, client)

    assert record.status is not RowStatus.FAILED
    assert record.page_verdict == ""
    assert record.title.value == "Indigo Cotton Stole"


@respx.mock
async def test_the_evidence_travels_with_the_row(settings: Settings) -> None:
    """So a false positive can be traced to the string that caused it without
    anyone having to reproduce it."""
    url = "https://handloom.example/p/1"
    respx.get(url).mock(
        return_value=httpx.Response(200, html=CLOUDFLARE, headers={"content-type": "text/html"})
    )

    async with build_client(settings) as client:
        record = await process_url(url, Provenance.OWN, settings, client)

    assert record.page_evidence
    assert any("cf-browser-verification" in line for line in record.page_evidence)
    assert any("The page said:" in note for note in record.notes)


# --------------------------------------------------------------------------
# §4.6 -- never silent
# --------------------------------------------------------------------------


def test_image_none_always_has_reason() -> None:
    """The property, not one test per path.

    `_no_image` is the only way to set NONE in `images/pipeline.py`, so this
    asserts the door rather than each room -- and that every member of the enum
    has a sentence a person can act on.
    """
    import inspect as _inspect

    from haat_lister.images import pipeline as image_pipeline

    source = _inspect.getsource(image_pipeline)
    body = source.split("def _no_image", 1)[1]
    # One assignment inside the helper, and nowhere else in the module.
    assert body.count("result.method = ImageMethod.NONE") == 1
    assert source.count("ImageMethod.NONE") == 2, (
        "a NONE outcome was set outside _no_image; it will have no reason"
    )

    for reason in NoImageReason:
        assert reason in REASONS, f"{reason} has no sentence for a human"
        assert len(REASONS[reason].what_to_do) > 40


def test_no_candidates_is_not_the_same_answer_as_all_candidates_failed() -> None:
    """One sends an operator to the page; the other sends them to the photos."""
    rejected = NoImageReason.ALL_CANDIDATES_REJECTED
    assert _candidate_reason([], rejected) is NoImageReason.NO_IMAGE_CANDIDATES
    assert _candidate_reason(["u"], rejected) is rejected


def test_the_helper_sets_all_three_together() -> None:
    result = _no_image(
        ImageResult(), NoImageReason.ALL_CANDIDATES_REJECTED, "direct_failed:too_small"
    )
    assert result.method is ImageMethod.NONE
    assert result.none_reason is NoImageReason.ALL_CANDIDATES_REJECTED
    assert result.reason == "direct_failed:too_small"


@respx.mock
async def test_a_row_that_never_reached_the_image_pipeline_still_says_why(
    settings: Settings,
) -> None:
    """A robots refusal and a dead host both end at `image: none` too."""
    url = "https://shop.example/p/1"
    respx.get(url).mock(
        side_effect=httpx.ConnectError("[Errno 11001] getaddrinfo failed")
    )

    async with build_client(settings) as client:
        record = await process_url(url, Provenance.OWN, settings, client)

    assert record.image.method is ImageMethod.NONE
    assert record.image.none_reason is NoImageReason.DNS_FAILURE
    # The row's own failure keeps the specific reason. v4 §2.5 split the old
    # `dns_or_connect_error` bucket: a name that does not resolve and a TLS
    # handshake that was refused send an operator to completely different
    # places, and one word for both sent them to neither.
    assert record.failure_reason == "dns_failure"


@respx.mock
async def test_a_refused_port_is_not_reported_as_a_name_that_does_not_resolve(
    settings: Settings,
) -> None:
    """v5 §3. `dns_failure` was the fallback for every `ConnectError`.

    So a shop whose port was closed -- or a demo server that was not running --
    was told "that address does not resolve, check the link for a typo" about a
    name that had resolved perfectly well. Wrong diagnosis, wrong next action,
    and confidently phrased.

    Found by pointing `diagnose` at the demo shop before starting it.
    """
    url = "https://shop.example/p/1"
    respx.get(url).mock(side_effect=httpx.ConnectError("All connection attempts failed"))

    async with build_client(settings) as client:
        record = await process_url(url, Provenance.OWN, settings, client)

    assert record.failure_reason != "dns_failure"
    assert record.image.none_reason is NoImageReason.TIMEOUT_CONNECT
    # The transport word stays finer-grained than the shared vocabulary: the
    # operator gets "the connection was never established", the log keeps why.
    assert record.image.reason == "connection_refused"


def test_review_csv_carries_the_word_and_the_evidence(settings: Settings) -> None:
    from haat_lister.output.review_writer import REVIEW_COLUMNS, review_row
    from haat_lister.pipeline import new_record

    record = new_record("https://shop.example/p/1", Provenance.OWN)
    record.image = _no_image(
        ImageResult(), NoImageReason.ALL_CANDIDATES_REJECTED, "direct_failed:below_min_dimensions"
    )

    row = dict(zip(REVIEW_COLUMNS, review_row(record, settings.config), strict=True))
    assert row["image_problem"] == "all_candidates_rejected"
    assert row["image_reason"] == "direct_failed:below_min_dimensions"


# --------------------------------------------------------------------------
# The report and the run must agree
# --------------------------------------------------------------------------


@respx.mock
async def test_diagnose_and_the_pipeline_agree_about_a_bot_check(settings: Settings) -> None:
    """Caught by pointing the console at the demo shop's own bot-check page.

    The report said `direct` and named the captcha image as the product photo,
    because the page-shape verdict was only consulted as a fallback when no
    candidates were found -- and a captcha page has an image on it. A diagnostic
    that disagrees with the pipeline about the exact case it was built for is
    worse than no diagnostic.
    """
    from haat_lister.diagnose import diagnose_url

    url = "https://shop.example/p/1"
    page = ROBOT_CHECK.replace(
        "https://images-na.ssl-images-amazon.com/captcha/abc/Captcha_xyz.jpg",
        "https://shop.example/img/captcha.jpg",
    )
    respx.get("https://shop.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
    )
    respx.get(url).mock(
        return_value=httpx.Response(200, html=page, headers={"content-type": "text/html"})
    )

    report = await diagnose_url(url, settings, render=False)

    assert report.images.reason == NoImageReason.BOT_CHALLENGE.value
    assert report.images.method == ImageMethod.NONE.value
    # And it stopped there: a blocked page's photos are not worth checking, and
    # checking them is how the captcha image got called a product photo.
    assert report.images.candidates == []

    async with build_client(settings) as client:
        record = await process_url(url, Provenance.OWN, settings, client)

    assert record.failure_reason == report.images.reason
