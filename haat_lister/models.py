"""The single data contract. Everything upstream of output/ speaks these types.

Two things here are load-bearing and deliberate:

1. `FieldValue` carries confidence and source alongside every value. The CSV
   writer emits only `.value`; the review writer reads the rest. That split is
   what lets the tool be honest about what it guessed.

2. `ProductRecord` has NO `gi_region` field. A GI tag is an Indian government
   certification, and marketing copy saying "authentic Banarasi" is not one. By
   leaving the field off the extractor's model entirely there is no code path
   that could populate it -- stronger than a runtime check that someone later
   removes. Any GI mention found in source text travels as
   `ProductRecord.gi_mention_found`, which reaches review.csv as a question for
   a human and never reaches the CSV.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class FieldSource(StrEnum):
    """Where a value came from. Appears verbatim in review.csv."""

    JSONLD = "jsonld"
    MICRODATA = "microdata"
    RDFA = "rdfa"
    OG = "og"
    TWITTER = "twitter"
    META = "meta"
    H1 = "h1"
    TITLE_TAG = "title_tag"
    SPEC_TABLE = "spec_table"
    VARIANTS = "variants"
    HEURISTIC = "heuristic"
    PLUGIN = "plugin"
    INFERRED = "inferred"
    FX_CONVERTED = "fx_converted"
    LLM = "llm"
    POLICY_DEFAULT = "policy_default"
    OPERATOR = "operator"


class Provenance(StrEnum):
    """Rule 2.1: required on every run, no default.

    OWN / AUTHORISED proceed normally. THIRD_PARTY forces every row to
    needs_review, blocks re-hosting of images the operator does not own, and
    forces description rewriting.
    """

    OWN = "own"
    AUTHORISED = "authorised"
    THIRD_PARTY = "third-party"


class ImageMode(StrEnum):
    MANIFEST = "manifest"
    URL_COLUMNS = "url_columns"
    BOTH = "both"

    @property
    def need_url(self) -> bool:
        """The CSV carries an image URL, so a URL must be produced."""
        return self in (ImageMode.URL_COLUMNS, ImageMode.BOTH)

    @property
    def need_file(self) -> bool:
        """A local image file is itself a deliverable."""
        return self in (ImageMode.MANIFEST, ImageMode.BOTH)


class ImageMethod(StrEnum):
    DIRECT = "direct"   # Tier 1: the source's own URL, validated
    LOCAL = "local"     # manifest mode: files only, no URL needed
    HOSTED = "hosted"   # Tier 2c: re-uploaded. Always names the Tier-1 failure.
    NONE = "none"       # Tier 3: honest failure


class RowStatus(StrEnum):
    OK = "ok"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class FetchStage(StrEnum):
    STATIC = "static"        # Stage A: httpx
    RENDERED = "rendered"    # Stage B: Playwright
    FAILED = "failed"        # Stage C
    CACHED = "cached"        # resumed from the ledger


class DescriptionMode(StrEnum):
    RAW = "raw"
    REWRITE = "rewrite"


class PriceStrategy(StrEnum):
    BLANK = "blank"
    COPY = "copy"
    CONVERT = "convert"
    MARKUP = "markup"


# ---------------------------------------------------------------------------
# FieldValue
# ---------------------------------------------------------------------------


class FieldValue(BaseModel, Generic[T]):
    """A value plus how much we trust it and where it came from.

    An absent value is a first-class, legitimate outcome: an empty cell that
    gets flagged is always correct, a confidently wrong value is not.
    """

    model_config = ConfigDict(frozen=True)

    value: T | None = None
    confidence: Confidence = Confidence.NONE
    source: FieldSource | None = None
    note: str | None = None

    @property
    def is_present(self) -> bool:
        return self.value is not None and self.value != ""

    @property
    def needs_human(self) -> bool:
        return not self.is_present or self.confidence in (Confidence.LOW, Confidence.NONE)

    @classmethod
    def missing(cls, note: str | None = None) -> FieldValue[T]:
        return cls(value=None, confidence=Confidence.NONE, source=None, note=note)

    @classmethod
    def found(
        cls,
        value: T,
        source: FieldSource,
        confidence: Confidence = Confidence.HIGH,
        note: str | None = None,
    ) -> FieldValue[T]:
        return cls(value=value, confidence=confidence, source=source, note=note)


StrField = FieldValue[str]
IntField = FieldValue[int]


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


class ValidationResult(BaseModel):
    """Outcome of the nine Tier-1 predicates for one candidate URL."""

    url: str
    ok: bool
    reason: str
    predicate: int | None = None          # which predicate decided it
    content_type: str | None = None
    content_length: int | None = None
    width: int | None = None
    height: int | None = None


class ImageFile(BaseModel):
    """One normalised local file, after Tier 2b."""

    order: int
    local_path: str
    original_source_url: str
    hosted_url: str | None = None
    bytes: int
    width: int
    height: int


class ImageResult(BaseModel):
    """What the image pipeline decided for one row, and why.

    `tier1_attempted` is True on every row without exception -- Tier 1 is never
    skipped. `reason` always names the failing predicate when the method is
    HOSTED, so a rising hosted ratio can be diagnosed rather than shrugged at.
    """

    url: str = ""
    method: ImageMethod = ImageMethod.NONE
    reason: str = ""
    files: list[ImageFile] = Field(default_factory=list)

    tier1_attempted: bool = True
    tier1_passed: bool = False
    candidate_results: list[ValidationResult] = Field(default_factory=list)

    download_used: bool = False
    upload_used: bool = False
    host_used: str | None = None
    bytes_downloaded: int = 0


# ---------------------------------------------------------------------------
# Product record
# ---------------------------------------------------------------------------


class ProductRecord(BaseModel):
    """The internal model. Note the absence of `gi_region` -- see module docstring.

    Field names match the CSV columns for the 18 writable columns purely as a
    convenience for the writer; `output/csv_writer.py` remains the only module
    that knows the column ORDER and the locked header.
    """

    # Identity and provenance
    row_key: str
    source_url: str
    canonical_url: str
    provenance: Provenance
    fetched_at: datetime | None = None
    fetch_stage: FetchStage = FetchStage.STATIC

    # --- CSV fields -------------------------------------------------------
    title: StrField = Field(default_factory=FieldValue)
    description: StrField = Field(default_factory=FieldValue)
    category_slug: StrField = Field(default_factory=FieldValue)
    subcategory_slug: StrField = Field(default_factory=FieldValue)
    custom_category: StrField = Field(default_factory=FieldValue)
    price_inr: IntField = Field(default_factory=FieldValue)
    hs_code: StrField = Field(default_factory=FieldValue)
    weight_g: IntField = Field(default_factory=FieldValue)
    length_cm: IntField = Field(default_factory=FieldValue)
    width_cm: IntField = Field(default_factory=FieldValue)
    height_cm: IntField = Field(default_factory=FieldValue)
    availability: StrField = Field(default_factory=FieldValue)
    stock_qty: IntField = Field(default_factory=FieldValue)
    sizes: StrField = Field(default_factory=FieldValue)
    # gi_region: intentionally absent. Do not add it.
    rfq_enabled: StrField = Field(default_factory=FieldValue)
    rfq_min_qty: IntField = Field(default_factory=FieldValue)
    bulk_only: StrField = Field(default_factory=FieldValue)
    seller_note: StrField = Field(default_factory=FieldValue)

    # --- Review-only context ---------------------------------------------
    source_price: float | None = None
    source_currency: str | None = None
    fx_rate_used: float | None = None
    fx_rate_as_of: str | None = None

    # A GI claim spotted in source text. Reaches review.csv as a question for a
    # human; never reaches the CSV.
    gi_mention_found: str | None = None

    policy_flags: list[str] = Field(default_factory=list)

    # Ranked output of extract/images.py and the input to the image pipeline.
    # Kept on the record so `validate-only` and the review file can both see
    # what was available before any network call was made.
    image_candidates: list[str] = Field(default_factory=list)
    structured_syntaxes: list[str] = Field(default_factory=list)
    image: ImageResult = Field(default_factory=ImageResult)

    status: RowStatus = RowStatus.OK
    failure_reason: str | None = None
    notes: list[str] = Field(default_factory=list)

    # field name -> the note emitted because that field was empty. See note_gap.
    gap_notes: dict[str, str] = Field(default_factory=dict)

    def field_values(self) -> dict[str, FieldValue[Any]]:
        """Every extracted field, by name.

        Discovered by introspection rather than a hardcoded list so that adding
        a field in Phase 5 cannot silently escape the review file.
        """
        return {
            name: value
            for name in type(self).model_fields
            if isinstance(value := getattr(self, name), FieldValue)
        }

    def note_gap(self, fields: str | list[str], text: str) -> None:
        """A note about a field being ABSENT, which a later stage may disprove.

        Ordinary notes are permanent because they describe something that
        happened. A gap note describes something that is *not there*, and Stage
        B or a plugin can make it untrue after the fact. Recording which fields
        each one is about is what lets `retract_filled_gaps` take it back --
        review.csv telling an operator to supply a weight that is already in the
        row is worse than saying nothing, because it costs them the trip.
        """
        self.note(text)
        for name in [fields] if isinstance(fields, str) else fields:
            self.gap_notes[name] = text

    def _is_filled(self, name: str) -> bool:
        value = getattr(self, name, None)
        return value.is_present if isinstance(value, FieldValue) else value is not None

    def retract_filled_gaps(self) -> None:
        """Drop gap notes whose field something later actually filled.

        A note covering several fields at once -- "no length, width or height" --
        survives until every field it names is filled, which is why the check is
        for any remaining claimant rather than a simple removal.
        """
        for name, text in list(self.gap_notes.items()):
            if self._is_filled(name):
                del self.gap_notes[name]
                if text not in self.gap_notes.values() and text in self.notes:
                    self.notes.remove(text)

    def note(self, text: str) -> None:
        """Record something a human should know, without calling the row suspect.

        Used for gaps we EXPECT: price_inr is blank by policy on every row, and
        haat's required dimensions are frequently absent from source pages. The
        row still reaches review.csv, because review_writer keys on
        `missing_required`, not on status. Marking these `needs_review` would
        make the status column mean "every row", which is the same as meaning
        nothing.
        """
        if text not in self.notes:
            self.notes.append(text)

    def flag(self, text: str) -> None:
        """Record something SURPRISING, and mark the row for attention.

        Reserved for judgement calls a human should overturn if we got them
        wrong -- a guessed dimension order, a shipping weight standing in for a
        product weight, a policy hit, an out-of-stock source.
        """
        self.note(text)
        if self.status is RowStatus.OK:
            self.status = RowStatus.NEEDS_REVIEW

    def fail(self, reason: str) -> None:
        self.status = RowStatus.FAILED
        self.failure_reason = reason
