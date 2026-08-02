"""Phase 5: price, dimensions and units, variants, availability, stock.

Everything here is about NOT inventing numbers. A blank cell that gets flagged
is always correct; a confidently wrong weight is a customs problem and a
confidently wrong stock count oversells a one-of-a-kind piece.
"""

from __future__ import annotations

import json

import pytest
from selectolax.parser import HTMLParser

from haat_lister.extract.dimensions import extract_dimensions
from haat_lister.extract.price import detect_currency, extract_price, parse_amount
from haat_lister.extract.specs import spec_pairs
from haat_lister.extract.structured import extract_structured
from haat_lister.extract.variants import (
    extract_availability,
    extract_sizes,
    extract_stock_qty,
)
from haat_lister.models import Confidence, PriceStrategy
from haat_lister.utils.units import (
    Measurement,
    parse_dimension_triple,
    parse_measurement,
    to_cm,
    to_grams,
)

BASE = "https://shop.example/products/x"


def parse(html: str):
    dom = HTMLParser(html)
    return extract_structured(html, BASE, dom), dom


def product_page(product: dict, body: str = "") -> str:
    return (
        f'<html><head><script type="application/ld+json">'
        f'{json.dumps({"@context": "https://schema.org", "@type": "Product", **product})}'
        f"</script></head><body>{body}</body></html>"
    )


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "grams"),
    [
        ("350 g", 350),
        ("350g", 350),
        ("1.2 kg", 1200),
        ("2 lb", 907),        # 907.184
        ("8 oz", 227),        # 226.796
        ("1 pound", 454),
        ("500 grams", 500),
    ],
)
def test_unit_conversions_weight(text, grams):
    assert to_grams(parse_measurement(text)) == grams


@pytest.mark.parametrize(
    ("text", "cm"),
    [
        ("70 cm", 70),
        ("12 inches", 30),    # 30.48
        ("2.5 in", 6),        # 6.35 -- convert THEN round
        ("150 mm", 15),
        ("1 m", 100),
        ('12"', 30),
    ],
)
def test_unit_conversions_length(text, cm):
    assert to_cm(parse_measurement(text)) == cm


def test_unknown_units_are_refused_not_guessed():
    assert to_grams(parse_measurement("350 widgets")) is None
    assert to_cm(Measurement(5, "furlongs")) is None


def test_schema_unit_codes_are_understood():
    assert to_grams(Measurement(350, "GRM")) == 350
    assert to_cm(Measurement(70, "CMT")) == 70
    assert to_grams(Measurement(2, "LBR")) == 907


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("70 x 50 x 2 cm", ([70, 50, 2], None)),
        ("70x50x2cm", ([70, 50, 2], None)),
        ("12 x 8 inches", ([30, 20], None)),
        ("L70 x W50 x H2 cm", ([70, 50, 2], ["length", "width", "height"])),
        # depth is the front-to-back measure, which is what haat calls length
        ("H 20 x W 15 x D 10 cm", ([20, 15, 10], ["height", "width", "length"])),
    ],
)
def test_dimension_triple_parsing(text, expected):
    assert parse_dimension_triple(text) == expected


def test_a_repeated_axis_is_reported_as_unstated():
    """"L x W x D" maps both L and D to length. Better to admit we cannot tell
    than to silently drop one of the numbers."""
    values, stated = parse_dimension_triple("L70 x W50 x D2 cm")
    assert values == [70, 50, 2]
    assert stated is None


# ---------------------------------------------------------------------------
# Price
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("2499", 2499.0),
        ("₹2,499.00", 2499.0),
        ("$70.97", 70.97),
        ("1.234,56", 1234.56),
        ("Rs. 1,899", 1899.0),
        ("free", None),
    ],
)
def test_amount_parsing(text, amount):
    assert parse_amount(text) == amount


def test_currency_detection(app_config):
    cfg = app_config.currency
    assert detect_currency("₹2,499", cfg) == ("INR", False)
    assert detect_currency("Rs. 1899", cfg) == ("INR", False)
    assert detect_currency("€45", cfg) == ("EUR", False)
    assert detect_currency("US$70", cfg) == ("USD", False)


def test_bare_dollar_is_recorded_as_ambiguous(app_config):
    """USD, CAD, AUD and SGD all use '$'."""
    code, ambiguous = detect_currency("$70.97", app_config.currency)
    assert code == "USD"
    assert ambiguous is True


def test_price_inr_blank_by_default_with_source_recorded(app_config):
    sd, dom = parse(
        product_page(
            {"name": "Saree", "offers": {"price": "70.97", "priceCurrency": "USD"}}
        )
    )
    result = extract_price(sd, dom, app_config.currency, PriceStrategy.BLANK)

    assert result.price_inr.value is None
    assert result.source_amount == 70.97
    assert result.source_currency == "USD"
    assert any("business decision" in n for n in result.notes)


