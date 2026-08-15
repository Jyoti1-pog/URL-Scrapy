"""Phase 1: links arrive in whatever shape the operator had them in.

The table below is §8's, one case per row. The two rows that matter most are
the ones that pull in opposite directions:

    https://a.com/x, https://b.com/y     -> 2
    https://a.com/p?ids=1,2,3            -> 1

Any implementation that splits on commas passes the first and silently breaks
the second, which is why this is written as extraction rather than splitting.
"""

from __future__ import annotations

import time

import pytest

from haat_lister.config import Settings
from haat_lister.jobs import DUPLICATE, INVALID, plan_urls
from haat_lister.utils.canonical import DEFAULT_RULES, CanonicalRule, Identity, rule_for
from haat_lister.utils.urls import canonicalise, extract_urls

# The §2.2 example, as pasted out of a browser.
AMAZON_TRACKED = (
    "https://www.amazon.in/Mivi-Marathon-Playtime-Wireless-Bluetooth/dp/B0FTFMNYBV/"
    "?_encoding=UTF8&pd_rd_w=Xk9Lm&content-id=amzn1.sym.abc&pf_rd_p=1e2f3a4b"
    "&pf_rd_r=ZZQ1J8&pd_rd_wg=qP2Vd&pd_rd_r=8c1b&ref_=pd_hp_d_atf_dealz&th=1"
)


def urls_of(blob: str) -> list[str]:
    return [found.url for found in extract_urls(blob).urls]


# --------------------------------------------------------------------------
# §8's table
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("blob", "expected"),
    [
        ("a.com/x\nb.com/y", ["https://a.com/x", "https://b.com/y"]),
        ("https://a.com/x, https://b.com/y", ["https://a.com/x", "https://b.com/y"]),
        ("https://a.com/x,https://b.com/y", ["https://a.com/x", "https://b.com/y"]),
        ("https://a.com/x; https://b.com/y", ["https://a.com/x", "https://b.com/y"]),
        ("https://a.com/x\thttps://b.com/y", ["https://a.com/x", "https://b.com/y"]),
        ("https://a.com/x | https://b.com/y", ["https://a.com/x", "https://b.com/y"]),
        ("https://a.com/x|https://b.com/y", ["https://a.com/x", "https://b.com/y"]),
        # The one that punishes splitting.
        ("https://a.com/p?ids=1,2,3", ["https://a.com/p?ids=1,2,3"]),
        (
            "https://a.com/x, https://b.com/p?ids=4,5",
            ["https://a.com/x", "https://b.com/p?ids=4,5"],
        ),
        ("<https://a.com/x>", ["https://a.com/x"]),
        ('"https://a.com/x"', ["https://a.com/x"]),
        ("'https://a.com/x'", ["https://a.com/x"]),
        ("[Product](https://a.com/x)", ["https://a.com/x"]),
        ('<a href="https://a.com/x">buy</a>', ["https://a.com/x"]),
        ("Check this out: https://a.com/x.", ["https://a.com/x"]),
        ("(see https://a.com/x)", ["https://a.com/x"]),
        # A parenthesis the URL actually owns survives.
        ("https://a.com/dp/B0FT?a=(1)", ["https://a.com/dp/B0FT?a=(1)"]),
        ("amazon.in/dp/B0FT", ["https://amazon.in/dp/B0FT"]),
        ("hello world", []),
    ],
)
def test_the_table(blob: str, expected: list[str]) -> None:
    assert urls_of(blob) == expected


def test_prose_with_no_link_comes_back_verbatim() -> None:
    """Rule 6. A typo an operator cannot see is a row they never know is missing."""
    result = extract_urls("hello world\nhtps://shop.example/kurta")
    assert result.urls == []
    assert [u.text for u in result.unparsed] == ["hello world", "htps://shop.example/kurta"]
    assert [u.line for u in result.unparsed] == [1, 2]


