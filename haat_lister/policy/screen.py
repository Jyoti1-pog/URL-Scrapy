"""Prohibited-content pre-flight (Rule 2.4).

Runs before any row is written. It FLAGS; it never silently drops. The screen is
deliberately noisy -- a false positive costs a human ten seconds, a false
negative costs a delisting or a seized parcel.

The one thing it does beyond flagging: any GI mention it finds is recorded as a
question for a human in `gi_mention_found`. The `gi_region` column stays empty
regardless of what the source page claims.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from ..utils.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class PolicyHit:
    category: str
    label: str
    term: str
    where: str  # "title" or "description"

    @property
    def flag(self) -> str:
        return f"{self.category}:{self.term}"


@dataclass
class Vocabulary:
    categories: dict[str, tuple[str, list[str]]]  # name -> (label, terms)
    gi_categories: set[str]
    brands: list[str]

    @property
    def is_empty(self) -> bool:
        return not self.categories and not self.brands


def _compile(term: str) -> re.Pattern[str]:
    """Word-boundary match so 'gun' does not fire on 'begun'."""
    return re.compile(rf"(?<!\w){re.escape(term.strip())}(?!\w)", re.IGNORECASE)


@lru_cache(maxsize=8)
def _patterns(terms: tuple[str, ...]) -> list[tuple[str, re.Pattern[str]]]:
    return [(term, _compile(term)) for term in terms]


def load_vocabulary(keywords_file: Path, brands_file: Path) -> Vocabulary:
    """Missing files degrade to an empty screen, loudly.

    An empty screen is a real risk, so `config-check` reports it rather than
    letting a run quietly skip policy screening.
    """
    categories: dict[str, tuple[str, list[str]]] = {}
    gi_categories: set[str] = set()

    if keywords_file.exists():
        raw = yaml.safe_load(keywords_file.read_text(encoding="utf-8")) or {}
        for name, block in (raw.get("categories") or {}).items():
            terms = [str(t) for t in (block.get("terms") or [])]
            categories[name] = (block.get("label", name), terms)
        gi_categories = set(raw.get("gi_categories") or [])
    else:
        log.warning("Policy keywords file not found: %s", keywords_file)

    brands: list[str] = []
    if brands_file.exists():
        brands = [
            line.strip()
            for line in brands_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")
        ]
    else:
        log.warning("Brand list not found: %s", brands_file)

    return Vocabulary(categories=categories, gi_categories=gi_categories, brands=brands)


def screen_text(title: str, description: str, vocabulary: Vocabulary) -> list[PolicyHit]:
    """Every match, in both fields. Order is stable for reproducible output."""
    hits: list[PolicyHit] = []
    fields = (("title", title or ""), ("description", description or ""))

    for category, (label, terms) in vocabulary.categories.items():
        for term, pattern in _patterns(tuple(terms)):
            for where, text in fields:
                if pattern.search(text):
                    hits.append(
                        PolicyHit(category=category, label=label, term=term, where=where)
                    )
                    break  # one hit per term is enough to earn a human's attention

    for term, pattern in _patterns(tuple(vocabulary.brands)):
        for where, text in fields:
            if pattern.search(text):
                hits.append(
                    PolicyHit(
                        category="brand_token",
                        label="Registered brand token -- counterfeit risk",
                        term=term,
                        where=where,
                    )
                )
                break

    return hits


def gi_mentions(hits: list[PolicyHit], vocabulary: Vocabulary) -> list[PolicyHit]:
    return [hit for hit in hits if hit.category in vocabulary.gi_categories]


def describe(hits: list[PolicyHit]) -> list[str]:
    """Human-readable lines for review.csv and the console."""
    by_category: dict[str, list[PolicyHit]] = {}
    for hit in hits:
        by_category.setdefault(hit.category, []).append(hit)

    lines: list[str] = []
    for hits_in_category in by_category.values():
        label = hits_in_category[0].label
        terms = ", ".join(sorted({h.term for h in hits_in_category}))
        lines.append(f"{label}: matched {terms}. Check before listing.")
    return lines
