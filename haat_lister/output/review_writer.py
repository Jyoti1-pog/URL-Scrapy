"""review.csv -- the human worklist.

Treated as a first-class deliverable, not debug output. Since haat's listing
creator marks price, HS code, weight and all three dimensions as required, most
rows will legitimately reach the CSV with those cells empty -- and this file is
the only thing telling an operator which ones, and why.

The goal an operator should be able to meet: clear this file without reopening a
single source page, except for price, which is a business decision nobody can
scrape.
"""

from __future__ import annotations

import csv
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from ..config import AppConfig
from ..models import Confidence, ProductRecord, RowStatus
from ..utils.atomic import atomic_text_writer

REVIEW_COLUMNS: tuple[str, ...] = (
    "row_key",
    "source_url",
    # Only populated when cleaning shortened it, so a bad cut is visible here
    # rather than only on the page.
    "title_original",
    "provenance",
    "status",
    "policy_flags",
    "missing_required",
    "low_confidence_fields",
    "source_price",
    "source_currency",
    "fx_rate_used",
    "suggested_hs_code",
    "gi_mention_found",
    "image_method",
    # The closed-enum word (§4.6) beside the diagnostic detail. Two columns
    # because an operator sorts and filters on the first and reads the second
    # only when the first is not enough.
    "image_problem",
    "image_reason",
    "local_image_path",
    "fetch_stage",
    "notes",
)


def extracted_anything(record: ProductRecord) -> bool:
    """Did this row ever produce content?

    A URL that was refused or never answered has an empty record. Everything
    downstream -- missing-field counts, review rows, the flag badge -- is a
    statement about a record that does not exist, and rendering it produced
    `⚑10` on three rows that had nothing to flag.
    """
    return any(field.is_present for field in record.field_values().values())


def missing_required(record: ProductRecord, cfg: AppConfig) -> list[str]:
    """Fields haat's own form marks with an asterisk that we could not fill.

    Blank is a legitimate CSV value, but a blank here stops the seller
    publishing, so these are the rows an operator must work through.

    EMPTY when the row extracted nothing. A record with no content has no
    field-level problems -- it has one reason, and that reason is the only true
    thing that can be said about it. Counting ten missing fields on a page that
    never loaded tells an operator to go and fill in a title for something they
    cannot see.
    """
    if not extracted_anything(record):
        return []
    fields = record.field_values()
    return [
        name
        for name in cfg.fields.required_by_haat
        if name in fields and not fields[name].is_present
    ]


def low_confidence_fields(record: ProductRecord) -> list[str]:
    """Populated, but not to be trusted without a glance."""
    return [
        f"{name}:{field.confidence.value}"
        for name, field in record.field_values().items()
        if field.is_present and field.confidence in (Confidence.LOW, Confidence.MEDIUM)
    ]


def needs_review(record: ProductRecord, cfg: AppConfig) -> bool:
    """Rows that reached listings.csv and still want a decision.

    A FAILED row is never one of them, and that is the fix for the six-rows-from-
    three-inputs bug: this used to return True for `status is not OK`, so every
    refused and every failed URL was written into review.csv as well as
    failed.csv, and then counted in both.

    review.csv is a POINTER INTO listings.csv, not a second list of problems.
    If a row is not in listings.csv it cannot be pointed at, and there is
    nothing on it for a human to decide -- the reason on the failed row is the
    whole story.
    """
    if record.status is RowStatus.FAILED or not extracted_anything(record):
        return False
    return bool(
        record.status is not RowStatus.OK
        or record.policy_flags
        or record.notes
        or record.gi_mention_found
        or missing_required(record, cfg)
        or low_confidence_fields(record)
    )


def review_row(record: ProductRecord, cfg: AppConfig) -> list[str]:
    hs = record.hs_code
    return [
        record.row_key,
        record.source_url,
        record.title_original if record.title_original != (record.title.value or "") else "",
        record.provenance.value,
        record.status.value,
        " | ".join(record.policy_flags),
        " | ".join(missing_required(record, cfg)),
        " | ".join(low_confidence_fields(record)),
        "" if record.source_price is None else str(record.source_price),
        record.source_currency or "",
        "" if record.fx_rate_used is None else f"{record.fx_rate_used} @ {record.fx_rate_as_of}",
        str(hs.value) if hs.is_present else "",
        record.gi_mention_found or "",
        record.image.method.value,
        record.image.none_reason.value if record.image.none_reason else "",
        record.image.reason,
        " | ".join(f.local_path for f in record.image.files),
        record.fetch_stage.value,
        " | ".join(record.notes) or (record.failure_reason or ""),
    ]


class ReviewWriter:
    """Rewritten whole each run, unlike listings.csv which appends.

    A worklist that accumulated stale rows from previous runs would be worse
    than useless -- an operator would not know which entries still apply.
    """

    def __init__(self, path: Path, cfg: AppConfig) -> None:
        self.path = path
        self._cfg = cfg
        self.written = 0
        self._handle: IO[str] | None = None
        self._writer: Any = None
        self._cm: AbstractContextManager[IO[str]] | None = None

    def __enter__(self) -> ReviewWriter:
        self._cm = atomic_text_writer(self.path)
        self._handle = self._cm.__enter__()
        self._writer = csv.writer(
            self._handle,
            lineterminator=self._cfg.csv.line_terminator,
            quoting=csv.QUOTE_MINIMAL,
        )
        self._writer.writerow(REVIEW_COLUMNS)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._cm is not None:
            self._cm.__exit__(exc_type, exc, tb)

    def write(self, record: ProductRecord) -> bool:
        assert self._writer is not None, "use ReviewWriter as a context manager"
        if not needs_review(record, self._cfg):
            return False
        self._writer.writerow(review_row(record, self._cfg))
        self.written += 1
        return True
