"""Jobs: identity, planning, layout, and the accounting that makes them honest.

Every run is a job -- CLI or web, no second concept. A job owns a directory, and
everything it produced is in there together:

    runs/j_7fk2m9qa/
      listings.csv  review.csv  image_manifest.csv  failed.csv
      images/<row_key>/…        job.json

The one rule this module exists to enforce:

    Every URL an operator pasted ends up in exactly one output file.

Not "usually". It is asserted at the end of every job against `job_urls`, which
gets a row for every pasted line at creation time with `outcome` NULL. A URL that
silently vanishes is the worst failure this tool has, because nobody notices
until a product is missing from the shop -- and by then the CSV has been imported
and the batch that produced it is a week old.

`listings.csv` is written live, because a 500-row job that dies at #480 must not
lose 480 rows. The other three are **projections**: regenerated from the ledger
in input order, at the end of a job and again on demand. That is what makes a
mid-run download valid, and what makes a post-edit re-export the same operation
rather than a second one.
"""

from __future__ import annotations

import json
import re
import secrets
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .config import AppConfig, Settings
from .models import ImageMode, ProductRecord
from .output import with_images
from .output.failed_writer import FailedWriter
from .output.manifest_writer import ManifestWriter
from .output.master import MasterStats
from .output.review_writer import ReviewWriter
from .store.ledger import Ledger
from .utils.canonical import DEFAULT_IDENTITY, Identity
from .utils.logging import get_logger
from .utils.urls import canonicalise, extract_urls, host_of

log = get_logger(__name__)

# Also the validation pattern for the download routes in Phase 2 -- a job id is
# the only path component a client supplies, so its shape is a security control
# as much as a naming convention.
JOB_ID_RE = re.compile(r"^j_[a-z0-9]{8}$")
_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def new_job_id() -> str:
    return "j_" + "".join(secrets.choice(_ALPHABET) for _ in range(8))


def is_job_id(value: str) -> bool:
    return bool(JOB_ID_RE.fullmatch(value))


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobPaths:
    root: Path
    listings: Path
    # The same rows plus every photo link. A companion, never a replacement:
    # `listings.csv` has to stay importable, and haat's template has no image
    # column to put these in.
    listings_with_images: Path
    review: Path
    manifest: Path
    failed: Path
    images: Path
    downloads: Path
    job_json: Path

    @property
    def zip(self) -> Path:
        return self.root.parent / f"haat-listings-{self.root.name}.zip"


def settings_for_job(settings: Settings, job_id: str) -> Settings:
    """A view of the settings whose image directories point inside the job.

    Deliberately done by redirecting config rather than by parameterising
    `images/pipeline.py`: that module owns the Tier 1 -> Tier 2 gate and is not
    to be touched, and "where the files land" is a configuration question
    anyway. The ledger path is left alone -- upload dedupe and the bad-host
    cache are worth more across jobs than inside one.
    """
    if not is_job_id(job_id):
        raise ValueError(f"not a job id: {job_id!r}")
    scoped = settings.model_copy(deep=True)
    base = f"{settings.config.paths.runs_dir}/{job_id}"
    scoped.config.paths.images_dir = f"{base}/images"
    scoped.config.paths.downloads_dir = f"{base}/downloads"
    return scoped


def job_paths(settings: Settings, job_id: str) -> JobPaths:
    if not is_job_id(job_id):
        raise ValueError(f"not a job id: {job_id!r}")
    root = settings.root / settings.config.paths.runs_dir / job_id
    return JobPaths(
        root=root,
        listings=root / "listings.csv",
        listings_with_images=root / "listings_with_images.csv",
        review=root / "review.csv",
        manifest=root / "image_manifest.csv",
        failed=root / "failed.csv",
        images=root / "images",
        downloads=root / "downloads",
        job_json=root / "job.json",
    )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------

