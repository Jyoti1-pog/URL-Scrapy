"""v2 Phase 1: one file, in input order, with nothing lost.

The three claims here are the ones a CSV tool lives or dies by, and all three are
about behaviour you cannot see by reading the code:

  Rows come out in the order they went in, even though they finish out of order.
  Every URL pasted ends up in exactly one output file, including after a cancel.
  A file downloaded mid-run is a valid CSV, not a snapshot with holes.

So completion order is deliberately scrambled -- `FakeProcessor` sleeps a
different amount per index -- and the assertions are about the file on disk.
"""

from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

import pytest

from haat_lister.batch import BatchOptions, BatchRunner, DomainLimiter, StopSignal
from haat_lister.config import Settings
from haat_lister.jobs import (
    DUPLICATE,
    INVALID,
    NEEDS_HUMAN,
    TERMINAL,
    WRITTEN,
    UnaccountedUrls,
    assert_accounted,
    is_job_id,
    job_paths,
    new_job_id,
    plan_urls,
)
from haat_lister.models import (
    Confidence,
    FieldSource,
    FieldValue,
    ImageMethod,
    ImageMode,
    ProductRecord,
    Provenance,
)
from haat_lister.pipeline import new_record
from haat_lister.store.ledger import Ledger

# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


@pytest.fixture
def job_settings(settings: Settings, tmp_path: Path) -> Settings:
    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    tuned.config.fetch.per_domain_delay_s = 0.0
    tuned.config.fetch.per_domain_delay_jitter_s = 0.0
    return tuned


def urls(n: int, host: str = "shop") -> list[str]:
    """One domain per URL, so the per-domain limiter does not serialise them and
    completion really can outrun input order."""
    return [f"https://{host}{i}.example/p/{i}" for i in range(n)]


class FakeProcessor:
    """Finishes in a deliberately scrambled order.

    The delay is a function of the index, not random, so a failure is
    reproducible: index 0 is the slowest, which is the worst case for a
    watermark writer -- everything else finishes and has to wait for it.
    """

    def __init__(
        self,
        *,
        jitter: bool = True,
        fail_for: set[str] | None = None,
        slowest_first: bool = True,
    ) -> None:
        self.calls: list[str] = []
        self.completion_order: list[str] = []
        self._jitter = jitter
        self._fail_for = fail_for or set()
        self._slowest_first = slowest_first

    async def __call__(
        self, url: str, provenance: Provenance = Provenance.OWN, *_: object, **__: object
    ) -> ProductRecord:
        self.calls.append(url)
        if self._jitter:
            index = int(url.rsplit("/", 1)[-1])
            delay = (0.05 - index * 0.004) if self._slowest_first else (index * 0.004)
            await asyncio.sleep(max(0.0, delay))
        else:
            await asyncio.sleep(0)
        self.completion_order.append(url)

        record = new_record(url, provenance)
        if url in self._fail_for:
            # What a real 503 produces after v5 §2.1: the enum member, with
            # the raw status in its own column. A fake that emits a transport
            # word tests a vocabulary the pipeline no longer speaks.
            record.fail("http_error_5xx")
            record.http_status = 503
            return record
        record.title = FieldValue.found(
            f"Product {url.rsplit('/', 1)[-1]}", FieldSource.JSONLD, Confidence.HIGH
        )
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


def run_job(
    settings: Settings,
    lines: list[str],
    *,
    process: FakeProcessor,
    concurrency: int = 5,
    job_id: str = "",
    resume: bool = False,
    stop: StopSignal | None = None,
    on_row: object = None,
):
    plan = plan_urls(lines)
    options = BatchOptions(
        provenance=Provenance.OWN,
        image_mode=ImageMode.MANIFEST,
        job_id=job_id,
        concurrency=concurrency,
        resume=resume,
        checkpoint_every=5,
    )

    async def go():
        with Ledger(settings.root / settings.config.paths.ledger) as ledger:
            runner = BatchRunner(
                settings,
                options,
                ledger=ledger,
                client=object(),  # the fake never touches it
                process=process,
                stop=stop,
                on_row=on_row,  # type: ignore[arg-type]
                limiter=DomainLimiter(per_domain_concurrency=1, delay_s=0.0, jitter_s=0.0),
            )
            return await runner.run(plan)

    return asyncio.run(go())


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# --------------------------------------------------------------------------
# Order
# --------------------------------------------------------------------------


def test_output_order_matches_input_order(job_settings: Settings) -> None:
    """The headline claim. Completion order is scrambled on purpose -- index 0
    is the slowest row, so every other row finishes before the one that has to
    be written first."""
    lines = urls(20)
    process = FakeProcessor(jitter=True, slowest_first=True)

    stats = run_job(job_settings, lines, process=process, concurrency=5)

    assert process.completion_order != lines, "the test did not actually scramble anything"

    paths = job_paths(job_settings, stats.job_id)
    titles = [row["title"] for row in read_csv(paths.listings)]
    assert titles == [f"Product {i}" for i in range(20)]


