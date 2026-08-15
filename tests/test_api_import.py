"""v5 §4 through the browser: the routes, not the ingestion.

The ingestion has its own file. What is checked here is what the routes add,
and all of it is about what a browser makes possible that a CLI does not: a
file that is not a path we chose, a size that arrives rather than being declared,
a mapping typed by somebody who might type anything, and a declaration that must
not be skippable just because the form is prettier.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from haat_lister.api.app import create_app
from haat_lister.config import Settings

EXPORT = (
    "Seller SKU,Product Name,Product URL,MRP,Net Weight,HSN Code,Category,Main Image\n"
    "KRT-1,Indigo kurta,https://shop.example/p/kurta-1,2499,320 g,6206,apparel,\n"
)

SAVED = """<!DOCTYPE html>
<!-- saved from url=(0038)https://blocked.example/p/kurta-1 -->
<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Indigo kurta",
 "description":"Hand-blocked cotton in natural indigo, stitched in Bhuj.",
 "offers":{"@type":"Offer","price":"2499","priceCurrency":"INR"}}
</script></head><body><h1>Indigo kurta</h1></body></html>"""


@pytest.fixture
def client(settings: Settings, tmp_path: Path) -> TestClient:
    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    tuned.config.render.enabled = False
    return TestClient(create_app(tuned))


def csv_upload(text: str = EXPORT, name: str = "catalogue.csv"):
    return {"file": (name, text.encode("utf-8"), "text/csv")}


# --------------------------------------------------------------------------
# Inspect: look, decide nothing
# --------------------------------------------------------------------------


def test_inspect_builds_no_rows(client: TestClient) -> None:
    """§4.1's whole reason for two calls. Nothing is committed by looking."""
    response = client.post("/api/import/inspect", files=csv_upload())

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "export"
    assert body["row_count"] == 1
    assert {c["header"]: c["target"] for c in body["columns"]}["Product URL"] == "source_url"


def test_the_server_decides_which_targets_exist(client: TestClient) -> None:
    """Sent rather than hardcoded in the console, so `gi_region` is absent in
    one place instead of two -- and so the two cannot drift."""
    targets = client.post("/api/import/inspect", files=csv_upload()).json()["targets"]

    assert "gi_region" not in targets
    assert "title" in targets and "source_url" in targets


def test_a_column_we_cannot_write_says_so_rather_than_reading_as_unknown(
    client: TestClient,
) -> None:
    body = client.post("/api/import/inspect", files=csv_upload()).json()
    columns = {c["header"]: c for c in body["columns"]}

    assert columns["Seller SKU"]["known_unused"] == "sku"
    assert columns["Seller SKU"]["target"] == ""


def test_inspect_reads_where_a_saved_page_came_from(client: TestClient) -> None:
    response = client.post(
        "/api/import/inspect", files={"file": ("kurta.html", SAVED.encode(), "text/html")}
    )

    assert response.status_code == 200
    assert response.json()["kind"] == "saved_page"
    assert response.json()["source_url"] == "https://blocked.example/p/kurta-1"


# --------------------------------------------------------------------------
# What a browser makes possible
# --------------------------------------------------------------------------


def test_a_wrong_file_is_a_422_with_a_next_action(client: TestClient) -> None:
    """Not a 500. A wrong file is the most likely thing to happen here."""
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10)).save(buffer, format="PNG")
    response = client.post(
        "/api/import/inspect", files={"file": ("holiday.png", buffer.getvalue(), "image/png")}
    )

    assert response.status_code == 400
    assert "Ctrl+S" in response.json()["detail"]


def test_an_oversized_upload_is_refused_as_it_arrives(client: TestClient, monkeypatch) -> None:
    """Reading first and checking afterwards means the limit is enforced once
    the bytes are already in memory, which is not a limit."""
    from haat_lister.api.routes import ingest

    monkeypatch.setattr(ingest, "MAX_UPLOAD_BYTES", 32)
    response = client.post("/api/import/inspect", files=csv_upload())

    assert response.status_code == 413
    assert "limit" in response.json()["detail"]


def test_an_empty_upload_is_refused(client: TestClient) -> None:
    response = client.post("/api/import/inspect", files=csv_upload(""))
    assert response.status_code == 400


def test_a_filename_is_never_used_as_a_path(client: TestClient) -> None:
    """A filename is attacker-controlled even when the attacker is a tired
    operator, and `../../x.csv` is a valid thing for a browser to send."""
    response = client.post("/api/import/inspect", files=csv_upload(name="../../../evil.csv"))

    assert response.status_code == 200
    assert "/" not in response.json()["filename"]
    assert ".." not in response.json()["filename"]


# --------------------------------------------------------------------------
# Provenance, on the prettier form too
# --------------------------------------------------------------------------


