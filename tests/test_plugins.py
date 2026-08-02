"""Phase 11: the plugin system.

A plugin runs last and its values win, which is the right design and also the
dangerous one. So most of this file is about the guard rails: that a plugin
cannot write gi_region, cannot claim its guess came from JSON-LD, cannot launder
a row's status, and cannot take the run down when it crashes.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from haat_lister.config import Settings
from haat_lister.extract.plugins import (
    Plugin,
    PluginContext,
    PluginError,
    PluginRegistry,
    PluginResult,
    apply_result,
    build_registry,
)
from haat_lister.extract.plugins.example_shopify import ShopifyPlugin
from haat_lister.fetch.static import build_client
from haat_lister.models import (
    Confidence,
    FieldSource,
    FieldValue,
    Provenance,
    RowStatus,
)
from haat_lister.pipeline import new_record, process_url

PAGE_URL = "https://shop.example/products/kurta"

BARE_HTML = """<!doctype html><html><head><title>Kurta | MyShop</title></head>
<body><h1>Blue kurta</h1><p>A kurta, in blue, made of cotton fabric and love.</p>
<img src="/img/one.jpg"></body></html>"""


class StubPlugin:
    name = "stub"

    def __init__(self, result: PluginResult | None = None, raises: Exception | None = None) -> None:
        self.result = result or PluginResult()
        self.raises = raises
        self.matched = 0
        self.extracted = 0

    def matches(self, url: str, html: str) -> bool:
        self.matched += 1
        return True

    def extract(self, ctx: PluginContext) -> PluginResult:
        self.extracted += 1
        if self.raises is not None:
            raise self.raises
        return self.result


def record_for(url: str = PAGE_URL):
    return new_record(url, Provenance.OWN)


# --------------------------------------------------------------------------
# Guard rails
# --------------------------------------------------------------------------


def test_a_plugin_cannot_write_gi_region() -> None:
    """A GI tag is a government certification and haat makes it a seller
    declaration. The record has no such field, and a plugin naming it gets told
    why rather than being quietly ignored."""
    result = PluginResult(
        fields={"gi_region": FieldValue.found("Kutch", FieldSource.PLUGIN, Confidence.HIGH)}
    )
    with pytest.raises(PluginError, match="may not set gi_region"):
        apply_result(record_for(), result, "stub")


def test_a_plugin_cannot_claim_its_value_came_from_json_ld() -> None:
    """Sources are stamped, not trusted. review.csv's `source` column is the
    accountability mechanism for the whole plugin design, so a plugin cannot be
    the one filling it in."""
    record = record_for()
    result = PluginResult(
        fields={"title": FieldValue.found("Kurta", FieldSource.JSONLD, Confidence.HIGH)}
    )

    apply_result(record, result, "stub")

    assert record.title.value == "Kurta"
    assert record.title.source is FieldSource.PLUGIN


def test_an_unknown_field_name_is_an_error_not_a_shrug() -> None:
    """A typo that silently did nothing would leave a plugin author debugging
    their selectors when the problem is a misspelt key."""
    result = PluginResult(
        fields={"weight_grams": FieldValue.found(350, FieldSource.PLUGIN, Confidence.HIGH)}
    )
    with pytest.raises(PluginError, match="unknown field"):
        apply_result(record_for(), result, "stub")


def test_a_bare_value_is_an_error() -> None:
    result = PluginResult(fields={"title": "Kurta"})  # type: ignore[dict-item]
    with pytest.raises(PluginError, match="bare value"):
        apply_result(record_for(), result, "stub")


def test_a_plugin_cannot_launder_a_flagged_row() -> None:
    """`apply_result` only ever adds. There is no path from a plugin to
    status=ok."""
    record = record_for()
    record.flag("Policy screen matched 'ivory'.")

    apply_result(
        record,
        PluginResult(
            fields={"title": FieldValue.found("Kurta", FieldSource.PLUGIN, Confidence.HIGH)}
        ),
        "stub",
    )

    assert record.status is RowStatus.NEEDS_REVIEW
    assert "ivory" in record.notes[0]


def test_a_plugin_supplying_anything_says_so_on_the_row() -> None:
    record = record_for()
    apply_result(
        record,
        PluginResult(
            fields={"sizes": FieldValue.found("S, M, L", FieldSource.PLUGIN, Confidence.HIGH)},
            image_candidates=["https://cdn.example/1.jpg"],
        ),
        "my_shop",
    )
    trail = " ".join(record.notes)
    assert "my_shop" in trail and "sizes" in trail and "source=plugin" in trail


# --------------------------------------------------------------------------
# Precedence and merging
# --------------------------------------------------------------------------


def test_a_plugin_beats_the_generic_extractor() -> None:
    """The whole reason a plugin exists is that the generic path got this shop
    wrong. Deferring to the generic answer would defeat the point."""
    record = record_for()
    record.title = FieldValue.found("Kurta | MyShop", FieldSource.TITLE_TAG, Confidence.HIGH)

    apply_result(
        record,
        PluginResult(
            fields={
                "title": FieldValue.found(
                    "Kutch mirror-work kurta", FieldSource.PLUGIN, Confidence.MEDIUM
                )
            }
        ),
        "stub",
    )

    assert record.title.value == "Kutch mirror-work kurta"


def test_plugin_images_go_first_but_still_face_tier_1() -> None:
    """Prepended, because the plugin knows this gallery. Not exempted -- they are
    candidates, and every candidate is validated."""
    record = record_for()
    record.image_candidates = ["https://cdn.example/generic.jpg"]

    apply_result(
        record,
        PluginResult(image_candidates=["https://cdn.example/hero.jpg"]),
        "stub",
    )

    assert record.image_candidates[0] == "https://cdn.example/hero.jpg"
    assert "https://cdn.example/generic.jpg" in record.image_candidates
    # Nothing here marks a URL as pre-validated; there is no such concept.
    assert record.image.tier1_passed is False


def test_a_plugin_price_feeds_policy_rather_than_the_csv() -> None:
    """A plugin recovers what the page states. What reaches price_inr is still
    `price.strategy`'s decision."""
    record = record_for()
    apply_result(record, PluginResult(source_price=2499.0, source_currency="INR"), "stub")

    assert record.source_price == 2499.0
    assert record.source_currency == "INR"
    assert not record.price_inr.is_present