def test_a_slow_first_row_does_not_lose_the_ones_behind_it(job_settings: Settings) -> None:
    """The watermark buffers rendered lines rather than records. Whatever it
    held has to come out."""
    lines = urls(30)
    stats = run_job(job_settings, lines, process=FakeProcessor(slowest_first=True), concurrency=8)

    assert stats.peak_pending > 1, "nothing ever finished ahead of the watermark"
    paths = job_paths(job_settings, stats.job_id)
    assert len(read_csv(paths.listings)) == 30


def test_a_failed_row_holds_its_place_without_writing_a_line(job_settings: Settings) -> None:
    """A failure has no title, so nothing goes in listings.csv -- but it must
    still advance the watermark, or one bad URL dams everything behind it."""
    lines = urls(6)
    stats = run_job(
        job_settings, lines, process=FakeProcessor(fail_for={lines[0], lines[3]}), concurrency=3
    )

    paths = job_paths(job_settings, stats.job_id)
    titles = [row["title"] for row in read_csv(paths.listings)]
    assert titles == ["Product 1", "Product 2", "Product 4", "Product 5"]
    assert stats.failed == 2


# --------------------------------------------------------------------------
# Accounting
# --------------------------------------------------------------------------


def test_every_input_url_accounted_for(job_settings: Settings) -> None:
    lines = [*urls(10), urls(10)[3], "not-a-url", "ftp://nope.example/x"]
    stats = run_job(job_settings, lines, process=FakeProcessor(), concurrency=4)

    with Ledger(job_settings.root / job_settings.config.paths.ledger) as ledger:
        counts = ledger.outcome_counts(stats.job_id)
        assert ledger.unaccounted(stats.job_id) == []
        assert_accounted(ledger, stats.job_id)

    # `listed` split into `written` and `needs_human` (v5 §1.1). Both are in
    # listings.csv; the difference is whether review.csv points at them.
    assert counts.get(WRITTEN, 0) + counts.get(NEEDS_HUMAN, 0) == 10
    assert counts[DUPLICATE] == 1
    assert counts[INVALID] == 2
    assert sum(counts.values()) == 13


def test_every_input_url_accounted_for_after_a_cancel(job_settings: Settings) -> None:
    """The case the assertion exists for. A cancelled job leaves URLs that were
    never begun, and they have to be somewhere an operator can find them."""
    lines = urls(40)
    stop = StopSignal()

    def on_row(stats: object) -> None:
        if stats.processed >= 5:  # type: ignore[attr-defined]
            stop.set()

    stats = run_job(
        job_settings, lines, process=FakeProcessor(), concurrency=2, stop=stop, on_row=on_row
    )

    assert stats.stopped_early
    with Ledger(job_settings.root / job_settings.config.paths.ledger) as ledger:
        assert ledger.unaccounted(stats.job_id) == []
        counts = assert_accounted(ledger, stats.job_id)

    # The four terminal states, summing to every URL that was attempted.
    # `not_started` rows from a cancel land in `failed`, which is where an
    # operator looks for work to re-run.
    assert sum(counts.get(state, 0) for state in TERMINAL) == 40

    paths = job_paths(job_settings, stats.job_id)
    listed = len(read_csv(paths.listings))
    failed = read_csv(paths.failed)
    assert listed + len(failed) == 40, "a URL went missing between the two files"
    assert any(row["reason"] == "not_started" for row in failed)


def test_an_unaccounted_url_raises_rather_than_shrugging() -> None:
    """A job that cannot say where a URL went has produced a CSV nobody should
    trust, and the loudest moment to learn that is before it is uploaded."""
    with Ledger() as ledger:
        ledger.create_job("j_abcd1234", "{}", [(0, "https://a/1", "https://a/1")])
        with pytest.raises(UnaccountedUrls, match="no recorded outcome"):
            assert_accounted(ledger, "j_abcd1234")


def test_failed_csv_is_re_runnable(job_settings: Settings) -> None:
    """Its URL column pastes straight back into a new job. A failure file you
    have to reformat before retrying is a report, not a work item."""
    lines = urls(5)
    stats = run_job(job_settings, lines, process=FakeProcessor(fail_for={lines[2]}))

    paths = job_paths(job_settings, stats.job_id)
    failed = read_csv(paths.failed)
    assert [r["source_url"] for r in failed] == [lines[2]]
    assert failed[0]["reason"] == "http_error_5xx"
    assert failed[0]["class"] == "failed"
    assert failed[0]["http_status"] == "503"
    assert failed[0]["http_status"] == "503"

    replan = plan_urls([r["source_url"] for r in failed])
    assert len(replan.accepted) == 1


