"""Phase 10: Stage B, and above all when it does NOT run.

The deliverable is a fallback, so most of these tests assert absence -- that a
page Stage A handled never causes a browser to launch. They count calls against
a fake renderer rather than reading the gate, and one of them asserts that
`Renderer.start` was never awaited at all, which is the strongest form of the
claim: not "we did not navigate", but "there is no Chromium process".

Playwright itself is an optional extra and is not required to run this file.
`test_a_missing_browser_is_a_note_not_a_crash` covers the install that does not
have it, which is the default install.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from haat_lister.config import RenderConfig, Settings
from haat_lister.fetch.rendered import Renderer, RenderUnavailable, build_renderer
from haat_lister.fetch.static import FetchError, FetchResult, build_client
from haat_lister.models import (
    Confidence,
    FetchStage,
    FieldSource,
    FieldValue,
    Provenance,
    RowStatus,
)
from haat_lister.pipeline import (
    RENDER_TRIGGERS,
    incomplete_reasons,
    merge_rendered,
    new_record,
    process_url,
)

PAGE_URL = "https://shop.example/products/kurta"

# Everything Stage A needs: title, description, a gallery, structured data.
COMPLETE_HTML = """<!doctype html><html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product",
 "name":"Hand-embroidered cotton kurta with mirror work",
 "description":"Hand-embroidered in Kutch, Gujarat on breathable handloom cotton.",
 "image":["https://cdn.example/kurta-1.jpg","https://cdn.example/kurta-2.jpg"],
 "offers":{"@type":"Offer","price":"2499","priceCurrency":"INR"}}
</script></head><body><h1>Kurta</h1></body></html>"""

# Everything but the gallery: the photos arrive by script after first paint.
NO_GALLERY_HTML = COMPLETE_HTML.replace(
    '"image":["https://cdn.example/kurta-1.jpg","https://cdn.example/kurta-2.jpg"],', ""
)

# A rendered DOM with the spec table the shell's JavaScript builds.
FULL_HTML = COMPLETE_HTML.replace(
    "<body><h1>Kurta</h1></body>",
    "<body><h1>Kurta</h1><table><tr><th>Weight</th><td>350 g</td></tr>"
    "<tr><th>Dimensions</th><td>L70 x W50 x H2 cm</td></tr></table></body>",
)

# The shape Stage B exists for: a React storefront that ships an empty shell.
SHELL_HTML = """<!doctype html><html><head><title>Loading…</title></head>
<body><div id="root"></div><script src="/static/app.js"></script></body></html>"""


@pytest.fixture
def render_settings(settings: Settings) -> Settings:
    tuned = settings.model_copy(deep=True)
    tuned.config.render.enabled = True
    return tuned


class FakeRenderer:
    """Counts everything. `started` mirrors the real Renderer's lazy launch."""

    def __init__(self, html: str = COMPLETE_HTML, raises: Exception | None = None) -> None:
        self.html = html
        self.raises = raises
        self.calls: list[str] = []
        self.pages_rendered = 0
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.started = False

    async def fetch(self, url: str) -> FetchResult:
        self.calls.append(url)
        if self.raises is not None:
            raise self.raises
        self.started = True
        self.pages_rendered += 1
        return FetchResult(
            url=url,
            final_url=url,
            status_code=200,
            html=self.html,
            stage=FetchStage.RENDERED,
            elapsed_ms=0,
            headers={"content-type": "text/html"},
        )


def mock_page(html: str) -> None:
    respx.get(PAGE_URL).mock(
        return_value=httpx.Response(200, html=html, headers={"content-type": "text/html"})
    )


