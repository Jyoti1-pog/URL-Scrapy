"""Phase 2: structured extraction, title, description, image candidates.

The HTML lives inline rather than in fixture files -- each case is small, and
seeing the markup next to the assertion is what makes these readable when a
heuristic later needs tweaking.
"""

from __future__ import annotations

import json

import pytest
from selectolax.parser import HTMLParser

from haat_lister.extract.description import clean_description, extract_description
from haat_lister.extract.images import Tier, collect_candidates, full_size_variant
from haat_lister.extract.structured import extract_structured
from haat_lister.extract.title import extract_title, strip_site_suffix
from haat_lister.models import Confidence, FieldSource
from haat_lister.utils.urls import canonicalise, declared_dimensions, row_key

BASE = "https://mystore.example/products/kurta"


def page(body: str, head: str = "") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


def jsonld(payload: dict) -> str:
    return f'<script type="application/ld+json">{json.dumps(payload)}</script>'


def parse(html: str, url: str = BASE):
    dom = HTMLParser(html)
    return extract_structured(html, url, dom), dom


# ---------------------------------------------------------------------------
# Structured data
# ---------------------------------------------------------------------------


def test_finds_product_inside_graph():
    html = page(
        "",
        jsonld(
            {
                "@context": "https://schema.org",
                "@graph": [
                    {"@type": "BreadcrumbList", "itemListElement": []},
                    {"@type": "Product", "name": "Kutch Mirror Kurta"},
                ],
            }
        ),
    )
    sd, _ = parse(html)
    assert sd.product is not None
    assert sd.product_str("name")[0] == "Kutch Mirror Kurta"
    assert sd.product_source is FieldSource.JSONLD


def test_malformed_jsonld_degrades_instead_of_raising():
    """Broken JSON-LD is common; it must fall through to og, not fail the row."""
    html = page(
        "",
        '<script type="application/ld+json">{"@type": "Product", name: broken]</script>'
        '<meta property="og:title" content="Fallback Title">',
    )
    sd, dom = parse(html)
    assert sd.product is None
    assert sd.og("og:title")[0] == "Fallback Title"


def test_microdata_is_used_when_no_jsonld(app_config):
    html = page(
        '<div itemscope itemtype="https://schema.org/Product">'
        '<span itemprop="name">Silver Jhumka</span></div>'
    )
    sd, dom = parse(html)
    assert sd.product_source is FieldSource.MICRODATA
    title = extract_title(sd, dom, app_config.extraction, 200)
    assert title.value == "Silver Jhumka"


# ---------------------------------------------------------------------------
# Title
# ---------------------------------------------------------------------------


def test_title_prefers_jsonld_over_og_and_h1(app_config):
    html = page(
        "<h1>Wrong H1</h1>",
        jsonld({"@type": "Product", "name": "Right Name"})
        + '<meta property="og:title" content="Wrong OG">',
    )
    sd, dom = parse(html)
    title = extract_title(sd, dom, app_config.extraction, 200)
    assert title.value == "Right Name"
    assert title.confidence is Confidence.HIGH
    assert title.source is FieldSource.JSONLD


def test_title_falls_back_through_og_then_h1(app_config):
    sd, dom = parse(page("<h1>From H1</h1>", '<meta property="og:title" content="From OG">'))
    assert extract_title(sd, dom, app_config.extraction, 200).source is FieldSource.OG

    sd, dom = parse(page("<h1>From H1</h1>"))
    title = extract_title(sd, dom, app_config.extraction, 200)
    assert title.value == "From H1"
    assert title.confidence is Confidence.MEDIUM


def test_missing_title_is_reported_not_invented(app_config):
    sd, dom = parse(page("<p>nothing useful</p>"))
    title = extract_title(sd, dom, app_config.extraction, 200)
    assert title.value is None
    assert title.needs_human


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Blue Kurta | MyStore | Free Shipping", "Blue Kurta"),
        ("Blue Kurta — MyStore", "Blue Kurta"),
        ("Blue Kurta", "Blue Kurta"),
        # The tail is too long to be branding, so it stays.
        (
            "Kurta | hand embroidered in Kutch by women artisans",
            "Kurta | hand embroidered in Kutch by women artisans",
        ),
    ],
)
def test_site_branding_is_stripped_from_title_tag(app_config, raw, expected):
    assert strip_site_suffix(raw, app_config.extraction) == expected


