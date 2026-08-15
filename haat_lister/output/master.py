"""`runs/master.csv` -- one sheet that fills up.

The gap this closes: every job wrote its own `listings.csv`, so ten jobs over a
week left ten files to merge by hand. The operator's mental model is a
catalogue that grows, not a folder of exports.

    runs/master.csv          accumulates every completed job
    runs/j_xxxx/listings.csv per-job, unchanged, still the audit trail

FOUR PROPERTIES, in the order they matter:

1. **It is always a valid haat import file.** Same nineteen columns, same
   header bytes, no bookkeeping columns bolted on. An operator must be able to
   upload it at any moment without thinking about it. Everything this module
   needs to know that a CSV cannot carry -- which job a row came from, when --
   is derived from the ledger instead, which is where that belongs anyway.

2. **A URL appears once.** Deduped on the canonical `source_url` across the
   whole file, using the same `Identity` the planner and the record use. Re-run
   the same paste and the sheet does not grow.

3. **Append order, not job order.** The sheet reads as a history of what was
   added when, which is the only ordering an accumulating file can honestly
   claim.

4. **Never from a partial job.** Appended on completion only. A cancelled job's
   rows are real and downloadable from the job itself; folding half of them into
   the working sheet would leave an operator unable to tell which half.

WHY THE INDEX IS A SEPARATE FILE. Deduping needs the canonical URL of every row
already in the sheet, and the sheet does not carry URLs -- the haat columns have
nowhere to put one. Re-deriving it from the ledger on every append works but
couples the sheet to a database that may be rebuilt; a sidecar next to the file
keeps the pair self-describing, and it is regenerated from the ledger if lost.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..models import ProductRecord
from ..utils.atomic import atomic_text_writer
from ..utils.logging import get_logger
from . import with_images
from .csv_writer import HAAT_COLUMNS, HeaderMismatch, read_header, row_values

log = get_logger(__name__)

# What to do when a URL is already in the sheet.
SKIP, REPLACE, APPEND = "skip", "replace", "append"
ON_DUPLICATE = (SKIP, REPLACE, APPEND)


class SheetLocked(Exception):
    """The file is open in another program.

    Its own exception because on Windows this is the single most likely failure
    and it is entirely the operator's to fix. A PermissionError traceback tells
    them nothing; "close Excel and press retry" tells them everything.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(
            f"{path.name} is open in another program (Excel keeps a lock on it), so it could "
            f"not be updated.\n"
            f"Close it and try again -- nothing was lost. The job's own listings.csv is "
            f"already written and this sheet is rebuilt from it."
        )
        self.path = path


@dataclass
class MasterStats:
    """What one append did, for the operator to be told without going to look."""

    added: int = 0
    replaced: int = 0
    skipped: int = 0
    total: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.replaced)

    def summary(self) -> str:
        parts = []
        if self.added:
            parts.append(f"added {self.added} row{'' if self.added == 1 else 's'}")
        if self.replaced:
            parts.append(f"replaced {self.replaced}")
        if self.skipped:
            parts.append(f"{self.skipped} already there")
        head = ", ".join(parts) if parts else "nothing to add"
        return f"{head} — the sheet now has {self.total} row{'' if self.total == 1 else 's'}"


@dataclass
class _Index:
    """The sidecar: canonical URL -> row position, plus provenance for `--stats`.

    Deliberately not in the CSV. Master has to stay a valid haat import file at
    all times, and a twentieth column would mean an operator's upload silently
    carrying a field haat never asked for.
    """

    urls: list[str] = field(default_factory=list)
    jobs: dict[str, str] = field(default_factory=dict)  # canonical url -> job id
    added_at: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> _Index:
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt index is recoverable -- the CSV is the deliverable and
            # the worst case is a duplicate row an operator can see. Losing the
            # sheet over it would not be.
            log.warning("master index at %s is unreadable; starting a fresh one", path)
            return cls()
        return cls(
            urls=list(raw.get("urls") or []),
            jobs=dict(raw.get("jobs") or {}),
            added_at=dict(raw.get("added_at") or {}),
        )

    def save(self, path: Path) -> None:
        payload = {"urls": self.urls, "jobs": self.jobs, "added_at": self.added_at}
        with atomic_text_writer(path) as handle:
            json.dump(payload, handle, indent=1)

    def position(self, url: str) -> int | None:
        try:
            return self.urls.index(url)
        except ValueError:
            return None


