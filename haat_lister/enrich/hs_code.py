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


def _material_match(text: str, category: str, cfg: HsCodesConfig) -> tuple[str, str] | None:
    """The description resolves the fork WITHIN a shelf, so the shelf is required.

    Keywords used to be global, and a global keyword hijacks every category: a
    brass bell in `more-crafts` was handed 7117, imitation jewellery, because
    `brass` was on the list for the jewellery fork. The shelf comes first
    because that is how classification actually works -- `sterling silver` is
    7113 on a necklace and cutlery on a spoon.
    """
    for keyword, code in cfg.by_material_keyword.get(category, {}).items():
        if re.search(rf"(?<!\w){re.escape(keyword.lower())}(?!\w)", text):
            return keyword, code
    return None


def suggest(record: ProductRecord, cfg: HsCodesConfig) -> HsResult:
    result = HsResult()
    text = f"{record.title.value or ''} {record.description.value or ''}".lower()
    category = str(record.category_slug.value or "")

    if found := _material_match(text, category, cfg):
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

    # Subcategory before category: on shelves where one code cannot be right
    # for the whole shelf, the sub-shelf is where it becomes fixed. `sarees`
    # and `home-textiles` are both `handwoven-textiles` and are not the same
    # chapter, so a category-level code would be wrong for most of the shelf.
    subcategory = str(record.subcategory_slug.value or "")
    if subcategory and (sub_code := cfg.by_subcategory.get(f"{category}/{subcategory}")):
        result.hs_code = FieldValue.found(
            sub_code,
            FieldSource.INFERRED,
            Confidence.MEDIUM,
            f"Suggested from the subcategory '{subcategory}'.",
        )
        result.notes.append(
            f"hs_code {sub_code} is a SUGGESTION from the subcategory "
            f"'{category}/{subcategory}'. HS classification is a customs declaration -- "
            "confirm it before importing."
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
