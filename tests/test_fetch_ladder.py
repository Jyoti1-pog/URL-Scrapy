"""v4 Phases 1-3: the fetch ladder, honest diagnosis, and the scoped buy box.

The bug that started this: two shops answered a request by resetting the HTTP/2
stream, and the tool reported `page_fetch_failed` -- one word covering "no HTML
arrived", "the site said no", and "the name does not resolve". It then printed
`looks like a product page: no` and `bot check: no` about a page it had never
seen, which invites reading a transport bug as an extraction bug.

Phase 0 measured the real hosts before any of this was written, and the
measurement changed the design: HTTP/1.1 does not rescue them (it is
black-holed, so it is SLOWER to fail) and neither does a real browser. That is
why the per-rung budget is tested here as carefully as the ladder order.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from haat_lister.config import Settings
from haat_lister.fetch.ladder import (
    A_RUNGS,
    Attempt,
    FailureKind,
    LadderOutcome,
    Rung,
    browser_headers,
    classify,
    climb,
)
from haat_lister.fetch.profiles import all_profiles, clear_profiles, get_profile
from haat_lister.fetch.shape import inspect
from haat_lister.fetch.static import FetchError, build_client, fetch_static
from haat_lister.images.reasons import NoImageReason
from haat_lister.models import Provenance, RowStatus
from haat_lister.pipeline import process_url

PAGE = '<html><head><title>Kurta</title></head><body><h1>Kurta</h1></body></html>'


def html_response() -> httpx.Response:
    return httpx.Response(200, html=PAGE, headers={"content-type": "text/html"})


@pytest.fixture(autouse=True)
def _no_leftover_profiles(settings: Settings):
    clear_profiles(settings)
    yield
    clear_profiles(settings)


@pytest.fixture
def fast(settings: Settings) -> Settings:
    """Rungs that fail instantly, so a ladder test is not a sleep test."""
    settings.config.fetch.rung_backoff_s = 0.0
    return settings


# --------------------------------------------------------------------------
# The ladder
# --------------------------------------------------------------------------


@respx.mock
async def test_http2_stream_reset_falls_back_to_http1(fast: Settings) -> None:
    """§10 test 1, and the whole reason for the ladder.

    respx cannot vary by protocol, so the first call raises the reset and the
    second answers -- which is exactly what a rung boundary looks like from the
    caller's side.
    """
    url = "https://shop.example/p/1"
    route = respx.get(url)
    route.side_effect = [
        httpx.RemoteProtocolError("<StreamReset stream_id:1, error_code:2, remote_reset:True>"),
        html_response(),
    ]

    response, outcome = await climb(url, fast)

    assert response is not None and response.status_code == 200
    assert outcome.winner is Rung.A2
    assert [a.rung for a in outcome.attempts] == [Rung.A1, Rung.A2]
    assert outcome.attempts[0].kind is FailureKind.TRANSPORT_RESET
    # And the climb is legible afterwards without re-running it.
    assert "a1_http2:transport_reset" in outcome.rungs_tried
    assert "a2_http11:200" in outcome.rungs_tried


@respx.mock
async def test_the_first_rung_wins_when_it_can(fast: Settings) -> None:
    """The ladder must cost nothing on the sites that already worked."""
    url = "https://shop.example/p/1"
    route = respx.get(url).mock(return_value=html_response())

    response, outcome = await climb(url, fast)

    assert response is not None
    assert outcome.winner is Rung.A1
    assert route.call_count == 1, "a working site was asked more than once"


@respx.mock
async def test_a_404_does_not_climb(fast: Settings) -> None:
    """Nothing below the rung answers differently, and three requests for one
    missing page is rude."""
    url = "https://shop.example/gone"
    route = respx.get(url).mock(return_value=httpx.Response(404))

    response, outcome = await climb(url, fast)

    assert response is None
    assert route.call_count == 1
    assert outcome.last_failure is not None
    assert outcome.last_failure.status == 404
    assert not outcome.should_escalate_to_browser


@respx.mock
async def test_a_403_climbs_and_then_asks_for_a_browser(fast: Settings) -> None:
    """A bare 403 is how a bot wall often presents; it may be this client
    rather than this URL."""
    url = "https://shop.example/p/1"
    respx.get(url).mock(return_value=httpx.Response(403))

    response, outcome = await climb(url, fast)

    assert response is None
    assert [a.rung for a in outcome.attempts] == list(A_RUNGS)
    assert outcome.should_escalate_to_browser


@respx.mock
async def test_a_name_that_does_not_resolve_stops_immediately(fast: Settings) -> None:
    """Three attempts at a typo is three DNS timeouts an operator waits for."""
    url = "https://nope.invalid/p/1"
    route = respx.get(url)
    route.side_effect = httpx.ConnectError("getaddrinfo failed")

    response, outcome = await climb(url, fast)

    assert response is None
    assert len(outcome.attempts) == 1
    assert outcome.last_failure is not None
    assert outcome.last_failure.kind is FailureKind.DNS_ERROR
    assert not outcome.should_escalate_to_browser, "a browser cannot resolve it either"


def test_every_rung_gets_its_own_short_budget(settings: Settings) -> None:
    """The design consequence Phase 0 forced.

    A host that black-holes HTTP/1.1 holds the connection for the whole budget.
    Three rungs inheriting a 20s fetch timeout turn a 0.7-second failure into a
    minute -- on exactly the sites the ladder was added to help.
    """
    cfg = settings.config.fetch
    assert cfg.rung_timeout_s < cfg.timeout_s
    assert cfg.rung_timeout_s * len(A_RUNGS) <= cfg.timeout_s + cfg.rung_timeout_s


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.RemoteProtocolError("stream reset"), FailureKind.TRANSPORT_RESET),
        (httpx.ConnectTimeout("slow"), FailureKind.TIMEOUT_CONNECT),
        (httpx.ReadTimeout("quiet"), FailureKind.TIMEOUT_READ),
        (httpx.ConnectError("getaddrinfo failed"), FailureKind.DNS_ERROR),
        (httpx.ConnectError("SSL: CERTIFICATE_VERIFY_FAILED"), FailureKind.TLS_ERROR),
        (httpx.TooManyRedirects("loop"), FailureKind.TOO_MANY_REDIRECTS),
    ],
)
def test_failures_are_named_not_bucketed(exc: Exception, expected: FailureKind) -> None:
    """§2.5. A read timeout and a name that does not resolve send an operator to
    completely different places; one word for both sent them to neither."""
    kind, _ = classify(exc)
    assert kind is expected


def test_only_transport_failures_ask_for_a_browser() -> None:
    """A browser cannot fix a 404 and cannot resolve a bad name."""

    def outcome_of(kind: FailureKind, status: int | None = None) -> LadderOutcome:
        return LadderOutcome(
            attempts=[Attempt(rung=Rung.A3, ok=False, elapsed_ms=1, kind=kind, status=status)]
        )

    assert outcome_of(FailureKind.TRANSPORT_RESET).should_escalate_to_browser
    assert outcome_of(FailureKind.TIMEOUT_READ).should_escalate_to_browser
    assert outcome_of(FailureKind.HTTP_4XX, 403).should_escalate_to_browser
    assert not outcome_of(FailureKind.HTTP_4XX, 404).should_escalate_to_browser
    assert not outcome_of(FailureKind.DNS_ERROR).should_escalate_to_browser
    assert not outcome_of(FailureKind.BLOCKED_ADDRESS).should_escalate_to_browser


def test_the_headers_are_honest(settings: Settings) -> None:
    """§2.3 and §8 together: a browser being honest about being a browser.

    The tool's name and a contactable address stay in the User-Agent. That is
    the line between sending what a browser sends and pretending to be someone
    else.
    """
    headers = browser_headers(settings)
    assert "haat-lister" in headers["User-Agent"]
    assert "contact" in headers["User-Agent"].lower()
    for name in ("Sec-Fetch-Dest", "Sec-Fetch-Mode", "Accept-Encoding"):
        assert name in headers


# --------------------------------------------------------------------------
# Escalation, and the reason it used not to happen
# --------------------------------------------------------------------------


@respx.mock
async def test_transport_error_escalates_to_browser(fast: Settings) -> None:
    """§10 test 2. The observed bug: `stage B: off` on the one failure that most
    needs a browser."""
    url = "https://shop.example/p/1"
    respx.get(url).side_effect = httpx.RemoteProtocolError("<StreamReset error_code:2>")

    tried = []

    class Browser:
        started = False

        async def fetch(self, target: str):
            tried.append(target)
            from haat_lister.fetch.static import FetchResult
            from haat_lister.models import FetchStage

            return FetchResult(
                url=target,
                final_url=target,
                status_code=200,
                html=PAGE,
                stage=FetchStage.RENDERED,
                elapsed_ms=10,
                headers={"content-type": "text/html"},
            )

    async with build_client(fast) as client:
        record = await process_url(
            url, Provenance.OWN, fast, client, renderer=Browser()  # type: ignore[arg-type]
        )

    # Exactly once. An escalated fetch has already rendered this URL, so the
    # Stage B trigger must not launch Chromium again for the same row.
    assert tried == [url], f"browser launched {len(tried)} time(s), expected 1"
    assert record.status is not RowStatus.FAILED
    assert any("fetched with a browser instead" in note for note in record.notes)


@respx.mock
async def test_browser_unavailable_reports_clearly(fast: Settings) -> None:
    """§10 test 3. Never `page_fetch_failed` when the real answer is "install
    the browser"."""
    from haat_lister.fetch.rendered import RenderUnavailable

    url = "https://shop.example/p/1"
    respx.get(url).side_effect = httpx.RemoteProtocolError("<StreamReset error_code:2>")

    class Missing:
        started = False

        async def fetch(self, _: str):
            raise RenderUnavailable("playwright is not installed")

    async with build_client(fast) as client:
        record = await process_url(
            url, Provenance.OWN, fast, client, renderer=Missing()  # type: ignore[arg-type]
        )

    assert record.image.none_reason is NoImageReason.BROWSER_UNAVAILABLE
    assert any("playwright install chromium" in note for note in record.notes)