def test_inr_source_is_still_blank_by_default(app_config):
    """Even an INR page does not get copied without being asked."""
    sd, dom = parse(
        product_page({"name": "Kurta", "offers": {"price": "2499", "priceCurrency": "INR"}})
    )
    result = extract_price(sd, dom, app_config.currency, PriceStrategy.BLANK)
    assert result.price_inr.value is None


def test_copy_strategy_copies_only_inr(app_config):
    sd, dom = parse(
        product_page({"name": "Kurta", "offers": {"price": "2499", "priceCurrency": "INR"}})
    )
    result = extract_price(sd, dom, app_config.currency, PriceStrategy.COPY)
    assert result.price_inr.value == 2499

    sd, dom = parse(
        product_page({"name": "Saree", "offers": {"price": "70.97", "priceCurrency": "USD"}})
    )
    result = extract_price(sd, dom, app_config.currency, PriceStrategy.COPY)
    assert result.price_inr.value is None
    assert any("only applies to INR" in n for n in result.notes)


def test_convert_strategy_does_not_invent_a_rate(app_config):
    """FX arrives in Phase 6; until then this must not produce a number."""
    sd, dom = parse(
        product_page({"name": "Saree", "offers": {"price": "70.97", "priceCurrency": "USD"}})
    )
    result = extract_price(sd, dom, app_config.currency, PriceStrategy.CONVERT)
    assert result.price_inr.value is None
    assert any("FX rate" in n for n in result.notes)


def test_price_falls_back_to_the_dom(app_config):
    sd, dom = parse('<html><body><span class="price">₹1,899</span></body></html>')
    result = extract_price(sd, dom, app_config.currency)
    assert result.source_amount == 1899.0
    assert result.source_currency == "INR"


# ---------------------------------------------------------------------------
# Spec tables
# ---------------------------------------------------------------------------


def test_spec_pairs_reads_tables_dls_and_lines():
    dom = HTMLParser(
        "<table><tr><th>Weight</th><td>350 g</td></tr></table>"
        "<dl><dt>Dimensions</dt><dd>70 x 50 x 2 cm</dd></dl>"
        "<li>Material: Handloom cotton</li>"
    )
    pairs = spec_pairs(dom)
    assert pairs["weight"] == "350 g"
    assert pairs["dimensions"] == "70 x 50 x 2 cm"
    assert pairs["material"] == "Handloom cotton"


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------


def test_weight_and_dimensions_from_a_spec_table(app_config):
    sd, dom = parse(
        "<html><body><table>"
        "<tr><th>Weight</th><td>350 g</td></tr>"
        "<tr><th>Dimensions</th><td>70 x 50 x 2 cm</td></tr>"
        "</table></body></html>"
    )
    result = extract_dimensions(sd, dom, app_config.extraction)
    assert result.weight_g.value == 350
    assert (result.length_cm.value, result.width_cm.value, result.height_cm.value) == (70, 50, 2)


def test_schema_quantitative_values_are_used(app_config):
    sd, dom = parse(
        product_page(
            {
                "name": "Kurta",
                "weight": {"@type": "QuantitativeValue", "value": 2, "unitCode": "LBR"},
                "width": {"@type": "QuantitativeValue", "value": 20, "unitCode": "INH"},
            }
        )
    )
    result = extract_dimensions(sd, dom, app_config.extraction)
    assert result.weight_g.value == 907
    assert result.width_cm.value == 51  # 50.8
    assert result.weight_g.confidence is Confidence.HIGH


def test_product_weight_beats_shipping_weight(app_config):
    sd, dom = parse(
        "<html><body><table>"
        "<tr><th>Shipping weight</th><td>600 g</td></tr>"
        "<tr><th>Product weight</th><td>350 g</td></tr>"
        "</table></body></html>"
    )
    result = extract_dimensions(sd, dom, app_config.extraction)
    assert result.weight_g.value == 350
    assert not result.flags


def test_shipping_weight_is_used_but_flagged(app_config):
    sd, dom = parse(
        "<html><body><table><tr><th>Shipping weight</th><td>600 g</td></tr></table></body></html>"
    )
    result = extract_dimensions(sd, dom, app_config.extraction)
    assert result.weight_g.value == 600
    assert result.weight_g.confidence is Confidence.LOW
    assert any("includes packaging" in f for f in result.flags)


def test_unlabelled_dimension_order_is_flagged(app_config):
    sd, dom = parse(
        "<html><body><table><tr><th>Dimensions</th><td>70 x 50 x 2 cm</td></tr>"
        "</table></body></html>"
    )
    result = extract_dimensions(sd, dom, app_config.extraction)
    assert any("source order" in f for f in result.flags)
    assert result.length_cm.confidence is Confidence.LOW