def test_the_run_route_will_not_start_without_provenance(client: TestClient) -> None:
    """§7. Required by the signature, so it cannot be forgotten by a check."""
    response = client.post("/api/import/run", files=csv_upload())

    assert response.status_code == 422
    assert "provenance" in response.text


def test_a_bad_provenance_is_rejected_rather_than_defaulted(client: TestClient) -> None:
    response = client.post(
        "/api/import/run", files=csv_upload(), data={"provenance": "probably-fine"}
    )
    assert response.status_code == 422


def test_a_run_produces_rows_and_counts_them(client: TestClient) -> None:
    response = client.post("/api/import/run", files=csv_upload(), data={"provenance": "own"})

    assert response.status_code == 200
    body = response.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["source_url"] == "https://shop.example/p/kurta-1"
    assert body["written"] + body["needs_human"] + body["failed"] == 1


# --------------------------------------------------------------------------
# The mapping is a thing a human typed
# --------------------------------------------------------------------------


def test_a_mapping_that_omits_the_url_is_refused_with_the_reason(client: TestClient) -> None:
    response = client.post(
        "/api/import/run",
        files=csv_upload(),
        data={"provenance": "own", "mapping": json.dumps({"Product Name": "title"})},
    )

    assert response.status_code == 422
    assert "keyed on" in response.json()["detail"]


def test_the_console_cannot_map_a_column_onto_gi_region(client: TestClient) -> None:
    """§7, on the route where somebody could hand-craft the request."""
    response = client.post(
        "/api/import/run",
        files=csv_upload(),
        data={
            "provenance": "own",
            "mapping": json.dumps(
                {"Product URL": "source_url", "Category": "gi_region", "MRP": "price_inr"}
            ),
        },
    )

    assert response.status_code == 200, response.text
    # The row still exists and the legitimate half of the mapping applied; the
    # refused target simply had nowhere to land.
    assert len(response.json()["rows"]) == 1


def test_a_malformed_mapping_is_a_message_not_a_500(client: TestClient) -> None:
    response = client.post(
        "/api/import/run",
        files=csv_upload(),
        data={"provenance": "own", "mapping": "{not json"},
    )

    assert response.status_code == 400
    assert "mapping" in response.json()["detail"]


def test_a_profile_saved_from_the_console_is_reusable(client: TestClient) -> None:
    """§8 test 17's server half: save a mapping, re-inspect, it comes back."""
    saved = client.post(
        "/api/import/run",
        files=csv_upload(),
        data={"provenance": "own", "save_profile": "Nilaya Panel"},
    )
    assert saved.status_code == 200
    assert saved.json()["profile_saved"] == "nilaya-panel.yaml"

    again = client.post("/api/import/inspect", files=csv_upload())
    assert again.json()["profile_used"] == "nilaya-panel"


# --------------------------------------------------------------------------
# §4.4 -- the warning, on the route that already existed
# --------------------------------------------------------------------------


def test_preflight_reports_what_a_host_did_last_time(
    client: TestClient, settings, tmp_path
) -> None:
    """Extended onto `/api/jobs/preflight` rather than added beside it.

    A second preflight route would be the duplication §7 forbids, and the two
    would eventually disagree about the same paste.
    """
    from haat_lister import preflight as preflight_core

    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    preflight_core.observe(tuned, "https://refused.example/p/1", "bot_challenge")

    response = client.post(
        "/api/jobs/preflight",
        json={
            "urls": ["https://refused.example/p/2"],
            "settings": {
                "provenance": "own",
                "image_mode": "manifest",
                "description_mode": "raw",
                "concurrency": 1,
                "seller_note": None,
                "render": False,
                "llm": False,
                "ignore_robots": True,
            },
        },
    )

    assert response.status_code == 200
    observed = response.json()["observed"]
    assert len(observed) == 1
    assert observed[0]["host"] == "refused.example"
    assert "may well have changed its mind" in observed[0]["detail"]


def test_the_preflight_response_has_no_field_that_could_block(client: TestClient) -> None:
    """§4.4. A console cannot refuse to submit on the strength of this reply,
    because there is nothing in it to refuse on."""
    response = client.post(
        "/api/jobs/preflight",
        json={
            "urls": ["https://shop.example/p/1"],
            "settings": {
                "provenance": "own",
                "image_mode": "manifest",
                "description_mode": "raw",
                "concurrency": 1,
                "seller_note": None,
                "render": False,
                "llm": False,
                "ignore_robots": True,
            },
        },
    )

    body = response.json()
    assert "blocked" not in body
    assert "allowed" not in body
    assert "proceed" not in body
    for entry in body["observed"]:
        assert "blocking" not in entry