def test_allcaps_title_is_normalised_and_downgraded(app_config):
    html = page("", jsonld({"@type": "Product", "name": "HANDMADE COTTON KURTA SALE"}))
    sd, dom = parse(html)
    title = extract_title(sd, dom, app_config.extraction, 200)
    assert title.value == "Handmade Cotton Kurta Sale"
    assert title.confidence is Confidence.MEDIUM
    assert "ALL-CAPS" in (title.note or "")


def test_title_is_truncated_at_the_configured_limit(app_config):
    long_name = " ".join(["kurta"] * 100)
    sd, dom = parse(page("", jsonld({"@type": "Product", "name": long_name})))
    title = extract_title(sd, dom, app_config.extraction, 200)
    assert len(title.value) <= 200


# ---------------------------------------------------------------------------
# Description
# ---------------------------------------------------------------------------


def test_boilerplate_lines_are_dropped_but_copy_is_kept(app_config):
    raw = (
        "Hand-embroidered in Kutch on handloom cotton.\n"
        "Add to cart\n"
        "Free shipping over 999\n"
        "Natural-dye finish with a mirror-work yoke."
    )
    cleaned = clean_description(raw, app_config.extraction, 5000)
    assert "Hand-embroidered in Kutch" in cleaned
    assert "mirror-work yoke" in cleaned
    assert "Add to cart" not in cleaned
    assert "Free shipping" not in cleaned


def test_control_characters_never_reach_the_csv(app_config):
    raw = "Good copy\x00\x07 with junk bytes in it, plenty long."
    cleaned = clean_description(raw, app_config.extraction, 5000)
    assert "\x00" not in cleaned and "\x07" not in cleaned


def test_description_falls_back_to_dom_block_with_low_confidence(app_config):
    html = page(
        '<div class="product-description">'
        "Handwoven in Bhuj from khadi cotton, finished with a natural indigo dye."
        "</div>"
    )
    sd, dom = parse(html)
    desc = extract_description(sd, dom, app_config.extraction, 5000)
    assert desc.source is FieldSource.HEURISTIC
    assert desc.confidence is Confidence.LOW
    assert desc.needs_human


def test_short_description_is_treated_as_absent(app_config):
    sd, dom = parse(page("", '<meta name="description" content="Kurta">'))
    assert extract_description(sd, dom, app_config.extraction, 5000).value is None


# ---------------------------------------------------------------------------
# Image candidates
# ---------------------------------------------------------------------------


def candidates(html: str, app_config, url: str = BASE):
    sd, dom = parse(html, url)
    return collect_candidates(sd, dom, url, app_config.images, app_config.validator)


def test_candidates_are_collected_from_every_source(app_config):
    html = page(
        '<img src="/img/gallery-1.jpg">'
        '<div style="background-image: url(\'/img/zoom.jpg\')"></div>',
        jsonld({"@type": "Product", "image": ["https://cdn.example/hero.jpg"]})
        + '<meta property="og:image" content="https://cdn.example/og.jpg">'
        '<meta name="twitter:image" content="https://cdn.example/tw.jpg">',
    )
    urls = [c.url for c in candidates(html, app_config)]
    assert "https://cdn.example/hero.jpg" in urls
    assert "https://cdn.example/og.jpg" in urls
    assert "https://cdn.example/tw.jpg" in urls
    assert "https://mystore.example/img/gallery-1.jpg" in urls
    assert "https://mystore.example/img/zoom.jpg" in urls


def test_relative_and_protocol_relative_urls_are_resolved(app_config):
    html = page('<img src="//cdn.example/a.jpg"><img src="../b.jpg">')
    urls = [c.url for c in candidates(html, app_config)]
    assert "https://cdn.example/a.jpg" in urls
    assert "https://mystore.example/b.jpg" in urls