# --------------------------------------------------------------------------
# Duplicates
# --------------------------------------------------------------------------


def test_duplicate_urls_collapsed_and_reported() -> None:
    """Collapsed *before* processing: the second copy of a URL must not cost a
    fetch to discover it was a copy. And never silently -- the count is shown."""
    plan = plan_urls(
        [
            "https://shop.example/p/1",
            "https://shop.example/p/1?utm_source=newsletter",
            "https://shop.example/p/1#reviews",
            "https://SHOP.example/p/2",
            "",
            "# a comment",
        ]
    )

    assert len(plan.accepted) == 2
    assert plan.duplicates == 2
    assert "2 duplicate" in plan.summary()
    # "link 1", not "line 1". Once a paste can put twelve comma-separated links
    # on one line, a line number stops identifying which one is meant.
    assert [u.note for u in plan.urls if u.status == DUPLICATE] == [
        "same product as link 1",
        "same product as link 1",
    ]


def test_a_duplicate_is_fetched_once_and_does_not_dam_the_watermark(
    job_settings: Settings,
) -> None:
    lines = ["https://a.example/p/0", "https://a.example/p/0?ref=x", "https://b.example/p/1"]
    process = FakeProcessor(jitter=False)
    stats = run_job(job_settings, lines, process=process)

    assert len(process.calls) == 2
    paths = job_paths(job_settings, stats.job_id)
    assert [r["title"] for r in read_csv(paths.listings)] == ["Product 0", "Product 1"]


def test_malformed_lines_are_named_not_just_counted() -> None:
    plan = plan_urls(["https://ok.example/p/1", "not-a-url", "javascript:alert(1)"])
    assert [u.raw for u in plan.invalid] == ["not-a-url", "javascript:alert(1)"]
    assert all(u.note for u in plan.invalid)


# --------------------------------------------------------------------------
# Mid-run validity
# --------------------------------------------------------------------------


def test_partial_download_mid_job_is_valid_csv(job_settings: Settings) -> None:
    """The file on disk is always a correctly ordered *prefix*: never a hole,
    never a row out of place. That is what makes "download what's done so far"
    an honest offer."""
    lines = urls(30)
    snapshots: list[list[str]] = []
    paths_holder: dict[str, Path] = {}

    def on_row(stats: object) -> None:
        path = paths_holder.get("listings")
        if path is None or not path.exists():
            return
        rows = read_csv(path)
        snapshots.append([r["title"] for r in rows])

    plan = plan_urls(lines)
    options = BatchOptions(
        provenance=Provenance.OWN,
        image_mode=ImageMode.MANIFEST,
        concurrency=6,
        checkpoint_every=3,
    )

    async def go():
        with Ledger(job_settings.root / job_settings.config.paths.ledger) as ledger:
            runner = BatchRunner(
                job_settings,
                options,
                ledger=ledger,
                client=object(),
                process=FakeProcessor(slowest_first=True),
                on_row=on_row,  # type: ignore[arg-type]
                limiter=DomainLimiter(per_domain_concurrency=1, delay_s=0.0, jitter_s=0.0),
            )
            paths_holder["listings"] = runner.paths.listings
            return await runner.run(plan)

    asyncio.run(go())

    assert snapshots, "listings.csv never appeared mid-run"
    for titles in snapshots:
        expected = [f"Product {i}" for i in range(len(titles))]
        assert titles == expected, "a mid-run snapshot had a hole or a row out of order"


# --------------------------------------------------------------------------
# Resume
# --------------------------------------------------------------------------


def test_resume_processes_only_the_remainder(job_settings: Settings) -> None:
    lines = urls(20)
    stop = StopSignal()

    def on_row(stats: object) -> None:
        if stats.processed >= 4:  # type: ignore[attr-defined]
            stop.set()

    first = FakeProcessor(jitter=False)
    stats = run_job(
        job_settings, lines, process=first, concurrency=2, stop=stop, on_row=on_row
    )
    assert stats.stopped_early
    done = len(first.calls)

    second = FakeProcessor(jitter=False)
    resumed = run_job(
        job_settings, lines, process=second, job_id=stats.job_id, resume=True, concurrency=4
    )

    assert len(second.calls) == 20 - done, "resume re-fetched pages the first run finished"

    paths = job_paths(job_settings, resumed.job_id)
    titles = [r["title"] for r in read_csv(paths.listings)]
    assert titles == [f"Product {i}" for i in range(20)]


