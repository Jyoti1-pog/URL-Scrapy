"""v5 §5 -- one clock per URL, and only asking again when asking again can help.

THE DEFECT these tests pin. Every timeout was per-attempt, and the attempts
nest: three rungs at 8s, a browser at 30s, a retry on top of each. Nothing held
the total, so a single URL could occupy the better part of a minute while every
individual limit was being respected.

THE OTHER HALF. A retry budget is not evasion, and exactly one thing makes that
true: `Retry-After` compliance. A tool that retries on its own schedule is
pressing; a tool that waits the length the site asked for is doing as it was
told. So the tests here care as much about what we DON'T retry -- and about
whose number decides the wait -- as about the retrying itself.
"""

from __future__ import annotations

import time

import httpx
import pytest
import respx

from haat_lister.config import Settings
from haat_lister.fetch.budget import BudgetExhausted, Spend, UrlBudget, budget_for
from haat_lister.fetch.retry import RETRYABLE, is_retryable, retry_after_seconds, wait_for
from haat_lister.fetch.static import FetchError, build_client, fetch_static
from haat_lister.images.reasons import REFUSED, NoImageReason

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _no_leftover_profiles(settings: Settings):
    from haat_lister.fetch.profiles import clear_profiles

    clear_profiles(settings)
    yield
    clear_profiles(settings)


@pytest.fixture
def fast(settings: Settings) -> Settings:
    settings.config.fetch.rung_backoff_s = 0.0
    settings.config.fetch.retry_base_s = 0.01
    return settings


def response_with(status: int, **headers: str) -> httpx.Response:
    return httpx.Response(status, headers=headers)


# --------------------------------------------------------------------------
# §8 test 8 -- the budget is a deadline, not a sum of limits
# --------------------------------------------------------------------------


def test_the_budget_clips_what_each_step_may_take() -> None:
    """§5. This is the whole mechanism.

    A rung configured for 8 seconds gets 8 when the row is fresh and 1.4 when
    it is nearly out, so the LAST rung is what gets squeezed rather than the row
    overrunning its deadline.
    """
    budget = UrlBudget(limit_s=20.0)

    assert budget.clip(8.0) == 8.0
    budget.started -= 19.0
    assert 0.9 < budget.clip(8.0) < 1.1
    budget.started -= 2.0
    assert budget.clip(8.0) == 0.0
    assert budget.exhausted


def test_an_exhausted_budget_raises_rather_than_returning_zero() -> None:
    """A zero timeout passed to httpx is an obscure error somewhere else."""
    budget = UrlBudget(limit_s=1.0)
    budget.check()

    budget.started -= 2.0
    with pytest.raises(BudgetExhausted):
        budget.check()


@respx.mock
async def test_url_timeout_budget_enforced(fast: Settings) -> None:
    """§8 test 8, against a host that hangs on every rung.

    The measured shape this exists for: two of the hosts that motivated the
    ladder do not RESET http/1.1, they black-hole it, so every rung waits its
    whole budget. Three of those is 24 seconds against a 20-second deadline,
    and before §5 nothing anywhere noticed.
    """
    fast.config.fetch.url_timeout_s = 1.0
    fast.config.fetch.rung_timeout_s = 5.0

    url = "https://blackhole.example/p/1"
    respx.get(url).side_effect = httpx.ReadTimeout("hung")

    started = time.monotonic()
    async with build_client(fast) as client:
        with pytest.raises(FetchError):
            await fetch_static(client, url, fast)
    elapsed = time.monotonic() - started

    # Generous, because respx raises instantly and the point is the CEILING:
    # without the budget this would try three rungs at five seconds each.
    assert elapsed < 6.0, f"the row ran for {elapsed:.1f}s against a 1s budget"


@respx.mock
async def test_a_failure_reports_what_it_spent(fast: Settings) -> None:
    """`21s` on its own is a complaint; the split is a diagnosis."""
    url = "https://gone.example/p/1"
    respx.get(url).side_effect = httpx.ConnectError("[Errno 11001] getaddrinfo failed")

    async with build_client(fast) as client:
        with pytest.raises(FetchError) as caught:
            await fetch_static(client, url, fast)

    spent = caught.value.spent
    assert "fetch" in spent and "parse" in spent and "idle" in spent