# WHAT AN INPUT LINE ENDED UP AS.
#
# `duplicate` and `invalid` are removed before any work happens (section 2.2:
# collapse before processing, not after). The other four are terminal states,
# and every attempted URL lands in exactly one of them:
#
#     written      extracted, every required field present   -> listings.csv
#     needs_human  extracted, something needs a decision     -> listings.csv AND review.csv
#     refused      the site declined; the tool was correct   -> failed.csv
#     failed       we reached the site, got no usable row    -> failed.csv
#
# `needs_human` is the ONE state that touches two files, and review.csv is a
# pointer into listings.csv rather than a second copy of the row. `refused` and
# `failed` never appear in review.csv: there is nothing on those rows for a
# human to decide, and putting them there is what made three input URLs produce
# six output rows.
#
# `refused` and `failed` are separate because they are not degrees of one thing.
# Retrying a refusal produces the same refusal forever, so the retry button must
# be able to tell them apart.
WRITTEN = "written"
NEEDS_HUMAN = "needs_human"
REFUSED = "refused"
FAILED = "failed"
DUPLICATE = "duplicate"
INVALID = "invalid"

# The four that mean "this URL was attempted and is finished".
TERMINAL: tuple[str, ...] = (WRITTEN, NEEDS_HUMAN, REFUSED, FAILED)

# Rows that reached listings.csv. Both, because a needs_human row IS written --
# that is the whole point of it being a pointer rather than a copy.
IN_LISTINGS: tuple[str, ...] = (WRITTEN, NEEDS_HUMAN, "listed")

# Kept so ledgers written before the split still read. `listed` was the single
# state that `written` and `needs_human` came out of, and it appears in
# IN_LISTINGS above for exactly that reason -- an old job's rows are still in
# its listings.csv and a re-export must not drop them.
LISTED = "listed"


def terminal_state(record: ProductRecord, cfg: AppConfig) -> str:
    """Which of the four this row reached. The single decision point.

    Written here rather than at the call site so the ledger, the counts, the
    files and the retry button all read one answer. When each of those decided
    for itself, they disagreed -- a refused row was counted as failed, written
    into review.csv, and offered for retry, all at once.

    The refused/failed split comes from the reason's own class, so adding a
    reason cannot forget to say which side it is on: `reasons.py` requires a
    class on every member and asserts it at import.
    """
    from .images.reasons import Klass, klass_of
    from .models import RowStatus
    from .output.review_writer import needs_review

    if record.status is RowStatus.FAILED:
        return REFUSED if klass_of(record.failure_reason) is Klass.REFUSED else FAILED
    return NEEDS_HUMAN if needs_review(record, cfg) else WRITTEN


@dataclass
class PlannedUrl:
    index: int
    raw: str
    canonical: str
    status: str  # "ok" | DUPLICATE | INVALID
    note: str = ""
    # We supplied the scheme for a bare domain like `amazon.in/dp/X`. Carried so
    # the preview can mark it: an assumed https that turns out to be wrong fails
    # the row with no visible cause otherwise.
    assumed_scheme: bool = False
    # 1-based position in the paste, for messages. Not the same as `index`,
    # which is this row's position in the output.
    line: int = 0

    @property
    def will_run(self) -> bool:
        return self.status == "ok"


@dataclass
class JobPlan:
    """What a preflight found. Nothing here has been fetched."""

    urls: list[PlannedUrl] = field(default_factory=list)

    @property
    def pasted(self) -> int:
        return len(self.urls)

    @property
    def accepted(self) -> list[PlannedUrl]:
        return [u for u in self.urls if u.will_run]

    @property
    def duplicates(self) -> int:
        return sum(1 for u in self.urls if u.status == DUPLICATE)

    @property
    def invalid(self) -> list[PlannedUrl]:
        return [u for u in self.urls if u.status == INVALID]

    @property
    def domains(self) -> Counter[str]:
        return Counter(host_of(u.canonical) for u in self.accepted)

    def estimate_seconds(self, concurrency: int, per_domain_delay: float) -> tuple[int, int]:
        """A range, not a number.

        Bounded below by the per-domain spacing on the busiest single domain --
        that is the real floor, because one host is served one request at a time
        no matter how much concurrency is set. A catalogue on one domain does
        not get faster with `--concurrency 20`, and an estimate that pretends
        otherwise teaches an operator to distrust every estimate after it.
        """
        if not self.accepted:
            return (0, 0)
        busiest = max(self.domains.values())
        floor = busiest * per_domain_delay
        parallel = len(self.accepted) / max(1, concurrency)
        low = max(floor, parallel * 1.5)
        return (int(low), int(low * 2.2))

    def summary(self) -> str:
        parts = [f"{self.pasted} pasted"]
        if self.duplicates:
            parts.append(f"{self.duplicates} duplicate")
        if self.invalid:
            parts.append(f"{len(self.invalid)} not a link")
        parts.append(f"{len(self.accepted)} to process")
        return " · ".join(parts)


