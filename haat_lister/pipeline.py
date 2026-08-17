"""Per-URL orchestration: fetch -> extract -> (enrich) -> (image) -> record.

`single` and `batch` both call `process_url`, so they cannot drift apart. Batch
mode is this coroutine plus a semaphore and a rate limiter, nothing more.

Phase 2 scope: Stage A fetch, structured extraction, title, description, image
candidates. Price, dimensions, variants, enrichment, policy screening and the
image pipeline arrive in later phases and slot in at the marked points.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from selectolax.parser import HTMLParser

from .config import FieldsConfig, RenderConfig, Settings
from .enrich.category import classify, validate_slugs
from .enrich.fx import convert as convert_price
from .enrich.hs_code import suggest as suggest_hs_code
from .enrich.rewrite import LlmEnricher, rewrite_wanted_for
from .extract.description import extract_description
from .extract.dimensions import (
    AXES,
    NO_WEIGHT_NOTE,
    extract_dimensions,
    no_dimensions_note,
)
from .extract.images import collect_candidates
from .extract.plugins import (
    PluginContext,
    PluginError,
    PluginRegistry,
    apply_result,
)
from .extract.price import NO_PRICE_NOTE, extract_price
from .extract.structured import extract_structured
from .extract.title import extract_title, tidy_title
from .extract.variants import NO_AVAILABILITY_NOTE, extract_variants
from .fetch.budget import BudgetExhausted, Spend, UrlBudget, budget_for
from .fetch.rendered import INSTALL_HINT, Renderer, RenderUnavailable
from .fetch.shape import inspect as inspect_shape
from .fetch.static import FetchError, FetchResult, fetch_static
from .images.pipeline import ImageResolver, apply_to_record
from .images.reasons import NoImageReason, explain
from .models import (
    Confidence,
    DescriptionMode,
    FetchStage,
    FieldSource,
    FieldValue,
    PriceStrategy,
    ProductRecord,
    Provenance,
    RowStatus,
)
from .policy.provenance import apply_gate
from .policy.screen import describe, gi_mentions, load_vocabulary, screen_text
from .utils.canonical import DEFAULT_IDENTITY, Identity
from .utils.logging import get_logger
from .utils.robots import ROBOTS_GUIDANCE, RobotsCache, RobotsDecision
from .utils.urls import canonicalise, row_key

log = get_logger(__name__)

# Progress reporting, in the core rather than the API: `single` and `batch` and
# the web console all want it, and a second copy in api/ would be the exact
# duplication the architecture rule warns about.
#
# The stages named here are the ones this coroutine can honestly observe. The
# finer image sub-stages a UI might want -- tier1 vs download vs upload -- would
# need `images/pipeline.py` to report them, and that module owns the Rule 1 gate
# and is not to be touched. `ImageResult.method` on the finished row says which
# path was taken, which is the same information after the fact and is not a
# spinner pretending to know something.
StageFn = Callable[[str], None]

STAGES: tuple[str, ...] = (
    "fetching",
    "extracting",
    "rendering",
    "enriching",
    "images",
    "written",
)


def _no_stage(_: str) -> None:
    """The default. Costs one call per stage and keeps the signature honest."""


async def _escalate_to_browser(
    record: ProductRecord,
    url: str,
    exc: FetchError,
    settings: Settings,
    renderer: Renderer | None,
) -> FetchResult | None:
    """Rung B. Returns a page, or None and the row fails with the A-rung reason.

    §2.4's rules live here as one expression: the ladder decides whether the
    failure is the KIND that a browser could help with, and this decides whether
    a browser is available to try.
    """
    if not exc.should_escalate_to_browser:
        return None
    if renderer is None:
        # "stage B: off" with no explanation was the old failure. The row now
        # says which rung it needed and why it could not have it.
        record.note(
            "This site refused every HTTP attempt, which usually means it wants a real browser. "
            "Stage B is switched off for this run, so it was not tried -- turn on --render, or "
            "install it with: playwright install chromium"
        )
        return None

    try:
        rendered = await renderer.fetch(url)
    except RenderUnavailable as exc_render:
        record.image.none_reason = NoImageReason.BROWSER_UNAVAILABLE
        record.note(
            f"This site refused every HTTP attempt and needs a real browser, but one is not "
            f"installed. {INSTALL_HINT} ({exc_render})"
        )
        return None
    except FetchError as exc_render:
        record.note(
            f"This site refused every HTTP attempt ({exc.reason}); a browser was tried and "
            f"also refused ({exc_render.reason})."
        )
        return None

    record.note(
        f"HTTP was refused ({exc.reason}), so the page was fetched with a browser instead. "
        f"Rungs tried: {exc.rungs_tried or 'none recorded'}."
    )
    return rendered


def _fail_fetch(
    record: ProductRecord, exc: FetchError, renderer: Renderer | None
) -> ProductRecord:
    """End a row the fetcher could not start, naming the transport cause.

    The fetcher's own reason stays as the row's failure -- `timeout_read` and
    `http_404` are more use to whoever is fixing it than a category -- while the
    image side gets the closed enum, because that is what the UI groups on.
    """
    reason = record.image.none_reason or _image_reason_for(exc)
    record.image.none_reason = reason
    # The row's failure is the ENUM, everywhere (§2.1). The fetcher's own word
    # -- `http_503`, `transport_reset` -- keeps its place in the diagnostic
    # slot, because it is the finer detail behind the enum rather than a second
    # vocabulary competing with it. Two names for one event is what made the job
    # page and `diagnose` disagree.
    record.image.reason = exc.reason
    record.http_status = _status_of(exc.reason)
    record.fail(reason.value)
    record.rungs_tried = exc.rungs_tried
    record.notes.append(
        f"Could not fetch the page: {exc.reason}."
        + (f" Tried: {exc.rungs_tried}." if exc.rungs_tried else "")
    )
    record.fetch_stage = FetchStage.FAILED
    return record


# The ladder's transport vocabulary, mapped onto the operator-facing one.
# One name per outcome (§2): the job page, `diagnose` and Find photos all read
# this table, so they cannot disagree about what happened the way they did when
# one said a catch-all and another said `timeout_read`.
_FETCH_REASONS: dict[str, NoImageReason] = {
    "transport_reset": NoImageReason.TIMEOUT_READ,
    "timeout_read": NoImageReason.TIMEOUT_READ,
    "timeout_connect": NoImageReason.TIMEOUT_CONNECT,
    # The port said no. Its `what_to_do` -- "the connection was never
    # established; if the shop is up in your browser this is usually the
    # network between you and it" -- is the right next action, so this needs
    # a transport word of its own rather than a reason of its own.
    "connection_refused": NoImageReason.TIMEOUT_CONNECT,
    "tls_error": NoImageReason.TIMEOUT_CONNECT,
    "dns_error": NoImageReason.DNS_FAILURE,
    "http_401": NoImageReason.BLOCKED_403,
    "http_403": NoImageReason.BLOCKED_403,
    "http_429": NoImageReason.BLOCKED_429,
    "not_html": NoImageReason.NOT_A_PRODUCT_PAGE,
    "too_many_redirects": NoImageReason.NOT_A_PRODUCT_PAGE,
    "blocked_address": NoImageReason.NOT_A_PRODUCT_PAGE,
}


def _status_of(reason: str) -> int | None:
    """The HTTP status out of a fetcher word like `http_503`, for failed.csv.

    Kept separate now that `failure_reason` is the enum: `http_error_5xx` is the
    right word for an operator and the wrong one for someone sorting a
    spreadsheet by status.
    """
    if reason.startswith("http_") and (tail := reason[5:]).isdigit():
        return int(tail)
    return None


def _image_reason_for(exc: FetchError) -> NoImageReason:
    """The fetcher's transport word as an operator-facing reason.

    A single catch-all used to live here -- one category standing in for every
    reason -- and having it meant the job page and `diagnose` printed different
    words for one request. It is deleted rather than aliased.

    A 5xx that survived every rung is the shop's own server; anything else
    unmapped is a read that never completed, which is the honest default because
    it is what the ladder reports when a host simply stops answering.
    """
    if mapped := _FETCH_REASONS.get(exc.reason):
        return mapped
    if exc.reason.startswith("http_5"):
        return NoImageReason.HTTP_ERROR_5XX
    if exc.reason.startswith("http_4"):
        # 401/403/429 are mapped above as refusals. Everything else 4xx is a
        # statement about the URL rather than about us -- a 404 is a page that
        # is not there, and calling it a read timeout sends the operator to look
        # at their network instead of their link.
        return NoImageReason.NOT_A_PRODUCT_PAGE
    return NoImageReason.TIMEOUT_READ


def _fail(record: ProductRecord, reason: NoImageReason | str, *notes: str) -> ProductRecord:
    """End a row before it reached the image pipeline, saying why in both places.

    A row that fails at robots or at a captcha wall never gets an ImageResult,
    so without this it would carry `image_method=none` with an empty reason --
    which §4.6 calls a bug, and which is exactly the silence being fixed.
    """
    text = reason.value if isinstance(reason, NoImageReason) else reason
    record.fail(text)
    for note in notes:
        record.note(note)
    if isinstance(reason, NoImageReason):
        record.image.none_reason = reason
        record.image.reason = record.image.reason or text
    record.fetch_stage = FetchStage.FAILED
    return record


def new_record(
    url: str, provenance: Provenance, identity: Identity = DEFAULT_IDENTITY
) -> ProductRecord:
    """The row's identity, decided once.

    `identity` must be the same one `jobs.plan_urls` used. The batch matches a
    finished record back to its planned position by canonical URL, so two
    different rule tables here and there means rows that arrive and are never
    claimed -- and the accounting assertion catches it, loudly, at the end of a
    long run rather than the start.
    """
    canonical = canonicalise(url, identity=identity)
    return ProductRecord(
        row_key=row_key(canonical),
        source_url=url,
        canonical_url=canonical,
        provenance=provenance,
    )


async def process_url(
    url: str,
    provenance: Provenance,
    settings: Settings,
    client: httpx.AsyncClient,
    robots: RobotsCache | None = None,
    seller_note: str | None = None,
    description_mode: DescriptionMode = DescriptionMode.RAW,
    resolver: ImageResolver | None = None,
    renderer: Renderer | None = None,
    plugins: PluginRegistry | None = None,
    enricher: LlmEnricher | None = None,
    on_stage: StageFn | None = None,
    url_timeout_s: float | None = None,
) -> ProductRecord:
    """Build one ProductRecord. Never raises for a bad page -- it returns a
    record with `status=failed` and a reason a human can act on."""

    record = new_record(url, provenance, settings.identity)
    stage = on_stage or _no_stage

    stage("fetching")
    if robots is not None and not (decision := await robots.decide(url)).may_fetch:
        # Which of the two happened decides what to tell them. "Their rules
        # forbid this path" and "their bot wall would not show us the rules"
        # are different facts with different remedies, and the tool used to
        # report both as the first.
        reason = (
            NoImageReason.BOT_CHALLENGE
            if decision is RobotsDecision.REFUSED_SIGHT
            else NoImageReason.ROBOTS_DISALLOWED
        )
        return _fail(record, reason, ROBOTS_GUIDANCE[decision])

    # §5. One clock for the whole row, created here so every attempt below --
    # the ladder, its retries, and the browser -- draws on the same deadline
    # rather than each respecting its own limit while the total runs away.
    budget = budget_for(settings, url_timeout_s)

    try:
        # Not wrapped in `budget.spending` here: `fetch_static` attributes its
        # own climb, and nesting two spending blocks on one budget counts the
        # same seconds twice -- which showed up as a report whose parts summed
        # to more than its total.
        fetched = await fetch_static(client, url, settings, budget)
    except BudgetExhausted:
        record.time_spent = budget.report()
        return _fail(
            record,
            NoImageReason.TIMEOUT_READ,
            f"This URL used its whole {budget.limit_s:.0f}s budget without finishing. "
            f"Spent: {budget.report()}. Raise it with --url-timeout if the shop is "
            "genuinely just slow.",
        )
    except FetchError as exc:
        record.time_spent = (exc.budget or budget).report()
        # A transport failure means no HTTP response arrived at all, which is
        # the single strongest signal that the browser is needed -- and it was
        # precisely the case where the old code gave up. Escalating here rather
        # than inside the ladder because Rung B needs a browser, not a client,
        # and `renderer is None` is how "Stage B is off" is expressed.
        # The browser is fetch time too, and it is the most expensive kind.
        with budget.spending(Spend.FETCH):
            escalated = await _escalate_to_browser(record, url, exc, settings, renderer)
        if escalated is None:
            return _fail_fetch(record, exc, renderer)
        fetched = escalated

    return await process_page(
        record,
        fetched,
        settings,
        seller_note=seller_note,
        description_mode=description_mode,
        resolver=resolver,
        renderer=renderer,
        plugins=plugins,
        enricher=enricher,
        on_stage=stage,
        budget=budget,
    )


async def process_page(
    record: ProductRecord,
    fetched: FetchResult,
    settings: Settings,
    *,
    seller_note: str | None = None,
    description_mode: DescriptionMode = DescriptionMode.RAW,
    resolver: ImageResolver | None = None,
    renderer: Renderer | None = None,
    plugins: PluginRegistry | None = None,
    enricher: LlmEnricher | None = None,
    extra_candidates: list[str] | None = None,
    on_stage: StageFn | None = None,
    budget: UrlBudget | None = None,
) -> ProductRecord:
    """Everything after the bytes arrive, whoever brought them.

    Split out for v5 §4 so a saved page and a live fetch run the SAME
    extraction, the same page-shape verdict, the same Tier-1 image chain and
    the same provenance gate. The alternative -- an import path that builds its
    own record -- is the second implementation §7 forbids, and it is how an
    imported row would quietly skip the gate that exists to stop us hosting
    somebody else's photographs.

    `renderer` is accepted and normally None here: a saved page has already been
    rendered by the operator's own browser, and re-fetching the live URL to
    improve on a file they gave us is a network call they did not ask for.

    `extra_candidates` are photographs that came with the page rather than in
    it -- the contents of a `_files` folder. They are appended AFTER whatever
    the page yielded, so a real URL that works is preferred to bytes we would
    otherwise have to host; and they are appended rather than substituted, so
    the ordinary ranking is what decides between them.
    """
    url = record.source_url
    stage = on_stage or _no_stage

    record.fetched_at = datetime.now(UTC)
    record.fetch_stage = fetched.stage

    budget = budget or budget_for(settings)

    stage("extracting")
    # Where Stage A's account of the page starts, so Stage B can retract it.
    before = _Mark(notes=len(record.notes), status=record.status)
    with budget.spending(Spend.PARSE):
        extract_into(record, fetched.html, fetched.final_url, settings, plugins)

    # --- was this actually the product page? --------------------------------
    #
    # A captcha wall, a sign-in interstitial and a "no longer available" notice
    # all arrive as a 200 and extract into a tidy record with a title and no
    # photos. Reporting that as "extracted successfully, no image" is the
    # conflation that made the whole defect invisible, so the row fails here
    # instead -- before Stage B, before enrichment, before any image work.
    #
    # Stage B is skipped deliberately rather than incidentally. Re-fetching a
    # bot check with a real browser to see if it lets us through is escalation,
    # and this tool does not escalate. The remedy offered is an export from the
    # operator's own account.
    if record.page_verdict:
        # Everything the extractors just said describes a page that is not the
        # product. "No weight found" on a captcha wall sends an operator to fill
        # in a weight for something we never saw, and review.csv is only worth
        # anything if every line in it is still true. Same retraction Stage B
        # does when a render succeeds, for the same reason.
        del record.notes[before.notes :]
        record.gap_notes.clear()

        evidence = (
            ["The page said: " + "; ".join(record.page_evidence[:4]) + "."]
            if record.page_evidence
            else []
        )
        return _fail(
            record, NoImageReason(record.page_verdict), explain(record.page_verdict), *evidence
        )

    # --- Stage B: rendered retry, only when Stage A came back thin ----------
    #
    # Placed here on purpose: before enrichment and before the image pipeline.
    # Classifying a category or downloading a gallery from a page we are about
    # to re-render would be work thrown away, and the image pipeline in
    # particular costs bytes.
    #
    # Skipped when the page ALREADY came from a browser: an escalated fetch
    # (§2.4) has just rendered this URL, and a rendered page with no gallery is
    # a fact about the shop, not a reason to launch Chromium a second time.
    # Caught by a test that counted browser launches rather than trusting the
    # flow -- on a transport-failing host every row was paying twice.
    if extra_candidates:
        have = set(record.image_candidates)
        record.image_candidates.extend(c for c in extra_candidates if c not in have)

    already_rendered = record.fetch_stage is FetchStage.RENDERED
    if (
        renderer is not None
        and not already_rendered
        and (reasons := incomplete_reasons(record, settings.config.render))
    ):
        stage("rendering")
        await _retry_rendered(record, url, reasons, settings, renderer, before, plugins)

    stage("enriching")
    _enrich(record, settings)

    # --- The optional --llm layer ------------------------------------------
    #
    # After enrichment, because whether a category needs a model's opinion is
    # something only the keyword classifier can tell us. Before the image
    # pipeline, so a row the model is about to re-categorise is not also
    # downloading bytes under the old one.
    if enricher is not None and enricher.enabled:
        await enricher.enhance(record, rewrite_wanted_for(record, description_mode))

    # Tier 1 always, Tier 2 gated. `resolver` is None only for validate-only,
    # which runs its own Tier 1 and must never download or upload.
    if resolver is not None:
        stage("images")
        apply_to_record(record, await resolver.resolve(record))

    apply_policy_defaults(record, settings, seller_note)
    _apply_extraction_flags(record)

    # The provenance gate runs last so nothing downstream can undo it.
    apply_gate(record, description_mode)
    record.time_spent = budget.report()
    stage("written")
    return record


def extract_into(
    record: ProductRecord,
    html: str,
    final_url: str,
    settings: Settings,
    plugins: PluginRegistry | None = None,
) -> None:
    """Everything derivable from one HTML document, onto one record.

    Split out so Stage B can run the exact same extractors over the rendered
    DOM. A separate "rendered extraction" path would be the obvious place for
    the two to drift apart, and there is no reason for them to differ: the only
    thing that changed is the quality of the HTML.
    """
    cfg = settings.config
    dom = HTMLParser(html)
    structured = extract_structured(html, final_url, dom)
    record.structured_syntaxes = list(structured.syntaxes_found)

    # Observed here, acted on in `process_url`. Extraction describes the page;
    # deciding a row's fate is the pipeline's job, and keeping those apart is
    # what lets Stage B re-run this over a rendered DOM without a row failing
    # twice for the same reason.
    shape = inspect_shape(html, final_url, dom, has_product_node=structured.product is not None)
    record.page_verdict = shape.verdict.value if shape.verdict else ""
    record.page_evidence = list(shape.evidence)

    # Extract, then tidy. Two steps rather than one so the extraction ORDER --
    # which source wins -- stays a separate question from the cleaning RULES,
    # and every source gets the same treatment.
    cleaned = tidy_title(
        extract_title(structured, dom, cfg.extraction, cfg.csv.max_title_length),
        cfg.extraction,
        cfg.csv.max_title_length,
    )
    record.title = cleaned.title
    record.title_original = cleaned.original
    record.title_attributes = cleaned.attributes
    record.description = extract_description(
        structured, dom, cfg.extraction, cfg.csv.max_description_length
    )

    candidates = collect_candidates(structured, dom, final_url, cfg.images, cfg.validator)
    record.image_candidates = [c.url for c in candidates]

    price = extract_price(structured, dom, cfg.currency, cfg.price.strategy)
    record.price_inr = price.price_inr
    record.source_price = price.source_amount
    record.source_currency = price.source_currency
    _absorb(record, price.notes, price.flags)

    measured = extract_dimensions(structured, dom, cfg.extraction)
    record.weight_g = measured.weight_g
    record.length_cm = measured.length_cm
    record.width_cm = measured.width_cm
    record.height_cm = measured.height_cm
    _absorb(record, measured.notes, measured.flags)

    variants = extract_variants(structured, dom, cfg.extraction, cfg.fields)
    record.sizes = variants.sizes
    record.availability = variants.availability
    record.stock_qty = variants.stock_qty
    _absorb(record, variants.notes, variants.flags)

    _register_gaps(record)

    # Last, and its values win. A plugin exists because the generic path got
    # this shop wrong; deferring to the generic answer would defeat the point.
    # Hooked here rather than in `process_url` so a Stage B render re-runs
    # plugins over the rendered DOM for free.
    _run_plugin(
        record,
        plugins,
        PluginContext(
            url=record.source_url,
            final_url=final_url,
            html=html,
            dom=dom,
            structured=structured,
            config=cfg,
        ),
    )


def _register_gaps(record: ProductRecord) -> None:
    """Mark which notes the generic extractors emitted purely because a field
    was empty, so a plugin or a Stage B render can take them back.

    Registered here rather than inside each extractor because an extractor
    returns a note list, not a record -- and keeping all four in one visible
    place makes it obvious when a fifth is added and forgotten. Matched against
    the extractors' own exported constants, so re-wording a note cannot silently
    desync this.
    """
    for name, text in (
        ("source_price", NO_PRICE_NOTE),
        ("weight_g", NO_WEIGHT_NOTE),
        ("availability", NO_AVAILABILITY_NOTE),
    ):
        if text in record.notes:
            record.gap_notes[name] = text

    empty = [axis for axis in AXES if not getattr(record, axis).is_present]
    if empty and (text := no_dimensions_note(empty)) in record.notes:
        for axis in empty:
            record.gap_notes[axis] = text


def _run_plugin(
    record: ProductRecord, plugins: PluginRegistry | None, ctx: PluginContext
) -> None:
    if plugins is None or not len(plugins):
        return
    plugin = plugins.match(ctx.url, ctx.html)
    if plugin is None:
        return

    try:
        result = plugin.extract(ctx)
    except Exception as exc:  # noqa: BLE001 -- a broken plugin must not eat the row
        log.exception("Plugin %s raised on %s", plugin.name, ctx.url)
        record.flag(
            f"Plugin {plugin.name!r} crashed on this page ({exc!r}); the generic extraction "
            "stands. Fix the plugin, or the rows it was written for will stay generic."
        )
        return

    if result.is_empty:
        log.debug("Plugin %s matched %s but found nothing", plugin.name, ctx.url)
        return

    try:
        apply_result(record, result, plugin.name)
        record.retract_filled_gaps()
    except PluginError as exc:
        # Loud: a plugin asking for something it may not have is a bug in the
        # plugin, and silently dropping the result would hide it.
        log.error("Plugin %s returned an unusable result: %s", plugin.name, exc)
        record.flag(f"Plugin {plugin.name!r} returned an unusable result: {exc}")


# ---------------------------------------------------------------------------
# Stage B
# ---------------------------------------------------------------------------

# The only things a browser is allowed to be launched over. Referenced by
# config.py's validator, so a typo in `render.retry_when_missing` is a startup
# error rather than a silent "never render".
RENDER_TRIGGERS: dict[str, Callable[[ProductRecord], bool]] = {
    "title": lambda r: not r.title.is_present,
    "description": lambda r: not r.description.is_present,
    "images": lambda r: not r.image_candidates,
    "price": lambda r: r.source_price is None,
    "structured_data": lambda r: not r.structured_syntaxes,
}


def incomplete_reasons(record: ProductRecord, cfg: RenderConfig) -> list[str]:
    """What Stage A missed that a browser could plausibly find. Empty means stop.

    This function is the whole gate. A record Stage A handled completely never
    reaches a browser, which on a large batch is the difference between minutes
    and hours -- and, on the source site's side, between one request per product
    and a full page load with every script it ships.
    """
    if not cfg.enabled or record.status is RowStatus.FAILED:
        return []
    return [name for name in cfg.retry_when_missing if RENDER_TRIGGERS[name](record)]


_CONFIDENCE_RANK = {
    Confidence.NONE: 0,
    Confidence.LOW: 1,
    Confidence.MEDIUM: 2,
    Confidence.HIGH: 3,
}


def merge_rendered(base: ProductRecord, rendered: ProductRecord) -> list[str]:
    """Fold a Stage B extraction into the Stage A record. Returns what was gained.

    Stage B does not simply win. It ran over a DOM that a shop's own JavaScript
    assembled, which is usually a superset of the static HTML but is also where
    recommendation carousels, recently-viewed strips and cookie banners live. So
    a rendered value is taken when Stage A had nothing, or when it arrived by a
    more trustworthy route; on a tie the static value stands, because it is the
    one the site serves to everybody.
    """
    gained: list[str] = []

    for name, fresh in rendered.field_values().items():
        if not fresh.is_present:
            continue
        current: FieldValue[Any] = getattr(base, name)
        if not current.is_present:
            setattr(base, name, fresh)
            gained.append(name)
        elif _CONFIDENCE_RANK[fresh.confidence] > _CONFIDENCE_RANK[current.confidence]:
            setattr(base, name, fresh)
            gained.append(f"{name} (better source)")

    if rendered.title_original and not base.title_original:
        base.title_original = rendered.title_original
        base.title_attributes = rendered.title_attributes

    if rendered.source_price is not None and base.source_price is None:
        base.source_price = rendered.source_price
        base.source_currency = rendered.source_currency
        gained.append("source_price")

    # A rendered gallery is the single most common thing Stage B recovers: the
    # static HTML carries one placeholder and the real photos arrive by script.
    if len(rendered.image_candidates) > len(base.image_candidates):
        found = len(rendered.image_candidates) - len(base.image_candidates)
        base.image_candidates = rendered.image_candidates
        gained.append(f"{found} more image candidate(s)")

    for syntax in rendered.structured_syntaxes:
        if syntax not in base.structured_syntaxes:
            base.structured_syntaxes.append(syntax)

    for note in rendered.notes:
        base.note(note)
    if rendered.status is RowStatus.NEEDS_REVIEW and base.status is RowStatus.OK:
        # `note()` deliberately does not raise the status, so a judgement call
        # Stage B made -- a guessed dimension order, a shipping weight standing
        # in for a product weight -- would otherwise arrive stripped of the one
        # thing that marks it as a judgement call.
        base.status = RowStatus.NEEDS_REVIEW

    return gained


@dataclass(frozen=True)
class _Mark:
    """Where the record stood before Stage A described the page."""

    notes: int
    status: RowStatus


async def _retry_rendered(
    record: ProductRecord,
    url: str,
    reasons: list[str],
    settings: Settings,
    renderer: Renderer,
    before: _Mark,
    plugins: PluginRegistry | None = None,
) -> None:
    """Re-fetch with a browser and fold the result in. Never fails the row.

    Stage B is an improvement attempt, not a requirement. If Chromium is absent,
    times out, or the site refuses it, the static record stands and the row says
    what was tried -- as a note rather than a flag, because a page that needs
    JavaScript is a fact about the shop, not a judgement call we made.
    """
    missing = ", ".join(reasons)
    try:
        fetched = await renderer.fetch(url)
    except RenderUnavailable as exc:
        log.warning("Stage B unavailable: %s", exc)
        record.note(
            f"Stage A found no {missing} and this page looks like it needs JavaScript, but "
            f"the browser for Stage B is not installed. {INSTALL_HINT}"
        )
        return
    except FetchError as exc:
        log.info("Stage B failed for %s: %s", url, exc.reason)
        record.note(
            f"Stage A found no {missing}; rendering the page was tried and failed "
            f"({exc.reason}). The static result stands."
        )
        return

    fresh = new_record(record.source_url, record.provenance, settings.identity)
    extract_into(fresh, fetched.html, fetched.final_url, settings, plugins)

    # Stage A's notes describe a page we have just replaced. "No weight found"
    # on a row whose weight Stage B recovered would send an operator to fill in
    # a cell that is already filled -- and review.csv is only worth anything if
    # every line in it is still true. Retracted only now that the render has
    # actually succeeded; a failed one leaves Stage A's account standing.
    del record.notes[before.notes :]
    record.status = before.status
    gained = merge_rendered(record, fresh)
    record.retract_filled_gaps()

    record.fetch_stage = FetchStage.RENDERED
    record.note(
        f"Stage A found no {missing}, so the page was rendered in a browser. "
        + (f"That recovered: {', '.join(gained)}." if gained else "It found nothing more.")
    )


def _stated(field: FieldValue) -> bool:
    """Did a human put this here, rather than an extractor or a classifier?

    v5 §4.1. An imported row's category and HS code come off the operator's own
    seller panel. Replacing those with our keyword classifier's opinion is not
    enrichment, it is overruling the only party who actually knows -- and for
    `hs_code` it means substituting a guess for a customs declaration.

    An operator-stated slug still has to be IN the taxonomy: `validate_slugs`
    runs after this and fails the row either way. Keeping their value cannot
    smuggle a bad slug through; it only decides whose answer we try first.
    """
    return field.is_present and field.source is FieldSource.OPERATOR


def _enrich(record: ProductRecord, settings: Settings) -> None:
    """Category, HS code, FX and the policy screen."""
    cfg = settings.config

    category = classify(record, settings.taxonomy)
    stated_category = str(record.category_slug.value) if _stated(record.category_slug) else ""
    keep_stated = bool(stated_category) and settings.taxonomy.has_category(stated_category)

    if keep_stated:
        # Their category, and the classifier's subcategory only when the two
        # agree about the parent -- a child slug carried over from a different
        # parent is exactly the kind of quiet mismatch `validate_slugs` exists
        # to catch, and it is better not to create it.
        if category.category_slug.value == stated_category:
            record.subcategory_slug = category.subcategory_slug
            _absorb(record, category.notes, category.flags)
        elif category.category_slug.is_present:
            record.note(
                f"Kept the category from your export ({stated_category}); this tool would "
                f"have said {category.category_slug.value}."
            )
    else:
        if stated_category:
            # Their word is not a haat slug. Failing the whole row over a
            # vocabulary mismatch throws away a product the operator does have;
            # substituting our slug and SAYING SO leaves them a row to correct.
            record.note(
                f'Your export says the category is "{stated_category}", which is not one of '
                f"haat's. Filed under {category.category_slug.value or 'nothing'} instead -- "
                "check it before uploading."
            )
        record.category_slug = category.category_slug
        record.subcategory_slug = category.subcategory_slug
        record.custom_category = category.custom_category
        _absorb(record, category.notes, category.flags)

    hs = suggest_hs_code(record, cfg.hs_codes)
    if not _stated(record.hs_code):
        record.hs_code = hs.hs_code
        _absorb(record, hs.notes, hs.flags)

    if cfg.price.strategy in (PriceStrategy.CONVERT, PriceStrategy.MARKUP):
        converted = convert_price(record.source_price, record.source_currency, cfg.price, cfg.fx)
        if converted.price_inr.is_present:
            record.price_inr = converted.price_inr
            record.fx_rate_used = converted.rate_used
            record.fx_rate_as_of = converted.rate_as_of
        _absorb(record, converted.notes, converted.flags)

    _screen_policy(record, settings)

    # Last line of defence: nothing may reach the CSV with an off-taxonomy slug.
    if reason := validate_slugs(record, settings.taxonomy):
        record.fail(reason)
        record.note(
            f"Refusing to write this row: {reason}. A slug that is not in taxonomy.yaml either "
            "rejects the import or files the listing where nobody will find it."
        )


def _screen_policy(record: ProductRecord, settings: Settings) -> None:
    vocabulary = load_vocabulary(
        settings.root / settings.config.policy.keywords_file,
        settings.root / settings.config.policy.brands_file,
    )
    hits = screen_text(record.title.value or "", record.description.value or "", vocabulary)
    if not hits:
        return

    record.policy_flags = [hit.flag for hit in hits]
    for line in describe(hits):
        record.flag(line)

    if gi := gi_mentions(hits, vocabulary):
        terms = ", ".join(sorted({hit.term for hit in gi}))
        # A question for a human. gi_region stays empty regardless.
        record.gi_mention_found = (
            f"Source text mentions {terms}. If this product genuinely carries a GI tag, confirm "
            "it against the Indian GI registry and set gi_region by hand."
        )


def _absorb(record: ProductRecord, notes: list[str], flags: list[str]) -> None:
    """Notes inform; flags also mark the row for attention. See ProductRecord.note."""
    for text in notes:
        record.note(text)
    for text in flags:
        record.flag(text)


def apply_policy_defaults(
    record: ProductRecord, settings: Settings, seller_note: str | None = None
) -> None:
    """Fields no source page can tell us, set from operator policy.

    These are the only fields written without evidence from the page, which is
    why they all come from explicit configuration rather than a guess.
    """
    fields = settings.config.fields

    if fields.rfq_default:
        record.rfq_enabled = FieldValue.found(
            fields.rfq_default, FieldSource.POLICY_DEFAULT, Confidence.HIGH
        )
        # Only meaningful alongside an enabled RFQ.
        if fields.rfq_min_qty is not None:
            record.rfq_min_qty = FieldValue.found(
                fields.rfq_min_qty, FieldSource.POLICY_DEFAULT, Confidence.HIGH
            )

    if fields.bulk_only_default:
        record.bulk_only = FieldValue.found(
            fields.bulk_only_default, FieldSource.POLICY_DEFAULT, Confidence.HIGH
        )

    note = _compose_seller_note(record, fields, seller_note)
    if note:
        # OPERATOR when they wrote any of it, because the whole string is then
        # partly theirs and `review.csv` should not claim we inferred it.
        source = FieldSource.OPERATOR if seller_note else FieldSource.POLICY_DEFAULT
        record.seller_note = FieldValue.found(note, source, Confidence.HIGH)


def _compose_seller_note(
    record: ProductRecord, fields: FieldsConfig, operator_note: str | None
) -> str:
    """The operator's own words, then where the row came from, then the facts.

    WHY THE SOURCE LINK LIVES HERE. haat's nineteen columns have nowhere to
    record which page a row was built from, and `seller_note` is the only
    free-text field among them. Six weeks after an import, "which page was
    this?" is the question that actually gets asked, and without this the
    answer is only in the ledger -- which is not what anybody opens.

    WHY THE WEIGHT IS REPEATED. It has its own column, and it is also the
    number customs charges on. Seeing `320 g` beside the link is how a wrong
    one gets noticed by the person who knows the product, rather than by a
    customs broker later.

    Only values the page actually yielded appear. A missing weight leaves the
    note shorter rather than saying "weight: unknown", because a note listing
    what is absent is a longer way of writing a blank cell.
    """
    parts: list[str] = []
    if operator_note:
        parts.append(operator_note)

    if fields.seller_note_includes_source and record.source_url:
        parts.append(f"Source: {record.source_url}")

    if fields.seller_note_includes_details:
        if record.weight_g.is_present:
            parts.append(f"Weight: {record.weight_g.value} g")
        axes = [record.length_cm, record.width_cm, record.height_cm]
        if all(axis.is_present for axis in axes):
            parts.append(
                f"Size: {axes[0].value} x {axes[1].value} x {axes[2].value} cm"
            )
        if record.sizes.is_present:
            parts.append(f"Options: {record.sizes.value}")
        if record.source_price is not None and record.source_currency:
            # What the page said, not what we are listing at. Those differ on
            # purpose -- `price_inr` is the maker's decision -- and recording
            # the observed one makes the difference auditable.
            parts.append(f"Listed on source at {record.source_price:g} {record.source_currency}")

    return fields.seller_note_separator.join(parts)


def _apply_extraction_flags(record: ProductRecord) -> None:
    """Judgement calls about the extraction itself, as opposed to field values."""

    if not record.title.is_present:
        # A listing with no name cannot be published, so this is a row failure
        # rather than a blank cell.
        record.fail("no_title")
        record.notes.append("No product title could be extracted.")
        return

    if not record.description.is_present:
        record.flag("No description found; the listing will need copy written by hand.")

    if not record.image_candidates:
        record.flag("No image candidates found on the page.")

    if record.title.needs_human:
        record.flag(f"Title confidence is {record.title.confidence.value}; check it reads well.")

    if record.description.needs_human and record.description.is_present:
        record.flag(
            f"Description confidence is {record.description.confidence.value}; "
            "check for leftover site furniture."
        )

    if record.status is RowStatus.OK and not record.structured_syntaxes:
        record.flag(
            "Page had no JSON-LD, microdata or RDFa; everything came from meta tags or the DOM."
        )
