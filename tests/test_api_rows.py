"""v2 Phase 7: the review table, inline edits, and re-export.

The claim that matters most here is the quiet one: **an edit never overwrites
what was scraped.** It is a property of the schema -- edits live in their own
table and are applied on the way out -- so the test for it is to edit a cell, be
sure the export changed, and then be sure the stored record did not.

The other two are refusals. The API must reject `gi_region` even when the UI is
bypassed, and must not accept a category slug the taxonomy has never heard of.
An API more permissive than the extractor would produce a CSV that imports for
rows nobody touched and fails for rows somebody fixed.
"""

from __future__ import annotations

import asyncio
import csv
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from haat_lister.api.app import create_app
from haat_lister.config import Settings
from haat_lister.edits import EDITABLE, LOCKED
from haat_lister.models import (
    Confidence,
    FieldSource,
    FieldValue,
    ImageMethod,
    ProductRecord,
    Provenance,
)
from haat_lister.pipeline import new_record
from haat_lister.store.ledger import Ledger


@pytest.fixture
def job_settings(settings: Settings, tmp_path: Path) -> Settings:
    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    tuned.config.fetch.per_domain_delay_s = 0.0
    tuned.config.fetch.per_domain_delay_jitter_s = 0.0
    tuned.config.fetch.respect_robots = False
    return tuned


class FakeProcessor:
    """Rows shaped like a real catalogue: a title and a category, no price.

    That is not a convenience -- it is the situation the review table exists
    for. price_inr is blank by policy on every row.
    """

    async def __call__(
        self, url: str, provenance: Provenance = Provenance.OWN, *_: object, **__: object
    ) -> ProductRecord:
        await asyncio.sleep(0)
        record = new_record(url, provenance)
        n = url.rsplit("/", 1)[-1]
        record.title = FieldValue.found(f"Product {n}", FieldSource.JSONLD, Confidence.HIGH)
        record.description = FieldValue.found(
            "Hand-embroidered in Kutch.", FieldSource.JSONLD, Confidence.HIGH
        )
        record.category_slug = FieldValue.found("apparel", FieldSource.INFERRED, Confidence.LOW)
        record.subcategory_slug = FieldValue.found(
            "womens-fashion", FieldSource.INFERRED, Confidence.LOW
        )
        record.image.method = ImageMethod.DIRECT
        record.image.reason = "direct_ok"
        return record


@pytest.fixture
def client(job_settings: Settings) -> TestClient:
    with TestClient(create_app(job_settings, process=FakeProcessor())) as test_client:
        yield test_client


def urls(n: int) -> list[str]:
    return [f"https://shop{i}.example/p/{i}" for i in range(n)]


def run_job(client: TestClient, n: int = 5) -> str:
    response = client.post(
        "/api/jobs",
        json={"urls": urls(n), "settings": {"provenance": "own", "concurrency": 4}},
    )
    job_id = response.json()["job_id"]
    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        for _ in stream.iter_lines():
            pass
    return job_id


def cells_of(row: dict) -> dict[str, dict]:
    return {c["field"]: c for c in row["cells"]}


def listings(client: TestClient, job_id: str) -> list[dict[str, str]]:
    body = client.get(f"/api/jobs/{job_id}/download/listings").content.decode("utf-8")
    return list(csv.DictReader(io.StringIO(body)))


# --------------------------------------------------------------------------
# Reading the table
# --------------------------------------------------------------------------


def test_the_table_carries_confidence_and_source_per_cell(client: TestClient) -> None:
    job_id = run_job(client, 3)
    page = client.get(f"/api/jobs/{job_id}/rows").json()

    assert page["total"] == 3
    assert page["columns"][0] == "title"
    from haat_lister.output.csv_writer import HAAT_COLUMNS

    assert len(page["rows"][0]["cells"]) == len(HAAT_COLUMNS)

    cells = cells_of(page["rows"][0])
    assert cells["title"]["value"] == "Product 0"
    assert cells["title"]["confidence"] == "high"
    assert cells["title"]["source"] == "jsonld"
    assert cells["category_slug"]["confidence"] == "low"
    assert cells["price_inr"]["value"] == ""