def test_data_uris_and_javascript_are_dropped(app_config):
    html = page('<img src="data:image/gif;base64,R0lGOD"><img src="javascript:void(0)">')
    assert candidates(html, app_config) == []


def test_obvious_non_products_are_rejected(app_config):
    html = page(
        '<img src="/img/logo.png">'
        '<img src="/img/payment-badge.png">'
        '<img src="/img/thumb/kurta.jpg">'
        '<img src="/img/real-product.jpg">'
    )
    urls = [c.url for c in candidates(html, app_config)]
    assert urls == ["https://mystore.example/img/real-product.jpg"]


def test_reject_list_does_not_match_the_hostname(app_config):
    """A shop at iconic-crafts.com must not lose every image to the 'icon' rule."""
    url = "https://iconic-crafts.example/products/x"
    html = page('<img src="https://iconic-crafts.example/media/kurta.jpg">')
    urls = [c.url for c in candidates(html, app_config, url)]
    assert urls == ["https://iconic-crafts.example/media/kurta.jpg"]


def test_tracking_params_are_stripped_from_image_urls(app_config):
    html = page('<img src="https://cdn.example/kurta.jpg?utm_source=ig&v=42">')
    assert candidates(html, app_config)[0].url == "https://cdn.example/kurta.jpg?v=42"


@pytest.mark.parametrize(
    ("sized", "full"),
    [
        ("https://cdn.shopify.com/s/files/kurta_300x300.jpg", "https://cdn.shopify.com/s/files/kurta.jpg"),
        ("https://cdn.shopify.com/s/files/kurta_1024x1024@2x.jpg", "https://cdn.shopify.com/s/files/kurta.jpg"),
        ("https://cdn.shopify.com/s/files/kurta_grande.jpg", "https://cdn.shopify.com/s/files/kurta.jpg"),
        ("https://shop.example/wp-content/uploads/kurta-600x600.jpg", "https://shop.example/wp-content/uploads/kurta.jpg"),
        ("https://cdn.example/kurta.jpg", None),
        # Unknown host: the _WxH convention is Shopify's, and applying it here
        # would be inventing a URL.
        ("https://cdn.unknown-host.example/kurta_300x300.jpg", None),
        ("https://cdn.unknown-host.example/kurta-300x300.jpg", None),
    ],
)
def test_known_cdn_full_size_variants(sized, full):
    assert full_size_variant(sized) == full


def test_upsized_variant_ranks_first_and_usable_original_is_kept(app_config):
    """Never replace blindly: if the full-size guess 404s, Tier 1 falls back to
    the URL the page actually published."""
    html = page('<img src="https://cdn.shopify.com/s/files/kurta_grande.jpg">')
    ranked = candidates(html, app_config)
    assert ranked[0].url == "https://cdn.shopify.com/s/files/kurta.jpg"
    assert ranked[0].upsized_from == "https://cdn.shopify.com/s/files/kurta_grande.jpg"
    assert ranked[1].url == "https://cdn.shopify.com/s/files/kurta_grande.jpg"


def test_original_known_to_be_too_small_is_not_kept_as_a_fallback(app_config):
    """A 300x300 cannot pass predicate 6, so retaining it only buys a wasted HEAD."""
    html = page('<img src="https://cdn.shopify.com/s/files/kurta_300x300.jpg">')
    urls = [c.url for c in candidates(html, app_config)]
    assert urls == ["https://cdn.shopify.com/s/files/kurta.jpg"]


def test_cap_counts_photos_not_urls(app_config):
    """Six slots means six product photos, each still carrying its fallback."""
    html = page(
        "".join(
            f'<img src="https://cdn.shopify.com/s/files/kurta-{i}_grande.jpg">' for i in range(10)
        )
    )
    ranked = candidates(html, app_config)
    primaries = [c for c in ranked if c.upsized_from]
    assert len(primaries) == app_config.images.max_images_per_product
    assert len(ranked) == 2 * app_config.images.max_images_per_product