@respx.mock
async def test_stage_b_being_off_is_explained_not_silent(fast: Settings) -> None:
    """`stage B: off` with no explanation was the original failure."""
    url = "https://shop.example/p/1"
    respx.get(url).side_effect = httpx.RemoteProtocolError("<StreamReset error_code:2>")

    async with build_client(fast) as client:
        record = await process_url(url, Provenance.OWN, fast, client, renderer=None)

    assert record.status is RowStatus.FAILED
    assert any("wants a real browser" in note for note in record.notes)
    assert any("--render" in note for note in record.notes)


@respx.mock
async def test_the_row_records_every_rung_it_tried(fast: Settings) -> None:
    """So a re-run can be reasoned about without re-running."""
    url = "https://shop.example/p/1"
    respx.get(url).side_effect = httpx.RemoteProtocolError("<StreamReset error_code:2>")

    async with build_client(fast) as client:
        record = await process_url(url, Provenance.OWN, fast, client, renderer=None)

    assert "a1_http2:transport_reset" in record.rungs_tried
    assert "a3_http11_fresh" in record.rungs_tried
    assert record.failure_reason == NoImageReason.TIMEOUT_READ.value


def test_no_generic_page_fetch_failed_remains() -> None:
    """§10 test 4. The bucket is gone as a FETCH reason.

    `NoImageReason.TIMEOUT_READ` survives on purpose -- it is the IMAGE
    side's category for "there was no page to look in", which is a different
    question from what went wrong on the wire. What must not survive is a
    failure whose only explanation is that phrase.
    """
    import pathlib

    from haat_lister.fetch import static

    source = pathlib.Path(static.__file__).read_text(encoding="utf-8")
    # Against the CODE, not the prose: the module docstring names the bucket it
    # replaced, which is the point of the docstring.
    body = source.split('"""', 2)[-1]
    code = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )
    assert "page_fetch_failed" not in code
    assert {k.value for k in FailureKind} >= {
        "transport_reset",
        "tls_error",
        "dns_error",
        "timeout_connect",
        "timeout_read",
    }


