"""/api/find -- Find photos, for a catalogue.

Shaped like the job routes on purpose: create, watch a stream, cancel,
download. An operator who has used the Compose screen already knows how this
one behaves.

Three things it does NOT share with a job. It writes no listings and touches no
sheet, so there is no artifact allowlist to guard beyond its own one file. It
never reaches an image host -- structurally, see `find.py`. And its results are
held in memory for the session rather than in the ledger's `jobs` table,
because a preview is not a run and should not appear in the history of things
that were produced.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from ...config import Settings
from ...find import FindRow, FindStats, StopFinding, find_photos
from ...jobs import is_job_id, new_job_id
from ...output.find_csv import read_table, write_image_links
from ...utils.logging import get_logger
from ..events import EventBroker, parse_last_event_id
from ..schemas import (
    MAX_BYTES,
    MAX_URLS,
    FindCreatedOut,
    FindOut,
    FindRowOut,
    FindStartIn,
    ParsedTableOut,
)

log = get_logger(__name__)
router = APIRouter(prefix="/api/find", tags=["find"])


@dataclass
class FindSession:
    """One find, and everything the screen needs to redraw itself.

    Held in memory deliberately: the rows are already cached in the ledger for
    a later job to reuse, and what is kept here is only the presentation of one
    session. A refresh refetches this; a restart loses it, which is the right
    lifetime for a preview.
    """

    find_id: str
    total: int
    rows: dict[int, FindRow] = field(default_factory=dict)
    stats: FindStats = field(default_factory=FindStats)
    extra_columns: list[str] = field(default_factory=list)
    stop: StopFinding = field(default_factory=StopFinding)
    task: asyncio.Task[Any] | None = None
    done: bool = False

    @property
    def running(self) -> bool:
        return not self.done and not self.stop.is_set


def _state(request: Request) -> Any:
    return request.app.state


def _sessions(request: Request) -> dict[str, FindSession]:
    """Lazily attached to app state. One dict per app, so a test client and a
    real server keep their own."""
    state = _state(request)
    if not hasattr(state, "finds"):
        state.finds = {}
    sessions: dict[str, FindSession] = state.finds
    return sessions


def _broker(request: Request) -> EventBroker:
    broker: EventBroker = _state(request).broker
    return broker


def _require(request: Request, find_id: str) -> FindSession:
    if not is_job_id(find_id):
        raise HTTPException(status_code=404, detail="No such find.")
    session = _sessions(request).get(find_id)
    if session is None:
        raise HTTPException(status_code=404, detail="No such find.")
    return session


def _check_size(urls: list[str]) -> None:
    """The same caps as Compose. §10.4, as a message rather than a 500."""
    if len(urls) > MAX_URLS:
        raise HTTPException(
            status_code=413,
            detail=f"{len(urls):,} lines is over the {MAX_URLS:,}-line limit. "
            "Split the list and find them in two passes.",
        )
    if (size := sum(len(u) for u in urls)) > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"{size // 1024:,} KB is over the {MAX_BYTES // 1024:,} KB limit.",
        )


@router.post("/parse-file", response_model=ParsedTableOut)
def parse_file(body: dict[str, str], request: Request) -> ParsedTableOut:
    """What we made of an uploaded CSV, before anything is fetched.

    Separate from starting a find so the operator can see which column was
    chosen and change it. A silent guess about which column holds the links is
    a run over the wrong data that looks like it worked.
    """
    text = body.get("text", "")
    if len(text.encode("utf-8", "ignore")) > MAX_BYTES:
        raise HTTPException(
            status_code=413, detail=f"That file is over the {MAX_BYTES // 1024:,} KB limit."
        )
    table = read_table(text, body.get("url_column") or None)
    _check_size(table.urls)

    return ParsedTableOut(
        columns=table.columns,
        url_column=table.url_column,
        url_column_hits=table.url_column_hits,
        had_header=table.had_header,
        delimiter=table.delimiter,
        found=len(table.urls),
        preview=table.urls[:50],
        extras_preview=table.extras[:5],
        unparsed=table.unparsed[:50],
    )


@router.post("", response_model=FindCreatedOut, status_code=202)
async def start(body: FindStartIn, request: Request) -> FindCreatedOut:
    settings: Settings = _state(request).settings

    if body.file_text:
        table = read_table(body.file_text, body.url_column or None)
        urls, extras = table.urls, table.extras
        extra_columns = [c for c in table.columns if c != table.url_column]
    else:
        from ...utils.urls import extract_urls

        found = extract_urls("\n".join(body.urls))
        urls = [u.url for u in found.urls]
        extras = [{} for _ in urls]
        extra_columns = []

    _check_size(urls)
    if not urls:
        raise HTTPException(status_code=422, detail="No usable product links in that.")

    find_id = new_job_id()
    session = FindSession(find_id=find_id, total=len(urls), extra_columns=extra_columns)
    _sessions(request)[find_id] = session
    broker = _broker(request)

    def on_row(row: FindRow) -> None:
        session.rows[row.index] = row
        broker.publish(find_id, "find_row", **_row_out(row).model_dump())
        broker.publish(
            find_id,
            "find_progress",
            done=session.stats.done,
            total=session.total,
            with_photo=session.stats.with_photo,
            without_photo=session.stats.without_photo,
            low_res=session.stats.low_res,
            failed=session.stats.failed,
            from_cache=session.stats.from_cache,
        )

    async def run() -> None:
        try:
            session.stats = await find_photos(
                urls,
                settings,
                extras=extras,
                concurrency=body.concurrency,
                ignore_robots=body.ignore_robots,
                render=body.render,
                use_cache=body.use_cache,
                on_row=on_row,
                stop=session.stop,
            )
        except Exception:  # noqa: BLE001 -- a find that crashes must still close its stream
            log.exception("find %s failed", find_id)
        finally:
            session.done = True
            broker.publish(find_id, "find_done", done=session.stats.done, total=session.total)
            broker.close(find_id)

    session.task = asyncio.create_task(run())
    return FindCreatedOut(find_id=find_id, accepted=len(urls))


@router.get("/{find_id}", response_model=FindOut)
def get(find_id: str, request: Request) -> FindOut:
    """Everything a refresh needs. Complete rather than a delta, for the same
    reason the job route is."""
    session = _require(request, find_id)
    return FindOut(
        find_id=find_id,
        total=session.total,
        running=session.running,
        extra_columns=session.extra_columns,
        rows=[_row_out(r) for _, r in sorted(session.rows.items())],
        done=session.stats.done,
        with_photo=session.stats.with_photo,
        without_photo=session.stats.without_photo,
        low_res=session.stats.low_res,
        failed=session.stats.failed,
        from_cache=session.stats.from_cache,
        host_calls=session.stats.host_calls,
    )


@router.get("/{find_id}/stream")
async def stream(find_id: str, request: Request) -> StreamingResponse:
    session = _require(request, find_id)
    broker = _broker(request)
    # Same replay contract as the job stream: a reconnect resumes rather
    # than repeating everything.
    last = parse_last_event_id(request.headers.get("Last-Event-ID"))

    async def events() -> AsyncIterator[str]:
        async for chunk in broker.subscribe(find_id, last_event_id=last):
            yield chunk

    if not session.running and not session.rows:
        # Nothing will ever arrive; say so rather than holding a connection.
        broker.close(find_id)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{find_id}/cancel")
def cancel(find_id: str, request: Request) -> dict[str, str]:
    session = _require(request, find_id)
    session.stop.stop()
    return {"state": "cancelling"}


@router.get("/{find_id}/download")
def download(find_id: str, request: Request) -> FileResponse:
    """`image_links.csv`. One artifact, one fixed path, no client-supplied name."""
    session = _require(request, find_id)
    settings: Settings = _state(request).settings

    if not session.rows:
        raise HTTPException(status_code=404, detail="Nothing found yet.")

    out = settings.root / settings.config.paths.runs_dir / f"image_links-{find_id}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_image_links(
        out, list(session.rows.values()), settings.config, session.extra_columns
    )
    return FileResponse(out, media_type="text/csv", filename="image_links.csv")


def _row_out(row: FindRow) -> FindRowOut:
    return FindRowOut(
        index=row.index,
        source_url=row.source_url,
        title=row.title,
        title_original=row.title_original,
        primary_image_url=row.primary_image_url,
        image_urls=[p.url for p in row.photos],
        image_count=row.image_count,
        width=row.width,
        height=row.height,
        method=row.method,
        reason=row.reason,
        explanation=row.explanation,
        price=row.price,
        currency=row.currency,
        category=row.category,
        description=row.description,
        weight_g=row.weight_g,
        dimensions=row.dimensions,
        extra=row.extra,
        failed=row.failed,
        from_cache=row.from_cache,
    )
