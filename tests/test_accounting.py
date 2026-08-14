"""v5 Phase 0: every URL in exactly one terminal state, named once.

Three input URLs produced six output rows for a whole release. Three in
`failed.csv`, three in `review.csv`, and the header counted each of them twice
-- `0 written · 3 need a human · 3 failed` against an input of three.

Nothing caught it because the accounting assertion only checked that no URL was
MISSING, never that none was counted twice, and `needs_review` returned True for
any row whose status was not OK -- which every failed row's is.

So the tests here are about the sum and about the split. Both are properties of
the whole job rather than of any one function, which is why they run jobs.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from haat_lister.config import Settings
from haat_lister.images.reasons import FAILED as FAILED_REASONS
from haat_lister.images.reasons import REASONS, REFUSED, Klass, NoImageReason, klass_of
from haat_lister.jobs import (
    FAILED,
    IN_LISTINGS,
    NEEDS_HUMAN,
    TERMINAL,
    WRITTEN,
    terminal_state,
)
from haat_lister.jobs import (
    REFUSED as REFUSED_STATE,
)
from haat_lister.models import (
    Confidence,
    FieldSource,
    FieldValue,
    ProductRecord,
    Provenance,
)
from haat_lister.output.review_writer import (
    extracted_anything,
    missing_required,
    needs_review,
)
from haat_lister.pipeline import new_record


def empty_row(url: str, reason: str) -> ProductRecord:
    """A URL that never returned a body. No title, no description, no fields."""
    record = new_record(url, Provenance.OWN)
    record.fail(reason)
    record.image.none_reason = NoImageReason(reason)
    return record


def full_row(url: str, complete: bool = True) -> ProductRecord:
    record = new_record(url, Provenance.OWN)
    record.title = FieldValue.found("Indigo Kurta", FieldSource.JSONLD, Confidence.HIGH)
    record.description = FieldValue.found("Handwoven.", FieldSource.JSONLD, Confidence.HIGH)
    record.category_slug = FieldValue.found("apparel", FieldSource.INFERRED, Confidence.HIGH)
    if complete:
        record.price_inr = FieldValue.found(2499, FieldSource.OPERATOR, Confidence.HIGH)
        record.hs_code = FieldValue.found("6206", FieldSource.INFERRED, Confidence.HIGH)
        record.weight_g = FieldValue.found(300, FieldSource.SPEC_TABLE, Confidence.HIGH)
    return record


# --------------------------------------------------------------------------
# §1.1 -- the four states
# --------------------------------------------------------------------------


@pytest.mark.parametrize("reason", sorted(REFUSED))
def test_every_refusal_lands_in_the_refused_state(
    reason: NoImageReason, settings: Settings
) -> None:
    """§8 test 1, over every member rather than a sample.

    A refusal is not a degree of failure. The site declined and the tool was
    correct to stop, so it is counted apart, filed with its class, and never
    offered for retry.
    """
    record = empty_row("https://shop.example/p/1", reason.value)
    assert terminal_state(record, settings.config) == REFUSED_STATE


@pytest.mark.parametrize("reason", sorted(FAILED_REASONS))
def test_every_failure_lands_in_the_failed_state(reason: NoImageReason, settings: Settings) -> None:
    record = empty_row("https://shop.example/p/1", reason.value)
    assert terminal_state(record, settings.config) == FAILED


def test_a_complete_row_is_written_and_an_incomplete_one_needs_a_human(
    settings: Settings,
) -> None:
    """The one deliberate exception: `needs_human` touches two files.

    review.csv is a POINTER into listings.csv, which is why the row is in both
    and why nothing else ever is.
    """
    settings.config.fields.required_by_haat = ["title", "price_inr"]

    complete = full_row("https://shop.example/p/1", complete=True)
    incomplete = full_row("https://shop.example/p/2", complete=False)

    assert terminal_state(complete, settings.config) == WRITTEN
    assert terminal_state(incomplete, settings.config) == NEEDS_HUMAN
    # Both reach listings.csv. That is what makes review.csv a pointer.
    assert WRITTEN in IN_LISTINGS and NEEDS_HUMAN in IN_LISTINGS


def test_the_four_states_are_the_whole_vocabulary() -> None:
    assert set(TERMINAL) == {WRITTEN, NEEDS_HUMAN, REFUSED_STATE, FAILED}


# --------------------------------------------------------------------------
# §1.3 -- refused and failed rows are not review rows
# --------------------------------------------------------------------------


@pytest.mark.parametrize("reason", sorted(REFUSED | FAILED_REASONS))
def test_refused_rows_absent_from_review_csv(reason: NoImageReason, settings: Settings) -> None:
    """§8 test 3, and the fix for the double count.

    `needs_review` used to return True for `status is not OK`, so every URL that
    never returned a body was written into review.csv as well as failed.csv.
    """
    record = empty_row("https://shop.example/p/1", reason.value)
    assert not needs_review(record, settings.config)


def test_a_real_row_that_needs_a_decision_still_reaches_review(settings: Settings) -> None:
    """The fix must not empty review.csv of the rows it exists for."""
    settings.config.fields.required_by_haat = ["title", "price_inr"]
    record = full_row("https://shop.example/p/1", complete=False)
    assert needs_review(record, settings.config)


# --------------------------------------------------------------------------
# §1.4 -- no field flags on an empty record
# --------------------------------------------------------------------------


def test_no_field_flags_on_empty_record(settings: Settings) -> None:
    """§8 test 4. `⚑10` appeared on three pages that never loaded.

    Ten missing required fields is a true statement about an empty record and a
    useless one: it tells an operator to go and fill in a title for something
    they cannot see. A row with no content has one reason, not ten problems.
    """
    settings.config.fields.required_by_haat = [
        "title", "description", "category_slug", "price_inr", "hs_code",
    ]
    record = empty_row("https://shop.example/p/1", "timeout_read")

    assert not extracted_anything(record)
    assert missing_required(record, settings.config) == []


def test_a_row_with_content_still_reports_what_is_missing(settings: Settings) -> None:
    settings.config.fields.required_by_haat = ["title", "price_inr"]
    record = full_row("https://shop.example/p/1", complete=False)

    assert extracted_anything(record)
    assert missing_required(record, settings.config) == ["price_inr"]


# --------------------------------------------------------------------------
# §2 -- one name per outcome
# --------------------------------------------------------------------------


def test_reason_strings_are_enum_members() -> None:
    """§8 test 5. `page_fetch_failed` above all.

    Checked against emission rather than prose: the modules that replaced the
    bucket name it in their docstrings to explain what was removed, and that
    explanation is worth more than a clean grep.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "haat_lister"
    offenders = []
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        # Strip module docstring and comments; what remains is code.
        body = source.split('"""', 2)[-1]
        code = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("#")
        )
        if "page_fetch_failed" in code:
            offenders.append(path.name)
    assert offenders == [], f"the deleted bucket is still emitted by: {offenders}"


