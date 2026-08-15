"""Title extraction and cleanup.

A title is the one field with no acceptable blank: a row without one cannot be
listed, so it fails rather than being written empty.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

from ..config import ExtractionConfig
from ..models import Confidence, FieldSource, FieldValue, StrField
from .structured import StructuredData

_WHITESPACE = re.compile(r"\s+")


def normalise_text(value: str) -> str:
    """NFKC and whitespace collapse. Applied to every extracted string.

    NFKC matters more than it looks: storefronts are full of full-width
    punctuation, non-breaking spaces, and decorative unicode that would
    otherwise reach the CSV.
    """
    text = unicodedata.normalize("NFKC", value)
    text = text.replace("​", "").replace("﻿", "")
    return _WHITESPACE.sub(" ", text).strip()


def is_shouty(text: str, threshold: float) -> bool:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 8:
        return False
    return sum(c.isupper() for c in letters) / len(letters) > threshold


def strip_site_suffix(title: str, cfg: ExtractionConfig) -> str:
    """Drop trailing branding from a <title>: "Blue Kurta | MyStore | Free Shipping".

    Only ever applied to the <title> tag, never to a Product.name or an <h1>,
    because those do not carry site branding and a hyphen in them is far more
    likely to be part of the product name.
    """
    for separator in cfg.title_suffix_separators:
        if separator not in title:
            continue
        segments = [s.strip() for s in title.split(separator) if s.strip()]
        while len(segments) > 1 and len(segments[-1].split()) <= cfg.title_suffix_max_words:
            segments.pop()
        if segments:
            title = separator.join(segments)
    return title.strip()


# The SEO tail a marketplace listing carries:
#
#   Mivi DuoPods Marathon Earbuds Wireless | Fast Charge | 70H Playtime |
#   BT v5.3 | 13mm Drivers | Noise Cancellation
#
# The product is the first segment; the rest are attributes the seller stuffed
# into the name so it ranks. On haat that whole string becomes the listing
# title, which reads like spam and truncates the actual name.
_TAIL_SEPARATORS = (" | ", " - ", " – ", " — ")

# Marketing that is never part of a product's name.
_MARKETING_TAILS = (
    "free shipping",
    "free delivery",
    "best price",
    "lowest price",
    "buy online",
    "shop online",
    "online shopping",
    "cash on delivery",
    "with warranty",
    "official store",
    "authorized dealer",
    "authorised dealer",
)

# How many words the first segment must have before we believe it is the whole
# product name. "Mivi" alone is a brand; "Mivi DuoPods Marathon Earbuds" is a
# product. Below this we keep more segments rather than truncate to a brand.
_MIN_HEAD_WORDS = 3


def split_seo_tail(title: str) -> tuple[str, list[str]]:
    """Split `Name | Feature | Feature` into the name and the discarded parts.

    Conservative twice over: it needs THREE or more segments before it will cut
    anything -- "Blue Kurta - Medium" is a name, not a name plus a tail -- and
    the head must be a plausible product name on its own. A wrong cut here is
    worse than a long title, because the operator cannot see what was removed
    unless they open review.csv.

    Returns the title unchanged and an empty list when nothing should be cut.
    """
    for separator in _TAIL_SEPARATORS:
        segments = [s.strip() for s in title.split(separator) if s.strip()]
        if len(segments) < 3:
            continue
        head, tail = segments[0], segments[1:]
        if len(head.split()) < _MIN_HEAD_WORDS:
            # The first segment is a brand or a code. Take two rather than one:
            # "Mivi | DuoPods Marathon Earbuds | 70H" should not become "Mivi".
            if len(segments) >= 3 and len(f"{head} {segments[1]}".split()) >= _MIN_HEAD_WORDS:
                head, tail = f"{head} {segments[1]}", segments[2:]
            else:
                continue
        return head, tail
    return title, []


def _drop_marketing(title: str) -> str:
    """Trailing marketing, removed only when it is the LAST thing in the name.

    Anchored to the end on purpose: a stole genuinely called "Free Spirit" keeps
    its name, and only "… Free Shipping" loses one.
    """
    lowered = title.lower()
    for phrase in _MARKETING_TAILS:
        if lowered.endswith(phrase):
            return title[: -len(phrase)].rstrip(" ,;-|–—")
    return title


def clean_title(raw: str, cfg: ExtractionConfig, max_length: int) -> tuple[str, bool]:
    """Returns the cleaned title and whether it had to be de-shouted."""
    text = normalise_text(raw)
    shouty = is_shouty(text, cfg.allcaps_ratio_threshold)
    if shouty:
        # Case-normalise rather than discard: the words are still the product's.
        text = text.title()
    if len(text) > max_length:
        cut = text[:max_length].rsplit(" ", 1)[0]
        text = (cut or text[:max_length]).rstrip(" ,;-")
    return text, shouty


def extract_title(
    sd: StructuredData, dom: HTMLParser, cfg: ExtractionConfig, max_length: int
) -> StrField:
    """JSON-LD Product.name -> og:title -> <h1> -> <title> minus site suffix."""

    if found := sd.product_str("name", "title"):
        value, source = found
        text, shouty = clean_title(value, cfg, max_length)
        if text:
            return FieldValue.found(
                text,
                source,
                Confidence.MEDIUM if shouty else Confidence.HIGH,
                "ALL-CAPS source title was case-normalised" if shouty else None,
            )

    if found := (sd.og("og:title") or sd.tw("twitter:title")):
        value, source = found
        text, shouty = clean_title(value, cfg, max_length)
        if text:
            return FieldValue.found(
                text,
                source,
                Confidence.MEDIUM if shouty else Confidence.HIGH,
                "ALL-CAPS source title was case-normalised" if shouty else None,
            )

    if h1 := dom.css_first("h1"):
        text, shouty = clean_title(h1.text(strip=True) or "", cfg, max_length)
        if text:
            return FieldValue.found(
                text,
                FieldSource.H1,
                Confidence.MEDIUM,
                "Taken from <h1>; no structured product name on the page.",
            )

    if title_tag := dom.css_first("title"):
        stripped = strip_site_suffix(normalise_text(title_tag.text(strip=True) or ""), cfg)
        text, _ = clean_title(stripped, cfg, max_length)
        if text:
            return FieldValue.found(
                text,
                FieldSource.TITLE_TAG,
                Confidence.LOW,
                "Taken from <title> with site branding stripped; check it reads as a product name.",
            )

    return FieldValue.missing("No title found in structured data, og:title, <h1>, or <title>.")


@dataclass
class TitleCleanup:
    """A tidied title, plus everything the tidying took away.

    `original` and `attributes` are kept rather than discarded for two separate
    reasons. The original goes to review.csv so a bad clean is visible without
    re-fetching the page -- an operator who cannot see what was removed cannot
    tell a good cut from a lost product name. The attributes go to the
    description rewriter, because "70H Playtime, BT v5.3, 13mm Drivers" is real
    product information that happened to be living in the wrong field.
    """

    title: StrField
    original: str = ""
    attributes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.original) and self.original != (self.title.value or "")


def tidy_title(value: StrField, cfg: ExtractionConfig, max_length: int) -> TitleCleanup:
    """Strip the marketplace SEO tail off an extracted title.

    Applied after extraction rather than inside it, so every source -- JSON-LD,
    og:title, <h1>, <title> -- gets the same treatment and the extraction order
    stays a separate question from the cleaning rules.
    """
    original = value.value or ""
    if not original:
        return TitleCleanup(title=value)

    head, attributes = split_seo_tail(original)
    head = _drop_marketing(head)
    cleaned, _ = clean_title(head, cfg, max_length)

    if not cleaned or cleaned == original:
        return TitleCleanup(title=value, original=original)

    note = (
        f"Shortened from the source title, which carried {len(attributes)} extra segment(s): "
        f"{original}"
    )
    return TitleCleanup(
        title=FieldValue.found(
            cleaned,
            value.source or FieldSource.HEURISTIC,
            value.confidence,
            note if not value.note else f"{value.note} {note}",
        ),
        original=original,
        attributes=attributes,
    )
