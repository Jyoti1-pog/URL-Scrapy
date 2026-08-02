"""image_manifest.csv -- the map from rows to image files.

In `manifest` mode this is how a human (or a later uploader script) knows which
photos belong to which listing, and in what order. The first file is the hero,
which is what haat's uploader calls "the first photo buyers see everywhere".
"""

from __future__ import annotations

import csv
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from ..config import AppConfig
from ..models import ProductRecord
from ..utils.atomic import atomic_text_writer

MANIFEST_COLUMNS: tuple[str, ...] = (
    "row_key",
    "image_order",
    "local_path",
    "original_source_url",
    "hosted_url",
    "method",
    "reason",
    "bytes",
    "width",
    "height",
)


class ManifestWriter:
    """Rewritten whole each run, like review.csv: a stale mapping is worse than none."""

    def __init__(self, path: Path, cfg: AppConfig) -> None:
        self.path = path
        self._cfg = cfg
        self.written = 0
        self._handle: IO[str] | None = None
        self._writer: Any = None
        self._cm: AbstractContextManager[IO[str]] | None = None

    def __enter__(self) -> ManifestWriter:
        self._cm = atomic_text_writer(self.path)
        self._handle = self._cm.__enter__()
        self._writer = csv.writer(
            self._handle,
            lineterminator=self._cfg.csv.line_terminator,
            quoting=csv.QUOTE_MINIMAL,
        )
        self._writer.writerow(MANIFEST_COLUMNS)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._cm is not None:
            self._cm.__exit__(exc_type, exc, tb)

    def write(self, record: ProductRecord) -> int:
        assert self._writer is not None, "use ManifestWriter as a context manager"
        image = record.image

        for file in image.files:
            self._writer.writerow(
                [
                    record.row_key,
                    file.order,
                    file.local_path,
                    file.original_source_url,
                    file.hosted_url or "",
                    image.method.value,
                    image.reason,
                    file.bytes,
                    file.width,
                    file.height,
                ]
            )
            self.written += 1

        return len(image.files)