def test_a_mistyped_scheme_is_not_quietly_rescued_as_a_bare_domain() -> None:
    """`htps://shop.example/x` must not become `https://shop.example/x`.

    Guessing here would be worse than reporting: the operator meant a specific
    URL, and a silently repaired one that 404s is harder to diagnose than a
    fragment they can see and fix.
    """
    result = extract_urls("htps://shop.example/kurta")
    assert result.urls == []
    assert result.unparsed[0].text == "htps://shop.example/kurta"


def test_prose_beside_a_good_link_is_not_reported_as_a_typo() -> None:
    """Otherwise pasting a chat thread buries the one real mistake."""
    result = extract_urls("Hey can you list this one https://shop.example/p/1 thanks")
    assert len(result.urls) == 1
    assert result.unparsed == []


def test_a_bare_domain_is_marked_as_assumed() -> None:
    found = extract_urls("amazon.in/dp/B0FT").urls[0]
    assert found.assumed_scheme
    assert found.url == "https://amazon.in/dp/B0FT"

    typed = extract_urls("https://amazon.in/dp/B0FT").urls[0]
    assert not typed.assumed_scheme


def test_a_filename_is_not_a_bare_domain() -> None:
    """`notes.txt` on its own line is a note, not a shop."""
    result = extract_urls("notes.txt\nexport.csv\nshop.example")
    assert urls_of("notes.txt\nexport.csv\nshop.example") == ["https://shop.example"]
    assert {u.text for u in result.unparsed} == {"notes.txt", "export.csv"}


def test_a_filename_in_a_path_is_untouched() -> None:
    assert urls_of("shop.example/product.html") == ["https://shop.example/product.html"]


def test_spreadsheet_invisibles_survive_a_paste() -> None:
    """Zero-width characters and non-breaking spaces are what a cell paste is
    actually made of, and one in the middle of a host breaks the fetch."""
    blob = "﻿https://a.com/x​, https://b.com/y‍"
    assert urls_of(blob) == ["https://a.com/x", "https://b.com/y"]


def test_repeats_are_reported_not_swallowed() -> None:
    """Collapsing belongs to `plan_urls`, which also counts them.

    Caught by the accounting test: an exact repeat deduped at this level
    vanished before anything could report it, and the job's own "every URL is
    accounted for" assertion is what noticed.
    """
    assert len(extract_urls("https://a.com/x, https://a.com/x").urls) == 2


def test_one_paste_can_mix_every_delimiter() -> None:
    blob = (
        "https://a.com/1, https://a.com/2;https://a.com/3\t"
        "https://a.com/4 | https://a.com/5\nhttps://a.com/6"
    )
    assert len(urls_of(blob)) == 6


def test_five_thousand_lines_parse_promptly() -> None:
    """A console that freezes on paste is a console people stop pasting into."""
    blob = "\n".join(
        f"https://shop{i}.example/p/{i}?utm_source=x, https://other{i}.example/p/{i}"
        for i in range(2500)
    )
    started = time.perf_counter()
    result = extract_urls(blob)
    elapsed = time.perf_counter() - started

    assert len(result.urls) == 5000
    assert elapsed < 0.2, f"5,000 links took {elapsed * 1000:.0f}ms"


# --------------------------------------------------------------------------
# Per-domain canonical forms
# --------------------------------------------------------------------------


def test_amazon_canonicalises_to_asin() -> None:
    assert canonicalise(AMAZON_TRACKED) == "https://amazon.in/dp/B0FTFMNYBV"


@pytest.mark.parametrize(
    "url",
    [
        "https://www.amazon.in/Some-Slug/dp/B0FTFMNYBV/?ref_=x",
        "https://www.amazon.in/gp/product/B0FTFMNYBV?th=1",
        "https://www.amazon.in/gp/aw/d/B0FTFMNYBV",
        "https://www.amazon.in/Other-Slug-Entirely/dp/b0ftfmnybv",
        # No www -- what a hand-typed or bare-domain link looks like.
        "https://amazon.in/dp/B0FTFMNYBV",
    ],
)
def test_every_shape_of_amazon_link_reaches_the_same_identity(url: str) -> None:
    assert canonicalise(url) == "https://amazon.in/dp/B0FTFMNYBV"


