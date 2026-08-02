"""v2 Phase 6: the four files and the zip, over HTTP.

Two client-supplied values reach the download route -- a job id and an artifact
name -- and both are controls. The tests that matter are therefore the ones that
try to make either of them mean a path.

The other claim under test is that a mid-run download is honest: `listings.csv`
is already the ordered prefix on disk, and the other three are regenerated from
the ledger on the way out, so what you download at row 200 describes those 200
rows rather than the last time a job finished.
"""

from __future__ import annotations

import asyncio
import csv
import io
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from haat_lister.api.app import create_app
from haat_lister.api.routes.downloads import ARTIFACTS
from haat_lister.config import Settings
from haat_lister.jobs import job_paths
from haat_lister.models import (
    Confidence,
    FieldSource,
    FieldValue,
    ImageMethod,
    Provenance,
)
from haat_lister.pipeline import new_record


@pytest.fixture
def job_settings(settings: Settings, tmp_path: Path) -> Settings:
    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    tuned.config.fetch.per_domain_delay_s = 0.0
    tuned.config.fetch.per_domain_delay_jitter_s = 0.0
    tuned.config.fetch.respect_robots = False
    return tuned


class FakeProcessor:
    def __init__(self, fail_for: set[str] | None = None) -> None:
        self._fail_for = fail_for or set()

    async def __call__(
        self, url: str, provenance: Provenance = Provenance.OWN, *_: object, **__: object
    ):
        await asyncio.sleep(0)
        record = new_record(url, provenance)
        if url in self._fail_for:
            record.fail("http_503")
            return record
        record.title = FieldValue.found(
            f"Product {url.rsplit('/', 1)[-1]}", FieldSource.JSONLD, Confidence.HIGH
        )
        record.description = FieldValue.found(
            "Hand-embroidered in Kutch.", FieldSource.JSONLD, Confidence.HIGH
        )
        record.category_slug = FieldValue.found("apparel", FieldSource.INFERRED, Confidence.HIGH)
        record.subcategory_slug = FieldValue.found(
            "womens-fashion", FieldSource.INFERRED, Confidence.HIGH
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


def run_job(client: TestClient, url_list: list[str]) -> str:
    response = client.post(
        "/api/jobs",
        json={"urls": url_list, "settings": {"provenance": "own", "concurrency": 4}},
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        for _ in stream.iter_lines():
            pass
    return job_id


# --------------------------------------------------------------------------
# The files
# --------------------------------------------------------------------------


def test_listings_downloads_and_is_the_import_file(client: TestClient) -> None:
    job_id = run_job(client, urls(5))
    response = client.get(f"/api/jobs/{job_id}/download/listings")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")

    body = response.content
    template = Path(__file__).resolve().parents[1] / "haat-bulk-listings-template.csv"
    assert body.split(b"\r\n")[0] == template.read_bytes().split(b"\r\n")[0], (
        "the downloaded header is not byte-identical to haat's template"
    )

    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8"))))
    assert len(rows) == 5
    assert [r["title"] for r in rows] == [f"Product {i}" for i in range(5)]
    assert all(r["gi_region"] == "" for r in rows)


def test_a_download_is_named_for_its_job(client: TestClient) -> None:
    """Three downloads in one folder should not be listings.csv, listings (1),
    listings (2)."""
    job_id = run_job(client, urls(2))
    disposition = client.get(f"/api/jobs/{job_id}/download/listings").headers[
        "content-disposition"
    ]
    assert job_id in disposition
    assert "listings" in disposition


def test_every_artifact_downloads(client: TestClient) -> None:
    job_id = run_job(client, [*urls(3), "not-a-url"])
    for name in ("listings", "review", "manifest", "failed"):
        response = client.get(f"/api/jobs/{job_id}/download/{name}")
        assert response.status_code == 200, f"{name}: {response.text}"
        assert response.content, f"{name} was empty"


def test_failed_csv_downloads_ready_to_re_run(client: TestClient, job_settings: Settings) -> None:
    """Its URL column pastes straight into a new job."""
    with TestClient(
        create_app(job_settings, process=FakeProcessor(fail_for={urls(3)[1]}))
    ) as failing:
        job_id = run_job(failing, urls(3))
        body = failing.get(f"/api/jobs/{job_id}/download/failed").content.decode("utf-8")

    rows = list(csv.DictReader(io.StringIO(body)))
    assert [r["source_url"] for r in rows] == [urls(3)[1]]
    assert rows[0]["reason"] == "http_503"


# --------------------------------------------------------------------------
# The zip
# --------------------------------------------------------------------------


def test_the_zip_holds_everything_the_job_produced(client: TestClient) -> None:
    job_id = run_job(client, urls(4))
    response = client.get(f"/api/jobs/{job_id}/download/zip")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert f"haat-listings-{job_id}.zip" in response.headers["content-disposition"]

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = {Path(n).name for n in archive.namelist()}
        assert {"listings.csv", "review.csv", "failed.csv", "job.json", "README.txt"} <= names
        # Everything is under one folder, so unzipping into Downloads does not
        # scatter five files loose.
        assert all(n.startswith(f"{job_id}/") for n in archive.namelist())


def test_the_zip_readme_says_what_the_files_are(client: TestClient) -> None:
    job_id = run_job(client, urls(2))
    response = client.get(f"/api/jobs/{job_id}/download/zip")

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        readme = archive.read(f"{job_id}/README.txt").decode("utf-8")

    assert "listings.csv" in readme and "the import file" in readme
    # The two facts an operator will otherwise ask about.
    assert "gi_region is blank in every row" in readme
    assert "price_inr is blank" in readme


def test_the_zip_is_rebuilt_when_the_job_changes(
    client: TestClient, job_settings: Settings
) -> None:
    """A zip taken after a resume must not be last week's."""
    job_id = run_job(client, urls(3))
    first = client.get(f"/api/jobs/{job_id}/download/zip").content

    listings = job_paths(job_settings, job_id).listings
    listings.write_bytes(listings.read_bytes() + b"\r\n")

    second = client.get(f"/api/jobs/{job_id}/download/zip").content
    assert first != second, "the zip was served from a stale build"


# --------------------------------------------------------------------------
# Mid-run
# --------------------------------------------------------------------------


def test_partial_download_mid_job_is_valid_csv(client: TestClient) -> None:
    """The file on disk is always a correctly ordered prefix, so "download what
    is done so far" is an honest offer rather than a snapshot with holes."""
    response = client.post(
        "/api/jobs",
        json={"urls": urls(40), "settings": {"provenance": "own", "concurrency": 2}},
    )
    job_id = response.json()["job_id"]

    snapshots: list[list[str]] = []
    with client.stream("GET", f"/api/jobs/{job_id}/events") as stream:
        seen = 0
        for line in stream.iter_lines():
            if line.startswith("event: row_done"):
                seen += 1
                if seen % 7 == 0:
                    body = client.get(f"/api/jobs/{job_id}/download/listings").content
                    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8"))))
                    snapshots.append([r["title"] for r in rows])

    assert snapshots, "the job finished before a single mid-run download"
    for titles in snapshots:
        assert titles == [f"Product {i}" for i in range(len(titles))], (
            "a mid-run download had a hole or a row out of order"
        )


def test_the_projections_are_rebuilt_on_the_way_out(
    client: TestClient, job_settings: Settings
) -> None:
    """review.csv, the manifest and failed.csv are regenerated from the ledger
    when downloaded, so what arrives describes the rows that exist now rather
    than the last time a job happened to finish.

    Asserted by deleting them: a route that served whatever was on disk would
    404, and one that rebuilds hands back the same file.
    """
    job_id = run_job(client, [*urls(3), "not-a-url"])
    paths = job_paths(job_settings, job_id)

    before = {
        name: (paths.root / filename).read_bytes()
        for name, filename in (
            ("review", "review.csv"),
            ("manifest", "image_manifest.csv"),
            ("failed", "failed.csv"),
        )
    }
    for filename in ("review.csv", "image_manifest.csv", "failed.csv"):
        (paths.root / filename).unlink()

    for name, original in before.items():
        response = client.get(f"/api/jobs/{job_id}/download/{name}")
        assert response.status_code == 200, f"{name} was not rebuilt"
        assert response.content == original, f"{name} was rebuilt differently"


def test_listings_is_not_regenerated_because_its_order_is_the_point(
    client: TestClient, job_settings: Settings
) -> None:
    """The other three are projections. listings.csv is written in input order
    as the job runs, and rewriting it on download would throw that away."""
    job_id = run_job(client, urls(3))
    job_paths(job_settings, job_id).listings.unlink()

    response = client.get(f"/api/jobs/{job_id}/download/listings")
    assert response.status_code == 404
    assert "listings.csv" in response.text


# --------------------------------------------------------------------------
# Safety
# --------------------------------------------------------------------------


def test_download_path_traversal_rejected(client: TestClient) -> None:
    """The artifact name is an allowlist lookup, not a filename."""
    job_id = run_job(client, urls(2))

    for attempt in (
        "../../.env",
        "..%2f..%2fconfig.yaml",
        "%2e%2e%2f.env",
        "listings.csv",
        "job.json",
        "images",
        "",
    ):
        response = client.get(f"/api/jobs/{job_id}/download/{attempt}")
        assert response.status_code == 404, f"{attempt!r} was served"
        assert "ANTHROPIC" not in response.text
        assert "user_agent_template" not in response.text


def test_an_unknown_artifact_names_the_real_ones(client: TestClient) -> None:
    job_id = run_job(client, urls(1))
    response = client.get(f"/api/jobs/{job_id}/download/everything")
    assert response.status_code == 404
    for name in ARTIFACTS:
        assert name in response.text


def test_a_bad_job_id_never_reaches_the_filesystem(client: TestClient) -> None:
    for bad in ("j_UPPER123", "nonsense", "j_short", "j_abc-1234"):
        assert client.get(f"/api/jobs/{bad}/download/listings").status_code == 404


def test_a_download_for_an_unknown_job_is_a_404(client: TestClient) -> None:
    assert client.get("/api/jobs/j_abcd1234/download/listings").status_code == 404


# --------------------------------------------------------------------------
# What the Complete screen reads
# --------------------------------------------------------------------------


def test_the_job_lists_its_artifacts_with_sizes(client: TestClient) -> None:
    """So the screen can say "38 rows" rather than "4.1 KB"."""
    job_id = run_job(client, [*urls(4), "not-a-url"])
    data = client.get(f"/api/jobs/{job_id}").json()

    by_name = {a["name"]: a for a in data["artifacts"]}
    assert {"listings", "review", "failed"} <= set(by_name)
    assert by_name["listings"]["rows"] == 4
    assert by_name["listings"]["bytes"] > 0
    assert job_id in by_name["listings"]["filename"]
    assert data["duration_s"] is not None and data["duration_s"] >= 0
