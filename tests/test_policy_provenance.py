"""Phase 6: category mapping, HS suggestion, FX, the policy screen, provenance.

The two invariants the spec cares about most live here: gi_region is never
written, and price_inr is never silently invented.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from haat_lister.config import FxConfig, PriceConfig
from haat_lister.enrich.category import classify, validate_slugs
from haat_lister.enrich.fx import convert, rate_for
from haat_lister.enrich.hs_code import suggest
from haat_lister.models import (
    Confidence,
    DescriptionMode,
    FieldSource,
    FieldValue,
    ImageMethod,
    ImageMode,
    ImageResult,
    PriceStrategy,
    ProductRecord,
    Provenance,
    RowStatus,
)
from haat_lister.output.csv_writer import HAAT_COLUMNS, row_values
from haat_lister.output.review_writer import REVIEW_COLUMNS, review_row
from haat_lister.policy.provenance import (
    apply_gate,
    effective_description_mode,
    hosting_allowed,
)
from haat_lister.policy.screen import (
    describe,
    gi_mentions,
    load_vocabulary,
    screen_text,
)

POLICY_DIR = Path(__file__).resolve().parents[1] / "haat_lister" / "policy"


def make_record(title: str = "Kurta", description: str = "", **overrides) -> ProductRecord:
    record = ProductRecord(
        row_key="k",
        source_url="https://shop.example/p",
        canonical_url="https://shop.example/p",
        provenance=overrides.pop("provenance", Provenance.OWN),
        title=FieldValue.found(title, FieldSource.JSONLD),
        description=(
            FieldValue.found(description, FieldSource.JSONLD)
            if description
            else FieldValue.missing()
        ),
    )
    for key, value in overrides.items():
        setattr(record, key, value)
    return record


@pytest.fixture
def vocabulary():
    return load_vocabulary(POLICY_DIR / "keywords.yaml", POLICY_DIR / "brands.txt")


# ---------------------------------------------------------------------------
# Category mapping
# ---------------------------------------------------------------------------


def test_classifies_into_a_real_taxonomy_slug(shipped_taxonomy):
    record = make_record("Silver jhumka earrings with pearls")
    result = classify(record, shipped_taxonomy)
    assert result.category_slug.value == "jewellery"
    assert result.subcategory_slug.value == "earrings"


def test_classifies_a_saree_into_handwoven_textiles(shipped_taxonomy):
    record = make_record("Teal Blue Dola Silk Saree with Zari Woven Motifs")
    result = classify(record, shipped_taxonomy)
    assert result.category_slug.value == "handwoven-textiles"
    assert result.subcategory_slug.value == "sarees"


def test_category_confidence_never_exceeds_medium(shipped_taxonomy):
    """It is a suggestion, and §4 says suggest-and-flag."""
    result = classify(make_record("Silver jhumka earrings"), shipped_taxonomy)
    assert result.category_slug.confidence is Confidence.MEDIUM


def test_unmatched_product_falls_back_to_more_crafts(shipped_taxonomy):
    record = make_record("Brass temple bell from Kumbakonam")
    result = classify(record, shipped_taxonomy)
    assert result.category_slug.value == "more-crafts"
    assert result.custom_category.is_present
    assert result.custom_category.confidence is Confidence.LOW
    assert any("could not be determined" in f for f in result.flags)


def test_custom_category_only_when_more_crafts(shipped_taxonomy):
    result = classify(make_record("Silver jhumka earrings"), shipped_taxonomy)
    assert result.category_slug.value == "jewellery"
    assert not result.custom_category.is_present


def test_unmatched_shelf_is_blank_not_guessed(shipped_taxonomy):
    """Parent is confident, shelf is not: blank plus a flag beats a coin flip."""
    record = make_record("Handloom cotton yardage", "Woven khadi fabric sold by weight.")
    result = classify(record, shipped_taxonomy)
    assert result.category_slug.value == "handwoven-textiles"
    if not result.subcategory_slug.is_present:
        assert any("no shelf matched" in f for f in result.flags)


def test_every_emitted_slug_exists_in_the_taxonomy(shipped_taxonomy):
    for title in [
        "Silver jhumka earrings",
        "Handloom cotton saree",
        "Leather tote bag",
        "Men's kurta",
        "Brass temple bell",
    ]:
        record = make_record(title)
        result = classify(record, shipped_taxonomy)
        record.category_slug = result.category_slug
        record.subcategory_slug = result.subcategory_slug
        assert validate_slugs(record, shipped_taxonomy) is None


def test_unknown_category_slug_is_row_failure(shipped_taxonomy):
    """A plugin or the LLM assist could write one; this is the last gate."""
    record = make_record()
    record.category_slug = FieldValue.found("pottery-and-ceramics", FieldSource.PLUGIN)
    reason = validate_slugs(record, shipped_taxonomy)
    assert reason == "unknown_category_slug:pottery-and-ceramics"


def test_unknown_subcategory_slug_is_row_failure(shipped_taxonomy):
    record = make_record()
    record.category_slug = FieldValue.found("jewellery", FieldSource.INFERRED)
    record.subcategory_slug = FieldValue.found("anklets", FieldSource.PLUGIN)
    assert validate_slugs(record, shipped_taxonomy) == "unknown_subcategory_slug:jewellery/anklets"


# ---------------------------------------------------------------------------
# HS codes
# ---------------------------------------------------------------------------


def test_hs_code_is_suggested_from_the_evidenced_map(app_config):
    record = make_record("Cotton kurta")
    record.category_slug = FieldValue.found("apparel", FieldSource.INFERRED)
    result = suggest(record, app_config.hs_codes)
    assert result.hs_code.value == "6206"


def test_hs_code_confidence_is_capped_at_medium(app_config):
    """A wrong code is a legal and financial problem for the seller."""
    record = make_record("Silver earrings")
    record.category_slug = FieldValue.found("jewellery", FieldSource.INFERRED)
    result = suggest(record, app_config.hs_codes)
    assert result.hs_code.confidence is Confidence.MEDIUM
    # §4: a populated hs_code must still reach review.csv. Medium confidence is
    # what puts it there, via `low_confidence_fields`.
    assert result.notes, "a populated hs_code must still be explained"
    assert not result.flags, "a routine suggestion must not mark every row needs_review"


def test_unmapped_category_yields_a_blank_hs_code(app_config):
    record = make_record("Brass bell")
    record.category_slug = FieldValue.found("more-crafts", FieldSource.INFERRED)
    result = suggest(record, app_config.hs_codes)
    assert result.hs_code.value is None
    assert any("guessed" in n for n in result.notes)


# ---------------------------------------------------------------------------
# FX
# ---------------------------------------------------------------------------


def price_cfg(strategy: PriceStrategy, markup: float | None = None) -> PriceConfig:
    return PriceConfig(strategy=strategy, markup_percent=markup)


def fx_cfg(**kwargs) -> FxConfig:
    return FxConfig(as_of="2026-07-01", stale_after_days=30, rates_to_inr={"USD": 83.5}, **kwargs)


def test_price_conversion_records_rate():
    result = convert(70.97, "USD", price_cfg(PriceStrategy.CONVERT), fx_cfg())
    assert result.price_inr.value == int(round(70.97 * 83.5))
    assert result.rate_used == 83.5
    assert result.rate_as_of == "2026-07-01"
    assert "83.5" in (result.price_inr.note or "")


def test_conversion_without_a_rate_stays_blank():
    """Never convert at a guessed rate."""
    result = convert(45.0, "EUR", price_cfg(PriceStrategy.CONVERT), fx_cfg())
    assert result.price_inr.value is None
    assert any("No FX rate configured for EUR" in f for f in result.flags)


def test_markup_is_applied_on_top_of_conversion():
    result = convert(100.0, "USD", price_cfg(PriceStrategy.MARKUP, 20), fx_cfg())
    assert result.price_inr.value == int(round(100 * 83.5 * 1.2))


def test_markup_without_a_percentage_stays_blank():
    result = convert(100.0, "USD", price_cfg(PriceStrategy.MARKUP), fx_cfg())
    assert result.price_inr.value is None


def test_blank_strategy_does_no_conversion():
    result = convert(70.97, "USD", price_cfg(PriceStrategy.BLANK), fx_cfg())
    assert result.price_inr.value is None
    assert not result.flags


def test_inr_needs_no_rate():
    assert rate_for("INR", fx_cfg()) == 1.0


def test_converted_price_is_always_flagged():
    """It is our arithmetic, not the maker's decision."""
    result = convert(70.97, "USD", price_cfg(PriceStrategy.CONVERT), fx_cfg())
    assert any("CONVERTED, not set by the maker" in f for f in result.flags)