def test_the_report_names_the_total_first_then_the_split() -> None:
    budget = UrlBudget(limit_s=20.0)
    budget.spent[Spend.FETCH.value] = 19.8
    budget.spent[Spend.PARSE.value] = 0.2
    budget.spent[Spend.IDLE.value] = 1.0

    report = budget.report()

    assert report.startswith(f"{budget.elapsed:.0f}s")
    assert "fetch 19.8s" in report
    assert "parse 0.2s" in report
    assert "idle 1.0s" in report


def test_the_parts_never_sum_to_more_than_the_whole() -> None:
    """The one thing a breakdown must never do, and it did.

    A rung backoff sits between two requests: inside the fetch phase by the
    clock, outside it by the meaning. Counting it in both produced
    `8s - fetch 15.3s, parse 0.0s, idle 2.4s`, which is not a diagnosis, it is
    a reason to stop trusting the line.
    """
    budget = UrlBudget(limit_s=20.0)

    with budget.spending(Spend.FETCH):
        time.sleep(0.02)
        budget.waited(0.01)  # a backoff, inside the fetch block
        time.sleep(0.02)

    total = sum(budget.spent.values())
    assert total <= budget.elapsed + 0.01, f"{budget.report()} sums to more than it took"
    assert budget.spent[Spend.IDLE.value] == pytest.approx(0.01, abs=0.005)


def test_a_honoured_retry_after_is_named_as_theirs_not_ours() -> None:
    """A 30-second idle looks like a bug until you know they asked for it."""
    budget = UrlBudget(limit_s=60.0)
    budget.waited(30.0, requested=True)

    assert "Retry-After" in budget.report()
    assert budget.spent[Spend.IDLE.value] == 30.0


def test_time_is_attributed_even_when_the_step_raises() -> None:
    """A timeout is exactly when somebody wants to know how long we waited, and
    a naive implementation loses precisely that number."""
    budget = UrlBudget(limit_s=20.0)

    with pytest.raises(ValueError), budget.spending(Spend.FETCH):
        time.sleep(0.01)
        raise ValueError("boom")

    assert budget.spent[Spend.FETCH.value] > 0.0


# --------------------------------------------------------------------------
# §8 test 6 (again) and 10 -- what is retried, and whose clock decides
# --------------------------------------------------------------------------


def test_retry_excludes_refused_class() -> None:
    """§5's list, checked against §2's table rather than against a copy of it.

    A second list of "and is it worth retrying" would be the same drift in a
    new place, so `RETRYABLE` is derived from the vocabulary.
    """
    # §5's four, all of them retryable.
    assert {"timeout_connect", "timeout_read", "http_error_5xx", "blocked_429"} <= {
        r.value for r in RETRYABLE
    }
    # And nothing a FETCH can produce beyond those four is. (`host_upload_failed`
    # is retryable and is a Tier-2 outcome, so it never reaches this policy --
    # asserted here rather than assumed, since the day it does reach it is the
    # day a failed upload starts re-fetching the page.)
    from haat_lister.fetch.ladder import FailureKind
    from haat_lister.fetch.static import _retry_word

    produced = {_retry_word(k, None) for k in FailureKind} | {
        _retry_word(FailureKind.HTTP_4XX, s) for s in (403, 404, 429)
    } | {_retry_word(FailureKind.HTTP_5XX, 503)}
    assert {w for w in produced if w and is_retryable(w)} <= {
        "timeout_connect",
        "timeout_read",
        "http_error_5xx",
        "blocked_429",
    }

    for reason in REFUSED - {NoImageReason.BLOCKED_429}:
        assert not is_retryable(reason), f"{reason} is a decision, not a fault"
    assert not is_retryable(NoImageReason.DNS_FAILURE)
    assert not is_retryable(NoImageReason.NOT_A_PRODUCT_PAGE)


def test_an_unrecognised_reason_is_not_retried() -> None:
    """Deliberately the strict direction: retrying something we do not
    understand doubles the requests to a site that just did something we do not
    understand."""
    assert not is_retryable("the site was rude to me")
    assert not is_retryable(None)


