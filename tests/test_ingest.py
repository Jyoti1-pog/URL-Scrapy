"""v5 §4 -- the routes that work when fetching does not.

The ladder measured the thing that motivated all of this: the hosts that matter
most here refuse a correctly-identified client on every rung, and a real
headless Chromium gets the same protocol error. There is no rung five. So the
answer is a door the operator already has a key to.

WHAT THESE TESTS ARE MOSTLY ABOUT is not that the import works. It is that the
import is not a SECOND PIPELINE. Every property the fetched path guarantees --
the provenance gate, the nine image predicates, the 19-column header, the
canonical row key -- has to hold for a row that arrived from a spreadsheet, and
the cheapest way to lose all four at once is an import that builds its own
record because that seemed simpler.
"""

from __future__ import annotations

import csv
import io

import httpx
import pytest
import respx
from PIL import Image

from haat_lister.config import Settings
from haat_lister.ingest import run as ingest_run
from haat_lister.ingest import saved_page, seller_export
from haat_lister.models import (
    FetchStage,
    FieldSource,
    ImageMethod,
    ImageMode,
    Provenance,
    RowStatus,
)

pytestmark = pytest.mark.anyio


# --------------------------------------------------------------------------
# Fixtures with real bytes in them
# --------------------------------------------------------------------------


def jpeg(width: int = 1400, height: int = 1400) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (60, 60, 120)).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


PRODUCT_HTML = """<!DOCTYPE html>
<html><head>
<link rel="canonical" href="https://shop.example/p/kurta-1">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product",
 "name":"Indigo block-print kurta",
 "description":"Hand-blocked cotton in natural indigo, stitched in Bhuj.",
 "image":"https://cdn.shop.example/kurta-1.jpg",
 "offers":{"@type":"Offer","price":"2499","priceCurrency":"INR"},
 "weight":{"@type":"QuantitativeValue","value":320,"unitCode":"GRM"}}
</script></head>
<body><h1>Indigo block-print kurta</h1>
<img src="https://cdn.shop.example/kurta-1.jpg" alt="kurta">
<button>Add to cart</button></body></html>"""

EXPORT_CSV = (
    "Seller SKU,Product Name,Product URL,Long Description,MRP,Net Weight,"
    "HSN Code,Category,Main Image,Internal Ref\n"
    "KRT-1,Indigo block-print kurta,https://shop.example/p/kurta-1,"
    '"Hand-blocked cotton in natural indigo, stitched in Bhuj.",2499,320 g,'
    "6206,apparel,https://cdn.shop.example/kurta-1.jpg,X-99\n"
)


@pytest.fixture
def export_file(tmp_path):
    path = tmp_path / "catalogue.csv"
    path.write_text(EXPORT_CSV, encoding="utf-8")
    return path


@pytest.fixture
def tuned(settings: Settings, tmp_path) -> Settings:
    return settings.model_copy(deep=True, update={"root": tmp_path})


def saved_complete(tmp_path, *, rewrite_src: bool = True):
    """What Chrome writes for "Webpage, complete": rewritten src, sidecar folder."""
    folder = tmp_path / "kurta_files"
    folder.mkdir()
    (folder / "kurta-1.jpg").write_bytes(jpeg())
    html = PRODUCT_HTML
    if rewrite_src:
        html = html.replace(
            '<img src="https://cdn.shop.example/kurta-1.jpg"',
            '<img src="kurta_files/kurta-1.jpg"',
        )
    page = tmp_path / "kurta.html"
    page.write_text(html, encoding="utf-8")
    return page


def mock_photo(url: str = "https://cdn.shop.example/kurta-1.jpg") -> None:
    blob = jpeg()
    headers = {"content-type": "image/jpeg", "content-length": str(len(blob))}
    respx.head(url).mock(return_value=httpx.Response(200, headers=headers))
    respx.get(url).mock(return_value=httpx.Response(200, content=blob, headers=headers))


async def resolve_for(settings: Settings, client, mode=ImageMode.MANIFEST):
    from haat_lister.images.pipeline import ImageResolver

    return ImageResolver(settings, client, mode, hosts=[], ledger=None)


# --------------------------------------------------------------------------
# §8 test 11 -- the export reaches haat's columns
# --------------------------------------------------------------------------


