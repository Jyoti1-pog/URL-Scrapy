"""v4 Phase 5: the export that carries image links.

haat's template ends with one `image_urls` cell, which is not enough for a
record of what was found and how, so the file
that imports cannot be the file that carries photo URLs. Two files, then -- and
the entire risk of two files is that they disagree.

So the tests here are mostly about the relationship between them: identical row
order keyed on source_url, a header that does not change shape between runs, and
an import file that still matches haat's header exactly however much the companion
grows.
"""

from __future__ import annotations

import csv
from pathlib import Path

from haat_lister.config import Settings
from haat_lister.models import (
    Confidence,
    FieldSource,
    FieldValue,
    ImageFile,
    ImageMethod,
    ImageMode,
    ImageResult,
    ProductRecord,
    Provenance,
    ValidationResult,
)
from haat_lister.output import with_images
from haat_lister.output.csv_writer import HAAT_COLUMNS
from haat_lister.pipeline import new_record


def record(url: str, title: str, photos: int = 0, method: ImageMethod = ImageMethod.NONE):
    made: ProductRecord = new_record(url, Provenance.OWN)
    made.title = FieldValue.found(title, FieldSource.JSONLD, Confidence.HIGH)
    urls = [f"{url}/img/{i}.jpg" for i in range(photos)]
    made.image_candidates = urls
    made.image = ImageResult(
        method=method,
        url=urls[0] if urls and method is not ImageMethod.NONE else "",
        reason="direct_ok" if urls else "",
        candidate_results=[
            ValidationResult(url=u, ok=(i == 0), reason="direct_ok", width=1200, height=1200)
            for i, u in enumerate(urls)
        ],
        files=[
            ImageFile(
                order=i + 1,
                local_path=f"images/row/{i}.jpg",
                original_source_url=u,
                bytes=50_000,
                width=1200,
                height=1200,
            )
            for i, u in enumerate(urls)
        ]
        if method in (ImageMethod.LOCAL, ImageMethod.DIRECT)
        else [],
    )
    if method is ImageMethod.NONE and urls:
        from haat_lister.images.reasons import NoImageReason

        made.image.none_reason = NoImageReason.ALL_CANDIDATES_REJECTED
    return made


