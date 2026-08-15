"""`diagnose` -- why this page produced the image it did, on one screen.

The defect this closes: a row that ends with `image: none` tells the operator
nothing they can act on. Zero candidates and six candidates that each failed
predicate 6 look identical from the outside, and so does a captcha wall. The
only way to tell them apart was to read the extractor.

So this module runs the real path -- the same fetcher, the same structured
extraction, the same collection rules, the same nine predicates in the same
order -- and reports every step instead of only the verdict. Nothing here
re-implements a decision; where it needs to know something the pipeline knows,
it calls the pipeline's own function and asks.

WHAT IT COSTS. One page fetch, plus one HEAD (occasionally a ranged GET) per
image candidate until one passes, plus one hotlink request for the winner. No
CSV is written, no image is kept, and no image host can be reached -- Tier 2
is not on this path at all.

WHAT IT DELIBERATELY DOES NOT DO. It does not try harder than the pipeline. If
a page blocks us, the report says so and stops; the remedy offered is an export
from the operator's own account, never a way around the block.
"""

from __future__ import annotations

import time
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx
from pydantic import BaseModel, Field, computed_field
from selectolax.parser import HTMLParser

from .config import Settings
from .extract.images import RULES, CollectionTrace, collect_candidates
from .extract.structured import extract_structured
from .fetch.rendered import RenderUnavailable
from .fetch.shape import inspect as inspect_shape
from .fetch.static import FetchError, fetch_static
from .images.reasons import NoImageReason, explain
from .images.validator import EVALUATION_ORDER, Tier1Validator
from .models import ImageMethod, ProductRecord, Provenance, ValidationResult
from .pipeline import extract_into, incomplete_reasons, merge_rendered, new_record
from .utils.logging import get_logger
from .utils.netguard import BlockedHost
from .utils.robots import RobotsCache

if TYPE_CHECKING:
    from .extract.plugins import PluginRegistry
    from .fetch.ladder import LadderOutcome
    from .fetch.rendered import Renderer
    from .store.ledger import Ledger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# The report. Pydantic rather than dataclasses so /api/diagnose serialises it
# without a second set of wire models to keep in step.
# ---------------------------------------------------------------------------


class Check(StrEnum):
    """§3.1. A check has three answers, and the third one is not `no`.

    The defect: `bot check: no` printed about a page that never arrived. It is
    a true statement about a variable and a false one about the world, and it
    sends whoever reads it to the extractor for a transport bug.

    Modelled as a value rather than a bool-plus-an-`evaluated`-flag because the
    flag has to be remembered at every render site, and it was not. NOT_REACHED
    is the DEFAULT, so a report that never ran a check says so without anyone
    having written a branch for it.
    """

    YES = "yes"
    NO = "no"
    NOT_REACHED = "not reached"

    @classmethod
    def of(cls, value: bool | None) -> Check:
        """`None` means the check did not run -- never that it ran and said no."""
        if value is None:
            return cls.NOT_REACHED
        return cls.YES if value else cls.NO

    @property
    def ran(self) -> bool:
        return self is not Check.NOT_REACHED

    def __bool__(self) -> bool:
        """Truthy only for YES, so `if shape.captcha:` cannot fire on a check
        that never ran -- which is how the report claimed findings it did not
        have."""
        return self is Check.YES


class AttemptReport(BaseModel):
    """§3.2. One rung, what it got, how long it took.

    Kept for successful climbs as well as failed ones: `a1_http2` reset and
    `a2_http11` answering in 900ms is the whole story of a host that needs
    HTTP/1.1, and it is invisible if only the winner is reported.
    """

    rung: str
    transport: str
    outcome: str
    elapsed_ms: int
    ok: bool = False
    detail: str = ""


class StageBState(StrEnum):
    """§3.3. `off` is not a reason, and the three reasons are not the same.

    An operator seeing `off` cannot tell whether to pass `--render`, fix the
    URL, or stop -- and those are the three different next actions behind it.
    """

    RAN = "ran"
    FAILED = "tried and failed"
    DISABLED = "disabled (--no-browser)"
    NOT_NEEDED = "not needed (stage A complete)"
    NO_RESPONSE = "not attempted -- stage A returned no response"
    # Stage A failed, a browser was available, and the failure is one no browser
    # reads differently: a name that does not resolve, a 404, a blocked address.
    POINTLESS = "not attempted -- a browser would get the same answer"