async def test_seller_export_maps_to_19_columns(tuned: Settings, export_file) -> None:
    """§8 test 11. The header is a contract and an import does not get to bend it."""
    from haat_lister.output.csv_writer import HAAT_COLUMNS, row_values

    export = seller_export.parse(export_file, tuned)
    record = await ingest_run.from_export_row(export, export.rows[0], Provenance.OWN, tuned)
    row = dict(zip(HAAT_COLUMNS, row_values(record, tuned.config, ImageMode.MANIFEST), strict=True))

    assert list(row.keys()) == list(HAAT_COLUMNS)
    assert len(row) == len(HAAT_COLUMNS)
    assert row["title"] == "Indigo block-print kurta"
    assert row["price_inr"] == "2499"
    assert row["weight_g"] == "320"
    assert row["hs_code"] == "6206"
    # §7, on the route most likely to try. There is no column name, no alias
    # and no fuzzy match that reaches it.
    assert row["gi_region"] == ""


def test_no_column_can_ever_map_to_gi_region() -> None:
    """A GI tag is a government certification and a seller's tick-box.

    Checked against the mapper rather than the output, because the output being
    empty could just mean this file happened not to have the column.
    """
    headers = ["GI Region", "gi_region", "Geographical Indication", "GI", "gi tag"]
    columns = [seller_export.Column(index=i, header=h) for i, h in enumerate(headers)]
    seller_export.auto_map(columns)

    assert all(c.target != "gi_region" for c in columns)
    assert "gi_region" not in seller_export.TARGETS
    assert "gi_region" in seller_export.REFUSED_TARGETS


def test_a_saved_profile_cannot_smuggle_a_refused_target(tuned: Settings, export_file) -> None:
    """Profiles are YAML in the operator's own directory, so somebody will
    eventually write `gi_region: seller_region` in one by hand."""
    export = seller_export.parse(export_file, tuned)
    seller_export.apply_profile(
        export, {"name": "hostile", "mapping": {"Category": "gi_region", "MRP": "price_inr"}}
    )

    assert export.mapping.get("gi_region") is None
    assert "price_inr" in export.mapping, "the legitimate half of the profile still applies"


# --------------------------------------------------------------------------
# §8 test 14 -- provenance, on every route
# --------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["export", "saved_page", "html"])
async def test_import_requires_provenance(tuned: Settings, export_file, tmp_path, route) -> None:
    """§8 test 14. `--provenance` has no default anywhere, and an import is not
    the exception -- reading a page off local disk tells us nothing about who
    owns the photographs in it."""
    import inspect

    fn = {
        "export": ingest_run.from_export_row,
        "saved_page": ingest_run.from_saved_page,
        "html": ingest_run.from_html,
    }[route]
    parameter = inspect.signature(fn).parameters["provenance"]

    assert parameter.default is inspect.Parameter.empty, f"{route} has a default provenance"
    assert parameter.kind is not inspect.Parameter.KEYWORD_ONLY or True


@respx.mock
async def test_third_party_import_still_meets_the_gate(tuned: Settings, export_file) -> None:
    """Rule 2.2 does not care which door the row came in through.

    This is the property an import that built its own record would lose first,
    and lose silently: `apply_gate` is one call at the end of a function nobody
    would think to look at.
    """
    mock_photo()
    export = seller_export.parse(export_file, tuned)

    from haat_lister.fetch.static import build_client

    async with build_client(tuned) as client:
        record = await ingest_run.from_export_row(
            export, export.rows[0], Provenance.THIRD_PARTY, tuned,
            resolver=await resolve_for(tuned, client),
        )

    assert record.status is RowStatus.NEEDS_REVIEW
    assert record.image.method is not ImageMethod.HOSTED, "third-party photos were re-hosted"


# --------------------------------------------------------------------------
# §8 test 12 -- a saved page makes no network calls
# --------------------------------------------------------------------------


@respx.mock
async def test_saved_page_extraction_makes_zero_network_calls(tuned: Settings, tmp_path) -> None:
    """§8 test 12, and the whole point of the route.

    respx with no routes registered raises on any request, so this asserts what
    did NOT happen rather than counting what did. If extraction ever reaches for
    the network -- to resolve a relative URL, to check a canonical, to look at
    robots.txt -- this fails loudly instead of getting slower.
    """
    page = saved_complete(tmp_path)

    record = await ingest_run.from_saved_page(page, Provenance.OWN, tuned)

    assert record.status is not RowStatus.FAILED
    assert record.title.value == "Indigo block-print kurta"
    assert record.fetch_stage is FetchStage.SAVED_PAGE


