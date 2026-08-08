"""v5 §3: `diagnose` must not report checks it did not run.

The report said `bot check: no` about pages that never arrived. That is a true
statement about a variable and a false one about the shop, and it is worse than
saying nothing -- it sends whoever reads it into the extractor looking for a
bug that lives in the transport.

The same defect had four more faces on the same screen: `structured: none`,
`title: none`, `kept 0 of 0 references found`, and `stage B: off`. Each reads
as a finding about the page. None of them was.

So the tests here are all one shape: run something that fails BEFORE a check
could have run, then assert the report does not answer that check. Rendering is
included, because the model already carried `evaluated` and every renderer
ignored it -- the honest value existed and never reached a human.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from haat_lister.config import Settings
from haat_lister.diagnose import Check, StageBState, diagnose_url
from haat_lister.fetch.ladder import FailureKind, classify

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _no_leftover_profiles(settings: Settings):
    """A remembered rung would skip A1 and change what the ledger shows."""
    from haat_lister.fetch.profiles import clear_profiles

    clear_profiles(settings)
    yield
    clear_profiles(settings)


@pytest.fixture
def fast(settings: Settings) -> Settings:
    """Rungs that fail instantly, so a ladder test is not a sleep test."""
    settings.config.fetch.rung_backoff_s = 0.0
    return settings


def _robots(host: str = "https://shop.example") -> None:
    respx.get(f"{host}/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
    )


def _photo(url: str = "https://shop.example/a.jpg") -> None:
    """Tier 1 HEADs every candidate. Nothing here turns on whether it passes --
    what matters is that reaching the check is what makes the page's other
    answers real rather than defaults."""
    respx.head(url).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "image/jpeg", "content-length": "400000"}
        )
    )
    respx.get(url).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "image/jpeg", "content-length": "400000"}
        )
    )


def render(report) -> str:  # noqa: ANN001 -- diagnose.Diagnosis
    """What an operator actually sees, captured.

    Asserting on the model would have passed throughout the entire life of this
    bug: `shape.evaluated` was correct and `_render_diagnosis` never read it.
    The gap between a right answer and a printed one is exactly where this
    lived, so the tests cross it.
    """
    from rich.console import Console

    import haat_lister.cli as cli

    original = cli.console
    cli.console = Console(width=200, force_terminal=False, no_color=True)
    try:
        with cli.console.capture() as captured:
            cli._render_diagnosis(report)
        return captured.get()
    finally:
        cli.console = original


# --------------------------------------------------------------------------
# §3.1 -- three states
# --------------------------------------------------------------------------


@respx.mock
async def test_a_page_that_never_arrived_answers_no_checks(fast: Settings) -> None:
    """§3.1. Six unanswered questions, not six clean bills of health."""
    url = "https://shop.example/p/1"
    _robots()
    respx.get(url).side_effect = httpx.ConnectTimeout("timed out")

    report = await diagnose_url(url, fast, render=False)

    assert report.fetch.ok is False
    shape = report.shape
    assert shape.evaluated is False
    for field in ("looks_like_product", "captcha", "login_wall", "unavailable"):
        assert getattr(shape, field) is Check.NOT_REACHED, f"{field} answered without a page"

    text = render(report)
    assert "not reached" in text
    assert "captcha wall?               no" not in text, "a check that never ran printed a finding"


@respx.mock
async def test_a_page_that_did_arrive_answers_every_check(fast: Settings) -> None:
    """The fix must not turn every check into a shrug."""
    url = "https://shop.example/p/1"
    _robots()
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            html=(
                '<html><head><script type="application/ld+json">'
                '{"@context":"https://schema.org","@type":"Product","name":"Kurta",'
                '"image":"https://shop.example/a.jpg","offers":{"@type":"Offer","price":"10"}}'
                "</script></head><body><h1>Kurta</h1>"
                '<button>Add to cart</button></body></html>'
            ),
            headers={"content-type": "text/html"},
        )
    )

    _photo()

    report = await diagnose_url(url, fast, render=False)

    assert report.shape.evaluated is True
    assert report.shape.captcha is Check.NO, "an answered check must say no, not 'not reached'"
    assert report.shape.looks_like_product is Check.YES
    assert "not reached" not in render(report).split("IMAGE CANDIDATES")[0]


def test_not_reached_is_never_truthy() -> None:
    """`if shape.captcha:` is how the report claimed findings it did not have.

    The tri-state has to be safe under the idiom the old booleans invited, or
    the next person to write that line reintroduces the bug and no type checker
    objects.
    """
    assert not Check.NOT_REACHED
    assert not Check.NO
    assert Check.YES
    assert Check.of(None) is Check.NOT_REACHED
    assert Check.of(False) is Check.NO
    assert Check.of(True) is Check.YES


def test_evaluated_cannot_disagree_with_the_checks_it_describes() -> None:
    """Derived, not stored -- a flag and the thing it flags drift."""
    from haat_lister.diagnose import ShapeReport

    assert ShapeReport().evaluated is False
    assert ShapeReport(looks_like_product=Check.NO).evaluated is True
    # And it survives the wire, because /api/diagnose serialises this model.
    assert ShapeReport(captcha=Check.YES).model_dump()["evaluated"] is False


# --------------------------------------------------------------------------
# §3.2 -- the attempt ledger
# --------------------------------------------------------------------------


@respx.mock
async def test_every_rung_is_reported_not_just_the_last(fast: Settings) -> None:
    """§3.2. Three rungs tried, three lines, each with its own elapsed time."""
    url = "https://shop.example/p/1"
    _robots()
    respx.get(url).side_effect = httpx.RemoteProtocolError("<StreamReset error_code:2>")

    report = await diagnose_url(url, fast, render=False)

    assert len(report.fetch.attempts) == 3, "the climb was summarised instead of shown"
    assert [a.rung for a in report.fetch.attempts] == [
        "a1_http2",
        "a2_http11",
        "a3_http11_fresh",
    ]
    assert all(a.ok is False for a in report.fetch.attempts)
    assert all(a.outcome == "transport_reset" for a in report.fetch.attempts)

    text = render(report)
    assert "HTTP/2" in text and "HTTP/1.1, fresh connection" in text


@respx.mock
async def test_the_ledger_is_reported_on_the_way_up_too(fast: Settings) -> None:
    """A host that needs HTTP/1.1 is invisible when only the winner is shown.

    That is the single most useful thing `diagnose` can tell an operator about
    a site they are about to run 200 URLs against, and reporting attempts only
    on failure hides it precisely when the run is going to succeed slowly.
    """
    url = "https://shop.example/p/1"
    _robots()
    route = respx.get(url)
    route.side_effect = [
        httpx.RemoteProtocolError("<StreamReset error_code:2>"),
        httpx.Response(200, html="<html><body><h1>Kurta</h1></body></html>",
                       headers={"content-type": "text/html"}),
    ]

    report = await diagnose_url(url, fast, render=False)

    assert report.fetch.ok is True
    assert len(report.fetch.attempts) == 2, "the failed rung vanished once a later one won"
    assert report.fetch.attempts[0].ok is False
    assert report.fetch.attempts[1].ok is True
    assert report.fetch.attempts[1].outcome == "200"
    assert "fail" in render(report) and "ok" in render(report)


# --------------------------------------------------------------------------
# §3.3 -- stage B says why
# --------------------------------------------------------------------------


@respx.mock
async def test_stage_b_off_always_names_a_reason(fast: Settings) -> None:
    """§3.3. `off` is three different next actions wearing one word."""
    url = "https://shop.example/p/1"
    _robots()
    respx.get(url).side_effect = httpx.ConnectError("[Errno 11001] getaddrinfo failed")

    report = await diagnose_url(url, fast, render=False)

    assert report.stage_b.state is not StageBState.RAN
    assert str(report.stage_b.state) != "off"
    assert "not attempted" in str(report.stage_b.state) or "disabled" in str(
        report.stage_b.state
    )
    assert str(report.stage_b.state) in render(report)


@respx.mock
async def test_a_browser_that_would_get_the_same_answer_says_so(fast: Settings) -> None:
    """A name that does not resolve does not resolve in Chromium either.

    Reporting that as `off` invites the operator to run it again with
    `--render` and wait twice as long for the same word.
    """
    url = "https://shop.example/p/1"
    _robots()
    respx.get(url).side_effect = httpx.ConnectError("[Errno 11001] getaddrinfo failed")

    report = await diagnose_url(url, fast, render=True)

    assert report.stage_b.state is StageBState.POINTLESS
    assert report.stage_b.attempted is False
    assert "dns_error" in report.stage_b.error


@respx.mock
async def test_stage_b_not_needed_is_distinct_from_disabled(fast: Settings) -> None:
    """Two very different facts that both used to print as `off`."""
    url = "https://shop.example/p/1"
    _robots()
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            html=(
                '<html><head><script type="application/ld+json">'
                '{"@context":"https://schema.org","@type":"Product","name":"Kurta",'
                '"description":"A long enough description to satisfy the gate, woven by hand.",'
                '"image":"https://shop.example/a.jpg","offers":{"@type":"Offer","price":"10"}}'
                "</script></head><body><h1>Kurta</h1></body></html>"
            ),
            headers={"content-type": "text/html"},
        )
    )

    _photo()

    off = await diagnose_url(url, fast, render=False)
    assert off.stage_b.state is StageBState.DISABLED
    assert "--no-browser" in str(off.stage_b.state)


# --------------------------------------------------------------------------
# The renderer itself
# --------------------------------------------------------------------------


@respx.mock
async def test_the_report_renders_without_raising(fast: Settings) -> None:
    """`_render_diagnosis` read a field deleted two releases earlier.

    It sat at the very bottom of the function, so every line of the report
    printed before the AttributeError -- which is why it survived: the output
    looked complete and the CLI exited non-zero anyway, as it does for any page
    with no image.

    No test rendered a report. This one does, on both paths.
    """
    url = "https://shop.example/p/1"
    _robots()
    respx.get(url).side_effect = httpx.ConnectTimeout("timed out")
    render(await diagnose_url(url, fast, render=False))

    respx.get(url).side_effect = None
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            html='<html><body><h1>Kurta</h1><img src="/a.jpg"></body></html>',
            headers={"content-type": "text/html"},
        )
    )
    _photo()
    render(await diagnose_url(url, fast, render=False))


# --------------------------------------------------------------------------
# The classification behind the words
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("[Errno 11001] getaddrinfo failed", FailureKind.DNS_ERROR),
        ("Name or service not known", FailureKind.DNS_ERROR),
        ("Temporary failure in name resolution", FailureKind.DNS_ERROR),
        ("nodename nor servname provided", FailureKind.DNS_ERROR),
        ("All connection attempts failed", FailureKind.CONNECTION_REFUSED),
        ("[Errno 111] Connection refused", FailureKind.CONNECTION_REFUSED),
        ("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get issuer", FailureKind.TLS_ERROR),
    ],
)
def test_connect_errors_are_told_apart(message: str, expected: FailureKind) -> None:
    """DNS was the fallback, so everything unrecognised became "check the link
    for a typo" -- about names that had resolved fine."""
    kind, _ = classify(httpx.ConnectError(message))
    assert kind is expected
