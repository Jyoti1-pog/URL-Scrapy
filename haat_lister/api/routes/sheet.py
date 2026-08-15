"""/api/sheet -- runs/master.csv, for the console.

Read-only and deliberately so. The sheet is written by exactly one thing: a job
finishing. A route that could edit or clear it would be a second author of the
one file an operator depends on, and the two would eventually disagree about
what is in it.

The preview is capped rather than paginated. Someone wanting to work through
5,000 rows opens the file; what this screen answers is "did my job land, and how
big is the catalogue now".
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from ...config import Settings, collect_findings
from ...output.csv_writer import HAAT_COLUMNS
from ...output.master import master_path, preview, stats
from ..schemas import FindingOut, SheetOut

router = APIRouter(prefix="/api/sheet", tags=["sheet"])

PREVIEW_ROWS = 50


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


@router.get("", response_model=SheetOut)
def sheet(request: Request) -> SheetOut:
    settings = _settings(request)
    path = master_path(settings.root, settings.config)
    summary = stats(path, settings.config)

    return SheetOut(
        exists=summary.exists,
        rows=summary.rows,
        jobs=summary.jobs,
        first_added=summary.first_added,
        last_added=summary.last_added,
        bytes=summary.bytes,
        header_ok=summary.header_ok,
        # The folder, not the file: "Open the folder" is what an operator wants,
        # and a browser will not open a file:// link to a CSV anyway.
        folder=str(path.parent),
        columns=list(HAAT_COLUMNS),
        preview=preview(path, settings.config, PREVIEW_ROWS) if summary.exists else [],
        preview_limit=PREVIEW_ROWS,
        # Surfaced here as well as at startup: the three open items are all
        # facts about what is IN this sheet -- unconfirmed subcategory slugs, a
        # missing availability value, a two-entry HS map -- and the person
        # about to upload it is the person who needs to know.
        warnings=[
            FindingOut(level=f.level, title=f.title, detail=f.detail, fix=f.fix or "")
            for f in collect_findings(settings)
            if f.level in ("fail", "warn")
        ],
    )


@router.get("/download")
def download(request: Request) -> FileResponse:
    """One artifact, one fixed path, no client-supplied filename anywhere."""
    settings = _settings(request)
    path = master_path(settings.root, settings.config)
    if not path.exists():
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="No sheet yet. Finish a job first.")
    return FileResponse(path, media_type="text/csv", filename="master.csv")