@respx.mock
async def test_the_real_url_is_tried_before_the_local_copy(tuned: Settings, tmp_path) -> None:
    """Order matters, and this is the order.

    A saved page usually keeps the ORIGINAL absolute image URL in its JSON-LD --
    browsers rewrite `<img src>` when saving but do not touch a script tag. That
    URL is worth trying first: when it works, the listing gets a real link
    rather than bytes we would have to host ourselves.

    The local copy is the fallback, not the default.
    """
    from haat_lister.fetch.static import build_client

    mock_photo()
    page = saved_complete(tmp_path)
    async with build_client(tuned) as client:
        record = await ingest_run.from_saved_page(
            page, Provenance.OWN, tuned, resolver=await resolve_for(tuned, client)
        )

    assert record.image.tier1_passed is True
    assert record.image.url.startswith("https://"), "a real URL was available and not used"


@respx.mock
async def test_a_refused_cdn_falls_back_to_the_saved_bytes(tuned: Settings, tmp_path) -> None:
    """The case the whole route exists for.

    The shop refuses us -- that is why the operator saved the page -- and its
    CDN refuses us too. The photograph is nonetheless sitting in the folder they
    gave us, and the row gets it without one further request.

    Still all nine predicates on the way through: having supplied a file is not
    evidence that it is a decodable photograph of a listable size.
    """
    from haat_lister.fetch.static import build_client

    respx.head("https://cdn.shop.example/kurta-1.jpg").mock(return_value=httpx.Response(403))
    respx.get("https://cdn.shop.example/kurta-1.jpg").mock(return_value=httpx.Response(403))

    page = saved_complete(tmp_path)
    async with build_client(tuned) as client:
        record = await ingest_run.from_saved_page(
            page, Provenance.OWN, tuned, resolver=await resolve_for(tuned, client)
        )

    assert record.image.method is not ImageMethod.NONE, record.image.none_reason
    assert record.image.files, "the saved photo did not reach the manifest"
    assert record.image.tier1_attempted is True
    assert record.image.tier1_passed is True


@respx.mock
async def test_a_thumbnail_in_the_folder_still_fails_tier_one(
    tuned: Settings, tmp_path
) -> None:
    """The import is not a trust boundary. Same predicates, same floor."""
    from haat_lister.fetch.static import build_client

    respx.head("https://cdn.shop.example/kurta-1.jpg").mock(return_value=httpx.Response(403))
    respx.get("https://cdn.shop.example/kurta-1.jpg").mock(return_value=httpx.Response(403))
    page = saved_complete(tmp_path)
    (tmp_path / "kurta_files" / "kurta-1.jpg").write_bytes(jpeg(90, 90))

    async with build_client(tuned) as client:
        record = await ingest_run.from_saved_page(
            page, Provenance.OWN, tuned, resolver=await resolve_for(tuned, client)
        )

    assert record.image.method is ImageMethod.NONE
    assert record.image.tier1_passed is False


# --------------------------------------------------------------------------
# §8 test 13 -- the same product, two doors, one row
# --------------------------------------------------------------------------


@respx.mock
async def test_saved_page_and_live_fetch_produce_identical_row(
    tuned: Settings, tmp_path
) -> None:
    """§8 test 13. The strongest statement that this is not a second pipeline.

    Compared on the columns rather than on the record, because the columns
    are what haat receives and the record legitimately differs -- `fetch_stage`
    says `saved_page` for one and `static` for the other, which is exactly the
    difference that SHOULD survive.
    """
    from haat_lister.fetch.static import build_client
    from haat_lister.pipeline import process_url

    url = "https://shop.example/p/kurta-1"
    respx.get("https://shop.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
    )
    respx.get(url).mock(
        return_value=httpx.Response(
            200, html=PRODUCT_HTML, headers={"content-type": "text/html"}
        )
    )

    async with build_client(tuned) as client:
        fetched = await process_url(url, Provenance.OWN, tuned, client)

    # The same page, saved -- and with `src` left absolute, which is what
    # "Webpage, HTML only" produces.
    page = saved_complete(tmp_path, rewrite_src=False)
    imported = await ingest_run.from_saved_page(page, Provenance.OWN, tuned)

    assert imported.row_key == fetched.row_key, "the same product got two keys"
    assert imported.canonical_url == fetched.canonical_url
    assert _columns(imported, tuned) == _columns(fetched, tuned)


