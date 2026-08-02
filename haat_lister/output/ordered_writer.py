"""Rows out in the order they went in, without holding the batch in memory.

The brief asks for two things that pull against each other: rows in **input**
order, and an incremental write that never keeps the job in memory. With
concurrency 5, URL #7 routinely finishes before #3, so one of them has to bend.

The three ways out, and why this is the one:

  *Write on completion, sort at the end.* A 500-URL job that dies at #480 leaves
  a file in the wrong order, and a mid-run download is unsorted. Fails the
  second requirement outright.

  *Buffer completed records until their predecessor lands.* Correct, but if row
  3 sits on a 20-second timeout while 4-200 finish, the buffer holds 197 full
  ProductRecords -- description text, per-candidate validation results, image
  metadata. That is precisely the memory profile batch mode was built to avoid.

  *This.* Every row commits to the ledger the instant it completes, tagged with
  its input index. The writer holds a **watermark** and buffers only the
  *rendered CSV line* for rows that finished ahead of it -- roughly a kilobyte
  each, so the same 197-row stall costs ~200 KB of strings rather than ~20 MB of
  objects. When the watermark's row arrives, it and every buffered successor
  flush in one go.

The consequence worth noticing: the file on disk is always a correctly ordered
*prefix* of the finished job. Never a hole, never a row out of place. That is
what makes "download what's done so far" a valid CSV rather than a snapshot with
gaps, and it is why the same class serves the mid-run download, the final write
and the post-edit re-export.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType

from ..config import AppConfig
from ..models import ImageMode, ProductRecord, RowStatus
from ..utils.logging import get_logger
from .csv_writer import HaatCsvWriter, row_values

log = get_logger(__name__)


class OrderedCsvWriter:
    """Wraps HaatCsvWriter with a watermark. Same bytes, ordered arrival.

    Deliberately not a subclass: the header lock, the injection guard and the
    19-column contract all live in HaatCsvWriter and are none of this class's
    business. This only decides *when* a row is handed over.
    """

    def __init__(
        self,
        path: Path,
        cfg: AppConfig,
        mode: ImageMode,
        multi_image_columns: int = 0,
        checkpoint_every: int = 250,
    ) -> None:
        # Always a fresh file. This is a projection of the ledger, not an
        # accumulating log: a resumed job replays its finished rows through here
        # in index order, so appending to what was there would double them.
        path.unlink(missing_ok=True)
        self._writer = HaatCsvWriter(path, cfg, mode, multi_image_columns, ledger=None)
        self._cfg = cfg
        self._mode = mode
        self._multi = multi_image_columns
        self._checkpoint_every = checkpoint_every

        # Rendered lines only. See the module docstring on why not records.
        self._pending: dict[int, list[str]] = {}
        self._watermark = 0
        self.written = 0
        self.skipped_failed = 0
        self.peak_pending = 0

    @property
    def path(self) -> Path:
        return self._writer.path

    @property
    def columns(self) -> list[str]:
        return self._writer.columns

    def __enter__(self) -> OrderedCsvWriter:
        self._writer.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Whatever is still buffered belongs to rows whose predecessors never
        # arrived -- a cancelled or crashed job. Dropping them is right: writing
        # them now would put the file out of order, and the ledger still holds
        # every one of them for a resume or a re-export.
        if self._pending:
            log.info(
                "%d row(s) finished ahead of the watermark and were left for a resume: %s",
                len(self._pending),
                ", ".join(str(i) for i in sorted(self._pending)[:10]),
            )
        self._writer.__exit__(exc_type, exc, tb)

    # -- writing -----------------------------------------------------------

    def add(self, index: int, record: ProductRecord) -> int:
        """Offer a completed row. Returns how many rows this released.

        A `failed` record occupies its index without producing a line: it has no
        title, so there is nothing to import. It still advances the watermark --
        otherwise one bad URL would dam everything behind it.
        """
        if index < self._watermark:
            raise ValueError(f"row {index} arrived after the watermark passed it")

        if record.status is RowStatus.FAILED:
            self.skipped_failed += 1
            self._pending[index] = []
        else:
            self._pending[index] = row_values(record, self._cfg, self._mode, self._multi)

        self.peak_pending = max(self.peak_pending, len(self._pending))
        return self._flush_ready()

    def skip(self, index: int) -> int:
        """Account for an index that will never produce a row -- a duplicate, a
        robots-disallowed URL. Same damming problem, same fix."""
        if index < self._watermark:
            return 0
        self._pending[index] = []
        return self._flush_ready()

    def _flush_ready(self) -> int:
        released = 0
        while (values := self._pending.pop(self._watermark, None)) is not None:
            if values:
                self._writer.write_values(values)
                self.written += 1
                released += 1
                if self._checkpoint_every and self.written % self._checkpoint_every == 0:
                    self._writer.checkpoint()
            self._watermark += 1
        return released

    def checkpoint(self) -> None:
        """Make what has been written durable, then carry on appending."""
        self._writer.checkpoint()

    @property
    def watermark(self) -> int:
        """The next input index the file is waiting for."""
        return self._watermark

    @property
    def stalled_on(self) -> int | None:
        """Which row is holding the file up, if any. Worth surfacing: a single
        slow URL blocking 190 finished rows looks like a hang from outside."""
        return self._watermark if self._pending else None
