"""Phases 4-6: the gallery, the size floor, and the title.

The observed failure this whole pass exists for was one Amazon URL producing:

    image: none    title: "Mivi DuoPods Marathon Earbuds Wireless | Fast Charge |
                            70H Playtime | BT v5.3 | 13mm Drivers | Noise Cancella…"

Three separate defects in one row. Each is tested here against the page shape
that caused it.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from PIL import Image
from selectolax.parser import HTMLParser

from haat_lister.config import Settings
from haat_lister.extract.plugins import PluginContext, build_registry
from haat_lister.extract.plugins.amazon import AmazonPlugin, strip_size_modifier
from haat_lister.extract.structured import extract_structured
from haat_lister.extract.title import split_seo_tail, tidy_title
from haat_lister.images.pipeline import _best_low_res
from haat_lister.models import (
    Confidence,
    FieldSource,
    FieldValue,
    ImageMethod,
    Provenance,
    ValidationResult,
)

SEO_TITLE = (
    "Mivi DuoPods Marathon Earbuds Wireless | Fast Charge | 70H Playtime | "
    "BT v5.3 | 13mm Drivers | Noise Cancellation"
)

DYNAMIC = {
    "https://m.media-amazon.com/images/I/61hero._AC_SX679_.jpg": [679, 679],
    "https://m.media-amazon.com/images/I/61hero._AC_SX1500_.jpg": [1500, 1500],
    "https://m.media-amazon.com/images/I/61hero._AC_SX300_.jpg": [300, 300],
}

COLOR_IMAGES = [
    {
        "hiRes": "https://m.media-amazon.com/images/I/71second._SL1600_.jpg",
        "large": "https://m.media-amazon.com/images/I/71second._AC_SX679_.jpg",
        "variant": "PT01",
    },
    {
        "hiRes": None,
        "large": "https://m.media-amazon.com/images/I/71third._AC_SX679_.jpg",
        "variant": "PT02",
    },
]

AMAZON_PAGE = f"""<html><head><title>{SEO_TITLE}</title>
<meta property="og:title" content="{SEO_TITLE}">
</head><body>
<h1 id="title">{SEO_TITLE}</h1>
<div id="imgTagWrapperId">
  <img id="landingImage"
       src="https://m.media-amazon.com/images/I/61hero._AC_SX679_.jpg"
       data-old-hires="https://m.media-amazon.com/images/I/61hero._SL1500_.jpg"
       data-a-dynamic-image='{json.dumps(DYNAMIC)}'>
</div>
<div id="altImages">
  <img src="https://m.media-amazon.com/images/I/31thumb._AC_US40_.jpg">
  <img src="https://m.media-amazon.com/images/G/01/sprite-icons._V1_.png">
</div>
<script type="text/javascript">
  P.when('A').register("ImageBlockATF", function(A){{
    var data = {{"colorImages": {{"initial": {json.dumps(COLOR_IMAGES)}}},
                 "heroImage": {{}}, "alt": "a caption with [brackets] in it"}};
    return data;
  }});
