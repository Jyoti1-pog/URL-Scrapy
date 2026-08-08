"""§4 in a browser: inspect a file, then run it.

TWO CALLS, NOT ONE, and the split is the whole design. `POST /api/import/inspect`
reads the file and returns what it thinks the columns are; `POST /api/import/run`
takes a mapping back and does the work. Nothing is committed in between.

That shape exists because §4.1 says unmapped columns are shown and never
silently discarded, and a single upload-and-go endpoint has nowhere to show them
-- by the time there is a response, the rows are already built under a mapping
nobody agreed to.

WHAT THIS ROUTE OWNS, because the core cannot know it:

  * `provenance` is a required form field with no default, exactly as on the
    CLI (§7). An import is not a loophole in Rule 2.1.
  * uploads are bounded and land in a temporary directory that is deleted on
    the way out. A file the operator picked is not a path we accept.
  * one import at a time, for the same reason `diagnose` is: a console that
    let somebody start four would be four crawls of one shop.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from ...config import Settings
from ...ingest import run as ingest_run
from ...ingest import saved_page, seller_export
from ...models import ImageMode, Provenance
from ...utils.logging import get_logger

router = APIRouter(tags=["import"])

log = get_logger(__name__)

MAX_UPLOAD_BYTES = 32 * 1024 * 1024
CHUNK = 1024 * 1024
QUEUE_TIMEOUT_S = 120.0

_gate = asyncio.Semaphore(1)


# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class ColumnOut(BaseModel):
    index: int
    header: str
    samples: list[str] = Field(default_factory=list)
    target: str = ""
    confidence: float = 0.0
    # "" when we do not recognise it at all; otherwise the field it plainly is,
    # which we have no haat column for. Two different messages, and only one of
    # them tells the operator to stop looking.
    known_unused: str = ""


class InspectOut(BaseModel):
    kind: str  # "export" | "saved_page"
    filename: str
    signature: str = ""
    profile_used: str = ""
    columns: list[ColumnOut] = Field(default_factory=list)
    row_count: int = 0
    # Every field an operator may map a column onto. Sent rather than hardcoded
    # in the console so the two cannot drift, and so `gi_region` is absent in
    # one place instead of two.
    targets: list[str] = Field(default_factory=list)
    source_url: str = ""
    error: str = ""


class ImportedRow(BaseModel):
    source_url: str
    title: str = ""
    status: str = ""
    image_method: str = ""
    no_image_reason: str = ""
    notes: list[str] = Field(default_factory=list)


class RunOut(BaseModel):
    rows: list[ImportedRow] = Field(default_factory=list)
    written: int = 0
    needs_human: int = 0
    failed: int = 0
    profile_saved: str = ""


# ---------------------------------------------------------------------------
# Upload handling
# ---------------------------------------------------------------------------


async def _spool(upload: UploadFile, folder: Path) -> Path:
    """Stream the upload to disk, refusing anything oversized as it arrives.

    Streamed rather than `await upload.read()` because reading first and
    checking the size afterwards means the limit is enforced only once the
    bytes are already in memory, which is not a limit.

    The name is taken for its SUFFIX only. A filename is attacker-controlled
    even when the attacker is a tired operator, and `../../etc/passwd` is a
    valid thing for a browser to send.
    """
    suffix = Path(upload.filename or "upload").suffix.lower()[:12]
    if suffix not in (*seller_export.SUFFIXES, *saved_page.SUFFIXES):
        raise HTTPException(
            status_code=400,
            detail=(
                "Give me a seller export (.csv, .tsv, .xlsx) or a page you saved from your "
                'browser (.html, .mhtml). Ctrl+S, then "Webpage, complete".'
            ),
        )

    target = folder / f"upload{suffix}"
    written = 0
    with target.open("wb") as handle:
        while chunk := await upload.read(CHUNK):
            written += len(chunk)
            if written > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"That file is over the {MAX_UPLOAD_BYTES // 1_048_576} MB limit.",
                )
            handle.write(chunk)
    if not written:
        raise HTTPException(status_code=400, detail="That file is empty.")
    return target


def _inspect_export(path: Path, settings: Settings, filename: str) -> InspectOut:
    export = seller_export.parse(path, settings)
    return InspectOut(
        kind="export",
        filename=filename,
        signature=export.signature,
        profile_used=export.profile_used,
        row_count=len(export.rows),
        targets=list(seller_export.TARGETS),
        columns=[
            ColumnOut(
                index=column.index,
                header=column.header,
                samples=column.samples,
                target=column.target,
                confidence=column.confidence,
                known_unused=seller_export.known_unused(column.header),
            )
            for column in export.columns
        ],
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/api/import/inspect", response_model=InspectOut)
async def inspect(request: Request, file: Annotated[UploadFile, File()]) -> InspectOut:
    """Read the file and say what is in it. Builds no rows and writes nothing.

    No `provenance` here on purpose: looking at a spreadsheet's headers is not
    an ingestion, and demanding the declaration before the operator can see what
    they are declaring about would train them to pick one at random.
    """
    settings: Settings = request.app.state.settings
    folder = Path(tempfile.mkdtemp(prefix="haat-import-"))
    try:
        path = await _spool(file, folder)
        name = Path(file.filename or path.name).name
        if path.suffix.lower() in saved_page.SUFFIXES:
            page = saved_page.load(path)
            return InspectOut(kind="saved_page", filename=name, source_url=page.source_url)
        return _inspect_export(path, settings, name)
    except (saved_page.SavedPageError, seller_export.ExportError) as exc:
        # A wrong file is the most likely thing to happen on this route and it
        # is not a server error. 422 with the operator's own next action.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@router.post("/api/import/run", response_model=RunOut)
async def run_import(
    request: Request,
    file: Annotated[UploadFile, File()],
    provenance: Annotated[Provenance, Form()],
    mapping: Annotated[str, Form()] = "",
    source_url: Annotated[str, Form()] = "",
    save_profile: Annotated[str, Form()] = "",
) -> RunOut:
    """Build the rows. `provenance` is required by the signature, not by a check.

    `mapping` is JSON of `{header: target}` as confirmed on the mapper screen.
    It goes through `seller_export.apply_profile`, which drops refused targets
    -- so the console cannot map a column onto `gi_region` even if it tries.
    """
    import json

    settings: Settings = request.app.state.settings

    try:
        await asyncio.wait_for(_gate.acquire(), timeout=QUEUE_TIMEOUT_S)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=429, detail="An import is already running. One at a time."
        ) from exc

    folder = Path(tempfile.mkdtemp(prefix="haat-import-"))
    try:
        path = await _spool(file, folder)
        if path.suffix.lower() in saved_page.SUFFIXES:
            records = [
                await _one_page(path, provenance, settings, source_url)
            ]
            profile_saved = ""
        else:
            records, profile_saved = await _rows_from_export(
                path, provenance, settings,
                json.loads(mapping) if mapping else None,
                save_profile,
            )
    except (saved_page.SavedPageError, seller_export.ExportError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="The column mapping was not valid.") from exc
    finally:
        _gate.release()
        shutil.rmtree(folder, ignore_errors=True)

    from ...jobs import NEEDS_HUMAN, WRITTEN, terminal_state

    rows = [
        ImportedRow(
            source_url=record.source_url,
            title=str(record.title.value or ""),
            status=terminal_state(record, settings.config),
            image_method=record.image.method.value,
            no_image_reason=(
                record.image.none_reason.value if record.image.none_reason else ""
            ),
            notes=list(record.notes),
        )
        for record in records
    ]
    return RunOut(
        rows=rows,
        written=sum(1 for r in rows if r.status == WRITTEN),
        needs_human=sum(1 for r in rows if r.status == NEEDS_HUMAN),
        failed=sum(1 for r in rows if r.status not in (WRITTEN, NEEDS_HUMAN)),
        profile_saved=profile_saved,
    )


async def _one_page(path: Path, provenance: Provenance, settings: Settings, source_url: str):
    from ...extract.plugins import build_registry
    from ...fetch.static import build_client
    from ...images.pipeline import ImageResolver
    from ...store.ledger import Ledger

    mode = settings.config.images.default_mode
    with Ledger(settings.root / settings.config.paths.ledger) as ledger:
        async with build_client(settings) as client:
            resolver = ImageResolver(settings, client, mode, hosts=[], ledger=ledger)
            return await ingest_run.from_saved_page(
                path,
                provenance,
                settings,
                source_url=source_url,
                resolver=resolver,
                plugins=build_registry(settings.config, settings.root),
            )


async def _rows_from_export(
    path: Path,
    provenance: Provenance,
    settings: Settings,
    mapping: dict | None,
    save_profile: str,
):
    from ...fetch.static import build_client
    from ...images.pipeline import ImageResolver
    from ...store.ledger import Ledger

    export = seller_export.parse(path, settings)
    if mapping:
        seller_export.apply_profile(export, {"name": "console", "mapping": mapping})
        export.profile_used = ""

    if "source_url" not in export.mapping:
        raise HTTPException(
            status_code=422,
            detail=(
                "No column is mapped to the product URL, and every row needs one -- it is "
                "what the row is keyed on and deduplicated by."
            ),
        )

    profile_saved = ""
    if save_profile:
        profile_saved = seller_export.save_profile(settings, save_profile, export).name

    mode: ImageMode = settings.config.images.default_mode
    records = []
    with Ledger(settings.root / settings.config.paths.ledger) as ledger:
        async with build_client(settings) as client:
            resolver = ImageResolver(settings, client, mode, hosts=[], ledger=ledger)
            for row in export.rows:
                records.append(
                    await ingest_run.from_export_row(
                        export, row, provenance, settings, resolver=resolver
                    )
                )
    return records, profile_saved
