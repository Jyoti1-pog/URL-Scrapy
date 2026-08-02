"""Title extraction and cleanup.

A title is the one field with no acceptable blank: a row without one cannot be
listed, so it fails rather than being written empty.
"""

from __future__ import annotations

import re
import unicodedata

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