class FetchReport(BaseModel):
    ok: bool = False
    # Which rung answered, and the whole climb. An operator seeing
    # `a2_http11` learns that this host needs HTTP/1.1 without reading a log.
    rung: str = ""
    rungs_tried: str = ""
    status_code: int | None = None
    content_type: str = ""
    bytes: int = 0
    elapsed_ms: int = 0
    final_url: str = ""
    redirected: bool = False
    robots_checked: bool = False
    robots_allowed: bool = True
    error_reason: str = ""
    error_detail: str = ""
    # §3.2. Every rung, in order, whether or not one of them answered.
    attempts: list[AttemptReport] = Field(default_factory=list)


class ShapeReport(BaseModel):
    """What the page turned out to be -- or that we never got to look.

    Every field defaults to NOT_REACHED, so a fetch that returned no HTML
    reports six unanswered questions rather than six clean bills of health.
    """

    looks_like_product: Check = Check.NOT_REACHED
    captcha: Check = Check.NOT_REACHED
    login_wall: Check = Check.NOT_REACHED
    unavailable: Check = Check.NOT_REACHED
    # WHERE the availability marker sat. `elsewhere on page` on a listing with a
    # live buy box is the false positive §3 exists to kill, and seeing which one
    # fired is how the next one gets caught at a glance.
    unavailable_in_buy_box: Check = Check.NOT_REACHED
    buy_box_found: Check = Check.NOT_REACHED
    thin: Check = Check.NOT_REACHED
    verdict: str = ""
    evidence: list[str] = Field(default_factory=list)
    product_signals: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def evaluated(self) -> bool:
        """Derived, not stored. A flag that can disagree with the checks it
        describes is one more thing to keep in step, and this one did not."""
        return self.looks_like_product.ran


class TitleReport(BaseModel):
    value: str = ""
    source: str = ""
    confidence: str = ""
    note: str = ""


class RuleReport(BaseModel):
    """One collection rule and what it yielded. Zero is the interesting value."""

    rule: str
    found: int = 0


class DropReport(BaseModel):
    url: str
    why: str


class StepReport(BaseModel):
    predicate: int
    name: str
    # ok | fail | not reached | skipped
    outcome: str
    detail: str = ""


class CandidateReport(BaseModel):
    index: int
    url: str
    rule: str = ""
    source: str = ""
    checked: bool = False
    ok: bool = False
    reason: str = ""
    stopped_at: int | None = None
    width: int | None = None
    height: int | None = None
    content_type: str = ""
    content_length: int | None = None
    steps: list[StepReport] = Field(default_factory=list)


class StageBReport(BaseModel):
    """Whether a browser ran, what it was worth, and if not -- why not."""

    enabled: bool = False
    state: StageBState = StageBState.DISABLED
    triggers: list[str] = Field(default_factory=list)
    attempted: bool = False
    ok: bool = False
    error: str = ""
    gained: list[str] = Field(default_factory=list)
    candidates_before: int = 0
    candidates_after: int = 0


class ThresholdsReport(BaseModel):
    min_width: int
    min_height: int
    min_bytes: int
    max_images_per_product: int
    hotlink_test: bool


class ImageReport(BaseModel):
    # §3.1 again, for the section rather than a single check. `kept 0 of 0` is
    # a true sentence about a gallery nobody looked at, and it reads exactly
    # like a page whose gallery was empty.
    collected: bool = False
    rules: list[RuleReport] = Field(default_factory=list)
    raw_found: int = 0
    dropped: list[DropReport] = Field(default_factory=list)
    # The list the pipeline would actually walk. Differs from the rule counts
    # when a plugin supplies its own -- which is the point of a plugin, and
    # worth seeing rather than inferring.
    candidates: list[CandidateReport] = Field(default_factory=list)
    plugin_used: str = ""
    plugin_replaced_candidates: bool = False
    method: str = ImageMethod.NONE.value
    winner: str = ""
    reason: str = ""
    explanation: str = ""


