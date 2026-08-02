"""Atomic file writes.

A crash mid-run must never truncate an operator's work. Every output file is
built in a sibling `.tmp` and moved into place with `os.replace`, which is atomic
on the same filesystem on both Windows and POSIX.

The cost is that appending to an existing file means copying it first. At the
5,000-row ceiling this project targets that is a few megabytes and well under a
second, which is a fair price for never handing back a half-written CSV.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO


@contextmanager
def atomic_text_writer(
    path: Path,
    *,
    encoding: str = "utf-8",
    newline: str = "",
    seed_from_existing: bool = False,
) -> Iterator[IO[str]]:
    """Yield a handle writing to `path.tmp`, then move it over `path`.

    `seed_from_existing` copies the current contents in first, which is how
    appending stays atomic.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")

    if seed_from_existing and path.exists():
        tmp.write_bytes(path.read_bytes())
        handle = tmp.open("a", encoding=encoding, newline=newline)
    else:
        handle = tmp.open("w", encoding=encoding, newline=newline)

    try:
        yield handle
        handle.flush()
        os.fsync(handle.fileno())
    except BaseException:
        handle.close()
        # Leave the .tmp behind on failure: it is evidence, and the operator's
        # existing file is still intact.
        raise
    else:
        handle.close()
        os.replace(tmp, path)