def test_every_reason_carries_a_class_and_a_next_action() -> None:
    """§2.3. A diagnosis that ends without a route wastes the person reading it."""
    for reason in NoImageReason:
        spec = REASONS[reason]
        assert spec.klass in (Klass.REFUSED, Klass.FAILED)
        assert len(spec.what_to_do.split()) >= 8, f"{reason} has no usable next action"
        assert spec.label == reason.value


def test_retry_excludes_refused_class() -> None:
    """§8 test 6. Retrying `robots_disallowed` produces `robots_disallowed`,
    forever. A button that cannot change its own outcome should not exist.

    `blocked_429` is the one refusal that IS retryable -- it is a rate limit
    rather than a decision about us, and the site tells us when to come back.
    """
    for reason in REFUSED:
        if reason is NoImageReason.BLOCKED_429:
            assert reason.retryable
            continue
        assert not reason.retryable, f"{reason} is a refusal and cannot be retried into a success"

    assert NoImageReason.TIMEOUT_READ.retryable
    assert NoImageReason.HTTP_ERROR_5XX.retryable
    # A name that does not resolve will not resolve on the third attempt.
    assert not NoImageReason.DNS_FAILURE.retryable
    assert not NoImageReason.NOT_A_PRODUCT_PAGE.retryable


def test_a_record_written_before_the_vocabulary_closed_still_loads() -> None:
    """Closing the enum must not orphan the jobs that were run before it closed.

    Found by re-rendering a real historic job: its stored records carried the
    retired bucket, the strict enum refused them, and the download route raised
    a ValidationError -- so every pre-v5 job had become undownloadable. The
    ledger outlives the release that wrote it, and a projection that cannot be
    rebuilt is a projection that has silently become the source of truth.

    It reads as None rather than as a nearest neighbour, because the bucket
    stood for several different outcomes and picking one would be inventing
    evidence about a request nobody can repeat.
    """
    from haat_lister.models import ImageResult

    stored = '{"url": "", "method": "none", "none_reason": "page_fetch_failed"}'
    result = ImageResult.model_validate_json(stored)

    assert result.none_reason is None
    assert klass_of(result.none_reason) is Klass.FAILED, "an unreadable name is not a refusal"
    # A name still in the vocabulary is unaffected.
    assert ImageResult.model_validate_json(
        '{"none_reason": "robots_disallowed"}'
    ).none_reason is NoImageReason.ROBOTS_DISALLOWED


def test_an_unknown_reason_counts_as_a_failure_not_a_refusal() -> None:
    """Calling something "refused" is a claim about the site. We only make it
    when we actually saw a refusal."""
    assert klass_of("something_nobody_defined") is Klass.FAILED
    assert klass_of(None) is Klass.FAILED
    assert klass_of("robots_disallowed") is Klass.REFUSED


# --------------------------------------------------------------------------
# The whole job, end to end
# --------------------------------------------------------------------------