class Diagnosis(BaseModel):
    url: str
    elapsed_ms: int = 0
    fetch: FetchReport = Field(default_factory=FetchReport)
    shape: ShapeReport = Field(default_factory=ShapeReport)
    title: TitleReport = Field(default_factory=TitleReport)
    stage_b: StageBReport = Field(default_factory=StageBReport)
    images: ImageReport = Field(default_factory=ImageReport)
    thresholds: ThresholdsReport
    structured_syntaxes: list[str] = Field(default_factory=list)

    # True since Phase 3: `pipeline.py` fails a row on a page-shape verdict
    # rather than writing it out half-extracted. Kept as a field rather than
    # dropped, because a report that silently changed meaning between versions
    # is the kind of thing that gets believed for a year.
    shape_enforced: bool = True


# ---------------------------------------------------------------------------
# Predicate reconstruction
# ---------------------------------------------------------------------------


def human_bytes(count: int | None) -> str:
    if count is None:
        return "?"
    if count < 1024:
        return f"{count} B"
    if count < 1024 * 1024:
        return f"{count / 1024:.0f} KB"
    return f"{count / (1024 * 1024):.1f} MB"


def _detail_for(predicate: int, result: ValidationResult) -> str:
    """What the walk learned at this predicate, from data the result carries."""
    if predicate == 4:
        return (result.content_type or "").split(";")[0].strip()
    if predicate == 5:
        return human_bytes(result.content_length)
    if predicate == 6 and result.width and result.height:
        return f"{result.width}x{result.height}"
    return ""


def steps_for(result: ValidationResult, hotlink_test: bool) -> list[StepReport]:
    """Expand one result into the nine predicates, in evaluation order.

    A ValidationResult names only the predicate that decided it. That is the
    right thing to store and the wrong thing to show: "failed 6" leaves the
    operator to work out that 1, 8, 9, 2, 3, 4 and 5 all passed, which is most
    of what tells them whether the URL is nearly right or nowhere near.
    """
    steps: list[StepReport] = []
    stopped = result.predicate if not result.ok else None
    reached = True

    for number, name in EVALUATION_ORDER:
        if number == 7 and not hotlink_test:
            steps.append(
                StepReport(
                    predicate=7,
                    name=name,
                    outcome="skipped",
                    detail="hotlink_test is off; this result is optimistic",
                )
            )
            continue
        if not reached:
            steps.append(StepReport(predicate=number, name=name, outcome="not reached"))
        elif stopped == number:
            steps.append(
                StepReport(
                    predicate=number,
                    name=name,
                    outcome="fail",
                    detail=result.reason
                    + (f" ({extra})" if (extra := _detail_for(number, result)) else ""),
                )
            )
            reached = False
        else:
            steps.append(
                StepReport(
                    predicate=number,
                    name=name,
                    outcome="ok",
                    detail=_detail_for(number, result),
                )
            )

    return steps


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


async def diagnose_url(
    url: str,
    settings: Settings,
    *,
    ignore_robots: bool = False,
    render: bool | None = None,
    check_all: bool = False,
    no_hotlink_test: bool = False,
) -> Diagnosis:
    """Fetch one page and explain its image outcome. Never raises for a bad page.

    `check_all` walks every candidate instead of stopping at the first that
    passes. Off by default because stopping early is what the pipeline does, and
    a report of a run that did not happen is worth less than a slower one.
    """
    from .extract.plugins import build_registry
    from .fetch.rendered import build_renderer
    from .fetch.static import build_client
    from .store.ledger import Ledger

    cfg = settings.config
    started = time.perf_counter()

    report = Diagnosis(
        url=url,
        thresholds=ThresholdsReport(
            min_width=cfg.validator.min_width,
            min_height=cfg.validator.min_height,
            min_bytes=cfg.validator.min_bytes,
            max_images_per_product=cfg.images.max_images_per_product,
            hotlink_test=cfg.validator.hotlink_test and not no_hotlink_test,
        ),
    )

    with Ledger(settings.root / cfg.paths.ledger) as ledger:
        async with build_client(settings) as client:
            robots = (
                None
                if ignore_robots or not cfg.fetch.respect_robots
                else RobotsCache(client, settings.user_agent)
            )
            renderer = build_renderer(settings, render)
            plugins = build_registry(cfg, settings.root)
            try:
                await _run(report, url, settings, client, robots, renderer, plugins, ledger,
                           check_all=check_all, no_hotlink_test=no_hotlink_test)
            finally:
                if renderer is not None and renderer.started:
                    await renderer.close()

    report.elapsed_ms = int((time.perf_counter() - started) * 1000)
    return report


