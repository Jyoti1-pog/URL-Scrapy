"""§4.4 -- what we already know about these domains, said before the run starts.

An operator pastes 200 links and waits four minutes to be told that 180 of them
were from a host that refuses this tool. Every one of those refusals was already
known -- from `robots.txt`, which we can read in one request per domain, and
from the last time we ran, which we wrote down.

So this is not new knowledge. It is knowledge that was arriving too late to
change anyone's mind, which is the same as not having it.

TWO SOURCES, AND THEY ARE DIFFERENT KINDS OF FACT.

    robots.txt      what the site SAYS. Authoritative, current, one request.
    domains.yaml    what the site DID, last time we asked. History, not law.

`domains.yaml` NEVER PREVENTS A RUN, and that is a design constraint rather
than an oversight. It is a record of observations, and observations go stale:
a site that rate-limited us on a Monday afternoon is not thereby a site we may
never speak to again. A file that quietly became a blocklist would turn one bad
afternoon into a permanent decision that nobody made and nobody can see.

`robots_disallowed` remains a hard stop, but that stop lives in the fetcher
where it always did. Nothing here changes a terminal state; it changes what the
operator knows before they press the button.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from .images.reasons import REFUSED, NoImageReason, parse
from .utils.logging import get_logger
from .utils.urls import host_of

if TYPE_CHECKING:
    import httpx

    from .config import Settings

log = get_logger(__name__)

# How long an observation is worth mentioning. Long enough to be useful across
# a working week, short enough that a site which has since changed its mind is
# not still being described by what it did in the spring.
TTL = timedelta(days=30)

FILENAME = "domains.yaml"


@dataclass
class DomainHistory:
    """What one host did, last time we asked."""

    host: str
    reason: str = ""
    count: int = 0
    last_seen: str = ""

    @property
    def refused(self) -> bool:
        return (parsed := parse(self.reason)) is not None and parsed in REFUSED

    @property
    def stale(self) -> bool:
        try:
            seen = datetime.fromisoformat(self.last_seen)
        except (TypeError, ValueError):
            return True
        return datetime.now(UTC) - seen > TTL


@dataclass
class DomainWarning:
    """One thing worth saying before the run, and how sure we are of it."""

    host: str
    urls: int
    # "robots" (what the site says now) or "history" (what it did before).
    source: str
    reason: str
    detail: str

    @property
    def blocking(self) -> bool:
        """Always False. There is no path from this module to a refusal.

        Kept as an explicit property rather than left implicit so that anyone
        adding a `if warning.blocking: skip` finds a constant here and has to
        decide, in the open, to change it.
        """
        return False


@dataclass
class Preflight:
    """The whole answer: what we checked, and what we found."""

    total_urls: int = 0
    hosts: int = 0
    warnings: list[DomainWarning] = field(default_factory=list)
    robots_checked: int = 0
    robots_unreachable: list[str] = field(default_factory=list)

    @property
    def urls_at_risk(self) -> int:
        return sum(w.urls for w in self.warnings)

    def summary(self) -> str:
        if not self.warnings:
            return (
                f"{self.total_urls} URL(s) across {self.hosts} host(s); "
                "nothing known against them."
            )
        return (
            f"{self.urls_at_risk} of {self.total_urls} URL(s) are on hosts we already know "
            f"something about. The run will still try all of them."
        )


# ---------------------------------------------------------------------------
# domains.yaml
# ---------------------------------------------------------------------------


def path_for(settings: Settings) -> Path:
    return settings.root / FILENAME


def load_history(settings: Settings) -> dict[str, DomainHistory]:
    """Read observed refusals. A broken file is a warning, never a crash.

    This file is in the operator's working directory and they are meant to read
    and edit it. That means it will sometimes be malformed, and the correct
    response to a malformed advisory file is to carry on without the advice.
    """
    path = path_for(settings)
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        log.warning("Ignoring unreadable %s: %s", FILENAME, exc)
        return {}
    if not isinstance(data, dict):
        return {}

    history: dict[str, DomainHistory] = {}
    for host, entry in (data.get("domains") or {}).items():
        if not isinstance(entry, dict):
            continue
        record = DomainHistory(
            host=str(host),
            reason=str(entry.get("reason", "")),
            count=int(entry.get("count", 0) or 0),
            last_seen=str(entry.get("last_seen", "")),
        )
        if not record.stale:
            history[record.host] = record
    return history


def observe(settings: Settings, url: str, reason: str) -> None:
    """Write down that this host refused us, for next time's preflight.

    Only refusals are recorded. A timeout is a fact about a network on one
    afternoon; a `robots_disallowed` or a `bot_challenge` is a fact about a
    decision the site has made, and only the second kind is worth telling
    somebody about a week later.
    """
    parsed = parse(reason)
    if parsed is None or parsed not in REFUSED:
        return
    host = host_of(url)
    if not host:
        return

    path = path_for(settings)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, yaml.YAMLError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    domains = data.setdefault("domains", {})
    if not isinstance(domains, dict):  # pragma: no cover -- hand-edited file
        domains = data["domains"] = {}

    existing = domains.get(host)
    entry: dict = existing if isinstance(existing, dict) else {}
    domains[host] = {
        "reason": parsed.value,
        "count": int(entry.get("count", 0) or 0) + 1,
        "last_seen": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    data.setdefault(
        "_note",
        "Observed refusals, for the preflight warning. This is history, not a "
        "blocklist -- nothing here prevents a run. Delete an entry to forget it.",
    )
    try:
        path.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8")
    except OSError as exc:  # pragma: no cover -- read-only working directory
        log.warning("Could not write %s: %s", FILENAME, exc)


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------


async def check(
    urls: list[str],
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    *,
    consult_robots: bool = True,
) -> Preflight:
    """What is worth saying about these URLs before the run.

    robots.txt is fetched once per HOST, not once per URL -- on a 200-link paste
    from one shop that is one request rather than two hundred, and asking a site
    two hundred times whether we may read it would be its own small rudeness.
    """
    from .utils.robots import RobotsCache

    by_host: dict[str, list[str]] = {}
    for url in urls:
        if host := host_of(url):
            by_host.setdefault(host, []).append(url)

    report = Preflight(total_urls=len(urls), hosts=len(by_host))
    history = load_history(settings)

    robots = (
        RobotsCache(client, settings.user_agent)
        if client is not None and consult_robots and settings.config.fetch.respect_robots
        else None
    )

    for host, host_urls in sorted(by_host.items()):
        # --- what the site says now ---------------------------------------
        if robots is not None:
            disallowed = [u for u in host_urls if not await robots.allowed(u)]
            report.robots_checked += 1
            if disallowed:
                report.warnings.append(
                    DomainWarning(
                        host=host,
                        urls=len(disallowed),
                        source="robots",
                        reason=NoImageReason.ROBOTS_DISALLOWED.value,
                        detail=(
                            f"{host}/robots.txt disallows "
                            + (
                                "these paths for any crawler."
                                if len(disallowed) == len(host_urls)
                                else f"{len(disallowed)} of these {len(host_urls)} paths."
                            )
                        ),
                    )
                )
                continue

        # --- what it did last time ----------------------------------------
        if (seen := history.get(host)) is not None and seen.refused:
            report.warnings.append(
                DomainWarning(
                    host=host,
                    urls=len(host_urls),
                    source="history",
                    reason=seen.reason,
                    detail=(
                        f"{host} answered {seen.reason} {seen.count}x, most recently "
                        f"{seen.last_seen[:10]}. It may well have changed its mind -- "
                        "this run will ask again."
                    ),
                )
            )

    return report


def counts_by_reason(report: Preflight) -> list[tuple[str, int]]:
    """For the UI, which groups the warning list rather than listing 40 hosts."""
    counter: Counter[str] = Counter()
    for warning in report.warnings:
        counter[warning.reason] += warning.urls
    return counter.most_common()