def test_srcset_variants_of_one_photo_collapse_into_one_group(app_config):
    html = page(
        '<img srcset="https://cdn.shopify.com/s/files/k_400x400.jpg 400w, '
        'https://cdn.shopify.com/s/files/k_1800x1800.jpg 1800w">'
    )
    ranked = candidates(html, app_config)
    assert ranked[0].url == "https://cdn.shopify.com/s/files/k.jpg"
    # The 1800 original survives as a fallback; the 400 one is known-too-small.
    assert [c.url for c in ranked[1:]] == ["https://cdn.shopify.com/s/files/k_1800x1800.jpg"]


def test_declared_too_small_ranks_below_unknown(app_config):
    """A URL that admits it is 200x200 cannot pass predicate 6, so it should not
    cost us a HEAD request ahead of an unknown one."""
    html = page(
        '<img src="https://cdn.example/small_200x200.jpg">'
        '<img src="https://cdn.example/unknown.jpg">'
        '<img src="https://cdn.example/big_1500x1500.jpg">'
    )
    ranked = candidates(html, app_config)
    order = [c.url for c in ranked]
    assert order.index("https://cdn.example/big_1500x1500.jpg") < order.index(
        "https://cdn.example/unknown.jpg"
    )
    assert order.index("https://cdn.example/unknown.jpg") < order.index(
        "https://cdn.example/small_200x200.jpg"
    )
    assert ranked[-1].url == "https://cdn.example/small_200x200.jpg"


def test_srcset_widths_are_parsed_and_ranked(app_config):
    html = page(
        '<img srcset="https://cdn.example/a.jpg 320w, https://cdn.example/b.jpg 1600w" '
        'src="https://cdn.example/c.jpg">'
    )
    ranked = candidates(html, app_config)
    assert ranked[0].url == "https://cdn.example/b.jpg"
    assert ranked[0].srcset_width == 1600


def test_lazy_attributes_are_collected(app_config):
    html = page('<img data-src="https://cdn.example/lazy.jpg" data-zoom-image="https://cdn.example/zoom.jpg">')
    urls = [c.url for c in candidates(html, app_config)]
    assert "https://cdn.example/lazy.jpg" in urls
    assert "https://cdn.example/zoom.jpg" in urls


def test_duplicates_are_removed_and_list_is_capped(app_config):
    html = page("".join(f'<img src="https://cdn.example/{i}.jpg">' for i in range(20)) * 2)
    ranked = candidates(html, app_config)
    assert len(ranked) == app_config.images.max_images_per_product
    assert len({c.url for c in ranked}) == len(ranked)


def test_tier_ordering_is_stable():
    assert Tier.UPSIZED < Tier.BIG_DECLARED < Tier.BIG_SRCSET < Tier.UNKNOWN < Tier.TOO_SMALL


# ---------------------------------------------------------------------------
# URL utilities
# ---------------------------------------------------------------------------


def test_canonicalise_strips_tracking_and_fragment_but_keeps_order():
    url = "HTTPS://Shop.Example:443/products/x?b=2&utm_source=ig&a=1#reviews"
    assert canonicalise(url) == "https://shop.example/products/x?b=2&a=1"


def test_canonicalise_is_stable_for_row_keys():
    a = canonicalise("https://shop.example/products/x?utm_campaign=spring")
    b = canonicalise("https://shop.example/products/x")
    assert a == b
    assert row_key(a) == row_key(b)


def test_row_key_is_readable_and_unique():
    key = row_key(canonicalise("https://shop.example/products/mirror-kurta"))
    assert key.startswith("shop-example-products-mirror-kurta-")
    assert key != row_key(canonicalise("https://shop.example/products/other-kurta"))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://cdn.example/a_1200x800.jpg", (1200, 800)),
        ("https://cdn.example/a.jpg?w=900", (900, None)),
        ("https://cdn.example/a.jpg", (None, None)),
    ],
)
def test_declared_dimensions(url, expected):
    assert declared_dimensions(url) == expected