# --------------------------------------------------------------------------
# Through the pipeline
# --------------------------------------------------------------------------


@respx.mock
async def test_a_crashing_plugin_flags_the_row_and_keeps_the_generic_result(
    settings: Settings,
) -> None:
    respx.get(PAGE_URL).mock(
        return_value=httpx.Response(200, html=BARE_HTML, headers={"content-type": "text/html"})
    )
    plugin = StubPlugin(raises=RuntimeError("selector moved"))

    async with build_client(settings) as client:
        record = await process_url(
            PAGE_URL,
            Provenance.OWN,
            settings,
            client,
            plugins=PluginRegistry([plugin]),
        )

    assert record.status is not RowStatus.FAILED
    assert "Blue kurta" in (record.title.value or "")  # generic result stands
    assert any("crashed on this page" in note for note in record.notes)


@respx.mock
async def test_a_plugin_asking_for_gi_region_flags_the_row_loudly(settings: Settings) -> None:
    respx.get(PAGE_URL).mock(
        return_value=httpx.Response(200, html=BARE_HTML, headers={"content-type": "text/html"})
    )
    plugin = StubPlugin(
        PluginResult(
            fields={"gi_region": FieldValue.found("Kutch", FieldSource.PLUGIN, Confidence.HIGH)}
        )
    )

    async with build_client(settings) as client:
        record = await process_url(
            PAGE_URL,
            Provenance.OWN,
            settings,
            client,
            plugins=PluginRegistry([plugin]),
        )

    assert record.status is RowStatus.NEEDS_REVIEW
    assert any("gi_region" in note for note in record.notes)


