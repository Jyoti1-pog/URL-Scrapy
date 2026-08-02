"""Batch mode: politeness, scale, and surviving a bad row.

Reshaped for v2. Ordering, accounting, duplicate collapsing, cancel and resume
all moved to `test_job_output.py`, where they can be asserted against the job
model that now owns them -- keeping a second copy here would have meant two
files disagreeing about what resume means the first time it changed.

What stays is what the job model does not touch: that `--concurrency 5` never
means five hits on one host, that a site's own Crawl-delay wins, that 5,000 rows
do not accumulate in memory, and that one crashing row does not end a run.
"""

from __future__ import annotations

import asyncio
import csv
import time
from pathlib import Path

import pytest

from haat_lister.batch import BatchOptions, BatchRunner, DomainLimiter
from haat_lister.config import Settings
from haat_lister.jobs import job_paths, plan_urls
from haat_lister.models import (
    Confidence,
    FieldSource,
    FieldValue,
    ImageMethod,
    ImageMode,
    ProductRecord,
    Provenance,
    RowStatus,
)
from haat_lister.pipeline import new_record
from haat_lister.store.ledger import Ledger

# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


@pytest.fixture
def batch_settings(settings: Settings, tmp_path: Path) -> Settings:
    """The real config, rooted somewhere disposable.

    Politeness delays are zeroed: this suite asserts the *ordering* the limiter
    enforces, and a test that also waited two real seconds per row would take
    three hours to say the same thing. The one test that cares about spacing
    sets its own delay.
    """
    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    tuned.config.fetch.per_domain_delay_s = 0.0
    tuned.config.fetch.per_domain_delay_jitter_s = 0.0
    return tuned


def make_record(url: str, *, title: str = "Hand-block-printed cotton kurta") -> ProductRecord:
    record = new_record(url, Provenance.OWN)
    record.title = FieldValue.found(title, FieldSource.JSONLD, Confidence.HIGH)
    record.description = FieldValue.found(
        "Hand-embroidered in Kutch on handloom cotton.", FieldSource.JSONLD, Confidence.HIGH
    )
    record.category_slug = FieldValue.found("apparel", FieldSource.INFERRED, Confidence.HIGH)
    record.subcategory_slug = FieldValue.found(
        "womens-fashion", FieldSource.INFERRED, Confidence.HIGH
    )
    record.image.method = ImageMethod.DIRECT
    record.image.reason = "direct_ok"
    return record