def read(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    return rows[0], rows[1:]


# --------------------------------------------------------------------------
# The import file must not change
# --------------------------------------------------------------------------


def test_listings_csv_matches_haats_header_exactly(settings: Settings, tmp_path: Path) -> None:
    """The guard that keeps the import file safe.

    Everything the companion adds is additive; this is the line it must not
    cross. An EXTRA column here is an import that fails at haat's end.

    It used to assert the literal 19, and haat then published a template with
    `image_urls` as a twentieth -- so the assertion went on passing about a
    number while the contract moved underneath it. Pinned to `HAAT_COLUMNS`,
    which is itself pinned to the template file by `test_csv_writer`.
    """
    from haat_lister.output.csv_writer import HaatCsvWriter

    path = tmp_path / "listings.csv"
    with HaatCsvWriter(path, settings.config, ImageMode.MANIFEST) as writer:
        writer.write(record("https://shop.example/p/1", "Kurta", photos=3))

    header, rows = read(path)
    assert header == list(HAAT_COLUMNS)
    assert all(len(row) == len(HAAT_COLUMNS) for row in rows)
    # The companion's own columns stay out of the import file. `image_urls` is
    # haat's twentieth and belongs; `image_url` and `image_1` are ours and do
    # not -- names close enough that only an exact check is worth anything.
    assert "image_url" not in header
    assert "image_1" not in header
    assert header[-1] == "image_urls"
    assert "source_url" not in header


def test_the_companion_starts_with_exactly_the_import_file(
    settings: Settings, tmp_path: Path
) -> None:
    """Anything reading the companion's leading columns gets `listings.csv`.
    The extra image columns are appended, never interleaved."""
    path = tmp_path / "listings_with_images.csv"
    with_images.write(
        path, [record("https://shop.example/p/1", "Kurta", 2, ImageMethod.DIRECT)],
        settings.config, ImageMode.MANIFEST,
    )

    header, rows = read(path)
    assert header[: len(HAAT_COLUMNS)] == list(HAAT_COLUMNS)
    assert header[len(HAAT_COLUMNS)] == "source_url"
    assert len(rows[0]) == len(header)


# --------------------------------------------------------------------------
# The two files must agree
# --------------------------------------------------------------------------


def test_listings_and_with_images_row_correspondence(
    settings: Settings, tmp_path: Path
) -> None:
    """§10 test 13. Two exports that disagree about which product is on line 40
    would be worse than one export.

    Run through a real job rather than by calling the writers side by side --
    the correspondence has to survive the ordering machinery, which is the only
    place it could realistically break.
    """
    from test_job_output import FakeProcessor, run_job, urls

    from haat_lister.jobs import job_paths

    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    tuned.config.fetch.per_domain_delay_s = 0.0
    tuned.config.fetch.per_domain_delay_jitter_s = 0.0

    lines = urls(8)
    stats = run_job(tuned, lines, process=FakeProcessor(), concurrency=3)
    paths = job_paths(tuned, stats.job_id)

    _, listing_rows = read(paths.listings)
    companion_header, companion_rows = read(paths.listings_with_images)

    assert len(listing_rows) == len(companion_rows)
    url_column = companion_header.index("source_url")
    for index, (plain, extended) in enumerate(zip(listing_rows, companion_rows, strict=True)):
        # haat's columns are identical, cell for cell.
        assert plain == extended[: len(HAAT_COLUMNS)], (
            f"row {index} differs between the two files"
        )
        assert extended[url_column], "the companion lost the URL it exists to carry"


def test_with_images_header_stable_across_runs(settings: Settings, tmp_path: Path) -> None:
    """§10 test 14. An operator building a spreadsheet over weeks cannot paste
    two files together if the columns move."""
    thin = tmp_path / "one.csv"
    fat = tmp_path / "six.csv"

    with_images.write(
        thin, [record("https://shop.example/p/1", "One photo", 1, ImageMethod.DIRECT)],
        settings.config, ImageMode.MANIFEST,
    )
    with_images.write(
        fat, [record("https://shop.example/p/2", "Six photos", 6, ImageMethod.DIRECT)],
        settings.config, ImageMode.MANIFEST,
    )

    assert read(thin)[0] == read(fat)[0]
    # And the empty trailing columns are present rather than absent.
    thin_row = read(thin)[1][0]
    assert len(thin_row) == len(read(fat)[1][0])
    assert thin_row.count("") >= 4


def test_a_row_with_no_photo_still_says_why(settings: Settings, tmp_path: Path) -> None:
    """The companion is where an operator looks when a photo is missing, so the
    reason has to be in it."""
    path = tmp_path / "c.csv"
    with_images.write(
        path, [record("https://shop.example/p/1", "No photo", 2, ImageMethod.NONE)],
        settings.config, ImageMode.MANIFEST,
    )

    header, rows = read(path)
    row = dict(zip(header, rows[0], strict=True))
    assert row["image_method"] == "none"
    assert row["image_reason"] == "all_candidates_rejected"
    assert row["image_url"] == ""
    # The candidates are still listed: they are what an operator would check.
    # The numbered columns hold PHOTOGRAPHS, and this row has none -- so they
    # are empty and `image_count` agrees with them. They used to be filled with
    # unvalidated candidates, which is how a five-photo product reported 55.
    assert row["image_1"] == ""
    assert row["image_count"] == "0"

    # What was considered and refused is still here, under its own name, so a
    # row the automatic path could not finish is a starting point for doing it
    # by hand rather than a dead end.
    assert "/img/0.jpg" in row["rejected_image_urls"]


def test_the_hero_leads_and_duplicates_collapse(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / "c.csv"
    made = record("https://shop.example/p/1", "Kurta", 3, ImageMethod.DIRECT)
    with_images.write(path, [made], settings.config, ImageMode.MANIFEST)

    header, rows = read(path)
    row = dict(zip(header, rows[0], strict=True))
    # The validated winner is both `image_url` and `image_1` -- it appears once
    # in the numbered columns, not twice.
    assert row["image_url"] == row["image_1"]
    assert row["image_count"] == "3"
    assert row["image_2"] != row["image_1"]


def test_csv_injection_is_guarded_here_too(settings: Settings, tmp_path: Path) -> None:
    """Same guards as the import file: this one gets opened in Excel too."""
    path = tmp_path / "c.csv"
    made = record("https://shop.example/p/1", "Kurta")
    made.image.url = "=HYPERLINK(\"http://evil.example\")"
    with_images.write(path, [made], settings.config, ImageMode.MANIFEST)

    header, rows = read(path)
    row = dict(zip(header, rows[0], strict=True))
    assert not row["image_url"].startswith("=")


# --------------------------------------------------------------------------
# The accumulating pair
# --------------------------------------------------------------------------


def test_master_gets_the_same_pair_deduped_identically(
    settings: Settings, tmp_path: Path
) -> None:
    from haat_lister.output.master import append, master_with_images_path
    from haat_lister.utils.urls import canonicalise

    runs = tmp_path / "runs"
    runs.mkdir()
    sheet = runs / "master.csv"
    companion = master_with_images_path(tmp_path, settings.config)

    def add(url: str, title: str, photos: int = 2):
        made = record(url, title, photos, ImageMethod.DIRECT)
        return append([made], [canonicalise(url)], sheet, settings.config, job_id="j_aaaaaaaa")

    add("https://shop.example/p/1", "One")
    add("https://shop.example/p/2", "Two")
    again = add("https://shop.example/p/1", "One again")

    assert again.skipped == 1
    plain_header, plain_rows = read(sheet)
    extended_header, extended_rows = read(companion)

    assert plain_header == list(HAAT_COLUMNS)
    assert extended_header[: len(HAAT_COLUMNS)] == list(HAAT_COLUMNS)
    assert len(plain_rows) == len(extended_rows) == 2
    for plain, extended in zip(plain_rows, extended_rows, strict=True):
        assert plain == extended[: len(HAAT_COLUMNS)]
    assert extended_rows[0][extended_header.index("image_url")].endswith("/img/0.jpg")


def test_a_companion_that_predates_the_sheet_is_padded_not_dropped(
    settings: Settings, tmp_path: Path
) -> None:
    """A sheet built before this file existed must not lose rows the first time
    it is appended to. Shorter-than-the-sheet is the one thing neither file may
    be."""
    from haat_lister.output.master import append, master_with_images_path
    from haat_lister.utils.urls import canonicalise

    runs = tmp_path / "runs"
    runs.mkdir()
    sheet = runs / "master.csv"

    # A sheet with rows and no companion at all.
    append(
        [record("https://shop.example/p/1", "Old")],
        [canonicalise("https://shop.example/p/1")],
        sheet,
        settings.config,
    )
    master_with_images_path(tmp_path, settings.config).unlink()

    append(
        [record("https://shop.example/p/2", "New", 2, ImageMethod.DIRECT)],
        [canonicalise("https://shop.example/p/2")],
        sheet,
        settings.config,
    )

    _, plain_rows = read(sheet)
    _, extended_rows = read(master_with_images_path(tmp_path, settings.config))
    assert len(plain_rows) == len(extended_rows) == 2