def master_path(root: Path, cfg: AppConfig) -> Path:
    return root / cfg.paths.runs_dir / cfg.paths.master_csv


def master_with_images_path(root: Path, cfg: AppConfig) -> Path:
    """The accumulating companion. Same rows, same dedupe, same order -- it is
    written from the same records in the same call, so the two sheets cannot
    drift apart the way two separate appends would."""
    sheet = master_path(root, cfg)
    return sheet.with_name(f"{sheet.stem}_with_images{sheet.suffix}")


def index_path(sheet: Path) -> Path:
    return sheet.with_suffix(".index.json")


def _read_rows(sheet: Path, encoding: str) -> tuple[list[str], list[list[str]]]:
    """The header and the data rows, exactly as they sit on disk."""
    if not sheet.exists():
        return list(HAAT_COLUMNS), []
    with sheet.open("r", encoding=encoding, newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        return list(HAAT_COLUMNS), []
    return rows[0], [r for r in rows[1:] if r]


def append(
    records: list[ProductRecord],
    canonicals: list[str],
    sheet: Path,
    cfg: AppConfig,
    *,
    job_id: str = "",
    on_duplicate: str = SKIP,
    image_mode: Any = None,
    max_images: int = with_images.DEFAULT_MAX_IMAGES,
) -> MasterStats:
    """Fold one job's rows into the sheet. Atomic, or it does not happen.

    `canonicals` is parallel to `records` and carries the identity to dedupe on,
    passed in rather than recomputed so that the sheet keys on exactly what the
    planner and the ledger keyed on.

    Rewrites the whole file rather than appending in place. That costs one full
    write per job and buys two things worth more: `replace` can update a row
    without leaving a hole, and a half-written append can never corrupt the one
    file an operator actually depends on.
    """
    if on_duplicate not in ON_DUPLICATE:
        raise ValueError(f"on_duplicate must be one of {ON_DUPLICATE}, not {on_duplicate!r}")

    encoding = "utf-8-sig" if cfg.csv.excel_bom else "utf-8"
    sheet.parent.mkdir(parents=True, exist_ok=True)

    header, rows = _read_rows(sheet, encoding)
    if header and list(header) != list(HAAT_COLUMNS):
        raise HeaderMismatch(
            f"{sheet} does not have haat's 19 columns, so appending to it would produce a file "
            f"the importer rejects.\nFound: {header}\nExpected: {list(HAAT_COLUMNS)}"
        )

    index = _Index.load(index_path(sheet))
    if len(index.urls) != len(rows):
        # The pair got out of step -- a hand-edited sheet, an interrupted write,
        # a restored backup. The CSV is the deliverable, so it wins and the
        # index is rebuilt around it. Worst case is a duplicate row that an
        # operator can see and delete.
        log.warning(
            "master.csv has %d rows and its index has %d; trusting the CSV",
            len(rows),
            len(index.urls),
        )
        index.urls = (index.urls + [""] * len(rows))[: len(rows)]

    stats = MasterStats()
    now = datetime.now(UTC).isoformat(timespec="seconds")

    from ..models import ImageMode

    mode = image_mode if image_mode is not None else ImageMode.MANIFEST

    # The companion's rows, carried in lockstep with the sheet's. Read from disk
    # so a row added by an earlier job keeps its photo links, and padded if the
    # two files ever got out of step -- the sheet is the authority for length,
    # exactly as it is for the index.
    companion = master_with_images_path(sheet.parent.parent, cfg)
    image_rows = [list(r) for r in with_images.rows_of(companion, encoding)]
    blank = [""] * len(with_images.image_columns(max_images))
    while len(image_rows) < len(rows):
        image_rows.append([])

    for record, canonical in zip(records, canonicals, strict=True):
        values = row_values(record, cfg, mode)[: len(HAAT_COLUMNS)]
        extra = [*values, *with_images.image_values(record, cfg, max_images)]
        existing = index.position(canonical) if canonical else None

        if existing is None or on_duplicate == APPEND:
            rows.append(values)
            image_rows.append(extra)
            index.urls.append(canonical)
            index.jobs[canonical] = job_id
            index.added_at[canonical] = now
            stats.added += 1
        elif on_duplicate == REPLACE:
            # In place, so the sheet's order -- which is its history -- survives
            # a correction.
            rows[existing] = values
            if existing < len(image_rows):
                image_rows[existing] = extra
            index.jobs[canonical] = job_id
            stats.replaced += 1
        else:
            stats.skipped += 1

    _write(sheet, rows, cfg, encoding)
    _write_companion(companion, rows, image_rows, blank, cfg, encoding, max_images)
    index.save(index_path(sheet))
    stats.total = len(rows)
    return stats


def _write_companion(
    path: Path,
    rows: list[list[str]],
    image_rows: list[list[str]],
    blank: list[str],
    cfg: AppConfig,
    encoding: str,
    max_images: int,
) -> None:
    """The accumulating `_with_images` sheet, exactly as long as the sheet.

    A row whose companion entry is missing -- because it predates this file --
    is written with its nineteen columns and empty image columns rather than
    skipped. A companion that is shorter than the sheet would silently drop
    products, which is the one thing neither file may do.
    """
    padded = [
        (image_rows[i] if i < len(image_rows) and image_rows[i] else [*row, *blank])
        for i, row in enumerate(rows)
    ]
    with atomic_text_writer(path, encoding=encoding) as handle:
        writer = csv.writer(
            handle,
            lineterminator=cfg.csv.line_terminator,
            quoting=csv.QUOTE_ALL if cfg.csv.quote_all else csv.QUOTE_MINIMAL,
        )
        handle.write(",".join(with_images.header(max_images)) + cfg.csv.line_terminator)
        writer.writerows(padded)


def _write(sheet: Path, rows: list[list[str]], cfg: AppConfig, encoding: str) -> None:
    """Whole file, atomically, with the header byte-identical to a job's."""
    try:
        with atomic_text_writer(sheet, encoding=encoding) as handle:
            writer = csv.writer(
                handle,
                lineterminator=cfg.csv.line_terminator,
                quoting=csv.QUOTE_ALL if cfg.csv.quote_all else csv.QUOTE_MINIMAL,
            )
            # Unquoted, matching `HaatCsvWriter._write_header` and the template.
            handle.write(",".join(HAAT_COLUMNS) + cfg.csv.line_terminator)
            writer.writerows(rows)
    except PermissionError as exc:
        raise SheetLocked(sheet) from exc


@dataclass
class SheetSummary:
    """`master --stats`, and the Sheet screen."""

    path: Path
    exists: bool = False
    rows: int = 0
    jobs: int = 0
    first_added: str = ""
    last_added: str = ""
    bytes: int = 0
    header_ok: bool = False


def stats(sheet: Path, cfg: AppConfig) -> SheetSummary:
    summary = SheetSummary(path=sheet)
    if not sheet.exists():
        return summary

    encoding = "utf-8-sig" if cfg.csv.excel_bom else "utf-8"
    header, rows = _read_rows(sheet, encoding)
    index = _Index.load(index_path(sheet))
    dates = sorted(index.added_at.values())

    summary.exists = True
    summary.rows = len(rows)
    summary.jobs = len({j for j in index.jobs.values() if j})
    summary.first_added = dates[0] if dates else ""
    summary.last_added = dates[-1] if dates else ""
    summary.bytes = sheet.stat().st_size
    summary.header_ok = read_header(sheet, encoding) == list(HAAT_COLUMNS)
    return summary


def preview(sheet: Path, cfg: AppConfig, limit: int = 25) -> list[list[str]]:
    """The first rows, for the Sheet screen. Never the whole file."""
    encoding = "utf-8-sig" if cfg.csv.excel_bom else "utf-8"
    _, rows = _read_rows(sheet, encoding)
    return rows[:limit]