class FakeProcessor:
    """Stands in for `pipeline.process_url`, and remembers exactly what it saw."""

    def __init__(
        self,
        *,
        work_s: float = 0.0,
        fail_for: set[str] | None = None,
        crash_for: set[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.windows: list[tuple[str, float, float]] = []
        self._work_s = work_s
        self._fail_for = fail_for or set()
        self._crash_for = crash_for or set()

    async def __call__(
        self,
        url: str,
        provenance: Provenance = Provenance.OWN,
        *_: object,
        **__: object,
    ) -> ProductRecord:
        started = time.monotonic()
        self.calls.append(url)
        if self._work_s:
            await asyncio.sleep(self._work_s)
        else:
            await asyncio.sleep(0)
        self.windows.append((url, started, time.monotonic()))

        if url in self._crash_for:
            raise RuntimeError("selectolax segfaulted, allegedly")

        record = make_record(url)
        if url in self._fail_for:
            record.fail("http_503")
            record.note("Stage A fetch failed: the shop was down.")
        return record


def run_batch(
    settings: Settings,
    urls: list[str],
    *,
    process: FakeProcessor,
    concurrency: int = 5,
    on_row: object = None,
    checkpoint_every: int = 250,
    limiter: DomainLimiter | None = None,
):
    options = BatchOptions(
        provenance=Provenance.OWN,
        image_mode=ImageMode.MANIFEST,
        concurrency=concurrency,
        checkpoint_every=checkpoint_every,
    )

    async def go():
        with Ledger(settings.root / settings.config.paths.ledger) as ledger:
            runner = BatchRunner(
                settings,
                options,
                ledger=ledger,
                client=object(),  # the fake processor never touches it
                process=process,
                on_row=on_row,  # type: ignore[arg-type]
                limiter=limiter
                or DomainLimiter(per_domain_concurrency=1, delay_s=0.0, jitter_s=0.0),
            )
            return await runner.run(plan_urls(urls))

    return asyncio.run(go())


def listings_rows(settings: Settings, job_id: str) -> list[dict[str, str]]:
    path = job_paths(settings, job_id).listings
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# --------------------------------------------------------------------------
# Politeness
# --------------------------------------------------------------------------


def test_concurrency_is_a_batch_budget_not_a_per_host_one(batch_settings: Settings) -> None:
    """`--concurrency 5` against one shop is one request at a time.

    Asserted by overlap: with per-domain concurrency 1, no two windows on the
    same host may intersect. Five different hosts, meanwhile, must overlap --
    otherwise the limiter is just a sequential loop wearing a semaphore.
    """
    same_host = [f"https://oneshop.example/p/{i}" for i in range(8)]
    process = FakeProcessor(work_s=0.02)
    run_batch(batch_settings, same_host, process=process, concurrency=5)

    windows = sorted(process.windows, key=lambda w: w[1])
    for earlier, later in zip(windows, windows[1:], strict=False):
        assert earlier[2] <= later[1] + 1e-6, f"{earlier[0]} overlapped {later[0]} on one host"

    spread = [f"https://shop{i}.example/p/1" for i in range(5)]
    parallel = FakeProcessor(work_s=0.05)
    started = time.monotonic()
    run_batch(batch_settings, spread, process=parallel, concurrency=5)
    assert time.monotonic() - started < 0.3, "five separate hosts ran sequentially"


def test_per_domain_delay_spaces_requests_to_the_same_host() -> None:
    limiter = DomainLimiter(per_domain_concurrency=1, delay_s=0.05, jitter_s=0.0)
    stamps: list[float] = []

    async def go() -> None:
        async def hit() -> None:
            async with limiter.slot("https://shop.example/p/1"):
                stamps.append(time.monotonic())

        await asyncio.gather(hit(), hit(), hit())

    asyncio.run(go())
    gaps = [b - a for a, b in zip(stamps, stamps[1:], strict=False)]
    assert all(gap >= 0.045 for gap in gaps), gaps


def test_a_sites_own_crawl_delay_wins_when_it_is_longer() -> None:
    """Our configured delay is a floor on politeness, never a ceiling."""

    class StatedDelay:
        def __init__(self, seconds: float | None) -> None:
            self.seconds = seconds
            self.asked = 0

        async def crawl_delay(self, url: str) -> float | None:
            self.asked += 1
            return self.seconds

    slower = StatedDelay(0.06)
    limiter = DomainLimiter(delay_s=0.0, jitter_s=0.0, robots=slower)  # type: ignore[arg-type]
    stamps: list[float] = []

    async def go(lim: DomainLimiter) -> None:
        for _ in range(3):
            async with lim.slot("https://slowshop.example/p/1"):
                stamps.append(time.monotonic())

    asyncio.run(go(limiter))
    assert stamps[-1] - stamps[0] >= 0.11
    assert slower.asked == 1, "robots.txt should be consulted once per domain, not once per row"

    faster = StatedDelay(0.001)
    lenient = DomainLimiter(delay_s=0.05, jitter_s=0.0, robots=faster)  # type: ignore[arg-type]
    stamps.clear()
    asyncio.run(go(lenient))
    assert stamps[-1] - stamps[0] >= 0.09, "a shorter Crawl-delay must not speed us up"


# --------------------------------------------------------------------------
# Scale
# --------------------------------------------------------------------------


def test_batch_5000_streams(batch_settings: Settings) -> None:
    """Five thousand rows complete, and they leave memory as they are made.

    Two things asserted rather than assumed. That no more rows are ever in
    flight than the concurrency allows -- the runner never reads ahead. And that
    listings.csv on disk is already most of the way full while the run is still
    going, which is only possible if rows are written and dropped instead of
    collected.

    Note what v2 changed: the *input* is no longer streamed, because the
    accounting guarantee needs the whole input set up front. That costs one URL
    string per line -- bounded, and nothing next to a ProductRecord.
    """
    urls = [f"https://shop{i % 250}.example/p/{i}" for i in range(5000)]
    on_disk_midway: list[int] = []
    holder: dict[str, Path] = {}

    def on_row(stats: object) -> None:
        if stats.processed == 3000:  # type: ignore[attr-defined]
            path = holder["listings"]
            with path.open("r", encoding="utf-8", newline="") as handle:
                on_disk_midway.append(sum(1 for _ in handle) - 1)

    options = BatchOptions(
        provenance=Provenance.OWN,
        image_mode=ImageMode.MANIFEST,
        concurrency=8,
        checkpoint_every=250,
    )
    process = FakeProcessor()

    async def go():
        with Ledger(batch_settings.root / batch_settings.config.paths.ledger) as ledger:
            runner = BatchRunner(
                batch_settings,
                options,
                ledger=ledger,
                client=object(),
                process=process,
                on_row=on_row,  # type: ignore[arg-type]
                limiter=DomainLimiter(per_domain_concurrency=1, delay_s=0.0, jitter_s=0.0),
            )
            holder["listings"] = runner.paths.listings
            return await runner.run(plan_urls(urls))

    stats = asyncio.run(go())

    assert stats.seen == 5000
    assert stats.written == 5000
    assert stats.failed == 0
    assert len(process.calls) == 5000
    assert stats.peak_in_flight <= 8, "the runner read ahead of its own workers"
    assert on_disk_midway and on_disk_midway[0] >= 2500, (
        f"only {on_disk_midway} rows were durable 3,000 rows in -- output is not streaming"
    )

    with holder["listings"].open("r", encoding="utf-8", newline="") as handle:
        assert sum(1 for _ in handle) == 5001  # header + 5,000


# --------------------------------------------------------------------------
# Surviving a bad row
# --------------------------------------------------------------------------


def test_one_crashing_row_does_not_end_the_batch(batch_settings: Settings) -> None:
    urls = [f"https://shop{i}.example/p/{i}" for i in range(5)]
    process = FakeProcessor(crash_for={urls[2]})

    stats = run_batch(batch_settings, urls, process=process, concurrency=2)

    assert stats.processed == 5
    assert stats.written == 4
    assert stats.failed == 1

    review = job_paths(batch_settings, stats.job_id).review
    with review.open("r", encoding="utf-8", newline="") as handle:
        crashed = [r for r in csv.DictReader(handle) if r["status"] == RowStatus.FAILED.value]
    assert len(crashed) == 1
    assert "crashed the pipeline" in crashed[0]["notes"]


def test_a_crashed_row_is_still_in_the_ledger(batch_settings: Settings) -> None:
    """So the job can re-emit it, and so resume knows the URL was attempted."""
    url = "https://shop.example/p/1"
    stats = run_batch(batch_settings, [url], process=FakeProcessor(crash_for={url}))

    with Ledger(batch_settings.root / batch_settings.config.paths.ledger) as ledger:
        payloads = ledger.all_rows(stats.job_id)
        assert len(payloads) == 1
        assert ledger.completed_urls(stats.job_id) == set(), (
            "a failure must not count as completed"
        )

    assert ProductRecord.model_validate_json(payloads[0]).status is RowStatus.FAILED
    assert listings_rows(batch_settings, stats.job_id) == []