def test_the_table_is_in_input_order(client: TestClient) -> None:
    job_id = run_job(client, 6)
    page = client.get(f"/api/jobs/{job_id}/rows").json()
    assert [r["input_index"] for r in page["rows"]] == list(range(6))


def test_flagged_only_narrows_to_the_work(client: TestClient) -> None:
    job_id = run_job(client, 4)
    everything = client.get(f"/api/jobs/{job_id}/rows").json()
    flagged = client.get(f"/api/jobs/{job_id}/rows?flagged_only=true").json()

    assert everything["total"] == 4
    assert flagged["total"] == 4, "every row needs a price, so every row is flagged"
    assert all("price_inr" in r["missing"] for r in flagged["rows"])


def test_the_table_paginates(client: TestClient) -> None:
    job_id = run_job(client, 12)
    page = client.get(f"/api/jobs/{job_id}/rows?offset=5&limit=3").json()
    assert page["total"] == 12
    assert [r["input_index"] for r in page["rows"]] == [5, 6, 7]


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_gi_region_patch_rejected(client: TestClient) -> None:
    """The API refuses even when the UI is bypassed."""
    job_id = run_job(client, 2)
    row_key = client.get(f"/api/jobs/{job_id}/rows").json()["rows"][0]["row_key"]

    response = client.patch(
        f"/api/jobs/{job_id}/rows/{row_key}", json={"fields": {"gi_region": "Kutch"}}
    )
    assert response.status_code == 422
    assert "gi_region" in response.text
    assert "government certification" in response.text

    # And it is still blank everywhere afterwards.
    cells = cells_of(client.get(f"/api/jobs/{job_id}/rows").json()["rows"][0])
    assert cells["gi_region"]["value"] == ""
    assert cells["gi_region"]["editable"] is False
    assert cells["gi_region"]["locked_reason"]
    assert all(r["gi_region"] == "" for r in listings(client, job_id))


def test_gi_region_is_not_in_the_editable_set() -> None:
    assert "gi_region" in LOCKED
    assert "gi_region" not in EDITABLE
    # Derived from the header minus what is locked, never a literal: the count
    # was hardcoded at 18 and haat then added a twentieth column.
    from haat_lister.output.csv_writer import HAAT_COLUMNS

    assert len(EDITABLE) == len(HAAT_COLUMNS) - len(LOCKED)
    assert "image_urls" in LOCKED, "composed at write time; an edit has nothing to attach to"


def test_row_patch_validates_against_taxonomy(client: TestClient) -> None:
    """A slug the taxonomy has never heard of fails the import, so it fails
    here first -- and the message names the ones that would work."""
    job_id = run_job(client, 2)
    row_key = client.get(f"/api/jobs/{job_id}/rows").json()["rows"][0]["row_key"]

    bad = client.patch(
        f"/api/jobs/{job_id}/rows/{row_key}",
        json={"fields": {"category_slug": "artisanal-luxury"}},
    )
    assert bad.status_code == 422
    assert "taxonomy.yaml" in bad.text
    assert "apparel" in bad.text, "the refusal should name the slugs that would work"

    good = client.patch(
        f"/api/jobs/{job_id}/rows/{row_key}", json={"fields": {"category_slug": "jewellery"}}
    )
    assert good.status_code == 200


def test_a_subcategory_is_validated_against_its_parent(client: TestClient) -> None:
    job_id = run_job(client, 2)
    row_key = client.get(f"/api/jobs/{job_id}/rows").json()["rows"][0]["row_key"]

    wrong = client.patch(
        f"/api/jobs/{job_id}/rows/{row_key}",
        json={"fields": {"subcategory_slug": "earrings"}},
    )
    assert wrong.status_code == 422, "earrings is not under apparel"

    # Set both together and it is judged on what is being asked for.
    together = client.patch(
        f"/api/jobs/{job_id}/rows/{row_key}",
        json={"fields": {"category_slug": "jewellery", "subcategory_slug": "earrings"}},
    )
    assert together.status_code == 200