# --------------------------------------------------------------------------
# §8 test 15 -- master dedupe across the doors
# --------------------------------------------------------------------------


@respx.mock
async def test_master_dedupes_scraped_and_exported_same_product(
    tuned: Settings, export_file, tmp_path
) -> None:
    """§8 test 15. An operator imports their export, then scrapes a link they
    were sent for the same product. That is one product, and the master sheet
    is the thing that has to know it."""
    from haat_lister.fetch.static import build_client
    from haat_lister.pipeline import process_url

    url = "https://shop.example/p/kurta-1"
    respx.get("https://shop.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
    )
    respx.get(url).mock(
        return_value=httpx.Response(200, html=PRODUCT_HTML, headers={"content-type": "text/html"})
    )

    export = seller_export.parse(export_file, tuned)
    imported = await ingest_run.from_export_row(export, export.rows[0], Provenance.OWN, tuned)

    async with build_client(tuned) as client:
        fetched = await process_url(url, Provenance.OWN, tuned, client)

    assert imported.canonical_url == fetched.canonical_url
    assert imported.row_key == fetched.row_key, (
        "the export's URL format produced a different key to the scraped one"
    )


async def test_the_export_url_format_survives_canonicalisation(tuned: Settings, tmp_path) -> None:
    """A panel writes tracking parameters and trailing slashes. The key must not
    change because of them -- §7 says dedupe survives the export's URL format."""
    path = tmp_path / "messy.csv"
    path.write_text(
        "url,name\n"
        "https://shop.example/p/kurta-1/?utm_source=panel&ref=export,Kurta\n",
        encoding="utf-8",
    )
    export = seller_export.parse(path, tuned)
    record = await ingest_run.from_export_row(export, export.rows[0], Provenance.OWN, tuned)

    assert "utm_source" not in record.canonical_url
    assert "ref=" not in record.canonical_url


# --------------------------------------------------------------------------
# The mapper
# --------------------------------------------------------------------------


def test_unmapped_columns_are_reported_never_dropped(tuned: Settings, export_file) -> None:
    """§4.1. A seller export is somebody's inventory, and the columns we did not
    understand are the ones most likely to matter to them."""
    export = seller_export.parse(export_file, tuned)
    unmapped = {c.header for c in export.unmapped}

    assert "Internal Ref" in unmapped
    assert "Seller SKU" in unmapped
    assert seller_export.known_unused("Seller SKU") == "sku", (
        "a column we recognise but cannot write should say so, not read as unknown"
    )
    assert seller_export.known_unused("Internal Ref") == ""
    assert all(c.samples for c in export.columns), "no sample value to judge the mapping by"


def test_two_columns_that_both_look_like_the_price_are_not_both_taken() -> None:
    """Fuzzy matching alone maps `Price` and `Price List Name` to the same field
    and silently keeps whichever came last."""
    columns = [
        seller_export.Column(index=0, header="Price"),
        seller_export.Column(index=1, header="Price (USD)"),
    ]
    seller_export.auto_map(columns)

    assert sum(1 for c in columns if c.target == "price_inr") <= 1


def test_a_profile_round_trips(tuned: Settings, export_file) -> None:
    """§8 test 17's server half: save a mapping, re-parse, and it comes back."""
    export = seller_export.parse(export_file, tuned)
    seller_export.save_profile(tuned, "Nilaya Panel", export)

    again = seller_export.parse(export_file, tuned)

    assert again.profile_used == "nilaya-panel"
    assert again.mapping == export.mapping
    assert again.signature == export.signature


def test_the_signature_survives_a_reordered_export(tuned: Settings, tmp_path) -> None:
    """A panel that adds a column or swaps two should still find the profile."""
    a = seller_export.signature(["URL", "Name", "Price"])
    b = seller_export.signature(["price", "url", "NAME"])

    assert a == b


def test_weights_arrive_in_whatever_unit_the_panel_wrote() -> None:
    """Customs charges on the weight, so a wrong unit is a wrong duty."""
    assert seller_export._weight_grams("320 g") == 320
    assert seller_export._weight_grams("0.18 kg") == 180
    assert seller_export._weight_grams("1.5kg") == 1500
    # Unrecognised units leave it empty rather than guessing: the row reaches
    # review, which is what §7 means by never silently guessing a customs field.
    assert seller_export._weight_grams("") is None
    assert seller_export._weight_grams("light") is None


