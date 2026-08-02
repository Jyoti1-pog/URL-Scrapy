"""failed.csv -- the URLs that produced nothing, and why.

Designed to be **re-runnable**: the first column is the URL exactly as the
operator pasted it, so this file's URL column can be selected and pasted straight
into a new job. A failure file you have to reformat before you can retry it is a
report; this one is a work item.

Kept separate from review.csv on purpose. review.csv is a worklist of rows that
exist and need attention; this is a list of rows that do not exist at all. An
operator clearing review.csv is editing; an operator clearing this is re-running.
"""

from __future__ import annotations

import csv
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from ..config import AppConfig
from ..models import ProductRecord
from ..utils.atomic import atomic_text_writer

FAILED_COLUMNS: tuple[str, ...] = (
    "source_url",
    "reason",
    "stage",
    "http_status",
    "attempted_at",
)

# `reason` values already carry the status for HTTP failures (http_404,
# http_503). Splitting it back out gives an operator a sortable column without
# asking the fetcher to report the same thing twice.
_HTTP_PREFIX = "http_"


def http_status_of(reason: str) -> str:
    if reason.startswith(_HTTP_PREFIX):
        tail = reason[len(_HTTP_PREFIX) :]
        return tail if tail.isdigit() else ""
    return ""


class FailedWriter:
    """Rewritten whole each run, like review.csv: a stale failure list is worse
    than none, because it sends an operator to re-run URLs that now work."""

    def __init__(self, path: Path, cfg: AppConfig) -> None:
        self.path = path
        self._cfg = cfg
        self.written = 0
        self._handle: IO[str] | None = None
        self._writer: Any = None
        self._cm: AbstractContextManager[IO[str]] | None = None

    def __enter__(self) -> FailedWriter:
        self._cm = atomic_text_writer(self.path)
        self._handle = self._cm.__enter__()
        self._writer = csv.writer(
            self._handle,
            lineterminator=self._cfg.csv.line_terminator,
            quoting=csv.QUOTE_MINIMAL,
        )
        self._writer.writerow(FAILED_COLUMNS)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._cm is not None:
            self._cm.__exit__(exc_type, exc, tb)

    def write(self, record: ProductRecord) -> None:
        reason = record.failure_reason or "unknown"
        self._row(record.source_url, reason, record.fetch_stage.value, record.fetched_at)

    def write_url(self, source_url: str, reason: str, stage: str = "not_attempted") -> None:
        """For URLs that never became a record at all -- robots-disallowed, or
        dropped when a job was cancelled before reaching them."""
        self._row(source_url, reason, stage, None)

    def _row(self, url: str, reason: str, stage: str, when: datetime | None) -> None:
        assert self._writer is not None, "use FailedWriter as a context manager"
        self._writer.writerow(
            [
                url,
                reason,
                stage,
                http_status_of(reason),
                (when or datetime.now(UTC)).isoformat(timespec="seconds"),
            ]
        )
        self.written += 1
