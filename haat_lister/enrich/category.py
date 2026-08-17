"""Category and subcategory assignment.

Two hard rules from the spec:

  - Never invent a slug. Every value emitted here comes out of taxonomy.yaml.
    An unrecognised slug is a row failure, not a warning, because it either
    rejects the import or buries the listing in an aisle nobody browses.
  - Ambiguity resolves to `more-crafts` plus `custom_category` plus a flag. That
    is an honest "I don't know", which is what haat's own "Other -- my craft
    isn't listed" option is for.

Scoring is deliberately simple keyword matching. It is auditable, it needs no
model, and when it is wrong the operator fixes it by editing a keyword list
rather than retraining anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import Taxonomy
from ..models import Confidence, FieldSource, FieldValue, ProductRecord, StrField

# A title hit is worth more than a description hit: descriptions mention
# materials and occasions, titles name the thing itself.
TITLE_WEIGHT = 3
DESCRIPTION_WEIGHT = 1
# The shop's own breadcrumb, weighted above its product title, because it is a
# different KIND of evidence: a title is marketing copy and a trail is the shop
# stating which shelf it files this on. "Sarees > Silk Sarees" settles both the
# category and the subcategory; "Rani Pink Dola Printed" settles neither
# reliably, and a product that matched no keyword fell all the way to
# `more-crafts` -- which has no shelves and no HS code, so one miss emptied
# three columns at once.
TRAIL_WEIGHT = 5

# Below this, we do not believe our own answer.
MIN_SCORE = TITLE_WEIGHT


@dataclass
class CategoryResult:
    category_slug: StrField = field(default_factory=FieldValue)
    subcategory_slug: StrField = field(default_factory=FieldValue)
    custom_category: StrField = field(default_factory=FieldValue)
    notes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    scores: dict[str, int] = field(default_factory=dict)


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    r"""A keyword, matching its plural too.

    The taxonomy is written in the singular -- `saree`, `dupatta`, `necklace` --
    and shops write shelves and titles in the plural. `(?!\w)` after the
    keyword meant "Sarees" did not match "saree", so a product whose title or
    breadcrumb said `Sarees` scored zero against the saree shelf and fell all
    the way to `more-crafts`, which has no shelves and no HS code. One missed
    plural emptied three columns.

    Only the regular forms, and only as a SUFFIX on the whole keyword. Anything
    cleverer -- stemming, a plural dictionary -- would start matching words the
    operator did not write into their taxonomy, which is the thing this file is
    careful not to do.
    """
    return re.compile(rf"(?<!\w){re.escape(keyword.lower())}(?:e?s)?(?!\w)")


def _score(keywords: list[str], title: str, description: str, trail: str = "") -> int:
    total = 0
    for keyword in keywords:
        pattern = _keyword_pattern(keyword)
        if pattern.search(title):
            total += TITLE_WEIGHT
        if pattern.search(description):
            total += DESCRIPTION_WEIGHT
        if trail and pattern.search(trail):
            total += TRAIL_WEIGHT
    return total


def _best(
    candidates: dict[str, list[str]], title: str, description: str, trail: str = ""
) -> tuple[str | None, dict[str, int], bool]:
    """Returns (winner, all scores, was_a_tie)."""
    scores = {
        slug: _score(keywords, title, description, trail)
        for slug, keywords in candidates.items()
    }
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

    if not ranked or ranked[0][1] < MIN_SCORE:
        return None, scores, False

    tied = len(ranked) > 1 and ranked[1][1] == ranked[0][1]
    return ranked[0][0], scores, tied


def _custom_category_from_title(title: str) -> str:
    """A starting point for haat's required "Name your category" field.

    haat asks for a craft name ("Brassware", "Hand tools"), not a product title,
    so this is explicitly low confidence and always flagged. It is offered
    because the field is required and a blank would block publishing -- the
    operator is expected to shorten it.
    """
    cleaned = re.sub(r"\s+", " ", title).strip()
    words = cleaned.split()
    return " ".join(words[:6])


def classify(
    record: ProductRecord, taxonomy: Taxonomy, trail: list[str] | None = None
) -> CategoryResult:
    title = (record.title.value or "").lower()
    description = (record.description.value or "").lower()
    crumbs = " ".join(trail or []).lower()
    result = CategoryResult()

    parent_keywords = {slug: cat.keywords for slug, cat in taxonomy.categories.items()}
    winner, scores, tied = _best(parent_keywords, title, description, crumbs)
    result.scores = scores

    if winner is None or tied:
        fallback = taxonomy.fallback_category
        reason = (
            "nothing matched a category keyword"
            if winner is None
            else f"'{winner}' tied with another category"
        )
        result.category_slug = FieldValue.found(
            fallback,
            FieldSource.INFERRED,
            Confidence.LOW,
            f"Fell back to {fallback} because {reason}.",
        )
        result.custom_category = FieldValue.found(
            _custom_category_from_title(record.title.value or ""),
            FieldSource.INFERRED,
            Confidence.LOW,
            "Derived from the title; haat wants a craft name, so shorten it.",
        )
        result.flags.append(
            f"Category could not be determined ({reason}); filed under {fallback} with a "
            "custom_category derived from the title. Set the real category and shorten the "
            "custom name."
        )
        return result

    result.category_slug = FieldValue.found(winner, FieldSource.INFERRED, Confidence.MEDIUM)

    category = taxonomy.categories[winner]
    if not category.subcategories:
        # `more-crafts` legitimately has no shelves.
        if winner == taxonomy.fallback_category:
            result.custom_category = FieldValue.found(
                _custom_category_from_title(record.title.value or ""),
                FieldSource.INFERRED,
                Confidence.LOW,
                "Derived from the title; haat wants a craft name, so shorten it.",
            )
        result.notes.append(f"'{winner}' has no shelves in taxonomy.yaml, so subcategory is blank.")
        return result

    child_keywords = {slug: sub.keywords for slug, sub in category.subcategories.items()}
    child, child_scores, child_tied = _best(child_keywords, title, description, crumbs)
    result.scores.update({f"{winner}/{k}": v for k, v in child_scores.items()})

    if child is None or child_tied:
        result.subcategory_slug = FieldValue.missing("No shelf matched confidently.")
        result.flags.append(
            f"Filed under '{winner}' but no shelf matched confidently; subcategory_slug is blank "
            "rather than guessed. Pick one from taxonomy.yaml."
        )
        return result

    result.subcategory_slug = FieldValue.found(child, FieldSource.INFERRED, Confidence.MEDIUM)
    return result


def validate_slugs(record: ProductRecord, taxonomy: Taxonomy) -> str | None:
    """Last line of defence before a row is written.

    Nothing in `classify` can produce an off-taxonomy slug, but a per-domain
    plugin (Phase 11) or the LLM assist (Phase 12) can, and both write to the
    same fields. Returns a failure reason, or None when the row is safe.
    """
    parent = record.category_slug
    child = record.subcategory_slug

    if parent.is_present and not taxonomy.has_category(str(parent.value)):
        return f"unknown_category_slug:{parent.value}"

    if child.is_present:
        if not parent.is_present:
            return f"subcategory_without_category:{child.value}"
        if not taxonomy.has_subcategory(str(parent.value), str(child.value)):
            return f"unknown_subcategory_slug:{parent.value}/{child.value}"

    return None
