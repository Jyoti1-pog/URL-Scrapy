"""Batch mode: many URLs, politely, restartably.

`single` and `batch` run the SAME per-URL coroutine. Everything here is
scheduling, politeness and bookkeeping around `pipeline.process_url`; there is no
second extraction path that could quietly drift from the first.

Three properties this module exists to guarantee.

**Politeness.** `--concurrency 5` is a budget across the batch, never five
simultaneous hits on one host. A per-domain semaphore (default 1) and a jittered
per-domain delay sit between the worker pool and the network, and a site's own
`Crawl-delay` wins whenever it is longer than ours. Feeding a batch 5,000 URLs
from one shop gets that shop one polite request at a time -- the concurrency only
pays off across domains, which is the only place it is ours to spend.

**Memory.** Records are the expensive object: description text, per-candidate
validation results, image files. None are accumulated -- each is committed to
the ledger and dropped. What is held is the plan (one URL string per input
line) and, briefly, the rendered CSV lines of rows that finished ahead of the
watermark. Both are strings; neither is a record.

v2 gave up streaming the *input* to get the accounting guarantee: every pasted
line needs a `job_urls` row before any work starts, or "every URL ended up
somewhere" cannot be asserted at the end. At the 10,000-line cap that is about a
megabyte.

**Restartability.** Every processed row is committed to the ledger immediately
and `listings.csv` is checkpointed to disk periodically, so an interrupt costs at
most the rows in flight. Ctrl-C sets a flag rather than raising: an interrupt
that unwound through the writers would discard the `.tmp` and lose the whole
run's CSV, which is the exact opposite of what the operator pressing it wants.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from . import preflight
from .config import Settings
from .enrich.rewrite import LlmEnricher
from .extract.plugins import PluginRegistry
from .fetch.rendered import Renderer
from .images.pipeline import ImageResolver
from .jobs import (
    FAILED,
    NEEDS_HUMAN,
    REFUSED,
    JobPlan,
    add_to_master,
    assert_accounted,
    job_paths,
    new_job_id,
    render_projections,
    terminal_state,
    update_job_json,
    write_job_json,
)
from .models import DescriptionMode, ImageMode, ProductRecord, Provenance, RowStatus
from .output.csv_writer import cell_depths
from .output.master import MasterStats
from .output.ordered_writer import OrderedCsvWriter
from .output.review_writer import missing_required, needs_review
from .pipeline import new_record, process_url
from .store.ledger import Ledger
from .utils.logging import get_logger
from .utils.robots import RobotsCache
from .utils.urls import host_of

log = get_logger(__name__)

# How often listings.csv is made durable. Small enough that an interrupted run
# loses little, large enough that the copy-forward stays invisible.
CHECKPOINT_EVERY = 250

# `process_url`'s signature, kept loose so tests can inject a stand-in without
# reproducing seven positional parameters.
ProcessFn = Callable[..., Awaitable[ProductRecord]]



class StopSignal:
    """A flag, not an exception.

    SIGINT handled the usual way raises KeyboardInterrupt wherever the
    interpreter happens to be -- including inside the writers, whose unwind path
    deliberately abandons the `.tmp` file to protect the operator's existing
    CSV. Correct for a crash, terrible for a deliberate stop. So Ctrl-C sets this
    instead, the producer stops feeding, in-flight rows finish, and the run exits
    through the normal path with its output committed.
    """

    def __init__(self) -> None:
        self._raised = False

    def set(self) -> None:
        self._raised = True

    def __bool__(self) -> bool:
        return self._raised


class DomainLimiter:
    """Per-domain concurrency and spacing. The only thing that touches the clock.

    Note what is deliberately absent: any notion of speeding up for a host that
    seems to tolerate it. The delay is the operator's stated politeness budget
    and a site's own robots.txt can raise it, never lower it.
    """

    def __init__(
        self,
        per_domain_concurrency: int = 1,
        delay_s: float = 2.0,
        jitter_s: float = 0.75,
        robots: RobotsCache | None = None,
    ) -> None:
        self._per_domain = max(1, per_domain_concurrency)
        self._delay_s = delay_s
        self._jitter_s = jitter_s
        self._robots = robots
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._delays: dict[str, float] = {}
        self._next_at: dict[str, float] = {}

    def _semaphore(self, domain: str) -> asyncio.Semaphore:
        if domain not in self._semaphores:
            self._semaphores[domain] = asyncio.Semaphore(self._per_domain)
        return self._semaphores[domain]

    async def _delay_for(self, domain: str, url: str) -> float:
        """Resolved once per domain, inside the semaphore.

        Being inside the semaphore matters: it means the first worker to reach a
        domain is the only one that can trigger the robots.txt fetch, so a batch
        of 500 URLs from one shop asks for its robots.txt once.
        """
        if domain in self._delays:
            return self._delays[domain]

        delay = self._delay_s
        if self._robots is not None:
            stated = await self._robots.crawl_delay(url)
            if stated is not None and stated > delay:
                log.info("%s asks for Crawl-delay %.1fs; using theirs over ours", domain, stated)
                delay = stated

        self._delays[domain] = delay
        return delay

    async def _await_turn(self, domain: str) -> None:
        while (wait := self._next_at.get(domain, 0.0) - time.monotonic()) > 0:
            await asyncio.sleep(wait)

    @asynccontextmanager
    async def slot(self, url: str) -> AsyncIterator[None]:
        """Hold a domain's slot for the whole of one row.

        Image downloads happen inside this, under the *page's* domain rather than
        the CDN's. Conservative -- a CDN could take more -- but a shop and its
        image host are usually the same capacity, and one clock per row is a lot
        easier to reason about than two.
        """
        domain = host_of(url) or url
        async with self._semaphore(domain):
            delay = await self._delay_for(domain, url)
            await self._await_turn(domain)
            try:
                yield
            finally:
                self._next_at[domain] = (
                    time.monotonic() + delay + random.uniform(0, self._jitter_s)
                )


@dataclass
class BatchStats:
    """What the run did, in the terms an operator asks about afterwards."""

    seen: int = 0
    processed: int = 0
    written: int = 0
    needs_review: int = 0
    failed: int = 0
    skipped_resume: int = 0
    skipped_duplicate_in_file: int = 0
    skipped_already_listed: int = 0
    job_id: str = ""
    invalid: int = 0
    failed_written: int = 0
    not_started: int = 0
    peak_pending: int = 0
    stalled_on: int | None = None
    host_calls: int = 0
    pages_rendered: int = 0
    llm_calls: int = 0
    llm_cache_hits: int = 0
    stopped_early: bool = False
    peak_in_flight: int = 0

    # The site declined and the tool was correct to stop. Counted apart from
    # `failed` because they are not degrees of one thing: a refusal cannot be
    # retried into a success, and reporting them together made a working robots
    # check look like a bug in the fetcher.
    refused: int = 0

    # What the accumulating sheet did with this job's rows, and why not if it
    # could not. `None` means master was off for this run -- an absent object
    # rather than a zeroed one, so "off" and "added nothing" cannot be confused.
    master: MasterStats | None = None
    master_error: str = ""


@dataclass
class BatchOptions:
    provenance: Provenance
    image_mode: ImageMode
    job_id: str = ""
    concurrency: int = 5
    resume: bool = False
    seller_note: str | None = None
    description_mode: DescriptionMode = DescriptionMode.RAW
    checkpoint_every: int = CHECKPOINT_EVERY

    # Fold this job's rows into runs/master.csv when it completes.
    #
    # The default differs by caller and that is deliberate, not an oversight:
    # the web operator's mental model is one sheet that fills up, so the console
    # passes True. A scripted CLI user is usually building one file for one
    # purpose and wants isolation, so `batch` passes this only when asked.
    master: bool = False
    on_duplicate: str = "skip"


class BatchRunner:
    """Streams URLs in, streams rows out, holds neither.

    In v2 the runner drives a **job**: it is handed a plan -- already parsed,
    canonicalised and deduped by `jobs.plan_urls` -- rather than raw lines, and
    every input index it is given has an outcome before it returns. The ordering
    guarantee itself lives in OrderedCsvWriter; what happens here is that each
    completed row is committed to the ledger *first*, so the file downstream of
    it can always be rebuilt.
    """

    def __init__(
        self,
        settings: Settings,
        options: BatchOptions,
        *,
        ledger: Ledger,
        client: httpx.AsyncClient,
        resolver: ImageResolver | None = None,
        renderer: Renderer | None = None,
        plugins: PluginRegistry | None = None,
        enricher: LlmEnricher | None = None,
        robots: RobotsCache | None = None,
        limiter: DomainLimiter | None = None,
        stop: StopSignal | None = None,
        process: ProcessFn = process_url,
        on_row: Callable[[BatchStats], None] | None = None,
        on_event: Callable[..., None] | None = None,
    ) -> None:
        self._settings = settings
        self._options = options
        self._ledger = ledger
        self._client = client
        self._resolver = resolver
        self._renderer = renderer
        self._plugins = plugins
        self._enricher = enricher
        self._robots = robots
        self._process = process
        self._on_row = on_row
        # (index, name, **fields). The console turns these into SSE frames; the
        # CLI ignores them. Neither knows about the other.
        self._on_event = on_event or (lambda *a, **k: None)
        # Not `stop or StopSignal()`: StopSignal defines __bool__, so an unset
        # one is falsy and would be silently replaced by a fresh object the
        # caller has no handle on.
        self.stop = StopSignal() if stop is None else stop
        self.stats = BatchStats()
        self.job_id = options.job_id or new_job_id()
        self.paths = job_paths(settings, self.job_id)

        cfg = settings.config.fetch
        self._limiter = limiter or DomainLimiter(
            per_domain_concurrency=cfg.per_domain_concurrency,
            delay_s=cfg.per_domain_delay_s,
            jitter_s=cfg.per_domain_delay_jitter_s,
            robots=robots,
        )
        self._in_flight = 0

    # -- the run -----------------------------------------------------------

    async def run(self, plan: JobPlan) -> BatchStats:
        """Process a plan. Returns only once every input index has an outcome."""
        options = self._options
        self.stats.job_id = self.job_id
        self.stats.seen = plan.pasted
        self.stats.skipped_duplicate_in_file = plan.duplicates
        self.stats.invalid = len(plan.invalid)

        resuming = self._ledger.job(self.job_id) is not None
        if not resuming:
            self._register(plan)

        # Rows this job already finished, from an interrupted earlier attempt.
        # Replayed through the writer from the ledger rather than refetched --
        # that is what makes resume cost nothing for work already done.
        done: dict[int, ProductRecord] = {}
        if resuming or options.resume:
            done = self._already_done(plan)
            self.stats.skipped_resume = len(done)

        todo = [u for u in plan.accepted if u.index not in done]
        queue: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue(
            maxsize=max(1, options.concurrency) * 2
        )

        self.paths.root.mkdir(parents=True, exist_ok=True)
        with OrderedCsvWriter(
            self.paths.listings,
            self._settings.config,
            options.image_mode,
            checkpoint_every=options.checkpoint_every,
        ) as listings:
            # An index that will never produce a line still has to advance the
            # watermark, or one duplicate dams every row behind it.
            for entry in plan.urls:
                if not entry.will_run:
                    listings.skip(entry.index)
            for index, record in sorted(done.items()):
                listings.add(index, record)

            workers = [
                asyncio.create_task(self._worker(queue, listings), name=f"batch-{i}")
                for i in range(max(1, options.concurrency))
            ]
            try:
                for entry in todo:
                    if self.stop:
                        self.stats.stopped_early = True
                        log.warning("Stop requested; the rest of the plan is untouched")
                        break
                    await queue.put((entry.index, entry.raw))
            finally:
                # Workers always drain to their sentinel, even when stopping, so
                # the producer can never be left blocked on a full queue.
                for _ in workers:
                    await queue.put(None)
                await asyncio.gather(*workers)

            self.stats.stalled_on = listings.stalled_on
            self.stats.peak_pending = listings.peak_pending
            self.stats.written = listings.written

        self._finalise(plan)
        return self.stats

    def _register(self, plan: JobPlan) -> None:
        """Every pasted line gets a job_urls row before any work starts."""
        self._ledger.create_job(
            self.job_id,
            json.dumps(self._settings_snapshot()),
            [(u.index, u.raw, u.canonical) for u in plan.urls],
        )
        # Collapsed and malformed lines are resolved immediately: they are known
        # outcomes, not pending ones, and leaving them NULL would turn the
        # accounting assertion into a question about timing.
        for entry in plan.urls:
            if not entry.will_run:
                self._ledger.set_outcome(
                    self.job_id, entry.index, entry.status, reason=entry.note
                )

    def _settings_snapshot(self) -> dict[str, object]:
        options = self._options
        return {
            "provenance": options.provenance.value,
            "images": options.image_mode.value,
            "description_mode": options.description_mode.value,
            "concurrency": options.concurrency,
            "seller_note": options.seller_note,
            "price_strategy": self._settings.config.price.strategy.value,
            "render_enabled": self._settings.config.render.enabled,
            "llm": self._enricher is not None and self._enricher.enabled,
        }

    def _already_done(self, plan: JobPlan) -> dict[int, ProductRecord]:
        """Rows this job has already produced, by input index.

        Failures are deliberately absent. A row that failed on a timeout failed
        because of the network rather than the page, and a resume that wrote
        those off permanently would turn one bad minute into a hole in the
        catalogue.
        """
        by_url = {u.canonical: u.index for u in plan.accepted}
        done: dict[int, ProductRecord] = {}
        for payload in self._ledger.iter_payloads(self.job_id):
            record = ProductRecord.model_validate_json(payload)
            if record.status is RowStatus.FAILED:
                continue
            if (index := by_url.get(record.canonical_url)) is not None:
                done[index] = record
        return done

    async def _worker(
        self, queue: asyncio.Queue[tuple[int, str] | None], listings: OrderedCsvWriter
    ) -> None:
        while True:
            item = await queue.get()
            if item is None:
                return
            index, url = item
            if self.stop:
                # Drain without working: exiting here could strand the producer
                # on a full queue. This branch, not the producer's, is what
                # makes a stop visible -- with a short plan the producer can
                # have read every URL before the signal ever arrives, and a run
                # that silently dropped the queue would still report success.
                self.stats.stopped_early = True
                self.stats.not_started += 1
                continue

            self._in_flight += 1
            self.stats.peak_in_flight = max(self.stats.peak_in_flight, self._in_flight)
            self._on_event("row_started", index=index, url=url)
            try:
                record = await self._process_one(index, url)
                self._emit(index, record, listings)
            finally:
                self._in_flight -= 1

    async def _process_one(self, index: int, url: str) -> ProductRecord:
        options = self._options

        def stage(name: str) -> None:
            self._on_event("row_stage", index=index, url=url, stage=name)

        try:
            async with self._limiter.slot(url):
                # By keyword: `process_url` has grown a parameter in each of the
                # last three phases, and a positional call here would keep
                # silently reassigning them.
                return await self._process(
                    url,
                    options.provenance,
                    self._settings,
                    self._client,
                    robots=self._robots,
                    seller_note=options.seller_note,
                    description_mode=options.description_mode,
                    resolver=self._resolver,
                    renderer=self._renderer,
                    plugins=self._plugins,
                    enricher=self._enricher,
                    on_stage=stage,
                )
        except Exception as exc:  # noqa: BLE001 -- one bad row must not end the batch
            log.exception("Unhandled error processing %s", url)
            record = new_record(url, options.provenance, self._settings.identity)
            record.fail("internal_error")
            record.flag(
                f"This row crashed the pipeline: {exc!r}. The batch carried on; this URL "
                "produced no listing and needs looking at by hand."
            )
            return record

    # -- output ------------------------------------------------------------

    def _emit(self, index: int, record: ProductRecord, listings: OrderedCsvWriter) -> None:
        """Commit to the ledger, then offer the row to the writer.

        That order is the whole design. The ledger is the source of truth and
        the file is a projection of it, so a crash between the two loses a line
        that can be regenerated, never a row that cannot.

        No lock: asyncio runs one coroutine at a time and nothing below awaits,
        so this method is atomic with respect to the other workers.
        """
        self.stats.processed += 1
        self._ledger.record_row(record, self.job_id, index)

        # Which of the four terminal states this row reached. Decided in one
        # place so the counts, the files and the retry button cannot disagree
        # about what happened -- they did, and three inputs produced six rows.
        outcome = terminal_state(record, self._settings.config)
        self._ledger.set_outcome(
            self.job_id,
            index,
            outcome,
            row_key=record.row_key,
            reason=record.failure_reason,
        )
        if outcome == REFUSED:
            self.stats.refused += 1
            # §4.4. Written down so the NEXT run can say so before it starts,
            # rather than after four minutes. History, not a blocklist -- see
            # `preflight.observe`, which records refusals only.
            preflight.observe(self._settings, record.source_url, record.failure_reason or "")
        elif outcome == FAILED:
            self.stats.failed += 1
        elif outcome == NEEDS_HUMAN:
            self.stats.needs_review += 1

        listings.add(index, record)

        # image_tier on the row event, per section 7: Rule 1's direct-vs-hosted
        # ratio should be watchable while it happens, not only in the summary.
        self._on_event(
            "row_failed" if outcome in (REFUSED, FAILED) else "row_done",
            index=index,
            url=record.source_url,
            row_key=record.row_key,
            title=record.title.value or "",
            status=record.status.value,
            outcome=outcome,
            image_tier=record.image.method.value,
            reason=record.failure_reason or record.image.reason,
            notes=len(record.notes),
            # The grid fills from the event, not from a poll: a cell landing
            # three seconds after the row it belongs to would make the one
            # animation on screen a lie about when the work happened.
            cells=cell_depths(record),
            needs_human=needs_review(record, self._settings.config),
            missing=missing_required(record, self._settings.config),
        )
        self._on_event(
            "job_progress",
            processed=self.stats.processed,
            written=self.stats.written or listings.written,
            failed=self.stats.failed,
            total=self.stats.seen - self.stats.skipped_duplicate_in_file - self.stats.invalid,
        )
        if self._on_row is not None:
            self._on_row(self.stats)

    def _add_to_master(self) -> None:
        """Never fails the job. The sheet is a convenience built from files that
        are already safely written; a locked master.csv must not cost an
        operator the run that produced it."""
        from .output.master import SheetLocked

        try:
            self.stats.master = add_to_master(
                self._ledger,
                self.job_id,
                self._settings,
                self._options.image_mode,
                self._options.on_duplicate,
            )
        except SheetLocked as exc:
            self.stats.master_error = str(exc)
            log.warning("master.csv not updated: %s", exc)
        except Exception as exc:  # noqa: BLE001 -- the job itself is finished and valid
            self.stats.master_error = f"master.csv could not be updated: {exc}"
            log.exception("master.csv could not be updated for %s", self.job_id)

    def _finalise(self, plan: JobPlan) -> None:
        """Account for everything, then render the three projection files."""
        # A stop leaves URLs that were never begun. From the operator's side
        # nothing came back for them, and failed.csv is the file they will paste
        # into the next job -- so that is where they belong.
        for index in self._ledger.unaccounted(self.job_id):
            self._ledger.set_outcome(self.job_id, index, FAILED, reason="not_started")

        state = "cancelled" if self.stats.stopped_early else "done"
        self._ledger.set_job_state(self.job_id, state)

        counts = assert_accounted(self._ledger, self.job_id)
        rendered = render_projections(
            self._ledger, self.job_id, self.paths, self._settings, self._options.image_mode
        )
        self.stats.needs_review = rendered["review"]
        self.stats.failed_written = rendered["failed"]

        # The sheet, on completion only. A cancelled job's rows are real and
        # downloadable from the job itself; folding half of them into the
        # working sheet would leave an operator unable to tell which half.
        if self._options.master and state == "done":
            self._add_to_master()

        write_job_json(self.paths, self.job_id, self._settings_snapshot())
        update_job_json(
            self.paths,
            state=state,
            counts=counts,
            written=self.stats.written,
            needs_review=self.stats.needs_review,
            failed=self.stats.failed_written,
            pages_rendered=self.stats.pages_rendered,
            host_calls=self.stats.host_calls,
            # So the Complete screen can say "also added 24 rows to your sheet"
            # without the operator going to look. Absent when master was off.
            master=(
                {
                    "added": self.stats.master.added,
                    "replaced": self.stats.master.replaced,
                    "skipped": self.stats.master.skipped,
                    "total": self.stats.master.total,
                    "error": self.stats.master_error,
                }
                if self.stats.master is not None
                else ({"error": self.stats.master_error} if self.stats.master_error else None)
            ),
        )