def test_two_tracking_urls_same_asin_dedupe_to_one_row() -> None:
    plan = plan_urls([AMAZON_TRACKED, "https://www.amazon.in/dp/B0FTFMNYBV/?ref_=elsewhere"])
    assert len(plan.accepted) == 1
    assert plan.duplicates == 1


def test_www_is_not_part_of_a_products_identity() -> None:
    """Found by running the planner on a real mixed paste.

    Now that a bare domain is accepted, `amazon.in/dp/X` sits next to
    `https://www.amazon.in/dp/X` in one paste and means one product. Only the
    identity is normalised; the URL fetched is still the one pasted.
    """
    plan = plan_urls(["amazon.in/dp/B0FTFMNYBV", "https://www.amazon.in/dp/B0FTFMNYBV"])
    assert len(plan.accepted) == 1
    assert plan.duplicates == 1
    # A single-label host is left alone -- `www.example` has no site behind it.
    assert canonicalise("https://www.example/p/1") == "https://www.example/p/1"


def test_an_amazon_page_that_is_not_a_product_is_left_alone() -> None:
    """A category URL has no ASIN. Rewriting it into one would be inventing a
    product page, which is worse than a long URL."""
    category = "https://www.amazon.in/s?k=wireless+earbuds"
    # The search terms are the point: `drop_query: ["*"]` firing here would
    # turn a category link into the shop's front page.
    assert canonicalise(category) == "https://amazon.in/s?k=wireless+earbuds"


def test_flipkart_keeps_the_product_and_drops_the_seller() -> None:
    tracked = (
        "https://www.flipkart.com/some-kurta/p/itm123"
        "?pid=ABC123&lid=LSTXYZ&marketplace=FLIPKART&srno=s_1_2&otracker=search"
        "&fm=organic&iid=abc&ppt=None&ssid=zzz"
    )
    assert canonicalise(tracked) == "https://flipkart.com/some-kurta/p/itm123?pid=ABC123"


def test_etsy_reduces_to_the_listing_id() -> None:
    assert (
        canonicalise("https://www.etsy.com/listing/123456789/hand-block-printed-kurta?ref=shop")
        == "https://etsy.com/listing/123456789"
    )


def test_a_host_with_no_rule_is_untouched() -> None:
    url = "https://handloom.example/collections/kurtas/products/indigo?size=m"
    assert canonicalise(url) == url
    assert rule_for("handloom.example", DEFAULT_RULES) is None


def test_shopify_variants_are_separate_listings_unless_asked_otherwise() -> None:
    """A different size at a different price is a different haat row. Collapsing
    them is a claim about the catalogue only the seller can make."""
    one = "https://shop.example/products/kurta?variant=111"
    two = "https://shop.example/products/kurta?variant=222"

    assert canonicalise(one) != canonicalise(two)

    merging = Identity(merge_variants=True)
    assert canonicalise(one, identity=merging) == canonicalise(two, identity=merging)
    assert canonicalise(one, identity=merging) == "https://shop.example/products/kurta"


def test_a_rule_is_data_not_a_branch() -> None:
    """Adding a marketplace must be an edit to a table."""
    myntra = CanonicalRule(
        name="myntra",
        host_pattern=r"(?:.+\.)?myntra\.com",
        path_pattern=r"/(\d+)/buy",
        path_template="/{0}/buy",
        drop_query=("*",),
    )
    identity = Identity(rules=(*DEFAULT_RULES, myntra))
    assert (
        canonicalise(
            "https://www.myntra.com/kurtas/brand/thing/987654/buy?rawQuery=kurta", identity=identity
        )
        == "https://myntra.com/987654/buy"
    )
    # And the built-ins still work alongside it.
    assert canonicalise(AMAZON_TRACKED, identity=identity) == "https://amazon.in/dp/B0FTFMNYBV"


