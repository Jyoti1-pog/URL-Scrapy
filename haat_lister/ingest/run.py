"""Turning an imported thing into a row, through the ordinary machinery.

One function per route, each of them thin. Everything they have in common --
extraction, the page-shape verdict, Stage B's absence, enrichment, Tier 1, the
policy defaults, the provenance gate -- lives in `pipeline.process_page`, which
is the same function a fetched URL goes through.

That is not tidiness. `apply_gate` is what stops us re-hosting somebody else's
photographs, and an import route that built its own record would skip it by
omission rather than by decision.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..models import DescriptionMode, ProductRecord, Provenance
from ..pipeline import new_record, process_page
from ..utils.logging import get_logger
from . import seller_export
from .saved_page import SavedPage, SavedPageError
from .saved_page import load as load_saved_page

if TYPE_CHECKING:
    from ..config import Settings
    from ..enrich.rewrite import LlmEnricher
    from ..extract.plugins import PluginRegistry
    from ..images.pipeline import ImageResolver

log = get_logger(__name__)

__all__ = ["SavedPageError", "from_export_row", "from_html", "from_saved_page"]


async def from_export_row(
    export: seller_export.Export,
    row: list[str],
    provenance: Provenance,
    settings: Settings,
    *,
    seller_note: str | None = None,
    description_mode: DescriptionMode = DescriptionMode.RAW,
    resolver: ImageResolver | None = None,
    enricher: LlmEnricher | None = None,
) -> ProductRecord:
    """§4.1. One row of the operator's own export, as a listing row.

    There is no page here, so `process_page` is not the seam -- there is nothing
    to extract. What still has to happen is everything downstream of extraction,
    and it happens through the same functions a fetched row uses: the identical
    Tier-1 image chain, the same policy defaults, the same provenance gate.

    Written out here rather than folded into `process_page` because inventing a
    fake `FetchResult` with a fake HTML body, purely to reach a code path, would
    put a lie in the record's `fetch_stage` to save nine lines.
    """
    from ..enrich.rewrite import rewrite_wanted_for
    from ..images.pipeline import apply_to_record
    from ..pipeline import _apply_extraction_flags, _enrich, apply_policy_defaults
    from ..policy.provenance import apply_gate

    record = seller_export.to_record(export, row, provenance, settings)
    _enrich(record, settings)

    if enricher is not None and enricher.enabled:
        await enricher.enhance(record, rewrite_wanted_for(record, description_mode))

    if resolver is not None:
        apply_to_record(record, await resolver.resolve(record))

    apply_policy_defaults(record, settings, seller_note)
    _apply_extraction_flags(record)
    # Last, so nothing above can undo it. An export is the operator's own data
    # far more often than not -- but `provenance` is what says so, and it is
    # passed in rather than assumed.
    apply_gate(record, description_mode)
    return record


async def from_saved_page(
    path: Path,
    provenance: Provenance,
    settings: Settings,
    *,
    source_url: str = "",
    seller_note: str | None = None,
    description_mode: DescriptionMode = DescriptionMode.RAW,
    resolver: ImageResolver | None = None,
    plugins: PluginRegistry | None = None,
    enricher: LlmEnricher | None = None,
) -> ProductRecord:
    """§4.2. A file the operator saved, as a row.

    `provenance` is required and has no default, exactly as it does on every
    other route (§7). An import is not a loophole in Rule 2.1: whether these
    photographs may be re-hosted is a fact about who owns the shop, and reading
    the page off local disk tells us nothing about that.
    """
    page = load_saved_page(path, source_url)
    return await _run(page, provenance, settings, seller_note, description_mode,
                      resolver, plugins, enricher)


async def from_html(
    html: str,
    source_url: str,
    provenance: Provenance,
    settings: Settings,
    *,
    seller_note: str | None = None,
    description_mode: DescriptionMode = DescriptionMode.RAW,
    resolver: ImageResolver | None = None,
    plugins: PluginRegistry | None = None,
    enricher: LlmEnricher | None = None,
) -> ProductRecord:
    """§4.3. Pasted markup -- the one-off version of a saved file.

    Deliberately the same code path rather than a lighter one. A paste that
    behaved differently from a file would be a second implementation of the
    import, and the operator reaching for it is usually the one who has already
    had the most trouble.
    """
    if not source_url:
        raise SavedPageError(
            "Paste the product's URL as well as the page. Without it the row has no "
            "source, cannot be deduplicated, and cannot be checked a year from now."
        )
    page = SavedPage(html=html, source_url=source_url, path=Path("<pasted>"), assets={})
    return await _run(page, provenance, settings, seller_note, description_mode,
                      resolver, plugins, enricher)


async def _run(
    page: SavedPage,
    provenance: Provenance,
    settings: Settings,
    seller_note: str | None,
    description_mode: DescriptionMode,
    resolver: ImageResolver | None,
    plugins: PluginRegistry | None,
    enricher: LlmEnricher | None,
) -> ProductRecord:
    record = new_record(page.source_url, provenance, settings.identity)
    return await process_page(
        record,
        page.as_fetch_result(),
        settings,
        seller_note=seller_note,
        description_mode=description_mode,
        resolver=resolver,
        # No renderer, ever. The operator's browser already rendered this page;
        # launching Chromium against the live URL to improve on the file they
        # gave us would be a request to a host that may well have refused them
        # into giving us the file in the first place.
        renderer=None,
        plugins=plugins,
        enricher=enricher,
        # The `_files` folder, ranked after whatever the page itself named.
        extra_candidates=page.local_photos,
    )
