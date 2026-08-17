"""`listings_with_images.csv` -- the same rows, plus every photo link.

haat's template is nineteen columns and none of them is an image, so the file
that imports cannot be the file that carries photo URLs. Adding a twentieth
column would break the import; leaving the URLs out entirely is why an operator
keeps asking where their image links went.

So there are two files, written from the same ledger rows in the same order:

    listings.csv               19 columns, header-locked   -> upload this to haat
    listings_with_images.csv   19 columns + image columns   -> for your own records

THE HEADER IS STABLE ACROSS RUNS. A job whose products have one photo each and a
job whose products have six produce the *same* header -- `image_1 … image_N` are
always present, empty where a product has fewer. That is not tidiness: an
operator building a spreadsheet up over weeks cannot paste two files together if
the columns move, and a header that changes shape between jobs is a file you
have to fix by hand every time.

ROW-FOR-ROW CORRESPONDENCE IS ASSERTED, not assumed. The two files are built
from one pass over one ordered source, and a test checks `source_url` lines up
between them. Two exports that disagree about which product is on line 40 would
be worse than one export.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable, Iterator
from pathlib import Path

from ..config import AppConfig
from ..models import ImageMode, ProductRecord
from ..utils.atomic import atomic_text_writer
from ..utils.logging import get_logger
from .csv_writer import HAAT_COLUMNS, _clean, row_values

log = get_logger(__name__)

# How many `image_N` columns. Ten, matching `images.max_images_per_product`, so
# a row that found ten photographs has ten cells to put them in -- at six, four
# of them existed only in the manifest and an operator filling gaps by hand had
# nowhere to type them.
#
# Fixed width on purpose: a job whose products have one photo and a job whose
# products have ten produce the same header, so the two files concatenate.
DEFAULT_MAX_IMAGES = 10

# Appended AFTER the nineteen, never interleaved. Anything that reads the first
# nineteen columns of this file gets exactly `listings.csv`.
def image_columns(max_images: int = DEFAULT_MAX_IMAGES) -> tuple[str, ...]:
    return (
        "source_url",
        "image_url",
        *(f"image_{i}" for i in range(1, max_images + 1)),
        "image_count",
        # What we found and did NOT use, so a row with no photo is still a
        # starting point rather than a dead end. These used to be mixed into
        # `image_1..N` and counted in `image_count`, which is how a five-photo
        # product reported 55. Named for what they are, they are useful: when
        # the automatic path gives you nothing, these are the URLs it looked at
        # and the `image_reason` says why each was refused.
        "rejected_image_urls",
        "image_method",
        "image_reason",
        "image_width",
        "image_height",
        "local_image_path",
    )


def header(max_images: int = DEFAULT_MAX_IMAGES) -> tuple[str, ...]:
    return (*HAAT_COLUMNS, *image_columns(max_images))


def _all_image_urls(record: ProductRecord) -> list[str]:
    """The PHOTOGRAPHS this row has, best first.

    Two things this deliberately no longer does.

    It used to append `record.image_candidates` -- every URL the extractor
    considered, most of them never validated and some of them rejected. The
    docstring called them "labelled as such by their position", which nothing
    in the file actually says, and `image_count` then reported the lot: a
    two-product job read `image_count 42` and `55` for products with eight and
    five photographs. A count of guesses under a column called `image_count` is
    the same defect as a count of URLs under a column called photos.

    And it counted size variants separately. `71rOScyvhRL.jpg` and
    `71rOScyvhRL._SL1500_.jpg` are one photograph at two resolutions, so
    `photo_identity` collapses them and the first (best-ranked) survives.
    """
    from ..extract.images import photo_identity

    urls: list[str] = []
    if record.image.url:
        urls.append(record.image.url)
    urls.extend(f.hosted_url or f.original_source_url for f in record.image.files)

    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if not url:
            continue
        key = photo_identity(url)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(url)
    return ordered


def _rejected_urls(record: ProductRecord) -> list[str]:
    """Candidates that were considered and not used, best-ranked first.

    Deliberately not deduplicated by photograph: if the full-size guess was
    refused and the published URL was not tried, an operator working by hand
    wants to see both.
    """
    used = {url for url in _all_image_urls(record)}
    return [url for url in record.image_candidates if url and url not in used]


def image_values(
    record: ProductRecord, cfg: AppConfig, max_images: int = DEFAULT_MAX_IMAGES
) -> list[str]:
    """The appended columns for one row, always the same length."""
    urls = _all_image_urls(record)
    hero = record.image.files[0] if record.image.files else None
    winner = next((r for r in record.image.candidate_results if r.ok), None)

    width = hero.width if hero else (winner.width if winner else None)
    height = hero.height if hero else (winner.height if winner else None)

    prefixes = cfg.csv.injection_prefixes
    padded = (urls[:max_images] + [""] * max_images)[:max_images]

    return [
        _clean(record.source_url, prefixes),
        _clean(record.image.url, prefixes),
        *(_clean(u, prefixes) for u in padded),
        str(len(urls)),
        _clean(" | ".join(_rejected_urls(record)), prefixes),
        record.image.method.value,
        # The closed-enum word where there is one, the diagnostic string
        # otherwise. An operator filtering this column wants to group on it.
        _clean(
            record.image.none_reason.value if record.image.none_reason else record.image.reason,
            prefixes,
        ),
        str(width) if width else "",
        str(height) if height else "",
        _clean(" | ".join(f.local_path for f in record.image.files), prefixes),
    ]


def write(
    path: Path,
    records: Iterable[ProductRecord],
    cfg: AppConfig,
    mode: ImageMode,
    max_images: int = DEFAULT_MAX_IMAGES,
) -> int:
    """Write the companion file. Atomic, same guards as the import file.

    Takes an iterable of records in the order they should appear -- the caller
    owns the ordering, because the caller is the thing that also writes
    `listings.csv` and the two must not each decide separately.
    """
    written = 0
    encoding = "utf-8-sig" if cfg.csv.excel_bom else "utf-8"

    with atomic_text_writer(path, encoding=encoding) as handle:
        writer = csv.writer(
            handle,
            lineterminator=cfg.csv.line_terminator,
            quoting=csv.QUOTE_ALL if cfg.csv.quote_all else csv.QUOTE_MINIMAL,
        )
        # Unquoted header, matching listings.csv byte for byte across its first
        # nineteen fields.
        handle.write(",".join(header(max_images)) + cfg.csv.line_terminator)
        for record in records:
            writer.writerow(
                [
                    *row_values(record, cfg, mode)[: len(HAAT_COLUMNS)],
                    *image_values(record, cfg, max_images),
                ]
            )
            written += 1
    return written


def rows_of(path: Path, encoding: str = "utf-8") -> Iterator[list[str]]:
    """Data rows, for the correspondence test and the download panel's count."""
    if not path.exists():
        return
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)
        for row in reader:
            if row:
                yield row