</script>
<p>Add to cart</p><p>₹2,499</p>
</body></html>"""


def run_plugin(settings: Settings, html: str = AMAZON_PAGE, url: str = "") -> list[str]:
    url = url or "https://www.amazon.in/Mivi-Marathon/dp/B0FTFMNYBV"
    dom = HTMLParser(html)
    ctx = PluginContext(
        url=url,
        final_url=url,
        html=html,
        dom=dom,
        structured=extract_structured(html, url, dom),
        config=settings.config,
    )
    return AmazonPlugin().extract(ctx).image_candidates


# --------------------------------------------------------------------------
# Phase 4 -- the Amazon plugin
# --------------------------------------------------------------------------


def test_the_plugin_ships_and_matches_a_product_url(settings: Settings) -> None:
    registry = build_registry(settings.config, settings.root)
    plugin = registry.match("https://www.amazon.in/Some-Slug/dp/B0FTFMNYBV", "")
    assert plugin is not None and plugin.name == "amazon"

    # A search page is not a product page, and `matches` must be cheap enough
    # to run on every row -- so it looks at the URL, not the HTML.
    assert registry.match("https://www.amazon.in/s?k=earbuds", "") is None


def test_amazon_dynamic_image_json_parsed(settings: Settings) -> None:
    """§8 test 3. The page states the dimensions, so the largest is free."""
    found = run_plugin(settings)
    assert found, "the plugin found nothing on a page with a gallery"
    # Largest by area leads, and its stripped original leads that.
    assert found[0] == "https://m.media-amazon.com/images/I/61hero.jpg"
    assert "https://m.media-amazon.com/images/I/61hero._AC_SX1500_.jpg" in found


def test_amazon_size_modifier_stripped_and_ranked_above_original(settings: Settings) -> None:
    """§8 test 4. Added, never substituted -- a wrong guess costs one HEAD."""
    found = run_plugin(settings)
    stripped = "https://m.media-amazon.com/images/I/61hero.jpg"
    modified = "https://m.media-amazon.com/images/I/61hero._AC_SX1500_.jpg"

    assert found.index(stripped) < found.index(modified)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://m.media-amazon.com/images/I/61abcDEF._AC_SX679_.jpg",
            "https://m.media-amazon.com/images/I/61abcDEF.jpg",
        ),
        (
            "https://m.media-amazon.com/images/I/71q._SL1600_.jpg",
            "https://m.media-amazon.com/images/I/71q.jpg",
        ),
        (
            "https://m.media-amazon.com/images/I/61x._SX38_SY50_CR,0,0,38,50_.jpg",
            "https://m.media-amazon.com/images/I/61x.jpg",
        ),
        # No modifier, nothing to do.
        ("https://m.media-amazon.com/images/I/61abcDEF.jpg", None),
        # An underscore that is part of the filename, not a modifier.
        ("https://m.media-amazon.com/images/I/61abc_DEF_.jpg", None),
        # Another CDN entirely: this convention is not ours to assume.
        ("https://shop.example/img/x._AC_SX679_.jpg", None),
    ],
)
def test_the_modifier_rule_is_gated_on_the_host(url: str, expected: str | None) -> None:
    """An earlier version of this regex matched nothing at all, silently. That
    is the exact failure mode this whole pass exists to stop."""
    assert strip_size_modifier(url) == expected


def test_the_whole_gallery_comes_back_not_just_the_hero(settings: Settings) -> None:
    """colorImages is the only rule that gets more than one photo, and a listing
    with one photo looks like a placeholder."""
    found = run_plugin(settings)
    assert "https://m.media-amazon.com/images/I/71second.jpg" in found
    assert "https://m.media-amazon.com/images/I/71third.jpg" in found


def test_amazon_furniture_is_not_a_product_photo(settings: Settings) -> None:
    found = run_plugin(settings)
    assert not any("sprite" in url for url in found)
    assert not any("/G/01/" in url for url in found)


def test_a_caption_with_brackets_does_not_swallow_the_page(settings: Settings) -> None:
    """The colorImages array is found by scanning, and a naive depth counter
    would never terminate on alt text containing `[`."""
    found = run_plugin(settings)
    assert len(found) < 40, "the array scan ran away"


def test_a_page_with_no_gallery_returns_nothing_quietly(settings: Settings) -> None:
    """A plugin that matched and found nothing is a page shape we have not seen.
    The row's own `no_candidates_extracted` already says so."""
    bare = "<html><body><h1>Something</h1></body></html>"
    assert run_plugin(settings, bare) == []


# --------------------------------------------------------------------------
# Phase 6 -- the low-res tier
# --------------------------------------------------------------------------


def result(url: str, reason: str, width: int, height: int) -> ValidationResult:
    return ValidationResult(
        url=url, ok=False, reason=reason, predicate=6, width=width, height=height
    )


