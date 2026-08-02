"""HS code suggestion.

Confidence is capped at MEDIUM by design and every value reaches review.csv even
when populated. An HS code is a customs declaration: a wrong one is a legal and
financial problem for the seller, not a cosmetic defect.

The map ships nearly empty on purpose -- only the two headings evidenced by
haat's own template sample rows. Everything else yields a blank cell and a flag
until an operator supplies an authoritative list. Two correct mappings and four
hundred flagged blanks beat a plausible invented table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import HsCodesConfig
from ..models import Confidence, FieldSource, FieldValue, ProductRecord, StrField


@dataclass
class HsResult:
    hs_code: StrField = field(default_factory=FieldValue)
    notes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def _material_match(text: str, cfg: HsCodesConfig) -> tuple[str, str] | None:
    """Material keywords are more specific than category, so they win."""
    for keyword, code in cfg.by_material_keyword.items():
        if re.search(rf"(?<!\w){re.escape(keyword.lower())}(?!\w)", text):
            return keyword, code
    return None


def suggest(record: ProductRecord, cfg: HsCodesConfig) -> HsResult:
    result = HsResult()
    text = f"{record.title.value or ''} {record.description.value or ''}".lower()
    category = str(record.category_slug.value or "")

    if found := _material_match(text, cfg):
        keyword, code = found
        result.hs_code = FieldValue.found(
            code,
            FieldSource.INFERRED,
            Confidence.MEDIUM,
            f"Suggested from the material keyword '{keyword}'.",
        )
        # A note, not a flag: every row with a code would otherwise be marked
        # needs_review, which would make that status mean "all rows". The row
        # still reaches review.csv, because a medium-confidence field always
        # lands in `low_confidence_fields`.
        result.notes.append(
            f"hs_code {code} is a SUGGESTION from the keyword '{keyword}'. HS classification is a "
            "customs declaration -- confirm it before importing."
        )
        return result

    category_code = cfg.by_category.get(category)
    if category_code:
        code = category_code
        result.hs_code = FieldValue.found(
            code,
            FieldSource.INFERRED,
            Confidence.MEDIUM,
            f"Suggested from the category '{category}'.",
        )
        result.notes.append(
            f"hs_code {code} is a SUGGESTION from the category '{category}', which is broad. "
            "HS classification is a customs declaration -- confirm it before importing."
        )
        return result

    result.notes.append(
        f"No HS code mapping for category '{category or 'unknown'}'. Left blank rather than "
        "guessed; extend config.yaml -> hs_codes when you have an authoritative list."
    )
    return result
