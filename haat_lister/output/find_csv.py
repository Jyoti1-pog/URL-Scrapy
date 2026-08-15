"""Reading an operator's own CSV, and writing `image_links.csv` back.

TWO HALVES OF ONE PROMISE. An operator who has a spreadsheet of products wants
their own SKU column to come back attached to the photo links -- that mapping is
usually the entire reason they have a CSV rather than a list of links. So the
reader carries every other column through, and the writer puts them back.

COLUMN DETECTION IS A GUESS, SHOWN AS ONE. The URL column is picked by counting
which column holds the most URL-shaped cells, the choice is reported, and the
operator can override it. A silent guess about which column is the link is a
job that runs over the wrong data and looks like it worked.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

from ..config import AppConfig
from ..find import FindRow
from ..utils.atomic import atomic_text_writer
from ..utils.logging import get_logger
from ..utils.urls import extract_urls

log = get_logger(__name__)

# Enough rows to be confident about which column holds the links without
# reading a 10,000-row file to answer a structural question.
SNIFF_ROWS = 200


@dataclass
class ParsedTable:
    """An uploaded file, understood."""

    columns: list[str] = field(default_factory=list)
    url_column: str = ""
    # Why that column: the count that won, so the choice can be argued with.
    url_column_hits: int = 0
    rows: list[dict[str, str]] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    extras: list[dict[str, str]] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    delimiter: str = ","
    had_header: bool = True

    @property
    def ok(self) -> bool:
        return bool(self.urls)


def _sniff_delimiter(text: str) -> str:
    """Comma, tab or semicolon. `csv.Sniffer` guesses wrong on files whose only
    delimiter appears inside a URL, so the candidates are counted directly."""
    head = "\n".join(text.splitlines()[:20])
    counts = {d: head.count(d) for d in (",", "\t", ";", "|")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def _url_like(value: str) -> bool:
    return bool(extract_urls(value).urls)


def read_table(text: str, url_column: str | None = None) -> ParsedTable:
    """Parse an uploaded .csv/.tsv/.txt into URLs plus their other columns.

    A file with no delimiter and no header -- a plain list of links -- is a
    legitimate and common shape, and falls through to the same URL extractor
    the textarea uses. One parser, as everywhere else.
    """
    table = ParsedTable()
    if not text.strip():
        return table

    table.delimiter = _sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text), delimiter=table.delimiter)
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        return table

    header = [cell.strip() for cell in rows[0]]
    # A header row is one whose cells are labels rather than links. If the first
    # row already contains a URL it is data, and the columns get positional
    # names so the operator still gets their extra fields back.
    table.had_header = not any(_url_like(cell) for cell in header)
    if table.had_header:
        table.columns = [name or f"column_{i + 1}" for i, name in enumerate(header)]
        body = rows[1:]
    else:
        table.columns = [f"column_{i + 1}" for i in range(len(header))]
        body = rows

    table.rows = [
        {
            table.columns[i]: (cell.strip() if i < len(table.columns) else "")
            for i, cell in enumerate(row[: len(table.columns)])
        }
        for row in body
    ]

    table.url_column, table.url_column_hits = _pick_url_column(table, url_column)

    for row in table.rows:
        raw = row.get(table.url_column, "")
        found = extract_urls(raw)
        if not found.urls:
            if raw.strip():
                table.unparsed.append(raw[:200])
            continue
        # One row, one product: a cell holding several links is a spreadsheet
        # accident far more often than a deliberate list, so the first wins and
        # the rest are reported rather than silently expanded into new rows the
        # operator's other columns would not line up with.
        table.urls.append(found.urls[0].url)
        table.extras.append({k: v for k, v in row.items() if k != table.url_column})
        if len(found.urls) > 1:
            table.unparsed.extend(u.url for u in found.urls[1:])

    return table


def _pick_url_column(table: ParsedTable, requested: str | None) -> tuple[str, int]:
    """The column with the most URL-shaped cells, or the one asked for."""
    if requested and requested in table.columns:
        hits = sum(1 for row in table.rows if _url_like(row.get(requested, "")))
        return requested, hits

    best, best_hits = "", 0
    for name in table.columns:
        hits = sum(1 for row in table.rows[:SNIFF_ROWS] if _url_like(row.get(name, "")))
        if hits > best_hits:
            best, best_hits = name, hits
    return (best or (table.columns[0] if table.columns else "")), best_hits


# ---------------------------------------------------------------------------
# image_links.csv
# ---------------------------------------------------------------------------

# Everything except the image URLs, which are generated per-photo below.
BASE_COLUMNS: tuple[str, ...] = (
    "source_url",
    "title",
    "image_count",
    "width",
    "height",
    "method",
    "reason",
    "price_found",
    "currency",
    "category_guess",
    "weight_g",
    "dimensions_cm",
    "description",
)


def image_columns(count: int) -> list[str]:
    """`image_url_1 ... image_url_N`, one photograph per cell.

    They used to share a single `all_image_urls` cell joined by ` | `. That is
    fine for a machine and useless in a spreadsheet: ten links in one cell
    cannot be sorted, filtered, clicked, or pasted into a bulk-upload column
    without splitting them by hand, every time.

    The width is fixed by `images.max_images_per_product` so every row has the
    same shape -- a ragged CSV is not a CSV. Rows with fewer photos leave the
    remaining cells empty, which is what a spreadsheet expects.
    """
    return [f"image_url_{n}" for n in range(1, count + 1)]


def write_image_links(
    path: Path,
    rows: list[FindRow],
    cfg: AppConfig,
    extra_columns: list[str] | None = None,
) -> int:
    """The find's own export. Not a haat import file and not pretending to be.

    The operator's own columns come first when they supplied any, because the
    file is only useful if they can line it up against the spreadsheet they
    started with.
    """
    from .csv_writer import _clean

    prefixes = cfg.csv.injection_prefixes
    extras = extra_columns or []
    # Never narrower than the widest row: a photo that exists and has no column
    # to sit in is a photo silently dropped.
    width = max(
        cfg.images.max_images_per_product,
        max((r.image_count for r in rows), default=0),
    )
    photo_columns = image_columns(width)
    header = (*extras, *BASE_COLUMNS, *photo_columns)

    with atomic_text_writer(path, encoding="utf-8-sig" if cfg.csv.excel_bom else "utf-8") as fh:
        writer = csv.writer(
            fh,
            lineterminator=cfg.csv.line_terminator,
            quoting=csv.QUOTE_ALL if cfg.csv.quote_all else csv.QUOTE_MINIMAL,
        )
        fh.write(",".join(header) + cfg.csv.line_terminator)
        for row in sorted(rows, key=lambda r: r.index):
            writer.writerow(
                [
                    *(_clean(row.extra.get(name, ""), prefixes) for name in extras),
                    _clean(row.source_url, prefixes),
                    _clean(row.title, prefixes),
                    str(row.image_count),
                    str(row.width or ""),
                    str(row.height or ""),
                    row.method,
                    _clean(row.reason, prefixes),
                    _clean(row.price, prefixes),
                    _clean(row.currency, prefixes),
                    _clean(row.category, prefixes),
                    str(row.weight_g or ""),
                    _clean(row.dimensions, prefixes),
                    _clean(row.description, prefixes),
                    *(
                        _clean(url, prefixes)
                        for url in _padded(row.usable_image_urls(), width)
                    ),
                ]
            )
    return len(rows)


def _padded(urls: list[str], width: int) -> list[str]:
    return [*urls[:width], *([""] * max(0, width - len(urls)))]
