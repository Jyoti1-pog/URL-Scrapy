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
from ..images.reasons import klass_of
from ..models import ProductRecord
from ..utils.atomic import atomic_text_writer

FAILED_COLUMNS: tuple[str, ...] = (
    "source_url",
    # `refused` or `failed`. First after the URL because it is the column an
    # operator filters on before pasting this file's URLs into a new job:
    # re-running a refusal produces the same refusal, so the two halves of this
    # file want completely different actions.
    "class",
    "reason",
    "stage",
    "http_status",
    # Which rungs the ladder tried and what each got, so a re-run can be
    # reasoned about without re-running: "h2 reset in 0.7s, h1.1 timed out at
    # 8s" says "this host refuses us" far more clearly than one word does.
    "rungs_tried",
    # v5 §5. What the row spent before giving up, split three ways. Next to
    # `rungs_tried` because the two answer one question together: what was
    # tried, and what it cost. `21s - fetch 19.8s` says "their shop is slow";
    # `21s - idle 19.0s` says "we waited because they asked us to".
    "time_spent",
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
        self._row(
            record.source_url,
            reason,
            record.fetch_stage.value,
            record.fetched_at,
            record.rungs_tried,
            record.http_status,
            record.time_spent,
        )

    def write_url(self, source_url: str, reason: str, stage: str = "not_attempted") -> None:
        """For URLs that never became a record at all -- robots-disallowed, or
        dropped when a job was cancelled before reaching them."""
        self._row(source_url, reason, stage, None, "", None, "")

    def _row(
        self,
        url: str,
        reason: str,
        stage: str,
        when: datetime | None,
        rungs: str = "",
        status: int | None = None,
        spent: str = "",
    ) -> None:
        assert self._writer is not None, "use FailedWriter as a context manager"
        self._writer.writerow(
            [
                url,
                klass_of(reason).value,
                reason,
                stage,
                str(status) if status else http_status_of(reason),
                rungs,
                spent,
                (when or datetime.now(UTC)).isoformat(timespec="seconds"),
            ]
        )
        self.written += 1