@respx.mock
async def test_no_registry_means_no_plugin_call(settings: Settings) -> None:
    respx.get(PAGE_URL).mock(
        return_value=httpx.Response(200, html=BARE_HTML, headers={"content-type": "text/html"})
    )
    plugin = StubPlugin()

    async with build_client(settings) as client:
        await process_url(PAGE_URL, Provenance.OWN, settings, client)
        await process_url(
            PAGE_URL, Provenance.OWN, settings, client, plugins=PluginRegistry([])
        )

    assert plugin.matched == 0


# --------------------------------------------------------------------------
# Gap notes
# --------------------------------------------------------------------------


def test_a_gap_note_is_retracted_once_the_field_is_filled() -> None:
    """Telling an operator to supply a weight that is already in the row is
    worse than saying nothing: it costs them the trip to find out."""
    record = record_for()
    record.note_gap("weight_g", "No weight found.")
    assert "No weight found." in record.notes

    record.weight_g = FieldValue.found(350, FieldSource.PLUGIN, Confidence.HIGH)
    record.retract_filled_gaps()

    assert record.notes == []
    assert record.gap_notes == {}


def test_a_gap_note_covering_several_fields_survives_a_partial_fill() -> None:
    """"No length, width or height" is still true when only the length arrived."""
    record = record_for()
    text = "No value found for length_cm, width_cm, height_cm."
    record.note_gap(["length_cm", "width_cm", "height_cm"], text)

    record.length_cm = FieldValue.found(70, FieldSource.PLUGIN, Confidence.HIGH)
    record.retract_filled_gaps()
    assert text in record.notes

    record.width_cm = FieldValue.found(50, FieldSource.PLUGIN, Confidence.HIGH)
    record.height_cm = FieldValue.found(2, FieldSource.PLUGIN, Confidence.HIGH)
    record.retract_filled_gaps()
    assert text not in record.notes


def test_an_ordinary_note_is_never_retracted() -> None:
    """A gap note describes something absent, which can be disproved. An
    ordinary note describes something that happened, which cannot."""
    record = record_for()
    record.note("Shipping weight was used in place of product weight.")
    record.weight_g = FieldValue.found(350, FieldSource.PLUGIN, Confidence.HIGH)

    record.retract_filled_gaps()

    assert len(record.notes) == 1


@respx.mock
async def test_a_plugin_price_retracts_the_generic_not_found_note(
    settings: Settings,
) -> None:
    """Caught in a live run against a Shopify-shaped shop: the plugin recovered
    the price from the theme's JavaScript and review.csv still said no price was
    found."""
    respx.get(PAGE_URL).mock(
        return_value=httpx.Response(200, html=BARE_HTML, headers={"content-type": "text/html"})
    )
    plugin = StubPlugin(PluginResult(source_price=2499.0, source_currency="INR"))

    async with build_client(settings) as client:
        record = await process_url(
            PAGE_URL,
            Provenance.OWN,
            settings,
            client,
            plugins=PluginRegistry([plugin]),
        )

    assert record.source_price == 2499.0
    assert not any("No source price was found" in note for note in record.notes)
    # The gap that is still real is still reported.
    assert any("availability" in note for note in record.notes)


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def test_a_broken_matcher_is_skipped_not_fatal() -> None:
    class Exploding:
        name = "exploding"

        def matches(self, url: str, html: str) -> bool:
            raise ValueError("regex compiled at import time, apparently not")

        def extract(self, ctx: PluginContext) -> PluginResult:
            raise AssertionError("should never be reached")

    working = StubPlugin()
    registry = PluginRegistry([Exploding(), working])

    assert registry.match(PAGE_URL, "") is working


def test_operator_plugins_are_ordered_ahead_of_builtins(
    settings: Settings, tmp_path
) -> None:
    """A plugin written for one shop should beat the generic Shopify one on that
    shop's pages."""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "my_shop.py").write_text(
        "from haat_lister.extract.plugins import register, PluginResult\n"
        "class MyShop:\n"
        "    name = 'my_shop'\n"
        "    def matches(self, url, html): return 'myshop.example' in url\n"
        "    def extract(self, ctx): return PluginResult()\n"
        "register(MyShop())\n",
        encoding="utf-8",
    )

    tuned = settings.model_copy(deep=True)
    tuned.config.extraction.plugins_dir = "plugins"
    registry = build_registry(tuned.config, tmp_path)

    names = [p.name for p in registry.plugins]
    assert names[0] == "my_shop"
    assert "example_shopify" in names

    shopify_page = '<script>var meta = {"product":{}};</script> cdn.shopify.com'
    assert registry.match("https://myshop.example/p/1", shopify_page).name == "my_shop"
    assert registry.match("https://other.example/p/1", shopify_page).name == "example_shopify"