def plan_from_ledger(ledger: Ledger, job_id: str) -> JobPlan:
    """Rebuild a job's plan from what was recorded when it was created.

    Resume needs this: the browser tab that submitted the URLs may be long gone,
    and the process may have restarted. `job_urls` holds every pasted line with
    its index and its outcome, so the plan is reconstructible exactly -- which is
    the same property that makes a page refresh cheap.
    """
    plan = JobPlan()
    for row in ledger.job_inputs(job_id):
        outcome = row["outcome"]
        status = outcome if outcome in (DUPLICATE, INVALID) else "ok"
        plan.urls.append(
            PlannedUrl(
                index=int(row["input_index"]),
                raw=row["source_url"],
                canonical=row["canonical"],
                status=status,
                note=row["reason"] or "",
            )
        )
    return plan


def plan_urls(lines: list[str], identity: Identity = DEFAULT_IDENTITY) -> JobPlan:
    """Parse, canonicalise, dedupe. No network, ever.

    The parsing is `extract_urls`, not a second reader living here. That matters
    more than it looks: the console previews a paste with one function and the
    job is planned with another, and if those two ever disagree the operator is
    shown a count the run does not honour.

    Duplicates are collapsed *before* processing rather than deduped out of the
    output afterwards -- otherwise the second copy of a URL costs a full fetch
    to discover it was a copy. The collapsed ones are kept in the plan rather
    than dropped, so the count can be shown; section 2.2 is explicit that
    silently dropping is not acceptable even when the drop is correct.

    `identity` decides which links are the same product. It is threaded from
    Settings rather than defaulted at each call site, because `new_record`
    computes the same identity later and the two disagreeing would mean rows
    that the plan expects and the batch never matches up.
    """
    plan = JobPlan()
    seen: dict[str, int] = {}

    # Comment lines are dropped before extraction, not after: `# https://old`
    # in a URL file is a note to a human, and reading the link out of it would
    # be the opposite of what the `#` was for.
    body = "\n".join(line for line in lines if not line.strip().startswith("#"))
    extraction = extract_urls(body)

    # Index is the position in the OUTPUT, not in the file -- blanks, comments
    # and prose do not occupy an output row, so counting them would leave
    # permanent gaps in the watermark that nothing ever fills.
    for found in extraction.urls:
        canonical = canonicalise(found.url, identity=identity)
        entry = PlannedUrl(
            index=len(plan.urls),
            raw=found.url,
            canonical=canonical,
            status="ok",
            assumed_scheme=found.assumed_scheme,
            line=found.line,
        )
        if (first := seen.get(canonical)) is not None:
            entry.status = DUPLICATE
            entry.note = f"same product as link {first + 1}"
        else:
            seen[canonical] = entry.index
        plan.urls.append(entry)

    # Last, so a link's position in the output is its position in the paste. An
    # unparsed fragment never becomes a row, so where it sits in the index is
    # only ever a reporting detail -- it carries its real line number for that.
    for leftover in extraction.unparsed:
        plan.urls.append(
            PlannedUrl(
                index=len(plan.urls),
                raw=leftover.text,
                canonical="",
                status=INVALID,
                note="not a link",
                line=leftover.line,
            )
        )

    return plan


# ---------------------------------------------------------------------------
# job.json
# ---------------------------------------------------------------------------


