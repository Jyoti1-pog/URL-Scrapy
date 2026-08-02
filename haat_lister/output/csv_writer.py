"""The haat import file. This is the ONLY module that knows column names.

Everything upstream speaks ProductRecord; the mapping to 19 named columns lives
here and nowhere else.

BYTE SHAPE, measured from haat-bulk-listings-template.csv rather than assumed:
  - header unquoted
  - data rows minimally quoted (only fields containing a comma carry quotes)
  - CRLF line endings, trailing newline
  - UTF-8, no BOM

Note that this contradicts the spec's instruction to write QUOTE_ALL: doing so
emits `"title","description",...`, which is not byte-identical to the template
header the spec ALSO requires. The template wins, because it is evidence about
the real importer. `csv.quote_all` remains available in config for operators who
need it, and the header is written unquoted regardless. No safety is lost --
CSV injection is handled by the apostrophe guard below, which quoting never
addressed.
"""

from __future__ import annotations

import csv
import re
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from ..config import AppConfig
from ..models import ImageMode, ProductRecord, RowStatus
from ..store.ledger import Ledger
from ..utils.atomic import atomic_text_writer
from ..utils.logging import get_logger

log = get_logger(__name__)

# The contract. Order is fixed; do not reorder, rename, add or drop.
HAAT_COLUMNS: tuple[str, ...] = (
    "title",
    "description",
    "category_slug",
    "subcategory_slug",
    "custom_category",
    "price_inr",
    "hs_code",
    "weight_g",
    "length_cm",
    "width_cm",
    "height_cm",
    "availability",
    "stock_qty",
    "sizes",
    "gi_region",
    "rfq_enabled",
    "rfq_min_qty",
    "bulk_only",
    "seller_note",
)

# A GI tag is an Indian government certification and haat's form makes it a
# seller-ticked declaration. The extractor's model has no gi_region field, so
# this constant is the only thing that can ever reach that column.
GI_REGION_ALWAYS_BLANK = ""

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class HeaderMismatch(Exception):
    """The file on disk is not the file we would be appending to."""


def escape_csv_injection(value: str, prefixes: list[str]) -> str:
    """Stop a spreadsheet executing a cell.

    `=cmd|'/c calc'!A1` in a product title is a formula in Excel and Sheets. A
    leading apostrophe forces it to text. Our numeric columns are non-negative
    integers, so this only ever touches free text.
    """
    if value and any(value.startswith(p) for p in prefixes):
        return "'" + value
    return value


def _clean(value: str, prefixes: list[str]) -> str:
    return escape_csv_injection(_CONTROL_CHARS.sub("", value), prefixes)


def image_columns(mode: ImageMode, multi: int = 0) -> list[str]:
    """Extra columns, appended only in URL modes.

    In `manifest` mode this returns nothing and the 19-column contract is
    emitted untouched -- which, given haat's uploader takes files, is the case
    that actually matters.
    """
    if not mode.need_url:
        return []
    return ["image_url", *(f"image_{i}" for i in range(1, multi + 1))]


def row_values(record: ProductRecord, cfg: AppConfig, mode: ImageMode, multi: int = 0) -> list[str]:
    """ProductRecord -> one row of strings, in column order."""
    prefixes = cfg.csv.injection_prefixes

    def text(name: str) -> str:
        field = getattr(record, name)
        return _clean(str(field.value), prefixes) if field.is_present else ""

    def number(name: str) -> str:
        field = getattr(record, name)
        return str(int(field.value)) if field.is_present else ""

    values = [
        text("title"),
        text("description"),
        text("category_slug"),
        text("subcategory_slug"),
        text("custom_category"),
        number("price_inr"),
        text("hs_code"),
        number("weight_g"),
        number("length_cm"),
        number("width_cm"),
        number("height_cm"),
        text("availability"),
        number("stock_qty"),
        text("sizes"),
        GI_REGION_ALWAYS_BLANK,
        text("rfq_enabled"),
        number("rfq_min_qty"),
        text("bulk_only"),
        text("seller_note"),
    ]
    assert len(values) == len(HAAT_COLUMNS), "row width drifted from the locked header"

    if mode.need_url:
        values.append(record.image.url)
        hosted = [f.hosted_url or "" for f in record.image.files]
        values.extend((hosted[i] if i < len(hosted) else "") for i in range(multi))

    return values


# The console's fill grid, encoded one character per column, in header order.
# Lives here because this module is the only one that knows the column order,
# and a grid drawn against a different order would be a picture of the wrong
# thing.
#
#   3 high   2 medium   1 low   0 nothing   - locked (gi_region, always)
#
# A string rather than a dict: at 500 rows this is 9.5 KB on the wire instead of
# 500 objects, and the console indexes it by position anyway.
_DEPTH_BY_CONFIDENCE = {"high": "3", "medium": "2", "low": "1", "none": "0"}


