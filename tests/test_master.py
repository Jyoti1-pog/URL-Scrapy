"""Phase 7: runs/master.csv, the sheet that fills up.

The gap: ten jobs over a week left ten listings.csv files to merge by hand. The
operator's mental model is one catalogue that grows.

The property that makes it usable is not "it accumulates" -- it is that it is a
valid haat import file at every moment. So the header test here compares BYTES
against a job's own listings.csv rather than comparing column names, and the
dedupe tests use the same canonical identity the planner uses.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from haat_lister.config import Settings
from haat_lister.models import (
    Confidence,
    FieldSource,
    FieldValue,
    ImageMode,
    ProductRecord,
    Provenance,
)
from haat_lister.output.csv_writer import HAAT_COLUMNS, HaatCsvWriter, HeaderMismatch
from haat_lister.output.master import (
    APPEND,
    REPLACE,
    SKIP,
    SheetLocked,
    append,
    index_path,
    preview,
    stats,
)
from haat_lister.pipeline import new_record
from haat_lister.utils.urls import canonicalise


def record(url: str, title: str, price: int | None = None) -> ProductRecord:
    made = new_record(url, Provenance.OWN)
    made.title = FieldValue.found(title, FieldSource.JSONLD, Confidence.HIGH)
    if price is not None:
        made.price_inr = FieldValue.found(price, FieldSource.OPERATOR, Confidence.HIGH)
    return made


def add(sheet: Path, settings: Settings, urls: list[tuple[str, str]], **kwargs: object):
    records = [record(url, title) for url, title in urls]
    canonicals = [canonicalise(url) for url, _ in urls]
    return append(records, canonicals, sheet, settings.config, **kwargs)  # type: ignore[arg-type]


def rows_of(sheet: Path) -> list[list[str]]:
    with sheet.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


# --------------------------------------------------------------------------
# It has to be a haat file at every moment
# --------------------------------------------------------------------------


def test_master_header_byte_identical_to_a_jobs_own(settings: Settings, tmp_path: Path) -> None:
    """§8 test 11. Compared as BYTES, not as column names: the header is
    unquoted and CRLF-terminated, and a writer that got either wrong would still
    pass a name comparison and still be rejected by the importer."""
    sheet = tmp_path / "master.csv"
    add(sheet, settings, [("https://shop.example/p/1", "Kurta")])

    listings = tmp_path / "listings.csv"
    with HaatCsvWriter(listings, settings.config, ImageMode.MANIFEST) as writer:
        writer.write(record("https://shop.example/p/1", "Kurta"))

    master_first = sheet.read_bytes().split(b"\r\n", 1)[0]
    job_first = listings.read_bytes().split(b"\r\n", 1)[0]
    assert master_first == job_first
    assert master_first.decode() == ",".join(HAAT_COLUMNS)


def test_the_sheet_carries_no_bookkeeping_columns(settings: Settings, tmp_path: Path) -> None:
    """A twentieth column would mean an operator's upload silently carrying a
    field haat never asked for."""
    sheet = tmp_path / "master.csv"
    add(sheet, settings, [("https://shop.example/p/1", "Kurta")])

    rows = rows_of(sheet)
    assert rows[0] == list(HAAT_COLUMNS)
    assert all(len(row) == len(HAAT_COLUMNS) for row in rows[1:])

    # The URLs it dedupes on live beside the file, not in it.
    assert index_path(sheet).exists()


def test_gi_region_is_still_blank_in_the_sheet(settings: Settings, tmp_path: Path) -> None:
    sheet = tmp_path / "master.csv"
    add(sheet, settings, [("https://shop.example/p/1", "Banarasi Silk Saree")])

    rows = rows_of(sheet)
    gi = rows[0].index("gi_region")
    assert all(row[gi] == "" for row in rows[1:])


def test_a_sheet_with_the_wrong_header_is_refused(settings: Settings, tmp_path: Path) -> None:
    """Appending to it would produce a file the importer rejects, and the
    operator would find out at upload time."""
    sheet = tmp_path / "master.csv"
    sheet.write_text("title,price\r\nKurta,100\r\n", encoding="utf-8")

    with pytest.raises(HeaderMismatch) as caught:
        add(sheet, settings, [("https://shop.example/p/1", "Kurta")])
    assert "19 columns" in str(caught.value)


# --------------------------------------------------------------------------
# Accumulation and dedupe
# --------------------------------------------------------------------------


def test_master_append_dedupes_across_jobs(settings: Settings, tmp_path: Path) -> None:
    """§8 test 10, and §9's "three jobs accumulate into one sheet"."""
    sheet = tmp_path / "master.csv"

    first = add(
        sheet, settings, [("https://shop.example/p/1", "A"), ("https://shop.example/p/2", "B")]
    )
    second = add(sheet, settings, [("https://shop.example/p/3", "C")])
    third = add(sheet, settings, [("https://shop.example/p/1", "A again")])

    assert (first.added, second.added, third.added) == (2, 1, 0)
    assert third.skipped == 1
    assert third.total == 3
    assert len(rows_of(sheet)) == 4  # header + 3