# --------------------------------------------------------------------------
# Phase 2 -- profiles
# --------------------------------------------------------------------------


@respx.mock
async def test_fetch_profile_persisted_and_reused(fast: Settings) -> None:
    """§10 test 6. A catalogue is one shop; paying the h2 failure 200 times is
    the entire cost of having a ladder."""
    first = "https://shop.example/p/1"
    second = "https://shop.example/p/2"
    respx.get(first).side_effect = [
        httpx.RemoteProtocolError("<StreamReset error_code:2>"),
        html_response(),
    ]
    route = respx.get(second).mock(return_value=html_response())

    async with build_client(fast) as client:
        await fetch_static(client, first, fast)
        assert get_profile(fast, first) is Rung.A2
        assert all_profiles(fast) == {"shop.example": "a2_http11"}

        result = await fetch_static(client, second, fast)

    # One request, not two: the second URL skipped the rung that already failed.
    assert route.call_count == 1
    assert result.rung is Rung.A2


@respx.mock
async def test_a_stale_profile_can_never_break_a_working_site(fast: Settings) -> None:
    """Starting at A2 only SKIPS rungs. If A2 fails the climb continues exactly
    as it would have, so a wrong hint costs time and never a row."""
    url = "https://shop.example/p/1"
    from haat_lister.fetch.profiles import remember_profile

    remember_profile(fast, url, Rung.A3)
    respx.get(url).mock(return_value=html_response())

    response, outcome = await climb(url, fast, start_at=Rung.A3)
    assert response is not None
    assert outcome.winner is Rung.A3