def test_only_dimension_failures_are_eligible() -> None:
    """A URL that 404s is not a small photo. It is not a photo."""
    assert _best_low_res([result("a", "http_404", 0, 0)]) is None
    assert _best_low_res([result("a", "wrong_content_type", 0, 0)]) is None
    assert _best_low_res([result("a", "unusably_small", 100, 100)]) is None
    assert _best_low_res([result("a", "below_min_dimensions", 679, 679)]) is not None


def test_the_largest_below_standard_photo_wins() -> None:
    best = _best_low_res(
        [
            result("small", "below_min_dimensions", 500, 500),
            result("bigger", "below_min_dimensions", 679, 679),
            result("dead", "http_404", 0, 0),
        ]
    )
    assert best is not None and best.url == "bigger"


def test_the_hard_floor_is_a_separate_predicate_reason(settings: Settings) -> None:
    """The tier must not turn the standard into a suggestion, so the two sizes
    get two names and only one of them is salvageable."""
    from haat_lister.images.validator import check_dimensions

    cfg = settings.config.validator
    assert check_dimensions(Image.new("RGB", (1000, 1000)), cfg) is None
    assert check_dimensions(Image.new("RGB", (679, 679)), cfg) == "below_min_dimensions"
    assert check_dimensions(Image.new("RGB", (100, 100)), cfg) == "unusably_small"


@respx.mock
async def test_low_res_image_flagged_not_dropped(settings: Settings, tmp_path) -> None:
    """§8 test 8. 679x679 ships, marked, with the size in the flag."""
    from haat_lister.fetch.static import build_client
    from haat_lister.images.pipeline import ImageResolver
    from haat_lister.models import ImageMode
    from haat_lister.pipeline import new_record

    url = "https://shop.example/img/small.jpg"
    buffer = _jpeg(679, 679)
    respx.head(url).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "image/jpeg", "content-length": str(len(buffer))}
        )
    )
    respx.get(url).mock(
        return_value=httpx.Response(200, content=buffer, headers={"content-type": "image/jpeg"})
    )

    record = new_record("https://shop.example/p/1", Provenance.OWN)
    record.image_candidates = [url]

    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    async with build_client(tuned) as client:
        resolver = ImageResolver(tuned, client, ImageMode.URL_COLUMNS, hotlink_test=False)
        outcome = await resolver.resolve(record)

    assert outcome.method is ImageMethod.DIRECT_LOW_RES
    assert outcome.method.is_low_res
    assert outcome.url == url
    assert "679x679" in outcome.reason


@respx.mock
async def test_a_low_res_photo_never_costs_a_host_call(settings: Settings, tmp_path) -> None:
    """The salvage may only ever CLOSE the upload path, never open it. A photo
    we already have a live URL for is not worth paying anyone for."""
    from haat_lister.fetch.static import build_client
    from haat_lister.images.pipeline import ImageResolver
    from haat_lister.models import ImageMode
    from haat_lister.pipeline import new_record

    url = "https://shop.example/img/small.jpg"
    buffer = _jpeg(679, 679)
    respx.head(url).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "image/jpeg", "content-length": str(len(buffer))}
        )
    )
    respx.get(url).mock(
        return_value=httpx.Response(200, content=buffer, headers={"content-type": "image/jpeg"})
    )

    calls = 0

    class CountingHost:
        name = "counting"

        async def upload(self, path: object) -> None:
            nonlocal calls
            calls += 1
            return None

    record = new_record("https://shop.example/p/1", Provenance.OWN)
    record.image_candidates = [url]

    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    async with build_client(tuned) as client:
        resolver = ImageResolver(
            tuned,
            client,
            ImageMode.URL_COLUMNS,
            hosts=[CountingHost()],  # type: ignore[list-item]
            hotlink_test=False,
        )
        await resolver.resolve(record)

    assert calls == 0, "a host was paid for a photo we already had a URL for"