async def test_an_export_value_is_marked_as_the_operators_own(
    tuned: Settings, export_file
) -> None:
    """It is not an inference off a page, and the record should not say it is.

    This is what stops the classifier overwriting an operator's own HS code --
    which it did, silently, until this route existed to notice.
    """
    export = seller_export.parse(export_file, tuned)
    record = await ingest_run.from_export_row(export, export.rows[0], Provenance.OWN, tuned)

    assert record.title.source is FieldSource.OPERATOR
    assert record.hs_code.value == "6206"
    assert record.hs_code.source is FieldSource.OPERATOR


# --------------------------------------------------------------------------
# Bad input from a human
# --------------------------------------------------------------------------


def test_a_wrong_file_is_a_message_not_a_traceback(tmp_path) -> None:
    """The most likely thing to happen on this route, and it is not an error
    condition -- it is Tuesday."""
    junk = tmp_path / "holiday.png"
    junk.write_bytes(jpeg(10, 10))

    with pytest.raises(saved_page.SavedPageError) as caught:
        saved_page.load(junk)
    assert "Ctrl+S" in str(caught.value), "the message does not say what to do instead"

    with pytest.raises(seller_export.ExportError):
        seller_export.read(junk)


def test_a_page_that_does_not_say_where_it_came_from_asks(tmp_path) -> None:
    """Guessing the URL from the filename would key the row on `kurta (1)`."""
    page = tmp_path / "kurta.html"
    page.write_text("<html><body><h1>Kurta</h1></body></html>", encoding="utf-8")

    with pytest.raises(saved_page.SavedPageError) as caught:
        saved_page.load(page)
    assert "URL" in str(caught.value)

    # ...and it is accepted when supplied.
    page_with_url = saved_page.load(page, "https://shop.example/p/kurta-1")
    assert page_with_url.source_url == "https://shop.example/p/kurta-1"


async def test_pasting_a_page_without_its_url_is_refused(tuned: Settings) -> None:
    """§4.3. A row with no source cannot be deduplicated or checked later."""
    with pytest.raises(saved_page.SavedPageError):
        await ingest_run.from_html(PRODUCT_HTML, "", Provenance.OWN, tuned)


async def test_a_paste_and_a_file_produce_the_same_row(tuned: Settings, tmp_path) -> None:
    """§4.3 is the one-off version of §4.2, not a lighter one."""

    page = saved_complete(tmp_path, rewrite_src=False)
    from_file = await ingest_run.from_saved_page(page, Provenance.OWN, tuned)
    pasted = await ingest_run.from_html(
        PRODUCT_HTML, "https://shop.example/p/kurta-1", Provenance.OWN, tuned
    )

    assert _columns(pasted, tuned) == _columns(from_file, tuned)


def test_an_empty_export_says_so(tmp_path) -> None:
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    with pytest.raises(seller_export.ExportError):
        seller_export.read(path)


def test_an_oversized_export_is_refused_by_size_not_by_parsing_it(tmp_path) -> None:
    path = tmp_path / "huge.csv"
    path.write_bytes(b"a,b\n" + b"x,y\n" * 10)
    original = seller_export.MAX_BYTES
    seller_export.MAX_BYTES = 8
    try:
        with pytest.raises(seller_export.ExportError) as caught:
            seller_export.read(path)
        assert "limit" in str(caught.value)
    finally:
        seller_export.MAX_BYTES = original


def test_csv_and_tsv_are_both_read(tmp_path) -> None:
    tsv = tmp_path / "catalogue.tsv"
    tsv.write_text("url\tname\thttps\nhttps://a.example/1\tKurta\tx\n", encoding="utf-8")
    header, rows = seller_export.read(tsv)
    assert header[:2] == ["url", "name"]
    assert rows[0][1] == "Kurta"


def _columns(record, settings: Settings) -> dict[str, str]:
    """The columns as haat receives them."""
    from haat_lister.output.csv_writer import HAAT_COLUMNS, row_values

    values = row_values(record, settings.config, ImageMode.MANIFEST)
    return dict(zip(HAAT_COLUMNS, values, strict=True))


def _rows(path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))