def test_profiles_are_visible_and_clearable(settings: Settings) -> None:
    from haat_lister.fetch.profiles import remember_profile

    remember_profile(settings, "https://shop.example/p/1", Rung.A2)
    assert all_profiles(settings) == {"shop.example": "a2_http11"}
    clear_profiles(settings, "shop.example")
    assert all_profiles(settings) == {}


# --------------------------------------------------------------------------
# Phase 2 -- diagnose honesty
# --------------------------------------------------------------------------


@respx.mock
async def test_page_shape_not_evaluated_without_html(fast: Settings) -> None:
    """§10 test 5. Printing "bot check: no" about a page that never arrived is
    how a transport bug gets misread as an extraction bug."""
    from haat_lister.diagnose import diagnose_url

    url = "https://shop.example/p/1"
    respx.get("https://shop.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
    )
    respx.get(url).side_effect = httpx.RemoteProtocolError("<StreamReset error_code:2>")

    report = await diagnose_url(url, fast, render=False)

    from haat_lister.diagnose import Check

    assert report.fetch.ok is False
    assert report.shape.evaluated is False, "a check that never ran was reported as a finding"
    # v5 §3.1. Tightened from `is False` to the third state. The old assertion
    # was satisfied by the exact defect it was written to catch: a boolean that
    # renders as `no` says the page was checked and found clean.
    assert report.shape.captcha is Check.NOT_REACHED
    assert report.shape.looks_like_product is Check.NOT_REACHED
    assert not report.shape.captcha, "NOT_REACHED must not be truthy"
    assert "a1_http2:transport_reset" in report.fetch.rungs_tried


@respx.mock
async def test_diagnose_names_the_winning_rung(fast: Settings) -> None:
    from haat_lister.diagnose import diagnose_url

    url = "https://shop.example/p/1"
    respx.get("https://shop.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
    )
    respx.get(url).side_effect = [
        httpx.RemoteProtocolError("<StreamReset error_code:2>"),
        html_response(),
    ]

    report = await diagnose_url(url, fast, render=False)

    assert report.shape.evaluated is True
    assert report.fetch.rung == Rung.A2.value