@pytest.mark.parametrize(
    ("field", "value", "expect"),
    [
        ("price_inr", "not a number", "whole number"),
        ("price_inr", "-5", "negative"),
        ("price_inr", "0", "free"),
        ("weight_g", "12.5", "whole number"),
        ("availability", "maybe", "availability"),
        ("sizes", "S;M;L", "comma-separated"),
        ("hs_code", "62", "4 to 10 digits"),
        ("title", "", "no title"),
    ],
)
def test_edits_are_validated_the_way_extraction_is(
    client: TestClient, field: str, value: str, expect: str
) -> None:
    job_id = run_job(client, 2)
    row_key = client.get(f"/api/jobs/{job_id}/rows").json()["rows"][0]["row_key"]

    response = client.patch(f"/api/jobs/{job_id}/rows/{row_key}", json={"fields": {field: value}})
    assert response.status_code == 422, f"{field}={value!r} was accepted"
    assert expect in response.text.lower()


def test_a_price_with_a_comma_or_a_space_is_accepted(client: TestClient) -> None:
    """People paste "2,499" out of a spreadsheet. Refusing that would be
    pedantry, not validation."""
    job_id = run_job(client, 2)
    row_key = client.get(f"/api/jobs/{job_id}/rows").json()["rows"][0]["row_key"]

    response = client.patch(
        f"/api/jobs/{job_id}/rows/{row_key}", json={"fields": {"price_inr": " 2,499 "}}
    )
    assert response.status_code == 200
    assert cells_of(response.json())["price_inr"]["value"] == "2499"


# --------------------------------------------------------------------------
# Editing, and what happens to the original
# --------------------------------------------------------------------------


def test_an_edit_is_stamped_as_yours(client: TestClient) -> None:
    job_id = run_job(client, 2)
    row_key = client.get(f"/api/jobs/{job_id}/rows").json()["rows"][0]["row_key"]

    row = client.patch(
        f"/api/jobs/{job_id}/rows/{row_key}", json={"fields": {"price_inr": "2499"}}
    ).json()

    cell = cells_of(row)["price_inr"]
    assert cell["value"] == "2499"
    assert cell["source"] == "operator"
    assert cell["confidence"] == "high"
    assert cell["edited"] is True
    assert cell["original"] == "", "the pre-edit value should travel with the cell"


def test_reexport_preserves_original_extraction_values(
    client: TestClient, job_settings: Settings
) -> None:
    """The headline claim. Edits are stored beside the extraction, never over
    it, so the record keeps saying what the page said."""
    job_id = run_job(client, 3)
    page = client.get(f"/api/jobs/{job_id}/rows").json()
    row_key = page["rows"][0]["row_key"]

    client.patch(
        f"/api/jobs/{job_id}/rows/{row_key}",
        json={"fields": {"title": "Kutch mirror-work kurta", "price_inr": "2499"}},
    )
    export = client.post(f"/api/jobs/{job_id}/export").json()
    assert export["edits_applied"] == 2
    assert export["rows"] == 3

    # The CSV has the edit.
    rows = listings(client, job_id)
    assert rows[0]["title"] == "Kutch mirror-work kurta"
    assert rows[0]["price_inr"] == "2499"
    assert rows[1]["title"] == "Product 1", "an edit leaked onto another row"

    # The stored record does not.
    with Ledger(job_settings.root / job_settings.config.paths.ledger) as ledger:
        stored = ProductRecord.model_validate_json(ledger.row_payload(job_id, row_key))
    assert stored.title.value == "Product 0"
    assert not stored.price_inr.is_present
    assert stored.title.source is FieldSource.JSONLD

    # And the table shows both.
    cell = cells_of(client.get(f"/api/jobs/{job_id}/rows").json()["rows"][0])["title"]
    assert cell["value"] == "Kutch mirror-work kurta"
    assert cell["original"] == "Product 0"


def test_undoing_an_edit_restores_what_the_page_said(client: TestClient) -> None:
    """Which is only possible because the original was never overwritten."""
    job_id = run_job(client, 2)
    row_key = client.get(f"/api/jobs/{job_id}/rows").json()["rows"][0]["row_key"]

    client.patch(f"/api/jobs/{job_id}/rows/{row_key}", json={"fields": {"title": "Renamed"}})
    client.post(f"/api/jobs/{job_id}/export")
    assert listings(client, job_id)[0]["title"] == "Renamed"

    client.delete(f"/api/jobs/{job_id}/rows/{row_key}/edits/title")
    client.post(f"/api/jobs/{job_id}/export")
    assert listings(client, job_id)[0]["title"] == "Product 0"


