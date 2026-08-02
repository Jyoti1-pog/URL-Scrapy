"""Phase 4: the import file and the review file.

The headline test here reads the real haat-bulk-listings-template.csv off disk
and compares bytes. If haat ever reissues the template, this is what tells us.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from haat_lister.config import AppConfig
from haat_lister.models import (
    Confidence,
    FieldSource,
    FieldValue,
    ImageMode,
    ProductRecord,
    Provenance,
    RowStatus,
)
from haat_lister.output.csv_writer import (
    GI_REGION_ALWAYS_BLANK,
    HAAT_COLUMNS,
    HaatCsvWriter,
    HeaderMismatch,
    cell_depths,
    escape_csv_injection,
    image_columns,
    row_values,
)
from haat_lister.output.review_writer import (
    REVIEW_COLUMNS,
    ReviewWriter,
    low_confidence_fields,
    missing_required,
    needs_review,
)
from haat_lister.pipeline import new_record
from haat_lister.store.ledger import Ledger

TEMPLATE = Path(__file__).resolve().parents[1] / "haat-bulk-listings-template.csv"


def make_record(**overrides) -> ProductRecord:
    record = ProductRecord(
        row_key="shop-example-products-kurta-abc12345",
        source_url="https://shop.example/products/kurta",
        canonical_url="https://shop.example/products/kurta",
        provenance=Provenance.OWN,
        title=FieldValue.found("Hand-embroidered cotton kurta", FieldSource.JSONLD),
        description=FieldValue.found("Hand-embroidered in Kutch.", FieldSource.JSONLD),
    )
    for key, value in overrides.items():
        setattr(record, key, value)
    return record


def written_bytes(path) -> bytes:
    return path.read_bytes()


# ---------------------------------------------------------------------------
# The locked header
# ---------------------------------------------------------------------------


def test_columns_match_the_template_exactly():
    with TEMPLATE.open("r", encoding="utf-8", newline="") as handle:
        header = next(csv.reader(handle))
    assert list(HAAT_COLUMNS) == header
    assert len(HAAT_COLUMNS) == 19


def test_csv_header_locked_and_ordered(tmp_path, app_config):
    """Byte-identical to the template's own first line, quoting included."""
    out = tmp_path / "listings.csv"
    with HaatCsvWriter(out, app_config, ImageMode.MANIFEST):
        pass

    template_header = TEMPLATE.read_bytes().split(b"\r\n", 1)[0]
    our_header = written_bytes(out).split(b"\r\n", 1)[0]
    assert our_header == template_header


def test_header_is_unquoted_even_when_quote_all_is_set(tmp_path, app_config):
    """quote_all is supported, but it must never break header byte-identity."""
    app_config.csv.quote_all = True
    out = tmp_path / "listings.csv"
    with HaatCsvWriter(out, app_config, ImageMode.MANIFEST) as writer:
        writer.write(make_record())

    lines = written_bytes(out).split(b"\r\n")
    assert lines[0] == TEMPLATE.read_bytes().split(b"\r\n", 1)[0]
    assert lines[1].startswith(b'"')  # the data row did honour quote_all


def test_line_endings_and_no_bom_match_the_template(tmp_path, app_config):
    out = tmp_path / "listings.csv"
    with HaatCsvWriter(out, app_config, ImageMode.MANIFEST) as writer:
        writer.write(make_record())

    raw = written_bytes(out)
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.endswith(b"\r\n")
    assert raw.count(b"\n") == raw.count(b"\r\n")


def test_excel_bom_is_opt_in(tmp_path, app_config):
    app_config.csv.excel_bom = True
    out = tmp_path / "listings.csv"
    with HaatCsvWriter(out, app_config, ImageMode.MANIFEST):
        pass
    assert written_bytes(out).startswith(b"\xef\xbb\xbf")


def test_manifest_mode_adds_no_columns():
    assert image_columns(ImageMode.MANIFEST) == []
    assert image_columns(ImageMode.URL_COLUMNS) == ["image_url"]
    assert image_columns(ImageMode.BOTH, multi=3) == [
        "image_url",
        "image_1",
        "image_2",
        "image_3",
    ]


def test_csv_append_header_mismatch_raises(tmp_path, app_config):
    out = tmp_path / "listings.csv"
    out.write_text("title,description\r\n", encoding="utf-8")

    with pytest.raises(HeaderMismatch) as exc:
        with HaatCsvWriter(out, app_config, ImageMode.MANIFEST):
            pass
    assert "Appending would corrupt the file" in str(exc.value)


