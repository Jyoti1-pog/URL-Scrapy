"""v5 §4.4 -- knowing before the run what we would otherwise learn after it.

An operator pastes 200 links and waits four minutes to be told that 180 of them
are on a host that refuses this tool. Every one of those refusals was knowable
at second zero: robots.txt is one request per domain, and the last run already
wrote down what happened.

The constraint that shapes this module, and the thing most of these tests are
actually about: `domains.yaml` NEVER PREVENTS A RUN. It is a record of
observations and observations go stale. A file that quietly became a blocklist
would turn one bad afternoon into a permanent decision nobody made.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
import yaml

from haat_lister import preflight
from haat_lister.config import Settings

pytestmark = pytest.mark.anyio


@pytest.fixture
def tuned(settings: Settings, tmp_path) -> Settings:
    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    tuned.config.fetch.respect_robots = True
    return tuned


def write_history(tuned: Settings, host: str, reason: str, *, days_ago: int = 1) -> None:
    seen = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")
    preflight.path_for(tuned).write_text(
        yaml.safe_dump({"domains": {host: {"reason": reason, "count": 3, "last_seen": seen}}}),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------
# §8 test 16 -- the file is history, not law
# --------------------------------------------------------------------------


@respx.mock
async def test_domains_yaml_never_blocks_a_run(tuned: Settings) -> None:
    """§8 test 16, asserted at the only level that matters: the run still runs.

    Checked by processing the URL rather than by reading a flag, because the
    flag being False proves nothing about whether some caller consults it.
    """
    from haat_lister.fetch.static import build_client
    from haat_lister.models import Provenance, RowStatus
    from haat_lister.pipeline import process_url

    url = "https://refused.example/p/1"
    write_history(tuned, "refused.example", "bot_challenge")

    respx.get("https://refused.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
    )
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            html='<html><head><title>Kurta</title></head><body><h1>Kurta</h1>'
            "<button>Add to cart</button></body></html>",
            headers={"content-type": "text/html"},
        )
    )

    async with build_client(tuned) as client:
        record = await process_url(url, Provenance.OWN, tuned, client)

    assert record.status is not RowStatus.FAILED, "a history entry stopped a run"
    assert record.title.is_present


async def test_a_warning_can_never_be_blocking(tuned: Settings) -> None:
    """The property, stated where somebody adding a `skip` will trip over it."""
    warning = preflight.DomainWarning(
        host="refused.example", urls=9, source="history",
        reason="bot_challenge", detail="",
    )
    assert warning.blocking is False


@respx.mock
async def test_history_is_reported_before_the_run(tuned: Settings) -> None:
    """The whole point: the news arrives at second zero rather than minute four."""
    from haat_lister.fetch.static import build_client

    write_history(tuned, "refused.example", "bot_challenge")
    respx.get("https://refused.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
    )
    urls = [f"https://refused.example/p/{n}" for n in range(9)]

    async with build_client(tuned) as client:
        report = await preflight.check(urls, tuned, client)

    assert len(report.warnings) == 1
    warning = report.warnings[0]
    assert warning.source == "history"
    assert warning.urls == 9
    assert "may well have changed its mind" in warning.detail
    assert "will still try all of them" in report.summary()


@respx.mock
async def test_a_stale_observation_is_forgotten(tuned: Settings) -> None:
    """A site that refused us in the spring is not thereby refused forever."""
    from haat_lister.fetch.static import build_client

    write_history(tuned, "refused.example", "bot_challenge", days_ago=400)
    respx.get("https://refused.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
    )

    async with build_client(tuned) as client:
        report = await preflight.check(["https://refused.example/p/1"], tuned, client)

    assert report.warnings == []


# --------------------------------------------------------------------------
# robots, once per host
# --------------------------------------------------------------------------


@respx.mock
async def test_robots_is_read_once_per_host_not_once_per_url(tuned: Settings) -> None:
    """200 links from one shop is one request, not two hundred.

    Asking a site two hundred times whether we may read it would be its own
    small rudeness, and it is the shape of preflight that makes it tempting.
    """
    from haat_lister.fetch.static import build_client

    route = respx.get("https://shop.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
    )
    urls = [f"https://shop.example/p/{n}" for n in range(200)]

    async with build_client(tuned) as client:
        await preflight.check(urls, tuned, client)

    assert route.call_count == 1, f"robots.txt was fetched {route.call_count} times"


@respx.mock
async def test_a_disallowed_path_is_named_before_the_run(tuned: Settings) -> None:
    """robots is what the site SAYS -- current, authoritative, and a hard stop
    when the run reaches it. Saying so first costs one request."""
    from haat_lister.fetch.static import build_client

    respx.get("https://shop.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private/")
    )
    urls = ["https://shop.example/private/1", "https://shop.example/p/2"]

    async with build_client(tuned) as client:
        report = await preflight.check(urls, tuned, client)

    assert len(report.warnings) == 1
    assert report.warnings[0].source == "robots"
    assert report.warnings[0].urls == 1, "the allowed URL was swept up in the warning"
    assert "1 of these 2 paths" in report.warnings[0].detail


# --------------------------------------------------------------------------
# Writing observations down
# --------------------------------------------------------------------------


def test_only_refusals_are_recorded(tuned: Settings) -> None:
    """A timeout is a fact about a network on one afternoon.

    A `bot_challenge` is a fact about a decision the site has made, and only the
    second kind is worth telling somebody about a week later.
    """
    preflight.observe(tuned, "https://slow.example/p/1", "timeout_read")
    preflight.observe(tuned, "https://slow.example/p/2", "dns_failure")
    preflight.observe(tuned, "https://refused.example/p/1", "robots_disallowed")

    history = preflight.load_history(tuned)

    assert "slow.example" not in history
    assert history["refused.example"].reason == "robots_disallowed"
    assert history["refused.example"].count == 1


def test_repeated_refusals_accumulate(tuned: Settings) -> None:
    for n in range(3):
        preflight.observe(tuned, f"https://refused.example/p/{n}", "bot_challenge")

    assert preflight.load_history(tuned)["refused.example"].count == 3


def test_the_file_says_what_it_is(tuned: Settings) -> None:
    """It lives in the operator's working directory and they will open it."""
    preflight.observe(tuned, "https://refused.example/p/1", "bot_challenge")
    text = preflight.path_for(tuned).read_text(encoding="utf-8")

    assert "not a blocklist" in text
    assert "nothing here prevents a run" in text