async def _run(
    report: Diagnosis,
    url: str,
    settings: Settings,
    client: httpx.AsyncClient,
    robots: RobotsCache | None,
    renderer: Renderer | None,
    plugins: PluginRegistry | None,
    ledger: Ledger,
    *,
    check_all: bool,
    no_hotlink_test: bool,
) -> None:
    cfg = settings.config

    # --- robots -----------------------------------------------------------
    if robots is not None:
        report.fetch.robots_checked = True
        report.fetch.robots_allowed = await robots.allowed(url)
        if not report.fetch.robots_allowed:
            _conclude(report, NoImageReason.ROBOTS_DISALLOWED)
            return

    # --- Stage A ----------------------------------------------------------
    try:
        fetched = await fetch_static(client, url, settings)
    except FetchError as exc:
        report.fetch.error_reason = exc.reason
        report.fetch.error_detail = exc.detail
        report.fetch.rungs_tried = exc.rungs_tried
        report.fetch.attempts = _attempts(exc.outcome)

        # The browser, on the same condition `process_url` uses. Without this
        # the report would say "could not fetch" about a URL the real run
        # resolves -- the same diagnose-disagrees-with-the-pipeline defect the
        # v3 pass fixed for bot checks.
        report.stage_b.enabled = cfg.render.enabled and renderer is not None
        rendered = None
        if renderer is None:
            report.stage_b.state = StageBState.DISABLED
        elif not exc.should_escalate_to_browser:
            # §3.3. Not "off": a browser was right here and was not used,
            # because a name that does not resolve does not resolve in Chromium
            # either. Saying which of those it is saves an experiment.
            report.stage_b.state = StageBState.POINTLESS
            report.stage_b.error = f"stage A ended in {exc.reason}"
        else:
            report.stage_b.attempted = True
            report.stage_b.triggers = ["transport failure on every HTTP rung"]
            try:
                rendered = await renderer.fetch(url)
                report.stage_b.ok = True
                report.stage_b.state = StageBState.RAN
            except (RenderUnavailable, FetchError) as exc_render:
                report.stage_b.error = str(exc_render)[:200]
                report.stage_b.state = StageBState.FAILED

        if rendered is None:
            # `shape.evaluated` stays False: no HTML arrived, so every
            # page-shape line is `not evaluated` rather than `no`.
            from .pipeline import _image_reason_for

            _conclude(report, _image_reason_for(exc))
            # The same word the job page and Find photos will show for this
            # event. Three screens naming one thing three ways is what §2 exists
            # to stop.
            report.images.reason = _image_reason_for(exc).value
            return
        fetched = rendered
        report.fetch.rung = "b_browser"

    report.fetch.ok = True
    report.fetch.status_code = fetched.status_code
    report.fetch.content_type = fetched.headers.get("content-type", "").split(";")[0].strip()
    report.fetch.bytes = len(fetched.html.encode("utf-8", "ignore"))
    report.fetch.elapsed_ms = fetched.elapsed_ms
    report.fetch.final_url = fetched.final_url
    report.fetch.redirected = fetched.final_url != url
    report.fetch.rung = fetched.rung.value if fetched.rung else ""
    report.fetch.rungs_tried = fetched.rungs_tried
    # Reported on the way UP too: two rungs that failed before the third
    # answered is the whole diagnosis for a host that needs HTTP/1.1.
    report.fetch.attempts = _attempts(getattr(fetched, "outcome", None))

    # --- what kind of page is this ----------------------------------------
    dom = HTMLParser(fetched.html)
    structured = extract_structured(fetched.html, fetched.final_url, dom)
    shape = inspect_shape(
        fetched.html, fetched.final_url, dom, has_product_node=structured.product is not None
    )
    report.shape = ShapeReport(
        looks_like_product=Check.of(shape.looks_like_product),
        captcha=Check.of(shape.captcha),
        login_wall=Check.of(shape.login_wall),
        unavailable=Check.of(shape.unavailable),
        unavailable_in_buy_box=Check.of(shape.unavailable_in_buy_box),
        buy_box_found=Check.of(shape.buy_box_found),
        thin=Check.of(shape.thin),
        verdict=shape.verdict.value if shape.verdict else "",
        evidence=list(shape.evidence),
        product_signals=list(shape.product_signals),
    )

    # A page that is not the product page stops here, exactly as `process_url`
    # stops. Anything past this point would be describing a bot check as though
    # it were a shop.
    #
    # Caught by pointing this at the demo shop's own bot-check page: the report
    # said `direct` and named the captcha image as the product photo, because
    # the verdict was only consulted as a fallback when no candidates were
    # found. A diagnostic that disagrees with the pipeline about the exact case
    # it was built for is worse than no diagnostic.
    if shape.verdict is not None:
        _fill_title(report, _extracted(url, settings, fetched, plugins))
        _conclude(report, shape.verdict)
        return

    # --- the real extraction, plugins and all ------------------------------
    record = new_record(url, Provenance.OWN, settings.identity)
    extract_into(record, fetched.html, fetched.final_url, settings, plugins)

    # The same collection call, instrumented. Run separately rather than
    # threaded through `extract_into` so that nothing on the pipeline's path
    # changes shape to support a diagnostic.
    trace = CollectionTrace()
    generic = collect_candidates(
        structured, dom, fetched.final_url, cfg.images, cfg.validator, trace
    )

    if plugins is not None and (plugin := plugins.match(url, fetched.html)) is not None:
        report.images.plugin_used = plugin.name

    report.images.collected = True
    report.images.rules = [
        RuleReport(rule=rule, found=count) for rule, count in _rule_counts(trace)
    ]
    report.images.raw_found = len(trace.raw)
    report.images.dropped = [DropReport(url=u, why=why) for u, why in trace.dropped]
    report.images.plugin_replaced_candidates = record.image_candidates != [c.url for c in generic]
    report.structured_syntaxes = list(record.structured_syntaxes)

    _fill_title(report, record)

    # --- Stage B, on the pipeline's own condition ---------------------------
    report.stage_b.enabled = cfg.render.enabled and renderer is not None
    report.stage_b.candidates_before = len(record.image_candidates)
    triggers = incomplete_reasons(record, cfg.render) if renderer is not None else []
    report.stage_b.triggers = list(triggers)

    if renderer is None:
        report.stage_b.state = StageBState.DISABLED
    elif not triggers:
        report.stage_b.state = StageBState.NOT_NEEDED
    else:
        await _try_render(report, record, url, settings, renderer, plugins)
    report.stage_b.candidates_after = len(record.image_candidates)

    # --- Tier 1, candidate by candidate ------------------------------------
    if not record.image_candidates:
        _conclude(report, NoImageReason.NO_IMAGE_CANDIDATES)
        return

    lookup = {c.url: c for c in generic}
    validator = Tier1Validator(
        client,
        cfg.validator,
        ledger,
        hotlink_test=not no_hotlink_test and cfg.validator.hotlink_test,
        allow_private_hosts=cfg.fetch.allow_private_hosts,
    )

    winner: ValidationResult | None = None
    for index, candidate_url in enumerate(record.image_candidates, start=1):
        origin = lookup.get(candidate_url)
        entry = CandidateReport(
            index=index,
            url=candidate_url,
            rule=origin.rule if origin else (report.images.plugin_used or "plugin"),
            source=origin.source.value if origin else "",
        )

        if winner is not None and not check_all:
            # What the pipeline does: stop at the first pass. Said out loud so
            # the report is not mistaken for a full survey of the gallery.
            report.images.candidates.append(entry)
            continue

        try:
            result = await validator.validate(candidate_url)
        except BlockedHost as exc:
            entry.checked = True
            entry.reason = "blocked_address"
            entry.steps = [
                StepReport(
                    predicate=1,
                    name="syntax",
                    outcome="fail",
                    detail=str(exc).splitlines()[0],
                )
            ]
            report.images.candidates.append(entry)
            continue

        entry.checked = True
        entry.ok = result.ok
        entry.reason = result.reason
        entry.stopped_at = result.predicate
        entry.width = result.width
        entry.height = result.height
        entry.content_type = result.content_type or ""
        entry.content_length = result.content_length
        entry.steps = steps_for(result, report.thresholds.hotlink_test)
        report.images.candidates.append(entry)

        if result.ok and winner is None:
            winner = result

    if winner is not None:
        report.images.method = ImageMethod.DIRECT.value
        report.images.winner = winner.url
        report.images.reason = "direct_ok"
        report.images.explanation = (
            f"{winner.url} passed all nine checks at {winner.width}x{winner.height}."
        )
        return

    _conclude(report, NoImageReason.ALL_CANDIDATES_REJECTED)