# ---------------------------------------------------------------------------
# Policy screen
# ---------------------------------------------------------------------------


def test_policy_screen_flags_ivory_and_brand_tokens(vocabulary):
    hits = screen_text(
        "Ivory-handled letter opener",
        "A Gucci-inspired design in genuine bone.",
        vocabulary,
    )
    flags = {hit.flag for hit in hits}
    assert "wildlife:ivory" in flags
    assert "wildlife:bone" in flags
    assert "brand_token:gucci" in flags


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Antique colonial era brass tray", "antiquities:antique"),
        ("Hand-forged Damascus knife", "weapons:knife"),
        ("Peacock feather earrings", "wildlife:peacock feather"),
        ("Certified GI tag Banarasi silk", "gi_claim:gi tag"),
    ],
)
def test_policy_categories_fire(vocabulary, text, expected):
    assert expected in {hit.flag for hit in screen_text(text, "", vocabulary)}


def test_word_boundaries_prevent_silly_matches(vocabulary):
    """'gun' must not fire on 'begun', 'fur' must not fire on 'furniture'."""
    hits = screen_text("Furniture polish, work begun in Jaipur", "", vocabulary)
    flags = {hit.flag for hit in hits}
    assert "weapons:gun" not in flags
    assert "wildlife:fur" not in flags