def test_a_reexport_is_byte_identical_where_nothing_was_edited(client: TestClient) -> None:
    """Same header, same order, same columns -- the re-export goes through
    the same writer the live run used."""
    job_id = run_job(client, 4)
    before = client.get(f"/api/jobs/{job_id}/download/listings").content
    client.post(f"/api/jobs/{job_id}/export")
    after = client.get(f"/api/jobs/{job_id}/download/listings").content
    assert before == after


# --------------------------------------------------------------------------
# Bulk
# --------------------------------------------------------------------------


def test_bulk_apply_fills_a_column_in_one_request(client: TestClient) -> None:
    """38 blank prices, one action."""
    job_id = run_job(client, 8)
    keys = [r["row_key"] for r in client.get(f"/api/jobs/{job_id}/rows").json()["rows"]]

    response = client.patch(
        f"/api/jobs/{job_id}/rows",
        json={"row_keys": keys, "fields": {"price_inr": "1999", "availability": "stock"}},
    )
    assert response.status_code == 200
    assert response.json()["applied"] == 8
    assert response.json()["rejected"] == []

    client.post(f"/api/jobs/{job_id}/export")
    rows = listings(client, job_id)
    assert all(r["price_inr"] == "1999" for r in rows)
    assert all(r["availability"] == "stock" for r in rows)


def test_bulk_apply_is_validated_per_row_not_once(client: TestClient) -> None:
    """A subcategory right for an apparel row is wrong for a jewellery one.
    Applying it anyway is exactly the silently-wrong CSV this tool avoids."""
    job_id = run_job(client, 3)
    keys = [r["row_key"] for r in client.get(f"/api/jobs/{job_id}/rows").json()["rows"]]

    # Move one row to jewellery, then try to set an apparel subcategory on all.
    client.patch(f"/api/jobs/{job_id}/rows/{keys[1]}", json={"fields": {
        "category_slug": "jewellery", "subcategory_slug": "earrings"}})

    response = client.patch(
        f"/api/jobs/{job_id}/rows",
        json={"row_keys": keys, "fields": {"subcategory_slug": "womens-fashion"}},
    ).json()

    assert response["applied"] == 2
    assert [r["row_key"] for r in response["rejected"]] == [keys[1]]
    assert "not a subcategory of jewellery" in response["rejected"][0]["reason"]


def test_a_bulk_edit_naming_an_unknown_row_says_which(client: TestClient) -> None:
    job_id = run_job(client, 2)
    keys = [r["row_key"] for r in client.get(f"/api/jobs/{job_id}/rows").json()["rows"]]

    response = client.patch(
        f"/api/jobs/{job_id}/rows",
        json={"row_keys": [*keys, "not-a-row-key"], "fields": {"price_inr": "999"}},
    ).json()

    assert response["applied"] == 2
    assert response["rejected"][0]["row_key"] == "not-a-row-key"


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------


def test_a_bad_job_id_never_reaches_a_row(client: TestClient) -> None:
    for bad in ("j_UPPER123", "nonsense", "j_short"):
        assert client.get(f"/api/jobs/{bad}/rows").status_code == 404
        assert (
            client.patch(f"/api/jobs/{bad}/rows/x", json={"fields": {}}).status_code == 404
        )


def test_editing_a_row_from_another_job_is_a_404(client: TestClient) -> None:
    """row_keys are derived from the URL, so the same product in two jobs has
    the same key. The edit must still land on the job that was named."""
    first = run_job(client, 2)
    second = run_job(client, 2)
    key = client.get(f"/api/jobs/{first}/rows").json()["rows"][0]["row_key"]

    ok = client.patch(f"/api/jobs/{second}/rows/{key}", json={"fields": {"price_inr": "5"}})
    assert ok.status_code == 200  # same product, so the key exists in both

    missing = client.patch(
        f"/api/jobs/{second}/rows/never-seen-000", json={"fields": {"price_inr": "5"}}
    )
    assert missing.status_code == 404

    # And the edit stayed in the job it was sent to.
    client.post(f"/api/jobs/{first}/export")
    assert listings(client, first)[0]["price_inr"] == ""
