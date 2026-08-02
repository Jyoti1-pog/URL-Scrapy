"""The review table: read the cells, edit them, re-export.

The reason the web console earns its keep. `price_inr` is blank by policy on
every row, `hs_code` is always a suggestion, and a subcategory is sometimes
ambiguous -- so a real catalogue arrives with dozens of cells a human has to
decide. Editing those in Excel and re-importing is miserable; editing them in a
focused table is not.

What this module will not do, and the API refuses even when the UI is bypassed:
set `gi_region`, accept a slug that is not in `taxonomy.yaml`, or write an edit
over the stored extraction. The last one is structural -- edits live in their own
table and are applied on the way out -- so "the original is preserved" is a
property of the schema rather than a promise about this code.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from ...config import Settings
from ...edits import EDITABLE, LOCKED, EditError, apply_edits, validate
from ...jobs import is_job_id, job_paths, render_listings, render_projections
from ...models import ImageMode, ProductRecord
from ...output.csv_writer import HAAT_COLUMNS
from ...output.review_writer import low_confidence_fields, missing_required, needs_review
from ...store.ledger import Ledger
from ...utils.logging import get_logger
from ..schemas import (
    BulkEditIn,
    CellOut,
    EditIn,
    ExportOut,
    RowPageOut,
    RowTableOut,
)

log = get_logger(__name__)
router = APIRouter(prefix="/api/jobs", tags=["rows"])


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _open(request: Request, job_id: str) -> tuple[Settings, Ledger, dict[str, Any]]:
    if not is_job_id(job_id):
        raise HTTPException(status_code=404, detail="No such job.")
    settings = _settings(request)
    ledger = Ledger(settings.root / settings.config.paths.ledger)
    row = ledger.job(job_id)
    if row is None:
        ledger.close()
        raise HTTPException(status_code=404, detail="No such job.")
    import json

    return settings, ledger, {"state": row["state"], "settings": json.loads(row["settings"])}


def _image_mode(job: dict[str, Any]) -> ImageMode:
    try:
        return ImageMode(job["settings"].get("images", "manifest"))
    except ValueError:
        return ImageMode.MANIFEST


def _cells(
    record: ProductRecord, edited: ProductRecord, row_edits: dict[str, str]
) -> list[CellOut]:
    """One entry per CSV column, in header order.

    Carries the pre-edit value alongside the current one, so the table can show
    "you changed this from what the page said" rather than only the result.
    """
    original_fields = record.field_values()
    current_fields = edited.field_values()

    out = []
    for name in HAAT_COLUMNS:
        if name in LOCKED:
            out.append(
                CellOut(
                    field=name,
                    value="",
                    confidence="none",
                    source="",
                    editable=False,
                    edited=False,
                    locked_reason="A GI tag is an Indian government certification. haat treats "
                    "it as a seller declaration, so this tool leaves it blank on every row.",
                )
            )
            continue

        current = current_fields.get(name)
        original = original_fields.get(name)
        was_edited = name in row_edits
        out.append(
            CellOut(
                field=name,
                value="" if current is None or not current.is_present else str(current.value),
                confidence=current.confidence.value if current else "none",
                source=(current.source.value if current and current.source else ""),
                editable=name in EDITABLE,
                edited=was_edited,
                original=(
                    ("" if original is None or not original.is_present else str(original.value))
                    if was_edited
                    else None
                ),
                note=current.note if current else None,
            )
        )
    return out


@router.get("/{job_id}/rows", response_model=RowPageOut)
def read_rows(
    job_id: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=500),
    flagged_only: bool = Query(default=False),
) -> RowPageOut:
    """Paginated table data, with confidence and flags per cell.

    Paginated because 500 rows x 19 cells is 9,500 objects, and an operator
    works through a screenful at a time regardless.
    """
    settings, ledger, job = _open(request, job_id)
    try:
        cfg = settings.config
        all_edits = ledger.edits_for(job_id)

        rows: list[RowTableOut] = []
        for payload in ledger.iter_payloads(job_id):
            record = ProductRecord.model_validate_json(payload)
            row_edits = all_edits.get(record.row_key, {})
            edited = apply_edits(record, row_edits, settings) if row_edits else record

            wanted = needs_review(edited, cfg)
            if flagged_only and not wanted:
                continue

            rows.append(
                RowTableOut(
                    row_key=record.row_key,
                    input_index=0,  # filled below from job_urls
                    source_url=record.source_url,
                    status=edited.status.value,
                    needs_human=wanted,
                    missing=missing_required(edited, cfg),
                    low_confidence=low_confidence_fields(edited),
                    notes=edited.notes,
                    cells=_cells(record, edited, row_edits),
                )
            )

        order = {
            row["row_key"]: int(row["input_index"])
            for row in ledger.job_inputs(job_id)
            if row["row_key"]
        }
        for row in rows:
            row.input_index = order.get(row.row_key, 0)
        rows.sort(key=lambda r: r.input_index)

        total = len(rows)
        page = rows[offset : offset + limit]
        return RowPageOut(
            job_id=job_id,
            total=total,
            offset=offset,
            limit=limit,
            columns=list(HAAT_COLUMNS),
            editable=sorted(EDITABLE),
            rows=page,
            pending_edits=sum(len(v) for v in all_edits.values()),
        )
    finally:
        ledger.close()


@router.patch("/{job_id}/rows/{row_key}", response_model=RowTableOut)
def edit_row(job_id: str, row_key: str, body: EditIn, request: Request) -> RowTableOut:
    settings, ledger, job = _open(request, job_id)
    try:
        record = _record(ledger, job_id, row_key)
        existing = ledger.edits_for(job_id).get(row_key, {})
        merged = {**existing, **body.fields}

        # Validated against the record as it will stand, so a request that sets
        # a category and a subcategory together is judged on what it is asking
        # for rather than on what the row used to be.
        try:
            edited = apply_edits(record, merged, settings)
        except EditError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        for field, raw in body.fields.items():
            ledger.record_edit(job_id, row_key, field, validate(field, raw, settings, edited))

        return _one_row(ledger, settings, job_id, record, row_key)
    finally:
        ledger.close()


@router.patch("/{job_id}/rows", response_model=dict)
def edit_many(job_id: str, body: BulkEditIn, request: Request) -> dict[str, Any]:
    """One price, one category, one availability across twenty rows.

    Validated per row rather than once: a subcategory that is right for an
    apparel row is wrong for a jewellery one, and applying it anyway would
    produce exactly the silently-wrong CSV this tool exists to avoid.
    """
    settings, ledger, job = _open(request, job_id)
    try:
        applied, rejected = 0, []
        for row_key in body.row_keys:
            record = _maybe_record(ledger, job_id, row_key)
            if record is None:
                rejected.append({"row_key": row_key, "reason": "no such row in this job"})
                continue
            existing = ledger.edits_for(job_id).get(row_key, {})
            try:
                edited = apply_edits(record, {**existing, **body.fields}, settings)
            except EditError as exc:
                rejected.append({"row_key": row_key, "reason": str(exc)})
                continue
            for field, raw in body.fields.items():
                ledger.record_edit(job_id, row_key, field, validate(field, raw, settings, edited))
            applied += 1

        return {"applied": applied, "rejected": rejected}
    finally:
        ledger.close()


@router.delete("/{job_id}/rows/{row_key}/edits/{field}", response_model=RowTableOut)
def undo_edit(job_id: str, row_key: str, field: str, request: Request) -> RowTableOut:
    """Removing an edit restores what the page said. That is the whole point of
    keeping them apart."""
    settings, ledger, job = _open(request, job_id)
    try:
        record = _record(ledger, job_id, row_key)
        ledger.delete_edit(job_id, row_key, field)
        return _one_row(ledger, settings, job_id, record, row_key)
    finally:
        ledger.close()


@router.post("/{job_id}/export", response_model=ExportOut)
def export(job_id: str, request: Request) -> ExportOut:
    """Regenerate listings.csv from the ledger, with edits applied."""
    settings, ledger, job = _open(request, job_id)
    try:
        paths = job_paths(settings, job_id)
        mode = _image_mode(job)
        written = render_listings(ledger, job_id, paths, settings, mode)
        render_projections(ledger, job_id, paths, settings, mode)
        edits = ledger.edits_for(job_id)
        return ExportOut(
            job_id=job_id,
            rows=written,
            edits_applied=sum(len(v) for v in edits.values()),
            rows_edited=len(edits),
        )
    finally:
        ledger.close()


# ---------------------------------------------------------------------------


def _record(ledger: Ledger, job_id: str, row_key: str) -> ProductRecord:
    payload = ledger.row_payload(job_id, row_key)
    if payload is None:
        raise HTTPException(status_code=404, detail="No such row in this job.")
    return ProductRecord.model_validate_json(payload)


def _maybe_record(ledger: Ledger, job_id: str, row_key: str) -> ProductRecord | None:
    payload = ledger.row_payload(job_id, row_key)
    return ProductRecord.model_validate_json(payload) if payload else None


def _one_row(
    ledger: Ledger, settings: Settings, job_id: str, record: ProductRecord, row_key: str
) -> RowTableOut:
    row_edits = ledger.edits_for(job_id).get(row_key, {})
    edited = apply_edits(record, row_edits, settings) if row_edits else record
    cfg = settings.config
    return RowTableOut(
        row_key=row_key,
        input_index=0,
        source_url=record.source_url,
        status=edited.status.value,
        needs_human=needs_review(edited, cfg),
        missing=missing_required(edited, cfg),
        low_confidence=low_confidence_fields(edited),
        notes=edited.notes,
        cells=_cells(record, edited, row_edits),
    )