def test_re_running_the_same_paste_grows_the_sheet_by_nothing(
    settings: Settings, tmp_path: Path
) -> None:
    """§8 test 15's tail, which is the whole point of the dedupe."""
    sheet = tmp_path / "master.csv"
    paste = [(f"https://shop.example/p/{i}", f"Product {i}") for i in range(6)]

    add(sheet, settings, paste)
    before = sheet.read_bytes()
    again = add(sheet, settings, paste)

    assert again.added == 0
    assert again.skipped == 6
    assert sheet.read_bytes() == before, "a no-op append rewrote the file"


def test_it_dedupes_on_the_same_identity_the_planner_uses(
    settings: Settings, tmp_path: Path
) -> None:
    """Two tracking URLs for one ASIN are one product in the plan, so they must
    be one row here. Anything else and the sheet disagrees with the job."""
    sheet = tmp_path / "master.csv"
    tracked = "https://www.amazon.in/Some-Slug/dp/B0FTFMNYBV/?ref_=x&th=1"
    plain = "https://amazon.in/dp/B0FTFMNYBV"

    add(sheet, settings, [(tracked, "Earbuds")])
    second = add(sheet, settings, [(plain, "Earbuds")])

    assert second.added == 0
    assert second.skipped == 1


def test_on_duplicate_replace_preserves_row_position(settings: Settings, tmp_path: Path) -> None:
    """§8 test 14. The order IS the history, so a correction must not move a row
    to the end."""
    sheet = tmp_path / "master.csv"
    add(
        sheet,
        settings,
        [
            ("https://shop.example/p/1", "First"),
            ("https://shop.example/p/2", "Second"),
            ("https://shop.example/p/3", "Third"),
        ],
    )

    outcome = add(
        sheet,
        settings,
        [("https://shop.example/p/2", "Second, corrected")],
        on_duplicate=REPLACE,
    )

    titles = [row[0] for row in rows_of(sheet)[1:]]
    assert titles == ["First", "Second, corrected", "Third"]
    assert (outcome.replaced, outcome.added, outcome.total) == (1, 0, 3)


def test_on_duplicate_append_allows_the_repeat(settings: Settings, tmp_path: Path) -> None:
    sheet = tmp_path / "master.csv"
    add(sheet, settings, [("https://shop.example/p/1", "First")])
    outcome = add(
        sheet, settings, [("https://shop.example/p/1", "First again")], on_duplicate=APPEND
    )

    assert outcome.added == 1
    assert [row[0] for row in rows_of(sheet)[1:]] == ["First", "First again"]


def test_an_unknown_duplicate_policy_is_refused(settings: Settings, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="on_duplicate"):
        add(sheet := tmp_path / "master.csv", settings, [], on_duplicate="merge")
    assert not sheet.exists()


def test_order_is_append_order_not_job_order(settings: Settings, tmp_path: Path) -> None:
    sheet = tmp_path / "master.csv"
    add(sheet, settings, [("https://shop.example/z", "Z"), ("https://shop.example/a", "A")])
    add(sheet, settings, [("https://shop.example/m", "M")])

    assert [row[0] for row in rows_of(sheet)[1:]] == ["Z", "A", "M"]


# --------------------------------------------------------------------------
# Failure modes an operator actually hits
# --------------------------------------------------------------------------