def test_an_unset_plugins_dir_loads_nothing_from_disk(settings: Settings) -> None:
    registry = build_registry(settings.config, settings.root)
    assert [p.name for p in registry.plugins] == ["example_shopify"]


def test_a_plugin_file_that_fails_to_import_is_skipped(settings: Settings, tmp_path) -> None:
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "broken.py").write_text("import nonexistent_module_xyz\n", encoding="utf-8")

    tuned = settings.model_copy(deep=True)
    tuned.config.extraction.plugins_dir = "plugins"
    registry = build_registry(tuned.config, tmp_path)

    assert [p.name for p in registry.plugins] == ["example_shopify"]


# --------------------------------------------------------------------------
# The worked example
# --------------------------------------------------------------------------

SHOPIFY_HTML = """<!doctype html><html><head>
<title>Kurta</title>
<script>
var meta = {"product":{"id":1,"title":"Kutch mirror-work cotton kurta",
 "type":"Kurta","vendor":"Kutch Craft",
 "variants":[{"id":11,"price":249900,"featured_image":"//cdn.shopify.com/s/a_600x600.jpg"},
             {"id":12,"price":279900,"featured_image":"//cdn.shopify.com/s/b_large.jpg"}],
 "images":["//cdn.shopify.com/s/hero_1024x1024.jpg","//cdn.shopify.com/s/detail_grande.jpg"]},
 "currency":"INR","page":{"pageType":"product"}};
</script></head><body><h1>Kurta</h1></body></html>"""


def shopify_ctx(settings: Settings, html: str = SHOPIFY_HTML) -> PluginContext:
    from selectolax.parser import HTMLParser

    from haat_lister.extract.structured import extract_structured

    dom = HTMLParser(html)
    return PluginContext(
        url=PAGE_URL,
        final_url=PAGE_URL,
        html=html,
        dom=dom,
        structured=extract_structured(html, PAGE_URL, dom),
        config=settings.config,
    )


def test_the_example_plugin_satisfies_the_protocol() -> None:
    assert isinstance(ShopifyPlugin(), Plugin)


def test_the_example_reads_minor_units_correctly(settings: Settings) -> None:
    """249900 is Rs 2,499.00. Getting this wrong by a factor of 100 in a price
    column is the most expensive mistake available here."""
    result = ShopifyPlugin().extract(shopify_ctx(settings))

    assert result.source_price == 2499.00
    assert result.source_currency == "INR"


def test_the_example_flags_variants_priced_differently(settings: Settings) -> None:
    """One CSV row cannot carry two prices, and picking one silently would be a
    decision the operator never made."""
    result = ShopifyPlugin().extract(shopify_ctx(settings))
    assert any("different prices" in flag for flag in result.flags)


def test_the_example_recovers_the_full_gallery_at_original_size(settings: Settings) -> None:
    result = ShopifyPlugin().extract(shopify_ctx(settings))

    assert len(result.image_candidates) == 4
    assert all("_600x600" not in u and "_grande" not in u for u in result.image_candidates)
    assert all(u.startswith("https://") for u in result.image_candidates)


def test_the_example_ignores_a_page_that_is_not_shopify(settings: Settings) -> None:
    plugin = ShopifyPlugin()
    assert not plugin.matches(PAGE_URL, BARE_HTML)
    assert plugin.extract(shopify_ctx(settings, BARE_HTML)).is_empty


def test_the_example_survives_a_theme_that_mangled_the_meta_object(
    settings: Settings,
) -> None:
    mangled = SHOPIFY_HTML.replace('"product"', "product")  # not JSON any more
    result = ShopifyPlugin().extract(shopify_ctx(settings, mangled))
    assert result.is_empty
