"""Job routes: create, preflight, watch, cancel, resume, list.

Every response here is built from the ledger, never from the runner's memory.
That is not a stylistic preference -- it is what makes a page refresh mid-job
free, what makes a second tab correct, and what makes the runner's own state a
convenience rather than a source of truth. If this file ever needs to ask the
runner "what happened", something has gone wrong upstream.

The one thing the runner is asked is *liveness*: is this job running right now,
or waiting behind another. The ledger cannot know that, and it is the only thing
a refresh legitimately needs re-establishing.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ...config import Settings
from ...jobs import (
    DUPLICATE,
    FAILED,
    INVALID,
    LISTED,
    JobPlan,
    is_job_id,
    job_paths,
    new_job_id,
    plan_urls,
)
from ...models import DescriptionMode, ImageMode, Provenance
from ...output.csv_writer import HAAT_COLUMNS, cell_depths
from ...output.review_writer import missing_required, needs_review
from ...store.ledger import Ledger
from ...utils.logging import get_logger
from ...utils.urls import origin_of
from ..events import parse_last_event_id
from ..runner import QueuedJob
from ..schemas import (
    MAX_BYTES,
    MAX_URLS,
    ArtifactOut,
    InvalidUrlOut,
    JobCreatedOut,
    JobCreateIn,
    JobOut,
    JobSummaryOut,
    PreflightOut,
    RowOut,
)

log = get_logger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def _state(request: Request) -> Any:
    return request.app.state


def _ledger(request: Request) -> Ledger:
    settings = _state(request).settings
    return Ledger(settings.root / settings.config.paths.ledger)


def _require_job_id(job_id: str) -> str:
    """The only path component a client supplies. Its shape is a control."""
    if not is_job_id(job_id):
        raise HTTPException(status_code=404, detail="No such job.")
    return job_id


def _check_size(urls: list[str]) -> None:
    """Section 10.4, as a message rather than a 500."""
    if len(urls) > MAX_URLS:
        raise HTTPException(
            status_code=413,
            detail=f"{len(urls):,} lines is over the {MAX_URLS:,}-line limit. "
            "Split the list and run it as two jobs -- the ledger will keep them apart.",
        )
    size = sum(len(u) for u in urls)
    if size > MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"{size // 1024:,} KB of URLs is over the {MAX_BYTES // 1024:,} KB limit.",
        )


def _invalid_out(plan: JobPlan) -> list[InvalidUrlOut]:
    return [
        InvalidUrlOut(line=u.index + 1, raw=u.raw[:300], reason=u.note or "not a product link")
        for u in plan.invalid
    ]


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


@router.post("/preflight", response_model=PreflightOut)
async def preflight(body: JobCreateIn, request: Request) -> PreflightOut:
    """What this job would do, before a single product page is fetched.

    This screen is where a mistake costs seconds instead of ten minutes, so it
    does the one network call an operator cannot make for themselves: robots.txt,
    once per origin. Product pages are not touched -- a preflight that fetched
    them would be the job.
    """
    _check_size(body.urls)
    settings = _state(request).settings
    plan = plan_urls(body.urls)

    disallowed: list[str] = []
    blocked: list[str] = []
    checked = False

    # The address check runs first and without touching the shop: an operator
    # should learn that a link points somewhere we refuse to fetch from before
    # a job starts, not row by row afterwards.
    from ...utils.netguard import BlockedHost, check_url

    for entry in plan.accepted:
        try:
            await check_url(entry.canonical, settings.config.fetch.allow_private_hosts)
        except BlockedHost as exc:
            blocked.append(f"{entry.raw} — {exc.reason}")

    if not body.settings.ignore_robots and settings.config.fetch.respect_robots:
        from ...fetch.static import build_client
        from ...utils.robots import RobotsCache

        checked = True
        async with build_client(settings) as client:
            robots = RobotsCache(client, settings.user_agent)
            seen: dict[str, bool] = {}
            for entry in plan.accepted:
                origin = origin_of(entry.canonical)
                if origin not in seen:
                    seen[origin] = await robots.allowed(entry.canonical)
                if not seen[origin]:
                    disallowed.append(entry.raw)

    low, high = plan.estimate_seconds(
        body.settings.concurrency, settings.config.fetch.per_domain_delay_s
    )
    return PreflightOut(
        pasted=plan.pasted,
        unique=len(plan.accepted),
        duplicates=plan.duplicates,
        invalid=_invalid_out(plan),
        domains=dict(plan.domains.most_common()),
        robots_disallowed=disallowed,
        robots_checked=checked,
        blocked_addresses=blocked,
        estimate_low_s=low,
        estimate_high_s=high,
        summary=plan.summary(),
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@router.post("", response_model=JobCreatedOut, status_code=202)
async def create(body: JobCreateIn, request: Request) -> JobCreatedOut:
    _check_size(body.urls)
    runner = _state(request).runner

    try:
        provenance = Provenance(body.settings.provenance)
        image_mode = ImageMode(body.settings.image_mode)
        description_mode = DescriptionMode(body.settings.description_mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    plan = plan_urls(body.urls)
    if not plan.accepted:
        raise HTTPException(
            status_code=422,
            detail=f"No usable product links. {plan.summary()}.",
        )

    from ...batch import BatchOptions
    from ...policy.provenance import effective_description_mode

    job_id = new_job_id()
    options = BatchOptions(
        provenance=provenance,
        image_mode=image_mode,
        job_id=job_id,
        concurrency=body.settings.concurrency,
        seller_note=body.settings.seller_note,
        description_mode=effective_description_mode(description_mode, provenance),
    )
    queued_behind = 1 if runner.current else 0
    runner.submit(
        QueuedJob(
            job_id=job_id,
            plan=plan,
            options=options,
            ignore_robots=body.settings.ignore_robots,
            render=body.settings.render,
            llm=body.settings.llm,
        )
    )

    return JobCreatedOut(
        job_id=job_id,
        accepted=len(plan.accepted),
        duplicates_removed=plan.duplicates,
        invalid=_invalid_out(plan),
        queued_behind=queued_behind,
    )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@router.get("", response_model=list[JobSummaryOut])
def history(request: Request, limit: int = Query(default=50, ge=1, le=200)) -> list[JobSummaryOut]:
    with _ledger(request) as ledger:
        return [
            JobSummaryOut(
                job_id=row["job_id"],
                state=row["state"],
                created_at=row["created_at"],
                finished_at=row["finished_at"],
                input_count=int(row["input_count"]),
                counts=ledger.outcome_counts(row["job_id"]),
            )
            for row in ledger.jobs(limit)
        ]


@router.get("/{job_id}", response_model=JobOut)
def read(job_id: str, request: Request) -> JobOut:
    """The whole state, from the ledger. This is what a refresh refetches."""
    _require_job_id(job_id)
    runner = _state(request).runner
    settings = _state(request).settings

    with _ledger(request) as ledger:
        job = ledger.job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="No such job.")

        inputs = ledger.job_inputs(job_id)
        counts = ledger.outcome_counts(job_id)

        # One pass over the stored records, so a row carries what it produced
        # rather than only that it finished.
        from ...models import ProductRecord

        by_key: dict[str, ProductRecord] = {}
        for payload in ledger.iter_payloads(job_id):
            stored = ProductRecord.model_validate_json(payload)
            by_key[stored.row_key] = stored

        cfg = settings.config
        rows = []
        needs_human = 0
        for row in inputs:
            record = by_key.get(row["row_key"]) if row["row_key"] else None
            wanted = needs_review(record, cfg) if record else False
            needs_human += 1 if wanted else 0
            rows.append(
                RowOut(
                    input_index=int(row["input_index"]),
                    source_url=row["source_url"],
                    outcome=row["outcome"],
                    row_key=row["row_key"],
                    title=(record.title.value or "") if record else "",
                    status=record.status.value if record else "",
                    image_tier=record.image.method.value if record else "",
                    reason=row["reason"] or "",
                    needs_human=wanted,
                    missing=missing_required(record, cfg) if record else [],
                    cells=cell_depths(record) if record else "",
                )
            )

    paths = job_paths(settings, job_id)
    artifacts = [
        ArtifactOut(
            name=name,
            filename=f"{path.stem}-{job_id}.csv",
            bytes=path.stat().st_size,
            rows=_data_rows(path),
        )
        for name, path in (
            ("listings", paths.listings),
            ("review", paths.review),
            ("manifest", paths.manifest),
            ("failed", paths.failed),
        )
        if path.exists()
    ]
    run = _run_facts(paths.job_json)

    total = sum(counts.values()) - counts.get(DUPLICATE, 0) - counts.get(INVALID, 0)
    return JobOut(
        job_id=job_id,
        state=job["state"],
        created_at=job["created_at"],
        finished_at=job["finished_at"],
        settings=json.loads(job["settings"]),
        counts=counts,
        total=total,
        processed=counts.get(LISTED, 0) + counts.get(FAILED, 0),
        written=counts.get(LISTED, 0),
        failed=counts.get(FAILED, 0),
        needs_human=needs_human,
        running=runner.is_running(job_id),
        queued=runner.is_queued(job_id),
        rows=rows,
        artifacts=artifacts,
        columns=list(HAAT_COLUMNS),
        host_calls=int(run.get("host_calls", 0) or 0),
        pages_rendered=int(run.get("pages_rendered", 0) or 0),
        duration_s=_duration(job["created_at"], job["finished_at"]),
    )


def _data_rows(path: Path) -> int | None:
    """Rows an operator would see, so the console can say "38 rows" rather than
    "4.1 KB". Cheap: these files top out in the low megabytes."""
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            return max(0, sum(1 for _ in handle) - 1)
    except OSError:
        return None


def _run_facts(job_json: Path) -> dict[str, Any]:
    if not job_json.exists():
        return {}
    try:
        data = json.loads(job_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(data) if isinstance(data, dict) else {}


def _duration(created_at: str, finished_at: str | None) -> float | None:
    if not finished_at:
        return None
    try:
        start = datetime.fromisoformat(created_at)
        end = datetime.fromisoformat(finished_at)
    except ValueError:
        return None
    return round((end - start).total_seconds(), 1)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@router.get("/{job_id}/events")
async def events(
    job_id: str,
    request: Request,
    last_event_id: str | None = Query(default=None, alias="last_event_id"),
) -> StreamingResponse:
    """SSE. Reconnects resume from `Last-Event-ID` rather than replaying.

    A stream that ignored the header would show a 200-row job as 400 rows after
    one dropped connection, which is worse than showing nothing.
    """
    _require_job_id(job_id)
    broker = _state(request).broker

    # The header is what EventSource sends on its own; the query parameter is
    # for a polling fallback that has to ask explicitly.
    since = parse_last_event_id(request.headers.get("last-event-id") or last_event_id)

    return StreamingResponse(
        broker.subscribe(job_id, since),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # nginx would otherwise hold frames back
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Cancel and resume
# ---------------------------------------------------------------------------


@router.post("/{job_id}/cancel")
async def cancel(job_id: str, request: Request) -> dict[str, object]:
    """Stop after the rows already in flight. Nothing finished is discarded."""
    _require_job_id(job_id)
    if not _state(request).runner.cancel(job_id):
        raise HTTPException(
            status_code=409,
            detail="That job is not running. Finished rows are already in its directory.",
        )
    return {"job_id": job_id, "cancelling": True}


@router.post("/{job_id}/resume")
async def resume(job_id: str, request: Request) -> dict[str, object]:
    """Process the indices that never got an outcome. Nothing is re-fetched."""
    _require_job_id(job_id)
    runner = _state(request).runner

    if runner.is_running(job_id) or runner.is_queued(job_id):
        raise HTTPException(status_code=409, detail="That job is already running.")

    with _ledger(request) as ledger:
        if ledger.job(job_id) is None:
            raise HTTPException(status_code=404, detail="No such job.")
        remaining = len(ledger.resumable(job_id))
        if not remaining:
            raise HTTPException(
                status_code=409,
                detail="Nothing left to do -- every URL in that job produced a row.",
            )
        runner.resume(ledger, job_id)

    return {"job_id": job_id, "resuming": remaining}


def artifact_path(settings: Settings, job_id: str, name: str) -> Path:
    """Name from a fixed allowlist, never a client-supplied path. Phase 6 serves
    these; the mapping lives here so there is one of it."""
    paths = job_paths(settings, job_id)
    allowed = {
        "listings": paths.listings,
        "review": paths.review,
        "manifest": paths.manifest,
        "failed": paths.failed,
    }
    if name not in allowed:
        raise HTTPException(status_code=404, detail="No such artifact.")
    return allowed[name]