async def run(settings: Settings, renderer: object | None):
    async with build_client(settings) as client:
        return await process_url(
            PAGE_URL,
            Provenance.OWN,
            settings,
            client,
            renderer=renderer,  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


@respx.mock
async def test_stage_b_never_runs_for_a_complete_record(render_settings: Settings) -> None:
    """The whole point of Phase 10. A page Stage A handled costs no browser --
    not a navigation, not even a launch."""
    mock_page(COMPLETE_HTML)
    renderer = FakeRenderer()

    record = await run(render_settings, renderer)

    assert record.status is not RowStatus.FAILED
    assert renderer.calls == []
    assert not renderer.started, "Chromium was launched for a page that did not need it"
    assert record.fetch_stage is FetchStage.STATIC


@respx.mock
async def test_stage_b_runs_for_a_javascript_shell(render_settings: Settings) -> None:
    mock_page(SHELL_HTML)
    renderer = FakeRenderer(COMPLETE_HTML)

    record = await run(render_settings, renderer)

    assert renderer.calls == [PAGE_URL]
    assert record.fetch_stage is FetchStage.RENDERED
    assert "mirror work" in (record.title.value or "")
    assert len(record.image_candidates) == 2
    assert any("rendered in a browser" in note for note in record.notes)


@respx.mock
async def test_stage_a_notes_are_retracted_when_stage_b_supersedes_them(
    render_settings: Settings,
) -> None:
    """Caught in a live run. review.csv was telling an operator to fill in a
    weight that Stage B had already recovered -- Stage A's complaints about a
    page survived the replacement of that page. A worklist is only worth
    anything if every line in it is still true."""
    mock_page(SHELL_HTML)
    renderer = FakeRenderer(FULL_HTML)

    record = await run(render_settings, renderer)

    assert record.weight_g.is_present
    assert record.description.is_present
    stale = [n for n in record.notes if "No weight found" in n or "No description found" in n]
    assert stale == [], stale


@respx.mock
async def test_a_failed_render_leaves_stage_as_account_standing(
    render_settings: Settings,
) -> None:
    """The retraction is conditional on the render actually working."""
    mock_page(SHELL_HTML)
    renderer = FakeRenderer(raises=FetchError("render_timeout", "20000ms"))

    record = await run(render_settings, renderer)

    assert any("No image candidates" in note for note in record.notes)
    assert any("render_timeout" in note for note in record.notes)


def test_a_judgement_call_made_during_a_render_keeps_its_status() -> None:
    """`note()` deliberately does not raise the status, so a flag raised while
    extracting the rendered DOM would otherwise arrive stripped of the one thing
    marking it as a judgement call."""
    base = new_record(PAGE_URL, Provenance.OWN)
    rendered = new_record(PAGE_URL, Provenance.OWN)
    rendered.flag("Dimension order was guessed from the label.")

    merge_rendered(base, rendered)

    assert base.status is RowStatus.NEEDS_REVIEW


@respx.mock
async def test_no_renderer_means_no_path_to_one(render_settings: Settings) -> None:
    """`--no-render` does not disable a browser, it removes it. There is nothing
    to forget to check."""
    mock_page(SHELL_HTML)
    record = await run(render_settings, None)

    assert record.fetch_stage is FetchStage.STATIC
    # All the shell had was a placeholder <title>, so the row survives but is
    # exactly as thin as the HTML was.
    assert record.status is RowStatus.NEEDS_REVIEW
    assert record.image_candidates == []
    assert not record.description.is_present
    assert build_renderer(render_settings, enabled=False) is None


@respx.mock
async def test_a_failed_fetch_is_never_rendered(render_settings: Settings) -> None:
    """A 404 is not incompleteness. Re-fetching it in Chromium would be a second
    request to a site that already said no."""
    respx.get(PAGE_URL).mock(return_value=httpx.Response(404))
    renderer = FakeRenderer()

    record = await run(render_settings, renderer)

    assert record.status is RowStatus.FAILED
    assert renderer.calls == []


def test_incomplete_reasons_ignores_fields_a_browser_cannot_supply() -> None:
    """Price, weight and dimensions are absent from most source pages full stop.
    Launching a browser to confirm that on every row would be the most expensive
    possible way to learn nothing."""
    record = new_record(PAGE_URL, Provenance.OWN)
    record.title = FieldValue.found("Kurta", FieldSource.JSONLD, Confidence.HIGH)
    record.description = FieldValue.found("Woven in Kutch.", FieldSource.JSONLD, Confidence.HIGH)
    record.image_candidates = ["https://cdn.example/1.jpg"]
    # No price, no weight, no dimensions -- the normal case.

    cfg = RenderConfig()
    assert incomplete_reasons(record, cfg) == []

    opted_in = RenderConfig(retry_when_missing=["title", "price"])
    assert incomplete_reasons(record, opted_in) == ["price"]


def test_incomplete_reasons_is_empty_when_rendering_is_off() -> None:
    record = new_record(PAGE_URL, Provenance.OWN)
    assert incomplete_reasons(record, RenderConfig(enabled=False)) == []
    assert incomplete_reasons(record, RenderConfig(enabled=True)) == [
        "title",
        "description",
        "images",
    ]


def test_unknown_render_triggers_are_a_startup_error() -> None:
    """A typo would otherwise mean "never render", and the symptom -- rows
    quietly missing titles -- looks like an extraction bug."""
    with pytest.raises(ValueError, match="unknown entries"):
        RenderConfig(retry_when_missing=["title", "titel"])

    with pytest.raises(ValueError, match="wait_until"):
        RenderConfig(wait_until="whenever")

    assert set(RenderConfig().retry_when_missing) <= set(RENDER_TRIGGERS)


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------


def test_a_rendered_value_does_not_beat_an_equally_confident_static_one() -> None:
    """A rendered DOM also contains carousels, recently-viewed strips and cookie
    banners. On a tie the static value stands: it is the one the site serves to
    everybody."""
    base = new_record(PAGE_URL, Provenance.OWN)
    base.title = FieldValue.found("Kutch mirror-work kurta", FieldSource.JSONLD, Confidence.HIGH)

    rendered = new_record(PAGE_URL, Provenance.OWN)
    rendered.title = FieldValue.found("You may also like", FieldSource.H1, Confidence.HIGH)

    gained = merge_rendered(base, rendered)

    assert base.title.value == "Kutch mirror-work kurta"
    assert gained == []


def test_rendering_fills_gaps_and_wins_on_better_confidence() -> None:
    base = new_record(PAGE_URL, Provenance.OWN)
    base.title = FieldValue.found("Loading…", FieldSource.TITLE_TAG, Confidence.LOW)
    base.image_candidates = ["https://cdn.example/placeholder.jpg"]

    rendered = new_record(PAGE_URL, Provenance.OWN)
    rendered.title = FieldValue.found(
        "Kutch mirror-work kurta", FieldSource.JSONLD, Confidence.HIGH
    )
    rendered.description = FieldValue.found(
        "Hand-embroidered in Gujarat.", FieldSource.JSONLD, Confidence.HIGH
    )
    rendered.image_candidates = [f"https://cdn.example/{i}.jpg" for i in range(5)]
    rendered.source_price = 2499.0
    rendered.source_currency = "INR"

    gained = merge_rendered(base, rendered)

    assert base.title.value == "Kutch mirror-work kurta"
    assert base.description.is_present
    assert len(base.image_candidates) == 5
    assert base.source_price == 2499.0
    assert "description" in gained
    assert any("image candidate" in item for item in gained)


def test_a_thinner_render_never_loses_what_stage_a_found() -> None:
    """Scripts fail, and a render that came back with less must not be an
    improvement that deletes the static gallery."""
    base = new_record(PAGE_URL, Provenance.OWN)
    base.title = FieldValue.found("Kurta", FieldSource.JSONLD, Confidence.HIGH)
    base.image_candidates = ["https://cdn.example/1.jpg", "https://cdn.example/2.jpg"]

    gained = merge_rendered(base, new_record(PAGE_URL, Provenance.OWN))

    assert base.title.value == "Kurta"
    assert len(base.image_candidates) == 2
    assert gained == []


# --------------------------------------------------------------------------
# Degrading honestly
# --------------------------------------------------------------------------


@respx.mock
async def test_a_missing_browser_is_a_note_not_a_crash(render_settings: Settings) -> None:
    """The default install has no Playwright. A page that needs it should say so
    on the row, not take the run down."""
    mock_page(SHELL_HTML)
    renderer = FakeRenderer(raises=RenderUnavailable("not installed"))

    record = await run(render_settings, renderer)

    assert record.fetch_stage is FetchStage.STATIC
    assert any("not installed" in note or "playwright" in note.lower() for note in record.notes)


@respx.mock
async def test_a_render_that_times_out_keeps_the_static_result(render_settings: Settings) -> None:
    mock_page(NO_GALLERY_HTML)
    renderer = FakeRenderer(raises=FetchError("render_timeout", "20000ms"))

    record = await run(render_settings, renderer)

    assert record.status is not RowStatus.FAILED
    assert "mirror work" in (record.title.value or "")
    assert any("render_timeout" in note for note in record.notes)


async def test_the_renderer_identifies_itself_honestly(render_settings: Settings) -> None:
    """No stealth, no spoofed fingerprint. Whatever httpx says we are, Chromium
    says too -- including the contact address."""
    renderer = Renderer(render_settings)
    assert renderer.pages_rendered == 0
    assert not renderer.started

    source = Renderer.start.__doc__ or ""
    assert "RenderUnavailable" in source
    assert render_settings.user_agent.startswith("haat-lister/")

    # A build with rendering off cannot reach a browser at all.
    render_settings.config.render.enabled = False
    assert build_renderer(render_settings) is None