def test_retry_after_respected_in_seconds_and_as_a_date() -> None:
    """§8 test 10. Both forms are real."""
    assert retry_after_seconds(response_with(429, **{"retry-after": "30"})) == 30.0

    dated = response_with(
        503,
        **{
            "retry-after": "Wed, 21 Oct 2015 07:28:37 GMT",
            "date": "Wed, 21 Oct 2015 07:28:07 GMT",
        },
    )
    assert retry_after_seconds(dated) == 30.0


def test_the_date_form_is_measured_against_their_clock_not_ours() -> None:
    """A client four minutes fast would otherwise decide the wait had already
    elapsed and hammer straight back."""
    skewed = response_with(
        429,
        **{
            "retry-after": "Wed, 21 Oct 2015 07:28:37 GMT",
            "date": "Wed, 21 Oct 2015 07:28:07 GMT",
        },
    )
    assert retry_after_seconds(skewed) == 30.0
    assert retry_after_seconds(response_with(429)) is None
    assert retry_after_seconds(None) is None


def test_their_number_wins_over_our_backoff() -> None:
    """The whole difference between a retry budget and pressing."""
    budget = UrlBudget(limit_s=120.0)
    stated = wait_for(
        "blocked_429", attempt=3, budget=budget, response=response_with(429, **{"retry-after": "5"})
    )

    assert stated == 5.0, "our exponential backoff overrode what the site asked for"


def test_an_hour_long_retry_after_is_read_as_a_refusal() -> None:
    """Sites answer 429 with anything up to an hour, and an hour is not a wait
    -- it is a refusal with a timestamp on it."""
    budget = UrlBudget(limit_s=3600.0)
    assert (
        wait_for("blocked_429", 1, budget, response_with(429, **{"retry-after": "3600"})) is None
    )


def test_a_wait_longer_than_the_row_has_left_is_not_taken() -> None:
    """Waiting the row's whole remaining time leaves nothing for the request,
    which is a slower way of failing."""
    budget = UrlBudget(limit_s=20.0)
    budget.started -= 18.0

    assert wait_for("blocked_429", 1, budget, response_with(429, **{"retry-after": "10"})) is None


def test_backoff_doubles_only_when_the_site_did_not_say() -> None:
    """A shop that just 503'd is a shop under load, and returning at the same
    interval is the behaviour that keeps it there."""
    budget = UrlBudget(limit_s=120.0)

    assert wait_for("http_error_5xx", 1, budget, None, base_s=1.0) == 1.0
    assert wait_for("http_error_5xx", 2, budget, None, base_s=1.0) == 2.0
    assert wait_for("http_error_5xx", 3, budget, None, base_s=1.0) == 4.0


@respx.mock
async def test_retry_after_header_respected(fast: Settings) -> None:
    """§8 test 10, end to end: a 429 with a header, then a page."""
    url = "https://busy.example/p/1"
    route = respx.get(url)
    route.side_effect = [
        httpx.Response(429, headers={"retry-after": "0"}),
        httpx.Response(
            200,
            html="<html><body><h1>Kurta</h1></body></html>",
            headers={"content-type": "text/html"},
        ),
    ]

    async with build_client(fast) as client:
        result = await fetch_static(client, url, fast)

    assert result.status_code == 200


@respx.mock
async def test_a_robots_refusal_is_never_retried(fast: Settings) -> None:
    """Retrying `robots_disallowed` produces `robots_disallowed`, forever.

    Asserted by counting requests rather than by reading a flag: the flag being
    right proves nothing about whether some caller consults it.
    """
    url = "https://private.example/p/1"
    route = respx.get(url).mock(return_value=httpx.Response(403))

    async with build_client(fast) as client:
        with pytest.raises(FetchError):
            await fetch_static(client, url, fast)

    # Three rungs, and not a fourth: a 403 is climbed (it can be a bot wall)
    # but never retried on a timer.
    assert route.call_count <= len(("a1", "a2", "a3"))