_TRANSPORTS = {
    "a1_http2": "HTTP/2",
    "a2_http11": "HTTP/1.1",
    "a3_http11_fresh": "HTTP/1.1, fresh connection",
    "b_browser": "headless browser",
}


def _attempts(outcome: LadderOutcome | None) -> list[AttemptReport]:
    """§3.2. The ladder's own record, rendered rather than re-derived.

    `LadderOutcome` already holds this. Parsing it back out of the packed
    `rungs_tried` string would be a second implementation of the climb, and the
    two would disagree the first time either changed.
    """
    if outcome is None:
        return []
    return [
        AttemptReport(
            rung=attempt.rung.value,
            transport=_TRANSPORTS.get(attempt.rung.value, attempt.rung.value),
            outcome=(
                str(attempt.status)
                if attempt.ok
                else (attempt.kind.value if attempt.kind else "failed")
            ),
            elapsed_ms=attempt.elapsed_ms,
            ok=attempt.ok,
            detail=attempt.detail[:160],
        )
        for attempt in outcome.attempts
    ]


def _rule_counts(trace: CollectionTrace) -> list[tuple[str, int]]:
    counts = trace.by_rule()
    return [(rule, counts.get(rule, 0)) for rule in RULES]


def _fill_title(report: Diagnosis, record) -> None:  # noqa: ANN001 -- ProductRecord
    title = record.title
    report.title = TitleReport(
        value=title.value or "",
        source=title.source.value if title.source else "",
        confidence=title.confidence.value,
        note=title.note or "",
    )