def test_clean_product_produces_no_flags(vocabulary):
    hits = screen_text(
        "Hand-embroidered cotton kurta",
        "Hand-embroidered in Kutch on handloom cotton with a mirror-work yoke.",
        vocabulary,
    )
    assert hits == []


def test_screen_describes_hits_in_plain_language(vocabulary):
    hits = screen_text("Ivory bangle", "", vocabulary)
    lines = describe(hits)
    assert lines and "Check before listing" in lines[0]


def test_gi_mentions_are_isolated_from_other_hits(vocabulary):
    hits = screen_text("GI tag certified Banarasi silk saree", "", vocabulary)
    assert gi_mentions(hits, vocabulary)


def test_gi_region_always_blank_even_with_a_loud_gi_claim(app_config, vocabulary):
    """The headline invariant: the source can shout, the cell stays empty."""
    record = make_record(
        "GI tag certified authentic Banarasi silk saree",
        "Officially GI registered, geographical indication protected.",
    )
    hits = screen_text(record.title.value, record.description.value, vocabulary)
    assert gi_mentions(hits, vocabulary)

    record.gi_mention_found = "Source claims a GI tag."
    values = row_values(record, app_config, ImageMode.MANIFEST)
    assert values[HAAT_COLUMNS.index("gi_region")] == ""

    review = dict(zip(REVIEW_COLUMNS, review_row(record, app_config), strict=True))
    assert review["gi_mention_found"] == "Source claims a GI tag."


# ---------------------------------------------------------------------------
# Provenance gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provenance", [Provenance.OWN, Provenance.AUTHORISED])
def test_own_and_authorised_proceed_normally(provenance):
    record = make_record(provenance=provenance)
    apply_gate(record, DescriptionMode.RAW)
    assert record.status is RowStatus.OK
    assert hosting_allowed(provenance)


def test_third_party_provenance_forces_review_and_blocks_hosting():
    record = make_record(
        "Kurta", "Copy from someone else's shop.", provenance=Provenance.THIRD_PARTY
    )
    apply_gate(record, DescriptionMode.RAW)

    assert record.status is RowStatus.NEEDS_REVIEW
    assert not hosting_allowed(Provenance.THIRD_PARTY)
    assert any("third-party" in n for n in record.notes)
    assert any("rewritten before listing" in n for n in record.notes)


def test_third_party_forces_description_rewrite():
    assert (
        effective_description_mode(DescriptionMode.RAW, Provenance.THIRD_PARTY)
        is DescriptionMode.REWRITE
    )
    assert (
        effective_description_mode(DescriptionMode.RAW, Provenance.OWN) is DescriptionMode.RAW
    )


def test_hosted_image_on_a_third_party_row_is_a_hard_error():
    """Defence in depth: the image pipeline is gated, and so is this."""
    record = make_record(provenance=Provenance.THIRD_PARTY)
    record.image = ImageResult(method=ImageMethod.HOSTED, url="https://host.example/x.jpg")
    with pytest.raises(AssertionError, match="must not upload"):
        apply_gate(record, DescriptionMode.REWRITE)
