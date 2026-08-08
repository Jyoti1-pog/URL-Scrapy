"""Find photos: "why no photo?" for a catalogue instead of one URL.

`diagnose` answers one link beautifully and one link at a time. An operator with
two hundred products wants the same answer for all of them at once, before
committing to a run -- which photos exist, which are too small, which shops
refuse us.

WHAT THIS IS NOT: a job. It writes no `listings.csv`, touches no `master.csv`,
and cannot reach an image host. It is a read-only preview of what a real run
would produce, and the point of a preview is that nothing is committed.

THE ZERO-HOST-CALL GUARANTEE IS STRUCTURAL, not a flag. This module never
constructs an `ImageResolver` and never imports `images.hosts`. Tier 1 --
`validate_all_candidates` -- is the only image code it can reach, and Tier 1
cannot upload: that is the whole point of the Rule 1 gate. A test asserts the
absence rather than the behaviour, because an absent object cannot be reached by
a future edit that forgets a flag.

WHAT IT SHARES WITH A REAL RUN: the fetch ladder, robots, the page-shape check,
extraction, plugins, and the nine predicates. Same code, same order, same
answers -- so a find is worth trusting, and Phase 7's cache lets a subsequent
job reuse the work instead of paying for it twice.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from .config import Settings
from .extract.plugins import build_registry
from .fetch.rendered import build_renderer
from .fetch.static import build_client
from .images.reasons import NoImageReason, explain
from .images.validator import Tier1Validator, validate_all_candidates
from .models import ImageMethod, Provenance, RowStatus
from .pipeline import process_url
from .store.ledger import Ledger
from .utils.logging import get_logger
from .utils.netguard import BlockedHost
from .utils.robots import RobotsCache
from .utils.urls import canonicalise, host_of

log = get_logger(__name__)


class FoundPhoto(BaseModel):
    url: str
    ok: bool
    width: int | None = None
    height: int | None = None
    reason: str = ""


class FindRow(BaseModel):
    """One product, as a preview of what a run would produce.

    Carries both the answer and the evidence: `method` and `reason` are what an
    operator filters on, `photos` is what they look at, and `why_url` is the
    single-URL report for when neither is enough.
    """

    index: int
    source_url: str
    canonical_url: str = ""
    # Columns the operator's own CSV carried alongside the URL. Passed straight
    # through so their SKU or reference number rides along -- that mapping is
    # often the whole reason they have a CSV rather than a list of links.
    extra: dict[str, str] = Field(default_factory=dict)

    title: str = ""
    title_original: str = ""
    price: str = ""
    currency: str = ""
    category: str = ""
    description: str = ""
    weight_g: int | None = None
    dimensions: str = ""

    photos: list[FoundPhoto] = Field(default_factory=list)
    primary_image_url: str = ""
    image_count: int = 0
    width: int | None = None
    height: int | None = None

    method: str = ImageMethod.NONE.value
    reason: str = ""
    explanation: str = ""
    failed: bool = False
    from_cache: bool = False
    elapsed_ms: int = 0

    @property
    def has_photo(self) -> bool:
        return bool(self.primary_image_url)

    @property
    def low_res(self) -> bool:
        return self.method.endswith("_low_res")

    def all_image_urls(self) -> str:
        return " | ".join(photo.url for photo in self.photos)


@dataclass
class FindStats:
    total: int = 0
    done: int = 0
    with_photo: int = 0
    without_photo: int = 0
    low_res: int = 0
    failed: int = 0
    from_cache: int = 0
    # Asserted, not assumed. A find that made a host call would be a find that
    # cost money, and the operator was told it was a preview.
    host_calls: int = 0


class StopFinding:
    """Cancellation, shaped like `batch.StopSignal` so the API can treat both
    the same way."""

    def __init__(self) -> None:
        self._set = False

    def stop(self) -> None:
        self._set = True

    @property
    def is_set(self) -> bool:
        return self._set


RowFn = Callable[[FindRow], None]


@dataclass
class _Limiter:
    """One request per host at a time, jittered. The same politeness a job
    keeps -- a preview that hammers a shop is not a preview, it is a crawl."""

    delay: float
    jitter: float
    _last: dict[str, float] = field(default_factory=dict)
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def lock(self, host: str) -> asyncio.Lock:
        if host not in self._locks:
            self._locks[host] = asyncio.Lock()
        return self._locks[host]

    async def wait(self, host: str) -> None:
        loop = asyncio.get_running_loop()
        elapsed = loop.time() - self._last.get(host, 0.0)
        gap = self.delay + random.uniform(0, self.jitter)
        if elapsed < gap:
            await asyncio.sleep(gap - elapsed)
        self._last[host] = loop.time()


async def find_photos(
    urls: Sequence[str],
    settings: Settings,
    *,
    extras: Sequence[dict[str, str]] | None = None,
    concurrency: int = 4,
    ignore_robots: bool = False,
    render: bool | None = None,
    use_cache: bool = True,
    on_row: RowFn | None = None,
    stop: StopFinding | None = None,
) -> FindStats:
    """Resolve every URL's photos. Never writes a CSV, never calls a host.

    Rows are reported through `on_row` as they finish, in completion order --
    the caller re-orders for display. That is deliberate: a preview whose rows
    appear in input order would have to hold finished work back behind a slow
    shop, and the whole value here is seeing answers early.
    """
    cfg = settings.config
    stats = FindStats(total=len(urls))
    signal = StopFinding() if stop is None else stop
    limiter = _Limiter(cfg.fetch.per_domain_delay_s, cfg.fetch.per_domain_delay_jitter_s)
    extra_by_index = list(extras or [])

    with Ledger(settings.root / cfg.paths.ledger) as ledger:
        async with build_client(settings) as client:
            robots = (
                None
                if ignore_robots or not cfg.fetch.respect_robots
                else RobotsCache(client, settings.user_agent)
            )
            renderer = build_renderer(settings, render)
            plugins = build_registry(cfg, settings.root)
            # No ledger: a preview should not write hotlink-failure reputation
            # that a later real run would then act on without having measured it.
            validator = Tier1Validator(
                client,
                cfg.validator,
                None,
                allow_private_hosts=cfg.fetch.allow_private_hosts,
            )
            semaphore = asyncio.Semaphore(max(1, concurrency))

            async def one(index: int, url: str) -> None:
                if signal.is_set:
                    return
                async with semaphore:
                    if signal.is_set:
                        return
                    host = host_of(url)
                    async with limiter.lock(host):
                        await limiter.wait(host)
                        row = await _resolve_one(
                            index,
                            url,
                            settings,
                            client,
                            robots,
                            renderer,
                            plugins,
                            validator,
                            ledger,
                            use_cache=use_cache,
                        )
                if index < len(extra_by_index):
                    row.extra = extra_by_index[index]
                _tally(stats, row)
                if on_row is not None:
                    on_row(row)

            try:
                await asyncio.gather(*(one(i, u) for i, u in enumerate(urls)))
            finally:
                if renderer is not None and renderer.started:
                    await renderer.close()

    return stats


def _tally(stats: FindStats, row: FindRow) -> None:
    stats.done += 1
    if row.failed:
        stats.failed += 1
    elif row.has_photo:
        stats.with_photo += 1
        if row.low_res:
            stats.low_res += 1
    else:
        stats.without_photo += 1
    if row.from_cache:
        stats.from_cache += 1


async def _resolve_one(
    index: int,
    url: str,
    settings: Settings,
    client: object,
    robots: RobotsCache | None,
    renderer: object,
    plugins: object,
    validator: Tier1Validator,
    ledger: Ledger,
    *,
    use_cache: bool,
) -> FindRow:
    started = time.perf_counter()
    canonical = canonicalise(url, identity=settings.identity)

    if use_cache and (cached := ledger.find_cached(canonical)) is not None:
        row = FindRow.model_validate_json(cached)
        row.index = index
        row.source_url = url
        row.from_cache = True
        return row

    record = await process_url(
        url,
        Provenance.OWN,
        settings,
        client,  # type: ignore[arg-type]
        robots,
        renderer=renderer,  # type: ignore[arg-type]
        plugins=plugins,  # type: ignore[arg-type]
        # No resolver: that is the object that could download and upload, and a
        # preview must not be able to reach either. Tier 1 runs below instead.
        resolver=None,
    )

    row = FindRow(
        index=index,
        source_url=url,
        canonical_url=canonical,
        title=record.title.value or "",
        title_original=record.title_original,
        price="" if record.source_price is None else f"{record.source_price:g}",
        currency=record.source_currency or "",
        category=record.category_slug.value or "",
        description=(record.description.value or "")[:300],
        weight_g=record.weight_g.value,
        dimensions=_dimensions(record),
    )

    if record.status is RowStatus.FAILED:
        row.failed = True
        row.method = ImageMethod.NONE.value
        row.reason = record.failure_reason or "failed"
        row.explanation = explain(
            record.image.none_reason or record.failure_reason or ""
        )
        row.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return row

    if not record.image_candidates:
        row.reason = NoImageReason.NO_IMAGE_CANDIDATES.value
        row.explanation = explain(NoImageReason.NO_IMAGE_CANDIDATES)
        row.elapsed_ms = int((time.perf_counter() - started) * 1000)
        _remember(ledger, canonical, row)
        return row

    try:
        winner, results = await validate_all_candidates(record.image_candidates, validator)
    except BlockedHost as exc:
        row.reason = "blocked_address"
        row.explanation = str(exc).splitlines()[0]
        row.elapsed_ms = int((time.perf_counter() - started) * 1000)
        return row

    row.photos = [
        FoundPhoto(url=r.url, ok=r.ok, width=r.width, height=r.height, reason=r.reason)
        for r in results
    ]
    row.image_count = sum(1 for p in row.photos if p.ok)

    if winner is not None:
        row.primary_image_url = winner.url
        row.width, row.height = winner.width, winner.height
        row.method = ImageMethod.DIRECT.value
        row.reason = "direct_ok"
    else:
        # The same salvage a real run would apply, so the preview does not
        # promise less than the job delivers.
        from .images.pipeline import _best_low_res

        if (best := _best_low_res(results)) is not None:
            row.primary_image_url = best.url
            row.width, row.height = best.width, best.height
            row.method = ImageMethod.DIRECT_LOW_RES.value
            row.reason = f"below_standard:{best.width}x{best.height}"
            row.explanation = (
                f"The best photo is {best.width}x{best.height}, below haat's "
                f"{settings.config.validator.min_width}x"
                f"{settings.config.validator.min_height} standard."
            )
        else:
            row.reason = NoImageReason.ALL_CANDIDATES_REJECTED.value
            row.explanation = explain(NoImageReason.ALL_CANDIDATES_REJECTED)

    row.elapsed_ms = int((time.perf_counter() - started) * 1000)
    _remember(ledger, canonical, row)
    return row


def _dimensions(record: object) -> str:
    axes = [getattr(record, name).value for name in ("length_cm", "width_cm", "height_cm")]
    return " x ".join(str(a) for a in axes if a) if any(axes) else ""


def _remember(ledger: Ledger, canonical: str, row: FindRow) -> None:
    """Cache the answer so a later job reuses it instead of re-fetching.

    Failures are deliberately NOT cached: a shop that was down for ten minutes
    should not be written off for a week, and the cheapest way to be sure of
    that is never to remember a "no".
    """
    if row.failed:
        return
    try:
        ledger.remember_find(canonical, row.model_dump_json())
    except Exception:  # noqa: BLE001 -- a cache that fails is not a run that fails
        log.debug("could not cache find result for %s", canonical)
