"""Downloads: the four files and the zip.

Two client-supplied values reach this module -- a job id and an artifact name --
and both are controls rather than parameters. The job id must match
`^j_[a-z0-9]{8}$` and the artifact must be one of five literal names; a path is
never accepted, constructed from user input, or joined.

Three of the four files are **projections**, regenerated from the ledger on the
way out. That is what makes a mid-run download honest: `review.csv` downloaded
at row 200 of 500 describes those 200 rows rather than the last time a job
happened to finish. `listings.csv` is not regenerated -- it is already the
correctly ordered prefix on disk, which is the whole point of the watermark
writer -- so downloading it mid-run costs nothing and yields a valid CSV.

Filenames carry the job id. Three downloads in one Downloads folder should not
be `listings.csv`, `listings (1).csv`, `listings (2).csv`.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from ...config import Settings
from ...jobs import job_paths, render_projections
from ...models import ImageMode
from ...store.ledger import Ledger
from ...utils.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["downloads"])

# The allowlist. Not a directory listing, not a glob -- five names, and anything
# else is a 404 before a path is ever built.
ARTIFACTS: dict[str, str] = {
    "listings": "listings.csv",
    "review": "review.csv",
    "manifest": "image_manifest.csv",
    "failed": "failed.csv",
    "zip": "",  # built on demand below
}

# review.csv, the manifest and failed.csv are rebuilt from the ledger before
# being served. listings.csv is not: it is written in input order as the job
# runs, and rewriting it would throw away the ordering guarantee.
REGENERATED = {"review", "manifest", "failed"}


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _require_job(request: Request, job_id: str) -> tuple[Settings, dict[str, Any]]:
    from ...jobs import is_job_id

    if not is_job_id(job_id):
        raise HTTPException(status_code=404, detail="No such job.")

    settings = _settings(request)
    with Ledger(settings.root / settings.config.paths.ledger) as ledger:
        row = ledger.job(job_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No such job.")
        return settings, {"state": row["state"], "settings": json.loads(row["settings"])}


def _image_mode(job: dict[str, Any]) -> ImageMode:
    try:
        return ImageMode(job["settings"].get("images", "manifest"))
    except ValueError:
        return ImageMode.MANIFEST


def _refresh(settings: Settings, job_id: str, job: dict[str, Any]) -> None:
    with Ledger(settings.root / settings.config.paths.ledger) as ledger:
        render_projections(
            ledger, job_id, job_paths(settings, job_id), settings, _image_mode(job)
        )


@router.get("/{job_id}/download/{artifact}")
def download(job_id: str, artifact: str, request: Request) -> FileResponse:
    if artifact not in ARTIFACTS:
        raise HTTPException(
            status_code=404,
            detail=f"No such file. Available: {', '.join(sorted(ARTIFACTS))}.",
        )
    settings, job = _require_job(request, job_id)
    paths = job_paths(settings, job_id)

    if artifact == "zip":
        return _zip_response(settings, job_id, job)

    if artifact in REGENERATED:
        _refresh(settings, job_id, job)

    path = paths.root / ARTIFACTS[artifact]
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"This job has no {ARTIFACTS[artifact]} yet.",
        )

    return FileResponse(
        path,
        media_type="text/csv; charset=utf-8",
        filename=f"{Path(ARTIFACTS[artifact]).stem}-{job_id}.csv",
    )


def _zip_response(settings: Settings, job_id: str, job: dict[str, Any]) -> FileResponse:
    """Everything the job produced, in one file.

    Rebuilt whenever anything in the job directory is newer than the zip, so a
    download taken after a resume is not last week's. Deflate on the CSVs and
    stored on the photos: they are already-compressed JPEGs and WebPs, and
    deflating them costs seconds to save nothing.
    """
    _refresh(settings, job_id, job)
    paths = job_paths(settings, job_id)
    target = paths.zip

    sources = [p for p in paths.root.rglob("*") if p.is_file() and p != target]
    newest = max((p.stat().st_mtime for p in sources), default=0.0)
    if not target.exists() or target.stat().st_mtime < newest:
        _build_zip(target, paths.root, sources, job_id, job)

    return FileResponse(
        target, media_type="application/zip", filename=f"haat-listings-{job_id}.zip"
    )


_ALREADY_COMPRESSED = {".jpg", ".jpeg", ".png", ".webp", ".zip", ".gz"}


def _build_zip(
    target: Path, root: Path, sources: list[Path], job_id: str, job: dict[str, Any]
) -> None:
    tmp = target.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{job_id}/README.txt", _readme(job_id, job, root))
        for path in sorted(sources):
            method = (
                zipfile.ZIP_STORED
                if path.suffix.lower() in _ALREADY_COMPRESSED
                else zipfile.ZIP_DEFLATED
            )
            archive.write(path, f"{job_id}/{path.relative_to(root).as_posix()}", method)
    tmp.replace(target)
    log.info("Built %s (%d files)", target.name, len(sources) + 1)


def _readme(job_id: str, job: dict[str, Any], root: Path) -> str:
    """Says what this is and, if the job was cancelled or is still running,
    says that too. A zip that looks complete and is not is worse than one that
    admits it."""
    state = job["state"]
    caveat = {
        "running": "\nTHIS JOB WAS STILL RUNNING when the zip was built. The rows in\n"
        "listings.csv are complete and in order, but they are not all of them.\n",
        "cancelled": "\nTHIS JOB WAS CANCELLED. listings.csv holds the rows that finished;\n"
        "the URLs that never ran are in failed.csv and can be pasted into a new job.\n",
    }.get(state, "")

    settings = job["settings"]
    return f"""haat-lister — job {job_id}
state: {state}
{caveat}
listings.csv          the import file. 19 columns, rows in the order you pasted.
review.csv            every row that needs a human, and which cells.
image_manifest.csv    which photo belongs to which row, in order. First is the hero.
failed.csv            URLs that produced nothing. Its URL column re-runs as a new job.
job.json              the exact settings this used.
images/<row_key>/     the photos, normalised and ready to upload.

settings used:
  provenance        {settings.get("provenance")}
  images            {settings.get("images")}
  descriptions      {settings.get("description_mode")}
  price strategy    {settings.get("price_strategy")}

gi_region is blank in every row. A GI tag is an Indian government certification
and haat makes it a seller declaration, so this tool never asserts one.

price_inr is blank unless you changed price.strategy. haat wants the maker's INR
price, which is a business decision rather than a scraped number.
"""