def test_append_preserves_existing_rows(tmp_path, app_config):
    out = tmp_path / "listings.csv"
    with HaatCsvWriter(out, app_config, ImageMode.MANIFEST) as writer:
        writer.write(make_record())
    with HaatCsvWriter(out, app_config, ImageMode.MANIFEST) as writer:
        writer.write(make_record(row_key="second", canonical_url="https://shop.example/b"))

    rows = list(csv.reader(io.StringIO(out.read_text(encoding="utf-8"))))
    assert rows[0] == list(HAAT_COLUMNS)
    assert len(rows) == 3


# ---------------------------------------------------------------------------
# Row content
# ---------------------------------------------------------------------------


def test_gi_region_always_blank(app_config):
    """The source page can shout about GI certification all it likes."""
    record = make_record(
        title=FieldValue.found("Authentic GI-certified Banarasi silk saree", FieldSource.JSONLD),
        gi_mention_found="Page claims: GI tag certified Banarasi",
    )
    values = row_values(record, app_config, ImageMode.MANIFEST)
    assert values[HAAT_COLUMNS.index("gi_region")] == ""
    assert GI_REGION_ALWAYS_BLANK == ""
    # ...and the claim is not lost, it becomes a question for a human.
    assert needs_review(record, app_config)


def test_price_inr_blank_by_default(app_config):
    """A USD source price never becomes a silent INR number."""
    record = make_record(source_price=70.97, source_currency="USD")
    values = row_values(record, app_config, ImageMode.MANIFEST)
    assert values[HAAT_COLUMNS.index("price_inr")] == ""
    assert "price_inr" in missing_required(record, app_config)


def test_blank_fields_are_emitted_as_empty_not_none(app_config):
    values = row_values(make_record(), app_config, ImageMode.MANIFEST)
    assert all(isinstance(v, str) for v in values)
    assert values[HAAT_COLUMNS.index("weight_g")] == ""


def test_integers_carry_no_separators_or_symbols(app_config):
    record = make_record(
        price_inr=FieldValue.found(124999, FieldSource.OPERATOR),
        weight_g=FieldValue.found(350, FieldSource.SPEC_TABLE),
    )
    values = row_values(record, app_config, ImageMode.MANIFEST)
    assert values[HAAT_COLUMNS.index("price_inr")] == "124999"
    assert values[HAAT_COLUMNS.index("weight_g")] == "350"


def test_rfq_uses_the_literal_yes_not_a_boolean(app_config):
    record = make_record(
        rfq_enabled=FieldValue.found("yes", FieldSource.POLICY_DEFAULT),
        rfq_min_qty=FieldValue.found(50, FieldSource.POLICY_DEFAULT),
    )
    values = row_values(record, app_config, ImageMode.MANIFEST)
    assert values[HAAT_COLUMNS.index("rfq_enabled")] == "yes"
    assert values[HAAT_COLUMNS.index("rfq_min_qty")] == "50"


def test_failed_rows_are_not_written_but_do_reach_review(tmp_path, app_config):
    out = tmp_path / "listings.csv"
    record = make_record(status=RowStatus.FAILED, failure_reason="no_title")

    with HaatCsvWriter(out, app_config, ImageMode.MANIFEST) as writer:
        assert writer.write(record) is False
    assert writer.skipped_failed == 1

    rows = list(csv.reader(io.StringIO(out.read_text(encoding="utf-8"))))
    assert len(rows) == 1  # header only
    assert needs_review(record, app_config)


# ---------------------------------------------------------------------------
# Injection and control characters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@", "\t", "\r"])
def test_csv_injection_escaped(app_config, prefix):
    payload = f"{prefix}cmd|'/c calc'!A1"
    assert escape_csv_injection(payload, app_config.csv.injection_prefixes) == "'" + payload


def test_ordinary_text_is_not_escaped(app_config):
    assert escape_csv_injection("Kurta", app_config.csv.injection_prefixes) == "Kurta"


def test_injection_guard_applies_to_written_rows(app_config):
    record = make_record(title=FieldValue.found("=HYPERLINK(\"evil\")", FieldSource.JSONLD))
    values = row_values(record, app_config, ImageMode.MANIFEST)
    assert values[0].startswith("'=")


def test_control_characters_are_stripped(app_config):
    record = make_record(
        description=FieldValue.found("Kurta\x00 with\x07 junk", FieldSource.JSONLD)
    )
    values = row_values(record, app_config, ImageMode.MANIFEST)
    assert values[1] == "Kurta with junk"


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------