def test_config_can_override_a_built_in_rule(settings: Settings) -> None:
    from haat_lister.config import CanonicalConfig, CanonicalRuleConfig

    settings.config.canonical = CanonicalConfig(
        rules=[
            CanonicalRuleConfig(
                name="amazon",
                host_pattern=r"(?:.+\.)?amazon\.[a-z.]+",
                keep_query=["th"],
            )
        ]
    )
    names = [r.name for r in settings.identity.rules]
    assert names.count("amazon") == 1, "an override must replace, not stack"
    # The override keeps `th` and does not rewrite the path.
    assert canonicalise(AMAZON_TRACKED, identity=settings.identity).endswith("th=1")


# --------------------------------------------------------------------------
# The planner and the scanner are the same thing
# --------------------------------------------------------------------------


def test_a_comma_separated_paste_of_twelve_plans_twelve_rows() -> None:
    """§9's line, asserted where it is decided rather than in the browser."""
    blob = ", ".join(f"https://shop.example/p/{i}" for i in range(12))
    plan = plan_urls([blob])

    assert len(plan.accepted) == 12
    assert [u.raw for u in plan.accepted] == [f"https://shop.example/p/{i}" for i in range(12)]
    assert [u.index for u in plan.accepted] == list(range(12))


def test_the_plan_and_the_preview_cannot_disagree() -> None:
    """One implementation, three callers. If `plan_urls` grew its own reader,
    the console would show a count the run does not honour."""
    blob = "https://a.com/1, https://a.com/2\nnonsense here\namazon.in/dp/B0FTFMNYBV"
    extraction = extract_urls(blob)
    plan = plan_urls([blob])

    assert len(plan.accepted) == len(extraction.urls)
    assert len(plan.invalid) == len(extraction.unparsed)


def test_a_comment_line_is_a_note_to_a_human_not_a_link() -> None:
    plan = plan_urls(["# https://old.example/p/1 -- retired", "https://shop.example/p/2"])
    assert [u.raw for u in plan.accepted] == ["https://shop.example/p/2"]
    assert plan.invalid == []


def test_an_invalid_fragment_carries_the_line_it_came_from() -> None:
    plan = plan_urls(["https://a.com/1", "", "utter nonsense"])
    bad = plan.invalid[0]
    assert bad.raw == "utter nonsense"
    assert bad.line == 3
    assert bad.status == INVALID


def test_a_bare_domain_survives_into_the_plan_marked() -> None:
    plan = plan_urls(["amazon.in/dp/B0FTFMNYBV"])
    assert plan.accepted[0].assumed_scheme
    assert plan.accepted[0].canonical == "https://amazon.in/dp/B0FTFMNYBV"


def test_links_keep_their_pasted_order_across_a_mixed_blob() -> None:
    plan = plan_urls(["amazon.in/dp/AAAAAAAAAA and then https://shop.example/p/2"])
    assert [u.canonical for u in plan.accepted] == [
        "https://amazon.in/dp/AAAAAAAAAA",
        "https://shop.example/p/2",
    ]


def test_the_row_the_operator_sees_is_the_url_they_pasted(settings: Settings) -> None:
    """§2.2: the review file shows the original so they recognise their own link.

    `raw` is what was pasted and `canonical` is the identity. Both are recorded,
    and the one shown to a human is the one they will recognise.
    """
    plan = plan_urls([AMAZON_TRACKED])
    entry = plan.accepted[0]
    assert entry.raw == AMAZON_TRACKED
    assert entry.canonical == "https://amazon.in/dp/B0FTFMNYBV"
    assert entry.status != DUPLICATE
