"""Wire shapes. Deliberately separate from `models.py`.

The core models are the extractor's working vocabulary -- FieldValue carries a
confidence and a source, ImageResult carries nine validation results per
candidate. None of that should leak onto the wire by accident, and a response
model that is merely `ProductRecord` would mean every field added to the core is
published the same day.

The separation also runs the other way: nothing here is allowed to *become* a
core model. Requests are validated into plain values and handed to functions
that already existed.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# The one field no request may ever set and no response may ever populate. Named
# once, here, so the UI reads it from /api/config instead of hardcoding a rule
# it might later disagree with.
LOCKED_FIELDS: tuple[str, ...] = ("gi_region",)


class FindingOut(BaseModel):
    level: str
    title: str
    detail: str
    fix: str = ""


class HealthOut(BaseModel):
    """`config-check`, as JSON. Same findings, same wording, one source."""

    ok: bool
    version: str
    config_path: str
    taxonomy_path: str
    user_agent: str
    blocking: int
    warnings: int
    findings: list[FindingOut]


class SubcategoryOut(BaseModel):
    slug: str
    label: str
    # True when the slug was inferred from haat's convention rather than read
    # from real haat data. The UI marks these, because a wrong slug either
    # rejects the import or files the listing where nobody will find it.
    derived: bool = False


class CategoryOut(BaseModel):
    slug: str
    label: str
    subcategories: list[SubcategoryOut] = Field(default_factory=list)


class HostOut(BaseModel):
    """Whether a host is usable. Never a key, never a fragment of one."""

    name: str
    configured: bool


class LlmOut(BaseModel):
    configured: bool
    model: str
    used_for: list[str]
    never_used_for: list[str]


class DefaultsOut(BaseModel):
    image_mode: str
    price_strategy: str
    description_mode: str
    concurrency: int
    render_enabled: bool
    per_domain_delay_s: float


class ConfigOut(BaseModel):
    version: str
    taxonomy_complete: bool
    fallback_category: str
    categories: list[CategoryOut]
    enums: dict[str, list[str]]
    image_hosts: list[HostOut]
    llm: LlmOut
    defaults: DefaultsOut
    locked_fields: list[str]
    allow_private_hosts: list[str]


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

# Section 10.4. Rejected with a message, not a 500.
MAX_URLS = 10_000
MAX_BYTES = 2 * 1024 * 1024


class JobSettingsIn(BaseModel):
    """Provenance has no default, here as on the command line.

    A pydantic field with no default is a 422 naming the field, which is the
    web equivalent of the CLI's panel: the run cannot start until a human says
    who made the content. That is the one setting the tool refuses to guess.
    """

    provenance: str
    image_mode: str = "manifest"
    description_mode: str = "raw"
    concurrency: int = Field(default=5, ge=1, le=20)
    seller_note: str | None = None
    render: bool | None = None
    llm: bool = False
    ignore_robots: bool = False


class JobCreateIn(BaseModel):
    urls: list[str]
    settings: JobSettingsIn


class InvalidUrlOut(BaseModel):
    line: int
    raw: str
    reason: str


class PreflightOut(BaseModel):
    """What a job would do, before it does any of it.

    No product page is fetched to build this. robots.txt is, because that is the
    one thing an operator cannot know from the URLs alone and the one that
    silently costs them rows.
    """

    pasted: int
    unique: int
    duplicates: int
    invalid: list[InvalidUrlOut]
    domains: dict[str, int]
    robots_disallowed: list[str]
    robots_checked: bool
    # Links that resolve somewhere the SSRF guard refuses. Reported here rather
    # than raised, so a preflight is an answer rather than a 500.
    blocked_addresses: list[str] = Field(default_factory=list)
    estimate_low_s: int
    estimate_high_s: int
    summary: str


class JobCreatedOut(BaseModel):
    job_id: str
    accepted: int
    duplicates_removed: int
    invalid: list[InvalidUrlOut]
    queued_behind: int


class RowOut(BaseModel):
    input_index: int
    source_url: str
    outcome: str | None
    row_key: str | None = None
    title: str = ""
    status: str = ""
    image_tier: str = ""
    reason: str = ""
    # The same test review.csv applies, not `status == needs_review`. A row can
    # be status=ok and still need a human -- a blank price is expected, not
    # surprising, so nothing flags it, but it is still the thing an operator has
    # to go and fill in. Two definitions of "needs a human" would mean the
    # console and the worklist disagreeing about how much work is left.
    needs_human: bool = False
    missing: list[str] = Field(default_factory=list)
    # One character per CSV column: 3 high, 2 medium, 1 low, 0 nothing,
    # - locked. Empty until the row has produced something.
    cells: str = ""


class ArtifactOut(BaseModel):
    name: str
    filename: str
    bytes: int
    rows: int | None = None


class JobOut(BaseModel):
    """Everything a page refresh needs, rebuilt from the ledger.

    Deliberately complete rather than a delta: the client's whole recovery
    strategy is "refetch this and start listening again", and that only works
    if this is authoritative.
    """

    job_id: str
    state: str
    created_at: str
    finished_at: str | None
    settings: dict[str, object]
    counts: dict[str, int]
    total: int
    processed: int
    written: int
    failed: int
    needs_human: int
    running: bool
    queued: bool
    rows: list[RowOut]
    artifacts: list[ArtifactOut]
    # The 19 column names, in header order. Sent rather than hardcoded in the
    # console: the CSV contract lives in one Python module and the grid has to
    # be a picture of that, not of a copy someone forgot to update.
    columns: list[str]
    # From job.json: what the run cost, for the summary. Zero on a job that
    # never reached its finalise step.
    host_calls: int = 0
    pages_rendered: int = 0
    duration_s: float | None = None


class JobSummaryOut(BaseModel):
    job_id: str
    state: str
    created_at: str
    finished_at: str | None
    input_count: int
    counts: dict[str, int]


# ---------------------------------------------------------------------------
# The review table
# ---------------------------------------------------------------------------


class CellOut(BaseModel):
    """One cell. Carries what it says AND how much to trust it.

    `original` is populated only when the cell has been edited, so the table can
    show what the page said next to what the operator decided. That pairing is
    the whole reason edits are stored apart from the extraction.
    """

    field: str
    value: str
    confidence: str
    source: str
    editable: bool
    edited: bool
    original: str | None = None
    note: str | None = None
    locked_reason: str | None = None


class RowTableOut(BaseModel):
    row_key: str
    input_index: int
    source_url: str
    status: str
    needs_human: bool
    missing: list[str]
    low_confidence: list[str]
    notes: list[str]
    cells: list[CellOut]


class RowPageOut(BaseModel):
    job_id: str
    total: int
    offset: int
    limit: int
    columns: list[str]
    editable: list[str]
    rows: list[RowTableOut]
    pending_edits: int


class EditIn(BaseModel):
    """`{"fields": {"price_inr": "2499"}}`. Values arrive as strings and are
    normalised by the same validator the extractor's rules live in."""

    fields: dict[str, str]


class BulkEditIn(BaseModel):
    row_keys: list[str]
    fields: dict[str, str]


class ExportOut(BaseModel):
    job_id: str
    rows: int
    edits_applied: int
    rows_edited: int