def test_non_standard_axis_order_is_normalised_and_flagged(app_config):
    sd, dom = parse(
        "<html><body><table><tr><th>Dimensions</th><td>H 20 x W 15 x D 10 cm</td></tr>"
        "</table></body></html>"
    )
    result = extract_dimensions(sd, dom, app_config.extraction)
    assert any("normalised to length x width x height" in f for f in result.flags)


def test_missing_dimensions_are_never_fabricated(app_config):
    sd, dom = parse(product_page({"name": "Kurta"}))
    result = extract_dimensions(sd, dom, app_config.extraction)
    assert result.weight_g.value is None
    assert result.length_cm.value is None
    assert any("blank rather than estimated" in n for n in result.notes)


# ---------------------------------------------------------------------------
# Sizes
# ---------------------------------------------------------------------------


def test_sizes_from_a_select_preserve_source_order(app_config):
    sd, dom = parse(
        "<html><body><select name='size'>"
        "<option>Select size</option><option>S</option><option>M</option>"
        "<option>L</option><option>XL</option>"
        "</select></body></html>"
    )
    assert extract_sizes(sd, dom, app_config.extraction).value == "S,M,L,XL"


def test_size_letters_are_upcased_but_free_sizes_are_left_alone(app_config):
    sd, dom = parse(
        "<html><body><select name='size'>"
        "<option>s</option><option>Free Size</option><option>38</option>"
        "</select></body></html>"
    )
    assert extract_sizes(sd, dom, app_config.extraction).value == "S,Free Size,38"


def test_non_apparel_has_no_sizes(app_config):
    sd, dom = parse(product_page({"name": "Silver pendant"}))
    assert extract_sizes(sd, dom, app_config.extraction).value is None


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def test_in_stock_maps_to_the_configured_value(app_config):
    sd, dom = parse(
        product_page(
            {"name": "Kurta", "offers": {"availability": "https://schema.org/InStock"}}
        )
    )
    field, notes, flags = extract_availability(sd, dom, app_config.fields)
    assert field.value == "stock"
    assert not flags


def test_made_to_order_is_blank_while_the_wire_value_is_unknown(app_config):
    """Guessing an enum would just get the row rejected at import."""
    assert app_config.fields.availability_made_to_order_value is None
    sd, dom = parse(
        product_page(
            {"name": "Kurta", "offers": {"availability": "https://schema.org/MadeToOrder"}}
        )
    )
    field, notes, flags = extract_availability(sd, dom, app_config.fields)
    assert field.value is None
    assert any("made-to-order" in f.lower() for f in flags)


def test_made_to_order_is_used_once_configured(app_config):
    app_config.fields.availability_made_to_order_value = "made_to_order"
    sd, dom = parse(
        product_page(
            {"name": "Kurta", "offers": {"availability": "https://schema.org/MadeToOrder"}}
        )
    )
    field, _, _ = extract_availability(sd, dom, app_config.fields)
    assert field.value == "made_to_order"


def test_out_of_stock_is_blank_and_flagged(app_config):
    sd, dom = parse(
        product_page(
            {"name": "Kurta", "offers": {"availability": "https://schema.org/OutOfStock"}}
        )
    )
    field, notes, flags = extract_availability(sd, dom, app_config.fields)
    assert field.value is None
    assert any("out of stock" in f.lower() for f in flags)


def test_unknown_availability_never_defaults_to_stock(app_config):
    """A note rather than a flag: plenty of pages simply never state it, so this
    is an expected gap, not a judgement call to overturn. The row still reaches
    review.csv because availability is in `required_by_haat`."""
    sd, dom = parse(product_page({"name": "Kurta"}))
    field, notes, flags = extract_availability(sd, dom, app_config.fields)
    assert field.value is None
    assert any("defaulting to stock" in n for n in notes)
    assert not flags
    assert "availability" in app_config.fields.required_by_haat


# ---------------------------------------------------------------------------
# Stock quantity
# ---------------------------------------------------------------------------


def test_stock_qty_from_inventory_level(app_config):
    sd, dom = parse(
        product_page(
            {
                "name": "Kurta",
                "offers": {
                    "inventoryLevel": {"@type": "QuantitativeValue", "value": "12"}
                },
            }
        )
    )
    field, _ = extract_stock_qty(sd, dom, app_config.extraction)
    assert field.value == 12


def test_vague_stock_copy_never_becomes_a_number(app_config):
    """'Only a few left' is not a quantity, and 1 or 10 are not defaults."""
    sd, dom = parse("<html><body><p>Hurry, only a few left!</p></body></html>")
    field, notes = extract_stock_qty(sd, dom, app_config.extraction)
    assert field.value is None
    assert any("never defaulted" in n for n in notes)


def test_explicit_count_in_copy_is_read_at_low_confidence(app_config):
    sd, dom = parse("<html><body><p>25 in stock</p></body></html>")
    field, _ = extract_stock_qty(sd, dom, app_config.extraction)
    assert field.value == 25
    assert field.confidence is Confidence.LOW