def test_master_locked_file_reports_clearly(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§8 test 13. On Windows this is the single most likely failure and it is
    entirely the operator's to fix, so it gets a sentence rather than a
    traceback."""
    sheet = tmp_path / "master.csv"
    add(sheet, settings, [("https://shop.example/p/1", "Kurta")])

    import haat_lister.utils.atomic as atomic

    def locked(*args: object, **kwargs: object):
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(atomic, "atomic_text_writer", locked)
    monkeypatch.setattr("haat_lister.output.master.atomic_text_writer", locked)

    with pytest.raises(SheetLocked) as caught:
        add(sheet, settings, [("https://shop.example/p/2", "Stole")])

    message = str(caught.value)
    assert "open in another program" in message
    assert "Close it and try again" in message
    assert "nothing was lost" in message


def test_a_corrupt_index_does_not_lose_the_sheet(settings: Settings, tmp_path: Path) -> None:
    """The CSV is the deliverable. The worst a broken sidecar may cost is a
    duplicate row an operator can see and delete."""
    sheet = tmp_path / "master.csv"
    add(sheet, settings, [("https://shop.example/p/1", "Kurta")])
    index_path(sheet).write_text("{ not json", encoding="utf-8")

    outcome = add(sheet, settings, [("https://shop.example/p/2", "Stole")])

    assert outcome.total == 2
    assert [row[0] for row in rows_of(sheet)[1:]] == ["Kurta", "Stole"]


def test_a_hand_edited_sheet_wins_over_its_index(settings: Settings, tmp_path: Path) -> None:
    """Someone deleted a row in Excel. The file is the truth."""
    sheet = tmp_path / "master.csv"
    add(sheet, settings, [("https://shop.example/p/1", "A"), ("https://shop.example/p/2", "B")])

    rows = rows_of(sheet)
    with sheet.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle, lineterminator="\r\n").writerows(rows[:2])

    outcome = add(sheet, settings, [("https://shop.example/p/3", "C")])
    assert outcome.total == 2
    assert [row[0] for row in rows_of(sheet)[1:]] == ["A", "C"]


# --------------------------------------------------------------------------
# Never from a partial job
# --------------------------------------------------------------------------


def test_master_not_written_from_incomplete_job(settings: Settings) -> None:
    """§8 test 12. Asserted at the call site, because that is where the decision
    is: `_finalise` only calls it when the run completed."""
    import inspect

    from haat_lister.batch import BatchRunner

    source = inspect.getsource(BatchRunner._finalise)
    assert 'self._options.master and state == "done"' in source
    assert "cancelled" in inspect.getsource(BatchRunner._finalise)


def test_master_is_off_by_default_and_on_for_the_console() -> None:
    """The two callers want different things and both are right."""
    from pathlib import Path as _Path

    from haat_lister.batch import BatchOptions
    from haat_lister.models import ImageMode as _Mode

    assert BatchOptions(provenance=Provenance.OWN, image_mode=_Mode.MANIFEST).master is False

    api = (_Path(__file__).resolve().parents[1] / "haat_lister/api/routes/jobs.py").read_text(
        encoding="utf-8"
    )
    assert "master=True" in api, "the console should default to one accumulating sheet"


def test_a_locked_sheet_never_fails_the_job() -> None:
    """The job's own files are already written and valid. Losing the run over a
    convenience file would be the wrong trade."""
    import inspect

    from haat_lister.batch import BatchRunner

    source = inspect.getsource(BatchRunner._add_to_master)
    assert "except SheetLocked" in source
    assert "master_error" in source


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def test_stats_reports_what_an_operator_asks(settings: Settings, tmp_path: Path) -> None:
    sheet = tmp_path / "master.csv"
    add(sheet, settings, [("https://shop.example/p/1", "A")], job_id="j_aaaaaaaa")
    add(sheet, settings, [("https://shop.example/p/2", "B")], job_id="j_bbbbbbbb")

    summary = stats(sheet, settings.config)
    assert summary.exists and summary.header_ok
    assert summary.rows == 2
    assert summary.jobs == 2
    assert summary.first_added and summary.last_added


def test_stats_on_a_sheet_that_does_not_exist_is_not_an_error(
    settings: Settings, tmp_path: Path
) -> None:
    summary = stats(tmp_path / "nothing.csv", settings.config)
    assert not summary.exists and summary.rows == 0


def test_the_summary_line_says_what_happened(settings: Settings, tmp_path: Path) -> None:
    sheet = tmp_path / "master.csv"
    outcome = add(sheet, settings, [("https://shop.example/p/1", "A")])
    assert outcome.summary() == "added 1 row — the sheet now has 1 row"

    repeat = add(sheet, settings, [("https://shop.example/p/1", "A")])
    assert "already there" in repeat.summary()
    assert not repeat.changed


def test_preview_never_reads_the_whole_file(settings: Settings, tmp_path: Path) -> None:
    sheet = tmp_path / "master.csv"
    add(sheet, settings, [(f"https://shop.example/p/{i}", f"P{i}") for i in range(40)])
    assert len(preview(sheet, settings.config, limit=5)) == 5


def test_skip_is_the_default(settings: Settings, tmp_path: Path) -> None:
    """A repeat should be reported, not silently doubled."""
    sheet = tmp_path / "master.csv"
    add(sheet, settings, [("https://shop.example/p/1", "A")])
    assert add(sheet, settings, [("https://shop.example/p/1", "A")], on_duplicate=SKIP).skipped == 1