def test_cancel_preserves_completed_rows(job_settings: Settings) -> None:
    lines = urls(30)
    stop = StopSignal()

    def on_row(stats: object) -> None:
        if stats.processed >= 6:  # type: ignore[attr-defined]
            stop.set()

    stats = run_job(
        job_settings, lines, process=FakeProcessor(jitter=False), concurrency=2,
        stop=stop, on_row=on_row,
    )

    paths = job_paths(job_settings, stats.job_id)
    rows = read_csv(paths.listings)
    assert rows, "a cancelled job threw away every finished row"
    assert [r["title"] for r in rows] == [f"Product {i}" for i in range(len(rows))]


# --------------------------------------------------------------------------
# Job identity and layout
# --------------------------------------------------------------------------


def test_job_ids_are_the_shape_the_download_routes_will_validate() -> None:
    for _ in range(50):
        assert is_job_id(new_job_id())
    for bad in ("j_ABCD1234", "j_short", "../../etc", "j_" + "a" * 9, ""):
        assert not is_job_id(bad)


def test_everything_a_job_produced_is_in_one_directory(job_settings: Settings) -> None:
    stats = run_job(job_settings, urls(4), process=FakeProcessor(fail_for={urls(4)[1]}))
    paths = job_paths(job_settings, stats.job_id)

    for path in (paths.listings, paths.review, paths.manifest, paths.failed, paths.job_json):
        assert path.exists(), f"{path.name} missing"
        assert path.parent == paths.root

    saved = json.loads(paths.job_json.read_text(encoding="utf-8"))
    assert saved["job_id"] == stats.job_id
    assert saved["provenance"] == "own"
    assert saved["images"] == "manifest"
    assert saved["state"] == "done"
    assert "tool_version" in saved


def test_a_second_job_over_the_same_catalogue_is_not_deduped_away(
    job_settings: Settings,
) -> None:
    """v1 deduped against the whole ledger, which under a job model would make
    every re-run produce an empty file."""
    lines = urls(3)
    first = run_job(job_settings, lines, process=FakeProcessor())
    second = run_job(job_settings, lines, process=FakeProcessor())

    assert first.job_id != second.job_id
    for stats in (first, second):
        paths = job_paths(job_settings, stats.job_id)
        assert len(read_csv(paths.listings)) == 3


def test_the_header_is_still_byte_identical_to_the_template(job_settings: Settings) -> None:
    """The 19-column lock survives the rewrite."""
    stats = run_job(job_settings, urls(2), process=FakeProcessor())
    paths = job_paths(job_settings, stats.job_id)

    template = Path(__file__).resolve().parents[1] / "haat-bulk-listings-template.csv"
    produced = paths.listings.read_bytes().split(b"\r\n")[0]
    assert produced == template.read_bytes().split(b"\r\n")[0]


# --------------------------------------------------------------------------
# Estimates
# --------------------------------------------------------------------------


def test_the_estimate_respects_the_per_domain_floor() -> None:
    """200 URLs on one domain do not get faster with --concurrency 20, and an
    estimate that pretends otherwise teaches an operator to ignore estimates."""
    one_domain = plan_urls([f"https://shop.example/p/{i}" for i in range(200)])
    low, _ = one_domain.estimate_seconds(concurrency=20, per_domain_delay=2.0)
    assert low >= 200 * 2.0

    spread = plan_urls([f"https://shop{i}.example/p/{i}" for i in range(200)])
    low_spread, _ = spread.estimate_seconds(concurrency=20, per_domain_delay=2.0)
    assert low_spread < low


def test_a_cancelled_job_is_both_accounted_for_and_resumable(job_settings: Settings) -> None:
    """Caught in a live run. Two requirements pull against each other here.

    Accounting says every URL must end in exactly one output file, so a cancel
    marks the ones that never started `failed / not_started` and puts them in
    failed.csv -- the file an operator re-runs. But that gave them an outcome,
    and resume was asking for rows with *no* outcome, so it reported "nothing
    left to do" on a job that was half finished.
    """
    lines = urls(20)
    stop = StopSignal()

    def on_row(stats: object) -> None:
        if stats.processed >= 4:  # type: ignore[attr-defined]
            stop.set()

    stats = run_job(
        job_settings, lines, process=FakeProcessor(jitter=False), concurrency=2,
        stop=stop, on_row=on_row,
    )
    assert stats.stopped_early

    with Ledger(job_settings.root / job_settings.config.paths.ledger) as ledger:
        # Both hold at once: nothing is unaccounted, and there is work to resume.
        assert ledger.unaccounted(stats.job_id) == []
        assert len(ledger.resumable(stats.job_id)) == 20 - stats.written

    second = FakeProcessor(jitter=False)
    resumed = run_job(
        job_settings, lines, process=second, job_id=stats.job_id, resume=True, concurrency=4
    )
    assert resumed.written == 20