def write_job_json(paths: JobPaths, job_id: str, settings_used: dict[str, object]) -> None:
    """Six weeks later, "why is this CSV different from that one" has an answer."""
    paths.root.mkdir(parents=True, exist_ok=True)
    paths.job_json.write_text(
        json.dumps(
            {
                "job_id": job_id,
                "tool_version": __version__,
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
                **settings_used,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )


def update_job_json(paths: JobPaths, **fields: object) -> None:
    if not paths.job_json.exists():
        return
    data = json.loads(paths.job_json.read_text(encoding="utf-8"))
    data.update(fields)
    paths.job_json.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------


def render_listings(
    ledger: Ledger, job_id: str, paths: JobPaths, settings: Settings, mode: ImageMode
) -> int:
    """Rebuild listings.csv from the ledger, with human edits applied.

    The re-export. Two things make it safe to run at any time:

    The stored record is never touched -- `apply_edits` returns a copy, so the
    ledger keeps saying what the page said and this file says what the operator
    decided. Remove an edit and the next export reverts that cell, which is what
    "preserved alongside, never destructively overwritten" has to mean if it is
    to mean anything.

    And it goes through the same OrderedCsvWriter the live run uses, so a
    re-exported file is byte-identical to one that was never edited: same
    header, same order, same 19 columns.
    """
    from .edits import apply_edits
    from .output.ordered_writer import OrderedCsvWriter

    edits = ledger.edits_for(job_id)
    outcomes = {
        int(row["input_index"]): row["outcome"] for row in ledger.job_inputs(job_id)
    }
    by_key: dict[str, ProductRecord] = {}
    for payload in ledger.iter_payloads(job_id):
        stored = ProductRecord.model_validate_json(payload)
        by_key[stored.row_key] = (
            apply_edits(stored, edits[stored.row_key], settings)
            if stored.row_key in edits
            else stored
        )

    written = 0
    paths.root.mkdir(parents=True, exist_ok=True)
    with OrderedCsvWriter(paths.listings, settings.config, mode) as listings:
        for row in ledger.job_inputs(job_id):
            index = int(row["input_index"])
            record = by_key.get(row["row_key"]) if row["row_key"] else None
            if record is not None and outcomes.get(index) in IN_LISTINGS:
                listings.add(index, record)
            else:
                listings.skip(index)
        written = listings.written

    # The companion, from the same ordered source. Rebuilt here as well as at
    # finalise so a re-export after edits updates both files together -- two
    # exports where one is stale would be worse than one export.
    render_with_images(ledger, job_id, paths, settings, mode)
    return written


def add_to_master(
    ledger: Ledger,
    job_id: str,
    settings: Settings,
    mode: ImageMode,
    on_duplicate: str = "skip",
) -> MasterStats:
    """Fold this job's listed rows into `runs/master.csv`, in input order.

    Called on COMPLETION only, and by exactly one caller per run. A cancelled
    job's rows are real and downloadable from the job itself; folding half of
    them into the working sheet would leave an operator unable to tell which
    half they were looking at.

    Human edits are applied first, for the same reason `render_listings` applies
    them: the sheet an operator uploads should say what they decided, not what
    the page said before they corrected it.
    """
    from .edits import apply_edits
    from .output.master import append, master_path

    edits = ledger.edits_for(job_id)
    by_key: dict[str, ProductRecord] = {}
    for payload in ledger.iter_payloads(job_id):
        stored = ProductRecord.model_validate_json(payload)
        by_key[stored.row_key] = (
            apply_edits(stored, edits[stored.row_key], settings)
            if stored.row_key in edits
            else stored
        )

    records: list[ProductRecord] = []
    canonicals: list[str] = []
    for row in ledger.job_inputs(job_id):
        if row["outcome"] not in IN_LISTINGS or not row["row_key"]:
            continue
        if (record := by_key.get(row["row_key"])) is not None:
            records.append(record)
            # The planner's canonical, not the record's, so the sheet dedupes on
            # exactly what the job deduped on.
            canonicals.append(row["canonical"] or record.canonical_url)

    return append(
        records,
        canonicals,
        master_path(settings.root, settings.config),
        settings.config,
        job_id=job_id,
        on_duplicate=on_duplicate,
        image_mode=mode,
    )


def render_with_images(
    ledger: Ledger, job_id: str, paths: JobPaths, settings: Settings, mode: ImageMode
) -> int:
    """`listings_with_images.csv`, from the ledger in input order.

    A projection like review.csv and the manifest, not a live file: it is
    regenerated at the end of a job and again on every download, which is what
    keeps it from ever disagreeing with `listings.csv`.

    Human edits are applied for the same reason `render_listings` applies them:
    this is the operator's own record, and it should say what they decided.
    """
    from .edits import apply_edits

    edits = ledger.edits_for(job_id)
    outcomes = {int(r["input_index"]): r["outcome"] for r in ledger.job_inputs(job_id)}
    by_key: dict[str, ProductRecord] = {}
    for payload in ledger.iter_payloads(job_id):
        stored = ProductRecord.model_validate_json(payload)
        by_key[stored.row_key] = (
            apply_edits(stored, edits[stored.row_key], settings)
            if stored.row_key in edits
            else stored
        )

    ordered = [
        record
        for row in ledger.job_inputs(job_id)
        if row["row_key"]
        and outcomes.get(int(row["input_index"])) in IN_LISTINGS
        and (record := by_key.get(row["row_key"])) is not None
    ]
    paths.root.mkdir(parents=True, exist_ok=True)
    return with_images.write(paths.listings_with_images, ordered, settings.config, mode)


def render_projections(
    ledger: Ledger, job_id: str, paths: JobPaths, settings: Settings, mode: ImageMode
) -> dict[str, int]:
    """Rebuild review.csv, image_manifest.csv and failed.csv from the ledger.

    Streamed one record at a time and written in input order. Cheap enough to do
    at the end of every job and again on every download, which is what keeps
    them from ever disagreeing with the ledger.
    """
    cfg = settings.config
    counts = {"review": 0, "manifest": 0, "failed": 0}
    paths.root.mkdir(parents=True, exist_ok=True)

    # The image companion is a projection too, and regenerating it here means a
    # finished job has both files without the batch having to remember.
    counts["with_images"] = render_with_images(ledger, job_id, paths, settings, mode)

    # Both halves of failed.csv: the site declined, or we could not extract.
    # They are written to one file with a `class` column rather than two files,
    # because the operator's action is the same shape -- filter, then decide --
    # and two files would mean two places to look for one URL.
    failures_by_url = {
        r["source_url"]: (r["reason"] or "unknown")
        for r in ledger.job_inputs(job_id)
        if r["outcome"] in (FAILED, REFUSED)
    }

    with (
        ReviewWriter(paths.review, cfg) as review,
        FailedWriter(paths.failed, cfg) as failed,
    ):
        manifest = ManifestWriter(paths.manifest, cfg) if mode.need_file else None
        if manifest is not None:
            manifest.__enter__()
        try:
            for payload in ledger.iter_payloads(job_id):
                record = ProductRecord.model_validate_json(payload)
                if review.write(record):
                    counts["review"] += 1
                if manifest is not None:
                    manifest.write(record)
                failures_by_url.pop(record.source_url, None)
                if record.failure_reason:
                    failed.write(record)
                    counts["failed"] += 1
        finally:
            if manifest is not None:
                manifest.__exit__(None, None, None)
                counts["manifest"] = manifest.written

        # URLs that never became a record at all: robots-disallowed, or never
        # reached because the job was cancelled. They belong here precisely
        # because this file is the one an operator re-runs.
        for url, reason in failures_by_url.items():
            failed.write_url(url, reason)
            counts["failed"] += 1

    return counts


# ---------------------------------------------------------------------------
# The accounting assertion
# ---------------------------------------------------------------------------


class UnaccountedUrls(Exception):
    """A URL reached the end of a job in no output file. Never swallowed."""


def assert_accounted(ledger: Ledger, job_id: str) -> dict[str, int]:
    """Section 2.4, enforced rather than intended.

        listed + failed == unique inputs

    Raises rather than logs. A job that cannot say where every URL went has
    produced a CSV nobody should trust, and the loudest possible moment to find
    that out is before the operator uploads it.
    """
    counts = ledger.outcome_counts(job_id)
    if missing := ledger.unaccounted(job_id):
        shown = ", ".join(str(i + 1) for i in missing[:10])
        raise UnaccountedUrls(
            f"{len(missing)} input line(s) finished with no recorded outcome "
            f"(line {shown}{'…' if len(missing) > 10 else ''}). "
            "Every pasted URL must end up in listings.csv or failed.csv; this job "
            "cannot say where these went, so its CSV should not be trusted."
        )

    # written + needs_human + refused + failed == every URL that was attempted.
    # Asserted rather than assumed: three inputs produced six output rows for a
    # whole release because nothing checked this sum.
    attempted = sum(counts.get(state, 0) for state in (*TERMINAL, LISTED))
    expected = sum(counts.values()) - counts.get(DUPLICATE, 0) - counts.get(INVALID, 0)
    if attempted != expected:
        breakdown = " + ".join(
            f"{state}({counts.get(state, 0)})" for state in TERMINAL if counts.get(state)
        )
        raise UnaccountedUrls(
            f"accounting does not balance: {breakdown or 'nothing'} = {attempted}, "
            f"expected {expected} unique input(s)."
        )
    return counts