def test_a_malformed_file_is_ignored_not_fatal(tuned: Settings) -> None:
    """The operator is meant to edit this, so it will sometimes be broken.

    The correct response to a malformed advisory file is to carry on without
    the advice -- refusing to run because a hint file has a stray tab would be
    the file becoming load-bearing by accident.
    """
    preflight.path_for(tuned).write_text("domains:\n  - this is: [not\n", encoding="utf-8")

    assert preflight.load_history(tuned) == {}

    # And it recovers: the next observation rewrites it.
    preflight.observe(tuned, "https://refused.example/p/1", "bot_challenge")
    assert "refused.example" in preflight.load_history(tuned)


def test_an_unknown_reason_in_the_file_is_not_treated_as_a_refusal(tuned: Settings) -> None:
    """Hand-edited, so somebody will write a word we have never emitted."""
    write_history(tuned, "odd.example", "the site was rude to me")
    history = preflight.load_history(tuned)

    assert history["odd.example"].refused is False


async def test_no_client_means_no_robots_but_still_a_report(tuned: Settings) -> None:
    """The UI asks for this before it has a client, and history alone is useful."""
    write_history(tuned, "refused.example", "bot_challenge")

    report = await preflight.check(["https://refused.example/p/1"], tuned, None)

    assert report.robots_checked == 0
    assert len(report.warnings) == 1
    assert report.warnings[0].source == "history"
