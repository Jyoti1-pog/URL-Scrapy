"""v4 Phases 6-7: Find photos, and the cache that makes it a first step.

`diagnose` answers one URL. This answers a catalogue -- and the promise it makes
is that it is a PREVIEW: nothing written, nothing published, nothing paid for.

The most important test in this file is the one that asserts an absence.
`test_find_photos_makes_zero_host_calls` checks that the module never builds the
object that could make one, rather than checking that it happened not to. A
behavioural test passes right up until someone adds a flag; an absent import
cannot be reached at all.
"""

from __future__ import annotations

import csv
from pathlib import Path

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from haat_lister.api.app import create_app
from haat_lister.config import Settings
from haat_lister.find import FindRow, FoundPhoto, find_photos
from haat_lister.output.find_csv import BASE_COLUMNS, read_table, write_image_links

PRODUCT = """<html><head><title>Indigo Kurta</title>
<meta property="og:image" content="https://shop.example/img/hero.jpg">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Indigo Kurta",
 "image":["https://shop.example/img/hero.jpg"],
 "offers":{"@type":"Offer","price":"2499","priceCurrency":"INR"}}
</script></head><body><h1>Indigo Kurta</h1><button>Add to cart</button></body></html>"""


def jpeg(width: int = 1200, height: int = 1200) -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.effect_noise((width, height), 64).convert("RGB").save(buffer, "JPEG", quality=92)
    return buffer.getvalue()


@pytest.fixture
def quick(settings: Settings, tmp_path: Path) -> Settings:
    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    tuned.config.fetch.per_domain_delay_s = 0.0
    tuned.config.fetch.per_domain_delay_jitter_s = 0.0
    tuned.config.fetch.rung_backoff_s = 0.0
    tuned.config.validator.hotlink_test = False
    return tuned


def mock_shop(photo: bytes | None = None):
    """Returns the product-page route so a test can count its calls.

    Handing it back matters: a test that re-registers the same pattern to get a
    counter silently replaces the mocked response with an empty 200, and then
    measures the wrong thing. Cost me a debugging session.
    """
    body = photo if photo is not None else jpeg()
    respx.get(url__regex=r"https://shop\.example/robots\.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
    )
    page = respx.get(url__regex=r"https://shop\.example/p/\d+").mock(
        return_value=httpx.Response(200, html=PRODUCT, headers={"content-type": "text/html"})
    )
    respx.head(url__regex=r"https://shop\.example/img/.*").mock(
        return_value=httpx.Response(
            200, headers={"content-type": "image/jpeg", "content-length": str(len(body))}
        )
    )
    respx.get(url__regex=r"https://shop\.example/img/.*").mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})
    )
    return page


# --------------------------------------------------------------------------
# The promise: a preview costs nothing
# --------------------------------------------------------------------------


def test_find_photos_makes_zero_host_calls() -> None:
    """§10 test 10, asserted as an ABSENCE.

    A behavioural test -- "run it and check the counter is zero" -- passes right
    up until someone adds a flag that reaches an uploader. An object that is
    never constructed cannot be reached by any future edit.
    """
    import pathlib

    from haat_lister import find

    source = pathlib.Path(find.__file__).read_text(encoding="utf-8")
    # Past the module docstring, which names the object precisely to explain
    # why it is absent.
    code = source.split('"""', 2)[-1]
    code = "\n".join(
        line for line in code.splitlines() if not line.strip().startswith("#")
    )
    assert "ImageResolver" not in code, "a find could reach the tier-2 uploader"
    assert "images.hosts" not in code
    assert "build_hosts" not in code
    # The only image code it may touch is Tier 1, which structurally cannot
    # upload -- that is what the Rule 1 gate is.
    assert "validate_all_candidates" in source


@respx.mock
async def test_a_find_writes_no_listings_and_touches_no_sheet(quick: Settings) -> None:
    mock_shop()
    stats = await find_photos(
        ["https://shop.example/p/1", "https://shop.example/p/2"], quick, concurrency=2
    )

    assert stats.done == 2
    assert stats.host_calls == 0
    runs = quick.root / quick.config.paths.runs_dir
    assert not (runs / "master.csv").exists()
    assert not list(runs.glob("*/listings.csv")) if runs.exists() else True