def test_rerunning_adds_zero_duplicate_rows(tmp_path, app_config):
    out = tmp_path / "listings.csv"
    with Ledger(":memory:") as ledger:
        with HaatCsvWriter(out, app_config, ImageMode.MANIFEST, ledger=ledger) as first:
            assert first.write(make_record()) is True
        with HaatCsvWriter(out, app_config, ImageMode.MANIFEST, ledger=ledger) as second:
            assert second.write(make_record()) is False
        assert second.skipped_duplicates == 1

    rows = list(csv.reader(io.StringIO(out.read_text(encoding="utf-8"))))
    assert len(rows) == 2  # header + one row


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def test_a_crash_leaves_the_previous_file_intact(tmp_path, app_config):
    out = tmp_path / "listings.csv"
    with HaatCsvWriter(out, app_config, ImageMode.MANIFEST) as writer:
        writer.write(make_record())
    before = written_bytes(out)

    with pytest.raises(RuntimeError):
        with HaatCsvWriter(out, app_config, ImageMode.MANIFEST) as writer:
            writer.write(make_record(canonical_url="https://shop.example/b"))
            raise RuntimeError("boom")

    assert written_bytes(out) == before


# ---------------------------------------------------------------------------
# review.csv
# ---------------------------------------------------------------------------


def test_review_lists_every_field_haat_marks_required(app_config):
    """Blank is fine for our CSV but stops the seller publishing."""
    missing = missing_required(make_record(), app_config)
    assert set(missing) >= {
        "price_inr",
        "hs_code",
        "weight_g",
        "length_cm",
        "width_cm",
        "height_cm",
    }
    assert "title" not in missing  # it is present on this record


def test_review_reports_low_confidence_but_present_fields(app_config):
    record = make_record(
        title=FieldValue.found("From a heading", FieldSource.H1, Confidence.MEDIUM)
    )
    assert "title:medium" in low_confidence_fields(record)


def test_clean_row_stays_out_of_the_worklist(app_config):
    """review.csv is a worklist, not a log."""
    app_config.fields.required_by_haat = []
    record = make_record()
    assert not needs_review(record, app_config)


def test_review_writer_emits_expected_columns(tmp_path, app_config):
    path = tmp_path / "review.csv"
    with ReviewWriter(path, app_config) as writer:
        writer.write(make_record(source_price=70.97, source_currency="USD"))

    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))
    assert rows[0] == list(REVIEW_COLUMNS)
    row = dict(zip(REVIEW_COLUMNS, rows[1], strict=True))
    assert row["source_price"] == "70.97"
    assert row["source_currency"] == "USD"
    assert row["provenance"] == "own"
    assert "price_inr" in row["missing_required"]


def test_review_is_rewritten_not_appended(tmp_path, app_config):
    """Stale rows from a previous run would make the worklist untrustworthy."""
    path = tmp_path / "review.csv"
    for _ in range(2):
        with ReviewWriter(path, app_config) as writer:
            writer.write(make_record())

    rows = list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))
    assert len(rows) == 2


# --------------------------------------------------------------------------
# The console's fill grid
# --------------------------------------------------------------------------


def test_cell_depths_is_one_character_per_locked_column(app_config: AppConfig) -> None:
    """The grid is a picture of the CSV, so its width has to be the CSV's."""
    record = new_record("https://shop.example/p/1", Provenance.OWN)
    assert len(cell_depths(record)) == len(HAAT_COLUMNS) == 19


def test_cell_depths_encodes_confidence_not_just_presence(app_config: AppConfig) -> None:
    """Depth of dye IS the confidence. A cell read from JSON-LD and one guessed
    from a heading are not the same cell, and the grid says so."""
    record = new_record("https://shop.example/p/1", Provenance.OWN)
    record.title = FieldValue.found("Kurta", FieldSource.JSONLD, Confidence.HIGH)
    record.description = FieldValue.found("Woven.", FieldSource.H1, Confidence.MEDIUM)
    record.category_slug = FieldValue.found("apparel", FieldSource.HEURISTIC, Confidence.LOW)

    cells = cell_depths(record)
    index = {name: i for i, name in enumerate(HAAT_COLUMNS)}

    assert cells[index["title"]] == "3"
    assert cells[index["description"]] == "2"
    assert cells[index["category_slug"]] == "1"
    assert cells[index["price_inr"]] == "0"


def test_gi_region_is_locked_in_the_grid_not_merely_empty(app_config: AppConfig) -> None:
    """On screen the distinction matters: a blank cell invites someone to fill
    it in, and a GI tag is a government certification that is not theirs to
    assert. It is `-` on every row, whatever else the row contains."""
    index = HAAT_COLUMNS.index("gi_region")

    empty = new_record("https://shop.example/p/1", Provenance.OWN)
    assert cell_depths(empty)[index] == "-"

    full = new_record("https://shop.example/p/2", Provenance.OWN)
    for name in HAAT_COLUMNS:
        if name != "gi_region" and hasattr(full, name):
            setattr(full, name, FieldValue.found("x", FieldSource.JSONLD, Confidence.HIGH))
    assert cell_depths(full)[index] == "-"
    assert "0" not in cell_depths(full).replace("-", "")