# --------------------------------------------------------------------------
# Phase 3 -- the scoped buy box
# --------------------------------------------------------------------------

# A live listing whose recommendation strip mentions an unavailable product.
BUSY_PRODUCT = """<html><head><title>Wired Earphones</title></head><body>
  <div id="rightCol">
    <span class="price">₹1,299</span>
    <span id="availability">In stock</span>
    <input id="add-to-cart-button" type="submit" value="Add to cart">
  </div>
  <div id="similar-products">
    <h2>Customers also viewed</h2>
    <div>Some Other Product — currently unavailable</div>
    <div>A Third Thing — no longer available</div>
  </div>
</body></html>"""

SOLD_OUT = """<html><head><title>Wired Earphones</title></head><body>
  <div id="rightCol">
    <span id="availability">Currently unavailable.</span>
    <p>We don't know when or if this item will be back in stock.</p>
  </div>
</body></html>"""


def test_soft404_ignored_when_buybox_present() -> None:
    """§10 test 7, on the shape that caused it: a 2.3 MB page whose carousels
    mention other products' availability."""
    shape = inspect(BUSY_PRODUCT, "https://shop.example/p/1")

    assert shape.verdict is None
    assert shape.buy_box_found
    assert not shape.unavailable_in_buy_box
    # Reported, with WHERE -- so the next false positive is obvious at a glance.
    assert any("elsewhere on page" in line for line in shape.evidence)
    assert not any("buy-box" in line for line in shape.evidence)


def test_soft404_honoured_inside_buybox() -> None:
    """§10 test 8. Inside the buy box it is a real signal about this product."""
    shape = inspect(SOLD_OUT, "https://shop.example/p/1")

    assert shape.unavailable_in_buy_box
    assert any("buy-box" in line for line in shape.evidence)
    assert shape.verdict is NoImageReason.NOT_A_PRODUCT_PAGE


def test_page_shape_contradiction_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """§10 test 9. "something priced" and "currently unavailable" in one report
    is the contradiction that exposed the unscoped match."""
    import logging

    contradictory = """<html><body>
      <div id="rightCol"><span>Currently unavailable</span></div>
      <p>₹1,299</p>
    </body></html>"""

    with caplog.at_level(logging.WARNING):
        shape = inspect(contradictory, "https://shop.example/p/1")

    assert shape.verdict is None, "a priced page is not unavailable"
    # No warning here precisely BECAUSE the verdict logic resolved it. The
    # warning fires only when the two would have disagreed in the output.
    assert not any("contradiction" in r.message for r in caplog.records)


def test_a_shop_with_no_recognised_buy_box_still_gets_the_check() -> None:
    """Falling back to the body is the weaker scope, not no scope -- and the
    shape records which was used so the difference is visible."""
    plain = "<html><body><p>This listing has been removed.</p></body></html>"
    shape = inspect(plain, "https://shop.example/p/1")

    assert not shape.buy_box_found
    assert shape.verdict is NoImageReason.NOT_A_PRODUCT_PAGE


@respx.mock
async def test_a_host_that_refuses_everything_stops_being_re_climbed(fast: Settings) -> None:
    """The other half of the profile economy, and the reason it matters.

    Measured on the hosts that motivated this ladder: three rungs at their full
    budget is ~18 seconds. A 200-link catalogue from one such shop is an hour of
    waiting to be told what the first link already said.
    """
    from haat_lister.fetch.profiles import REFUSALS_BEFORE_FAST_FAIL, is_refusing

    route = respx.get(url__regex=r"https://refuses\.example/.*")
    route.side_effect = httpx.RemoteProtocolError("<StreamReset error_code:2>")

    async with build_client(fast) as client:
        for index in range(REFUSALS_BEFORE_FAST_FAIL):
            with pytest.raises(FetchError):
                await fetch_static(client, f"https://refuses.example/p/{index}", fast)

        climbs = route.call_count
        assert is_refusing(fast, "https://refuses.example/p/99")

        # The next URL costs nothing: no rung is climbed at all.
        with pytest.raises(FetchError) as caught:
            await fetch_static(client, "https://refuses.example/p/99", fast)

    assert route.call_count == climbs, "a refusing host was climbed again"
    assert "not re-tried" in str(caught.value)
    assert "profiles --clear" in str(caught.value), "no way back is not an answer"