@respx.mock
async def test_it_finds_the_photo_and_its_size(quick: Settings) -> None:
    mock_shop()
    rows: list[FindRow] = []
    await find_photos(["https://shop.example/p/1"], quick, on_row=rows.append)

    row = rows[0]
    assert row.title == "Indigo Kurta"
    assert row.primary_image_url == "https://shop.example/img/hero.jpg"
    assert (row.width, row.height) == (1200, 1200)
    assert row.method == "direct"
    assert row.price == "2499" and row.currency == "INR"


@respx.mock
async def test_a_small_photo_is_reported_as_low_res_not_as_missing(quick: Settings) -> None:
    """The same salvage a real run applies, so the preview does not promise
    less than the job delivers."""
    mock_shop(photo=jpeg(679, 679))
    rows: list[FindRow] = []
    await find_photos(["https://shop.example/p/1"], quick, on_row=rows.append)

    assert rows[0].method == "direct_low_res"
    assert rows[0].low_res
    assert "679x679" in rows[0].reason


@respx.mock
async def test_rows_carry_a_reason_when_there_is_no_photo(quick: Settings) -> None:
    respx.get(url__regex=r"https://bare\.example/robots\.txt").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://bare.example/p/1").mock(
        return_value=httpx.Response(
            200,
            html="<html><head><title>Thing</title></head><body><h1>Thing</h1></body></html>",
            headers={"content-type": "text/html"},
        )
    )
    rows: list[FindRow] = []
    await find_photos(["https://bare.example/p/1"], quick, on_row=rows.append)

    assert rows[0].primary_image_url == ""
    assert rows[0].reason == "no_image_candidates"
    assert rows[0].explanation, "a row with no photo and no explanation is the old bug"


# --------------------------------------------------------------------------
# Phase 7 -- the cache
# --------------------------------------------------------------------------


@respx.mock
async def test_find_cache_reused_by_subsequent_job(quick: Settings) -> None:
    """§10 test 16. The cache is what makes a find a natural first step rather
    than duplicated work."""
    page = mock_shop()

    await find_photos(["https://shop.example/p/1"], quick)
    after_first = page.call_count

    rows: list[FindRow] = []
    await find_photos(["https://shop.example/p/1"], quick, on_row=rows.append)

    assert page.call_count == after_first, "a cached URL was fetched again"
    assert rows[0].from_cache
    assert rows[0].primary_image_url == "https://shop.example/img/hero.jpg"


@respx.mock
async def test_the_cache_can_be_bypassed(quick: Settings) -> None:
    """A shop that has changed its photos should not be invisible."""
    page = mock_shop()

    await find_photos(["https://shop.example/p/1"], quick)
    before = page.call_count
    await find_photos(["https://shop.example/p/1"], quick, use_cache=False)

    assert page.call_count > before


@respx.mock
async def test_failures_are_never_cached(quick: Settings) -> None:
    """A shop that was down for ten minutes must not be written off for a week."""
    respx.get(url__regex=r"https://down\.example/robots\.txt").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://down.example/p/1").mock(return_value=httpx.Response(503))

    rows: list[FindRow] = []
    await find_photos(["https://down.example/p/1"], quick, on_row=rows.append)
    assert rows[0].failed

    from haat_lister.store.ledger import Ledger
    from haat_lister.utils.urls import canonicalise

    with Ledger(quick.root / quick.config.paths.ledger) as ledger:
        assert ledger.find_cached(canonicalise("https://down.example/p/1")) is None


# --------------------------------------------------------------------------
# Input: the same parser, more shapes
# --------------------------------------------------------------------------


def test_find_photos_accepts_comma_and_newline_and_tab_mixed() -> None:
    """§10 test 12. One URL parser, as everywhere else."""
    blob = (
        "https://a.example/1, https://a.example/2\n"
        "https://a.example/3\thttps://a.example/4;https://a.example/5"
    )
    from haat_lister.utils.urls import extract_urls

    assert len(extract_urls(blob).urls) == 5


def test_csv_upload_column_autodetect() -> None:
    """§10 test 11. The URL column found among five, and the operator's own
    columns preserved -- that mapping is usually why they have a CSV at all."""
    table = read_table(
        "sku,product name,link,stock,notes\n"
        "KUR-001,Indigo Kurta,https://shop.example/p/1,12,restock soon\n"
        "KUR-002,Cotton Stole,https://shop.example/p/2,4,\n"
    )

    assert table.url_column == "link"
    assert table.url_column_hits == 2
    assert table.urls == ["https://shop.example/p/1", "https://shop.example/p/2"]
    assert table.extras[0] == {
        "sku": "KUR-001",
        "product name": "Indigo Kurta",
        "stock": "12",
        "notes": "restock soon",
    }