@respx.mock
async def test_a_transport_reset_is_not_retried_on_top_of_the_ladder(fast: Settings) -> None:
    """The ladder IS the retry for a reset -- three rungs, two of them for
    exactly this case, one on a fresh connection.

    Climbing it a second time doubles the requests to a host that has just
    reset us three times, which is the grinding the per-rung budget was
    introduced to stop.
    """
    url = "https://reset.example/p/1"
    route = respx.get(url)
    route.side_effect = httpx.RemoteProtocolError("<StreamReset error_code:2>")

    async with build_client(fast) as client:
        with pytest.raises(FetchError):
            await fetch_static(client, url, fast)

    assert route.call_count == 3, f"the ladder was climbed {route.call_count // 3} times"


# --------------------------------------------------------------------------
# §8 test 9 -- the circuit breaker
# --------------------------------------------------------------------------


@respx.mock
async def test_circuit_breaker_opens_after_n_refusals(fast: Settings) -> None:
    """§8 test 9. §5 sets the threshold to 5; the ladder shipped with 3.

    Extended rather than rebuilt: the ladder already counted consecutive
    whole-climb failures per host and already fast-failed on them, so making
    the number configurable was the entire change. A breaker built beside that
    one would be two things deciding the same question.
    """
    from haat_lister.fetch.profiles import is_refusing

    fast.config.fetch.refusals_before_fast_fail = 3
    route = respx.get(url__regex=r"https://refusing\.example/.*")
    route.side_effect = httpx.RemoteProtocolError("reset")

    async with build_client(fast) as client:
        for n in range(3):
            with pytest.raises(FetchError):
                await fetch_static(client, f"https://refusing.example/p/{n}", fast)
        before = route.call_count

        assert is_refusing(fast, "https://refusing.example/p/99")
        with pytest.raises(FetchError) as caught:
            await fetch_static(client, "https://refusing.example/p/99", fast)

    assert route.call_count == before, "the breaker was open and the host was asked anyway"
    assert "profiles --clear" in caught.value.detail, "no way out is offered"


@respx.mock
async def test_the_threshold_comes_from_config(fast: Settings) -> None:
    """§5 names 5, and it has to be the number that actually decides."""
    from haat_lister.fetch.profiles import is_refusing

    fast.config.fetch.refusals_before_fast_fail = 5
    route = respx.get(url__regex=r"https://slow\.example/.*")
    route.side_effect = httpx.RemoteProtocolError("reset")

    async with build_client(fast) as client:
        for n in range(4):
            with pytest.raises(FetchError):
                await fetch_static(client, f"https://slow.example/p/{n}", fast)
        assert not is_refusing(fast, "https://slow.example/p/9"), "opened one refusal early"

        with pytest.raises(FetchError):
            await fetch_static(client, "https://slow.example/p/4", fast)
        assert is_refusing(fast, "https://slow.example/p/9")


@respx.mock
async def test_one_success_closes_the_breaker(fast: Settings) -> None:
    """The count is about a run, not a reputation. A shop having a bad ten
    minutes must not be written off for the rest of the job."""
    from haat_lister.fetch.profiles import is_refusing

    fast.config.fetch.refusals_before_fast_fail = 2
    route = respx.get(url__regex=r"https://flaky2\.example/.*")
    route.side_effect = [
        httpx.RemoteProtocolError("reset"),
        httpx.RemoteProtocolError("reset"),
        httpx.RemoteProtocolError("reset"),
        httpx.Response(
            200, html="<html><body><h1>K</h1></body></html>",
            headers={"content-type": "text/html"},
        ),
    ]

    async with build_client(fast) as client:
        with pytest.raises(FetchError):
            await fetch_static(client, "https://flaky2.example/p/1", fast)
        await fetch_static(client, "https://flaky2.example/p/2", fast)

    assert not is_refusing(fast, "https://flaky2.example/p/3")


def test_the_default_budget_comes_from_one_place(settings: Settings) -> None:
    """CLI, API and batch cannot disagree about the deadline."""
    assert budget_for(settings).limit_s == settings.config.fetch.url_timeout_s
    assert budget_for(settings, 45.0).limit_s == 45.0
