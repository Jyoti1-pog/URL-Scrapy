"""Human edits: what may be changed, to what, and what happens to the original.

Three rules shape this module.

**Edits never overwrite the extraction.** They live in `row_edits`, beside the
stored record rather than inside it, and a re-export applies them on the way
out. Deleting an edit therefore restores what the page actually said -- which
matters because six weeks later "did we scrape this or did I type it?" is a real
question, and `review.csv` is the only thing that can answer it.

**The same rules as extraction, not a second set.** A slug must exist in
`taxonomy.yaml`; a price must be a positive integer; availability must be in the
configured vocabulary. If the API were more permissive than the extractor, the
CSV would import for rows nobody touched and fail for rows somebody fixed.

**`gi_region` is not editable, here either.** The CLI cannot write it because
the record has no such field. The API cannot write it because it is absent from
`EDITABLE` and explicitly named in the refusal. A GI tag is an Indian government
certification and haat makes it a seller declaration; a text box in an
extraction tool is the wrong place to assert one.

This lives in the core rather than in `api/` because it is domain logic. The
console is the first caller; it should not be the only possible one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Settings
from .models import Confidence, FieldSource, FieldValue, ProductRecord
from .output.csv_writer import HAAT_COLUMNS
from .utils.logging import get_logger

log = get_logger(__name__)

# Never editable, and named rather than merely omitted so the refusal can
# explain itself.
LOCKED: frozenset[str] = frozenset({"gi_region"})

# The 18 writable columns. Derived from the header so a column added to the CSV
# cannot silently become uneditable, or editable, without someone deciding.
EDITABLE: frozenset[str] = frozenset(HAAT_COLUMNS) - LOCKED

INTEGER_FIELDS: frozenset[str] = frozenset(
    {"price_inr", "weight_g", "length_cm", "width_cm", "height_cm", "stock_qty", "rfq_min_qty"}
)

_SIZES = re.compile(r"^[A-Za-z0-9./+-]+(,[A-Za-z0-9./+-]+)*$")
_HS_CODE = re.compile(r"^\d{4,10}$")


class EditError(ValueError):
    """A rejected edit. The message is shown to the operator verbatim, so it
    says what to do rather than what went wrong."""


@dataclass(frozen=True)
class Edit:
    field: str
    value: str


def validate(field: str, raw: str, settings: Settings, record: ProductRecord) -> str:
    """Return the normalised value, or raise EditError with a usable message.

    `record` is needed because a subcategory is only valid under its parent, and
    the parent may itself be being edited in the same request -- callers apply
    edits in order and pass the record as it stands.
    """
    if field in LOCKED:
        raise EditError(
            "gi_region cannot be set here. A GI tag is an Indian government certification "
            "and haat treats it as a seller declaration, so this tool leaves it blank on "
            "every row. If your product genuinely carries one, confirm it against the GI "
            "registry and set it in haat's own listing form."
        )
    if field not in EDITABLE:
        raise EditError(
            f"{field!r} is not a column in the import file. "
            f"Editable: {', '.join(sorted(EDITABLE))}."
        )

    value = raw.strip()
    cfg = settings.config

    if field in INTEGER_FIELDS:
        return _integer(field, value)

    if field == "category_slug":
        if value and not settings.taxonomy.has_category(value):
            raise EditError(
                f"{value!r} is not a category in taxonomy.yaml. "
                f"Choose one of: {', '.join(sorted(settings.taxonomy.categories))}."
            )
        return value

    if field == "subcategory_slug":
        parent = record.category_slug.value or ""
        if value and not settings.taxonomy.has_subcategory(parent, value):
            category = settings.taxonomy.categories.get(parent)
            available = sorted(category.subcategories) if category else []
            raise EditError(
                f"{value!r} is not a subcategory of {parent or '(no category yet)'}. "
                + (
                    f"Under {parent}: {', '.join(available)}."
                    if available
                    else "Set the category first."
                )
            )
        return value

    if field == "availability":
        allowed = list(cfg.fields.availability_values)
        if value and value not in allowed:
            raise EditError(
                f"{value!r} is not one of haat's availability values "
                f"({', '.join(allowed) or 'none configured'}). Leave it blank rather than "
                "guessing -- a value haat does not recognise fails the import."
            )
        return value

    if field == "sizes":
        compact = value.replace(" ", "")
        if compact and not _SIZES.fullmatch(compact):
            raise EditError(
                "Sizes are comma-separated with no spaces, like S,M,L or 38,40,42."
            )
        return compact

    if field == "hs_code":
        if value and not _HS_CODE.fullmatch(value):
            raise EditError(
                "An HS code is 4 to 10 digits. It is a customs declaration, so it is better "
                "left blank than guessed."
            )
        return value

    if field == "title":
        if not value:
            raise EditError("A listing with no title cannot be published.")
        return value[: cfg.csv.max_title_length]

    if field == "description":
        return value[: cfg.csv.max_description_length]

    if field in ("rfq_enabled", "bulk_only"):
        accepted = {"", cfg.fields.yes_value, cfg.fields.blank_value}
        if value not in accepted:
            raise EditError(
                f"{field} is {cfg.fields.yes_value!r} or blank."
            )
        return value

    return value


def _integer(field: str, value: str) -> str:
    if not value:
        return ""
    try:
        number = int(value.replace(",", "").replace(" ", ""))
    except ValueError:
        raise EditError(f"{field} is a whole number. {value!r} is not one.") from None
    if number < 0:
        raise EditError(f"{field} cannot be negative.")
    if field == "price_inr" and number == 0:
        raise EditError(
            "A price of 0 would publish the product as free. Leave it blank if you have "
            "not decided yet -- blank is a legitimate cell here."
        )
    return str(number)


def apply_edits(
    record: ProductRecord, edits: dict[str, str], settings: Settings
) -> ProductRecord:
    """A COPY of the record with the edits laid over it.

    The original is never mutated, which is what makes "preserved alongside the
    edit" true rather than aspirational: the stored payload keeps saying what the
    page said, and this is the view that reaches the CSV.
    """
    edited = record.model_copy(deep=True)
    # Ordered so category lands before subcategory: the latter is validated
    # against the former, and a request setting both should be judged on what
    # it is asking for rather than on what the row used to be.
    for field in sorted(edits, key=lambda f: 0 if f == "category_slug" else 1):
        value = validate(field, edits[field], settings, edited)
        setattr(
            edited,
            field,
            FieldValue.found(
                _coerce(field, value),
                FieldSource.OPERATOR,
                Confidence.HIGH,
                note="Set by hand in the review table.",
            )
            if value != ""
            else FieldValue(),
        )
    return edited


def _coerce(field: str, value: str) -> object:
    return int(value) if field in INTEGER_FIELDS else value


def describe_change(record: ProductRecord, field: str, value: str) -> str:
    """One line for the ledger's benefit and the operator's: what it was, what
    it is now. Shown in review.csv so an edit is never invisible."""
    current = getattr(record, field, None)
    before = "" if current is None or not current.is_present else str(current.value)
    return f"{field}: {before or '(blank)'} -> {value or '(blank)'}"