def test_a_low_res_row_says_the_actual_size(settings: Settings) -> None:
    from haat_lister.images.pipeline import _low_res_direct
    from haat_lister.models import ImageResult
    from haat_lister.pipeline import new_record

    record = new_record("https://shop.example/p/1", Provenance.OWN)
    _low_res_direct(
        ImageResult(), record, result("u", "below_min_dimensions", 679, 679), "below_min_dimensions"
    )
    # "small" is not a decision anyone can make. "679x679" is.
    assert any("679x679" in flag for flag in record.notes)


# --------------------------------------------------------------------------
# Phase 6 -- the title
# --------------------------------------------------------------------------


def test_title_seo_tail_stripped_original_retained(settings: Settings) -> None:
    """§8 test 9, on the exact string that was observed."""
    cleaned = tidy_title(
        FieldValue.found(SEO_TITLE, FieldSource.OG, Confidence.HIGH),
        settings.config.extraction,
        200,
    )

    assert cleaned.title.value == "Mivi DuoPods Marathon Earbuds Wireless"
    assert cleaned.original == SEO_TITLE
    assert cleaned.changed
    assert cleaned.attributes == [
        "Fast Charge",
        "70H Playtime",
        "BT v5.3",
        "13mm Drivers",
        "Noise Cancellation",
    ]
    assert SEO_TITLE in (cleaned.title.note or "")


@pytest.mark.parametrize(
    "title",
    [
        "Handwoven Cotton Stole",
        # Two segments is a name, not a name plus a tail.
        "Blue Kurta - Medium",
        "Indigo Stole – Handloom",
    ],
)
def test_an_ordinary_title_is_left_alone(title: str, settings: Settings) -> None:
    """A wrong cut is worse than a long title: the operator cannot see what was
    removed unless they open review.csv."""
    cleaned = tidy_title(
        FieldValue.found(title, FieldSource.JSONLD, Confidence.HIGH),
        settings.config.extraction,
        200,
    )
    assert cleaned.title.value == title
    assert not cleaned.changed


def test_a_brand_first_title_does_not_collapse_to_the_brand() -> None:
    head, tail = split_seo_tail("Mivi | DuoPods Marathon Earbuds | 70H Playtime | BT v5.3")
    assert head == "Mivi DuoPods Marathon Earbuds"
    assert tail == ["70H Playtime", "BT v5.3"]


def test_marketing_is_dropped_only_from_the_end(settings: Settings) -> None:
    def clean(title: str) -> str:
        return (
            tidy_title(
                FieldValue.found(title, FieldSource.OG, Confidence.HIGH),
                settings.config.extraction,
                200,
            ).title.value
            or ""
        )

    assert clean("Indigo Cotton Stole Free Shipping") == "Indigo Cotton Stole"
    # A product genuinely called this keeps its name.
    assert clean("Free Spirit Cotton Stole") == "Free Spirit Cotton Stole"


def test_review_csv_shows_the_title_the_page_stated(settings: Settings) -> None:
    from haat_lister.output.review_writer import REVIEW_COLUMNS, review_row
    from haat_lister.pipeline import new_record

    record = new_record("https://shop.example/p/1", Provenance.OWN)
    record.title = FieldValue.found("Mivi DuoPods Marathon Earbuds Wireless", FieldSource.OG)
    record.title_original = SEO_TITLE

    row = dict(zip(REVIEW_COLUMNS, review_row(record, settings.config), strict=True))
    assert row["title_original"] == SEO_TITLE


def _jpeg(width: int, height: int) -> bytes:
    import io

    buffer = io.BytesIO()
    # Noise rather than flat colour: a solid image compresses below the
    # `min_bytes` floor and would fail predicate 5 instead of 6.
    image = Image.effect_noise((width, height), 64).convert("RGB")
    image.save(buffer, format="JPEG", quality=95)
    return buffer.getvalue()