def test_counts_sum_to_input_length(settings: Settings, tmp_path: Path) -> None:
    """§8 test 2, run through a real job.

    The observed defect was a SUM, not a single value, so the assertion has to
    be one too: three inputs reported as 0 written + 3 needing a human + 3
    failed, which is six.
    """
    from test_job_output import FakeProcessor, run_job, urls

    from haat_lister.jobs import job_paths
    from haat_lister.store.ledger import Ledger

    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    tuned.config.fetch.per_domain_delay_s = 0.0
    tuned.config.fetch.per_domain_delay_jitter_s = 0.0

    lines = urls(6)
    stats = run_job(tuned, lines, process=FakeProcessor(fail_for={lines[1], lines[4]}))

    with Ledger(tuned.root / tuned.config.paths.ledger) as ledger:
        counts = ledger.outcome_counts(stats.job_id)

    assert sum(counts.get(state, 0) for state in TERMINAL) == len(lines)

    # And the files agree with the counts: no row is in two of them.
    paths = job_paths(tuned, stats.job_id)
    listings = _rows(paths.listings)
    failed = _rows(paths.failed)
    review = _rows(paths.review)

    assert len(listings) + len(failed) == len(lines)
    assert len(review) <= len(listings), "review.csv holds rows that are not in listings.csv"

    failed_urls = {r["source_url"] for r in failed}
    review_urls = {r["source_url"] for r in review}
    assert not (failed_urls & review_urls), "a URL is in both failed.csv and review.csv"


def test_the_four_header_counts_are_disjoint_and_sum(settings: Settings, tmp_path: Path) -> None:
    """§1.1 and §9, as arithmetic on the thing an operator reads.

    Found by running the console rather than by reading it. A four-row job
    rendered `4 written | 4 need a human | 0 refused | 0 failed` -- because
    `written` meant "reached listings.csv", which INCLUDES needs_human. Every
    number was defensible and the row of them was not: four counts side by side
    invite addition, and these added to eight against an input of four.

    So the four are now the four terminal states, disjoint by construction, and
    "how many rows are in listings.csv" has a field of its own.
    """
    from haat_lister.store.ledger import Ledger

    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    urls = [(i, f"https://a.example/{i}", "a.example") for i in range(4)]
    with Ledger(tuned.root / tuned.config.paths.ledger) as ledger:
        ledger.create_job("j_headers", "{}", urls)
        ledger.set_outcome("j_headers", 0, WRITTEN, row_key="a")
        ledger.set_outcome("j_headers", 1, NEEDS_HUMAN, row_key="b")
        ledger.set_outcome("j_headers", 2, REFUSED_STATE, row_key="c", reason="robots_disallowed")
        ledger.set_outcome("j_headers", 3, FAILED, row_key="d", reason="timeout_read")
        counts = ledger.outcome_counts("j_headers")

    header = [counts.get(state, 0) for state in TERMINAL]

    assert header == [1, 1, 1, 1]
    assert sum(header) == len(urls), "the four counts no longer sum to the input"
    # And the file count is the wider question, kept apart.
    in_listings = sum(counts.get(state, 0) for state in IN_LISTINGS)
    assert in_listings == 2, "listings.csv holds the written AND the needs_human rows"


def test_retry_does_not_pick_up_a_refused_row(settings: Settings, tmp_path: Path) -> None:
    """§9. "Retry the N that failed" must exclude the ajio row.

    Asserted against `resumable`, which is what the button actually calls --
    the count next to the button being right proves nothing about which rows
    the click re-runs.

    This holds because `refused` became an outcome of its own rather than a
    shade of `failed`; the SQL was never changed. That is the point of §1.1:
    one decision, made once, and everything downstream inherits it.
    """
    from haat_lister.store.ledger import Ledger

    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    with Ledger(tuned.root / tuned.config.paths.ledger) as ledger:
        ledger.create_job(
            "j_dodcheck",
            "{}",
            [(0, "https://a.example/1", "a.example"), (1, "https://b.example/2", "b.example")],
        )
        ledger.set_outcome("j_dodcheck", 0, REFUSED_STATE, row_key="a", reason="robots_disallowed")
        ledger.set_outcome("j_dodcheck", 1, FAILED, row_key="b", reason="timeout_read")

        resumable = ledger.resumable("j_dodcheck")

    assert resumable == [1], "the retry would re-run a refusal, which produces the refusal"


def test_failed_csv_says_which_class_each_row_is(settings: Settings, tmp_path: Path) -> None:
    """§2.2. The operator filters on this before pasting the URL column into a
    new job -- re-running a refusal is guaranteed to produce the refusal."""
    from haat_lister.output.failed_writer import FAILED_COLUMNS, FailedWriter

    path = tmp_path / "failed.csv"
    with FailedWriter(path, settings.config) as writer:
        writer.write(empty_row("https://a.example/1", "robots_disallowed"))
        writer.write(empty_row("https://b.example/2", "timeout_read"))

    assert FAILED_COLUMNS[1] == "class", "the class column is not where an operator will look"
    rows = _rows(path)
    assert [r["class"] for r in rows] == ["refused", "failed"]
    assert [r["reason"] for r in rows] == ["robots_disallowed", "timeout_read"]


def _rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))