def cell_depths(record: ProductRecord) -> str:
    """One character per CSV column: how deeply dyed that cell is.

    `gi_region` is `-` on every row without exception -- not empty, *locked*.
    The distinction matters on screen: a blank cell invites someone to fill it
    in, and this one is not theirs to fill.
    """
    fields = record.field_values()
    out = []
    for name in HAAT_COLUMNS:
        if name == "gi_region":
            out.append("-")
            continue
        value = fields.get(name)
        if value is None or not value.is_present:
            out.append("0")
        else:
            out.append(_DEPTH_BY_CONFIDENCE.get(value.confidence.value, "1"))
    assert len(out) == len(HAAT_COLUMNS)
    return "".join(out)


def read_header(path: Path, encoding: str = "utf-8") -> list[str] | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open("r", encoding=encoding, newline="") as handle:
        for row in csv.reader(handle):
            return row
    return None


class HaatCsvWriter:
    """Header-locked writer. Appends only after proving the header matches."""

    def __init__(
        self,
        path: Path,
        cfg: AppConfig,
        mode: ImageMode,
        multi_image_columns: int = 0,
        ledger: Ledger | None = None,
    ) -> None:
        self.path = path
        self._cfg = cfg
        self._mode = mode
        self._multi = multi_image_columns
        self._ledger = ledger
        self.columns = [*HAAT_COLUMNS, *image_columns(mode, multi_image_columns)]

        self.written = 0
        self.skipped_duplicates = 0
        self.skipped_failed = 0

        self._handle: IO[str] | None = None
        # csv.writer is a factory function, not a class, so it cannot annotate.
        self._writer: Any = None
        self._cm: AbstractContextManager[IO[str]] | None = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> HaatCsvWriter:
        existing = read_header(self.path)
        if existing is not None and existing != self.columns:
            raise HeaderMismatch(
                f"{self.path} already exists with a different header.\n"
                f"  on disk: {','.join(existing)}\n"
                f"  ours:    {','.join(self.columns)}\n"
                "Appending would corrupt the file. Move it aside, or re-run with the same "
                "--images mode that produced it."
            )

        self._open(seed=existing is not None)
        if existing is None:
            self._write_header()
        return self

    def _open(self, *, seed: bool) -> None:
        self._cm = atomic_text_writer(self.path, seed_from_existing=seed)
        self._handle = self._cm.__enter__()
        self._writer = csv.writer(
            self._handle,
            lineterminator=self._cfg.csv.line_terminator,
            quoting=csv.QUOTE_ALL if self._cfg.csv.quote_all else csv.QUOTE_MINIMAL,
        )

    def checkpoint(self) -> None:
        """Commit what has been written so far, then carry on appending.

        Between checkpoints the new rows live only in `listings.csv.tmp`, which
        is exactly right for a crash -- the operator's existing file is never
        half-written -- but it means a hard kill four thousand rows into a batch
        throws all four thousand away. Batch mode calls this periodically so the
        worst case is the rows since the last checkpoint.

        Cheap because it is rare: one fsync and one copy-forward of a file that
        tops out at a few megabytes at this project's row ceiling.
        """
        if self._cm is None:
            return
        self._cm.__exit__(None, None, None)
        self._open(seed=True)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._cm is not None:
            self._cm.__exit__(exc_type, exc, tb)

    def _write_header(self) -> None:
        """Always minimally quoted, whatever `quote_all` says: byte-identity
        with the template's header is a hard requirement."""
        assert self._handle is not None
        if self._cfg.csv.excel_bom:
            self._handle.write("﻿")
        header_writer = csv.writer(
            self._handle,
            lineterminator=self._cfg.csv.line_terminator,
            quoting=csv.QUOTE_MINIMAL,
        )
        header_writer.writerow(self.columns)

    # -- writing -----------------------------------------------------------

    def write(self, record: ProductRecord) -> bool:
        """Write one row. Returns False when the row was deliberately skipped.

        A `failed` row is never written: it has no title, so there is nothing to
        import. It still reaches review.csv, which is where failures belong.
        """
        assert self._writer is not None, "use HaatCsvWriter as a context manager"

        if record.status is RowStatus.FAILED:
            self.skipped_failed += 1
            return False

        if self._ledger is not None and self._ledger.has_row(record.canonical_url):
            self.skipped_duplicates += 1
            log.debug("Skipping duplicate %s", record.canonical_url)
            return False

        self.write_values(row_values(record, self._cfg, self._mode, self._multi))
        if self._ledger is not None:
            self._ledger.record_row(record)
        return True

    def write_values(self, values: list[str]) -> None:
        """Write a pre-rendered row. The seam OrderedCsvWriter needs.

        It buffers rendered lines rather than records, so by the time a row is
        released the record it came from may be long gone.
        """
        assert self._writer is not None, "use HaatCsvWriter as a context manager"
        assert len(values) == len(self.columns), "row width drifted from the locked header"
        self._writer.writerow(values)
        self.written += 1