async def _try_render(
    report: Diagnosis,
    record,  # noqa: ANN001 -- ProductRecord
    url: str,
    settings: Settings,
    renderer: Renderer,
    plugins: PluginRegistry | None,
) -> None:
    """Stage B, reported rather than folded in silently."""
    from .fetch.rendered import RenderUnavailable

    report.stage_b.attempted = True
    try:
        fetched = await renderer.fetch(url)
    except RenderUnavailable as exc:
        report.stage_b.error = str(exc)
        report.stage_b.state = StageBState.FAILED
        return
    except FetchError as exc:
        report.stage_b.error = f"{exc.reason}: {exc.detail}" if exc.detail else exc.reason
        report.stage_b.state = StageBState.FAILED
        return

    fresh = new_record(record.source_url, record.provenance, settings.identity)
    extract_into(fresh, fetched.html, fetched.final_url, settings, plugins)
    report.stage_b.ok = True
    report.stage_b.state = StageBState.RAN
    report.stage_b.gained = merge_rendered(record, fresh)


def _extracted(
    url: str, settings: Settings, fetched: object, plugins: PluginRegistry | None
) -> ProductRecord:
    """A record from a page we are about to reject, for the title line only.

    Worth showing: "the title on this row came from a challenge page" is more
    use to an operator than a blank, and it is exactly what they would otherwise
    have seen in the CSV.
    """
    record = new_record(url, Provenance.OWN, settings.identity)
    extract_into(record, fetched.html, fetched.final_url, settings, plugins)  # type: ignore[attr-defined]
    return record


def _conclude(report: Diagnosis, reason: NoImageReason) -> None:
    report.images.method = ImageMethod.NONE.value
    report.images.reason = reason.value
    report.images.explanation = explain(reason)