def test_the_chosen_column_can_be_overridden() -> None:
    """A guess shown is a guess that can be argued with."""
    text = "a,b\nhttps://x.example/1,https://y.example/1\n"
    assert read_table(text).url_column in ("a", "b")
    assert read_table(text, url_column="b").urls == ["https://y.example/1"]


def test_a_plain_list_of_links_is_not_mistaken_for_a_header() -> None:
    table = read_table("https://shop.example/p/1\nhttps://shop.example/p/2\n")
    assert not table.had_header
    assert len(table.urls) == 2


def test_a_tab_separated_file_works(tmp_path: Path) -> None:
    table = read_table("name\turl\nKurta\thttps://shop.example/p/9\n")
    assert table.delimiter == "\t"
    assert table.urls == ["https://shop.example/p/9"]


# --------------------------------------------------------------------------
# image_links.csv
# --------------------------------------------------------------------------


def test_image_links_csv_carries_the_operators_own_columns(
    settings: Settings, tmp_path: Path
) -> None:
    path = tmp_path / "image_links.csv"
    rows = [
        FindRow(
            index=0,
            source_url="https://shop.example/p/1",
            title="Indigo Kurta",
            primary_image_url="https://shop.example/img/a.jpg",
            photos=[
                FoundPhoto(url="https://shop.example/img/a.jpg", ok=True, width=1200, height=1200),
                FoundPhoto(url="https://shop.example/img/b.jpg", ok=True),
            ],
            image_count=2,
            width=1200,
            height=1200,
            method="direct",
            reason="direct_ok",
            extra={"sku": "KUR-001", "stock": "12"},
        )
    ]
    write_image_links(path, rows, settings.config, ["sku", "stock"])

    with path.open(encoding="utf-8", newline="") as handle:
        table = list(csv.reader(handle))

    assert table[0][:2] == ["sku", "stock"]
    assert table[0][2:] == list(BASE_COLUMNS)
    row = dict(zip(table[0], table[1], strict=True))
    assert row["sku"] == "KUR-001"
    assert row["primary_image_url"] == "https://shop.example/img/a.jpg"
    assert row["all_image_urls"] == (
        "https://shop.example/img/a.jpg | https://shop.example/img/b.jpg"
    )


def test_image_links_is_not_a_haat_import_file(settings: Settings, tmp_path: Path) -> None:
    """It has no reason to pretend, and pretending would get it uploaded."""
    from haat_lister.output.csv_writer import HAAT_COLUMNS

    path = tmp_path / "image_links.csv"
    write_image_links(path, [FindRow(index=0, source_url="https://a.example/1")], settings.config)
    with path.open(encoding="utf-8", newline="") as handle:
        header = next(csv.reader(handle))
    assert header != list(HAAT_COLUMNS)
    assert "gi_region" not in header


# --------------------------------------------------------------------------
# The route
# --------------------------------------------------------------------------


@pytest.fixture
def client(quick: Settings) -> TestClient:
    return TestClient(create_app(quick))


def test_the_upload_cap_applies_to_a_find_too(client: TestClient) -> None:
    response = client.post(
        "/api/find",
        json={"urls": [f"https://s{i}.example/p" for i in range(10_001)]},
    )
    assert response.status_code == 413
    assert "line limit" in response.text


def test_a_find_needs_no_provenance(client: TestClient) -> None:
    """It writes nothing and publishes nothing, so there is no content whose
    ownership matters yet. The question is asked at Compose, where it decides
    something -- asking it here would teach an operator it is a formality."""
    from haat_lister.api.schemas import FindStartIn

    assert "provenance" not in FindStartIn.model_fields


def test_parse_file_reports_the_column_it_chose(client: TestClient) -> None:
    response = client.post(
        "/api/find/parse-file",
        json={"text": "sku,link\nA-1,https://shop.example/p/1\nA-2,https://shop.example/p/2\n"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["url_column"] == "link"
    assert body["url_column_hits"] == 2
    assert body["found"] == 2
    assert body["columns"] == ["sku", "link"]


@pytest.mark.parametrize(
    "find_id", ["j_zzzzzzzz", "j_UPPER123", "j_short", "..%2f..%2fetc", "etc", "."]
)
def test_an_unknown_or_malformed_find_is_a_404_not_a_500(
    client: TestClient, find_id: str
) -> None:
    """The find id is the only path component a client supplies here, so its
    shape is a control -- same rule as the job download routes."""
    assert client.get(f"/api/find/{find_id}").status_code == 404