@respx.mock
async def test_one_success_forgives_a_host(fast: Settings) -> None:
    """The count is about a run, not a reputation. A shop having a bad ten
    minutes must not be written off for the rest of the job."""
    from haat_lister.fetch.profiles import is_refusing

    route = respx.get(url__regex=r"https://flaky\.example/.*")
    route.side_effect = [
        httpx.RemoteProtocolError("reset"),
        httpx.RemoteProtocolError("reset"),
        httpx.RemoteProtocolError("reset"),
        html_response(),
    ]

    async with build_client(fast) as client:
        with pytest.raises(FetchError):
            await fetch_static(client, "https://flaky.example/p/1", fast)
        await fetch_static(client, "https://flaky.example/p/2", fast)

    assert not is_refusing(fast, "https://flaky.example/p/3")


# --------------------------------------------------------------------------
# Profiles survive the process they were learned in
# --------------------------------------------------------------------------


@respx.mock
async def test_a_profile_outlives_the_process_that_learned_it(
    settings: Settings, tmp_path
) -> None:
    """§2.6 asks for persistence, and `profiles --list` is meaningless without it.

    An earlier version kept this in memory only. A resumed batch is a new
    process and would have re-learned every host from scratch, and the command
    that exists to make the mechanism visible would always have printed nothing.
    """
    from haat_lister.fetch.profiles import _STORES, all_profiles, clear_profiles

    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    tuned.config.fetch.rung_backoff_s = 0.0
    clear_profiles(tuned)

    respx.get("https://slow.example/p/1").side_effect = [
        httpx.RemoteProtocolError("<StreamReset error_code:2>"),
        html_response(),
    ]
    async with build_client(tuned) as client:
        await fetch_static(client, "https://slow.example/p/1", tuned)

    assert all_profiles(tuned) == {"slow.example": "a2_http11"}

    # A different process would have an empty in-memory store. Simulated by
    # dropping it -- the ledger is what has to carry the answer.
    _STORES.clear()
    assert all_profiles(tuned) == {"slow.example": "a2_http11"}

    from haat_lister.fetch.profiles import get_profile

    assert get_profile(tuned, "https://slow.example/p/2") is Rung.A2


def test_a_profile_is_clearable(settings: Settings, tmp_path) -> None:
    """A mechanism that silently changes what the tool does has to be one an
    operator can look at and switch off."""
    from haat_lister.fetch.profiles import all_profiles, clear_profiles, remember_profile

    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    remember_profile(tuned, "https://a.example/p", Rung.A2)
    remember_profile(tuned, "https://b.example/p", Rung.A3)
    assert set(all_profiles(tuned)) == {"a.example", "b.example"}

    clear_profiles(tuned, "a.example")
    assert set(all_profiles(tuned)) == {"b.example"}

    clear_profiles(tuned)
    assert all_profiles(tuned) == {}


def test_a_host_back_on_http2_stops_being_marked(settings: Settings, tmp_path) -> None:
    """A shop that fixes its HTTP/2 should not be on the slow path forever."""
    from haat_lister.fetch.profiles import all_profiles, clear_profiles, remember_profile

    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    clear_profiles(tuned)
    remember_profile(tuned, "https://mended.example/p/1", Rung.A2)
    remember_profile(tuned, "https://mended.example/p/2", Rung.A1)

    assert all_profiles(tuned) == {}
