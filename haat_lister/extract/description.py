"""Description extraction.

The structured sources are usually clean. The DOM fallback is where the mess
lives: "Add to cart", shipping banners, review counts and cookie notices sit
inside the same container as the actual copy on a lot of themes, so anything
reaching the DOM path gets line-level boilerplate filtering and a confidence
downgrade.
"""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from ..config import ExtractionConfig
from ..models import Confidence, FieldSource, FieldValue, StrField
from .structured import StructuredData
from .title import normalise_text

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLANK_RUNS = re.compile(r"\n{3,}")


def strip_control_characters(text: str) -> str:
    """Required before the CSV sees it; a stray control byte can break an import."""
    return _CONTROL_CHARS.sub("", text)


def _looks_like_boilerplate(line: str, patterns: list[str]) -> bool:
    lowered = line.lower()
    return any(p in lowered for p in patterns)


def clean_description(raw: str, cfg: ExtractionConfig, max_length: int) -> str:
    """Drop UI chrome line by line, keep paragraph structure, cap length.

    Filtering per line rather than per block is deliberate: a good description
    with one "Free shipping over ₹999" line in the middle should lose that line,
    not the whole description.
    """
    text = strip_control_characters(raw).replace("\r\n", "\n").replace("\r", "\n")

    kept: list[str] = []
    for line in text.split("\n"):
        cleaned = normalise_text(line)
        if not cleaned:
            kept.append("")
            continue
        if _looks_like_boilerplate(cleaned, cfg.description_boilerplate):
            continue
        kept.append(cleaned)

    result = _BLANK_RUNS.sub("\n\n", "\n".join(kept)).strip()

    if len(result) > max_length:
        cut = result[:max_length].rsplit(" ", 1)[0]
        result = (cut or result[:max_length]).rstrip(" ,;-") + "…"
    return result


def _dom_description(dom: HTMLParser, cfg: ExtractionConfig) -> str | None:
    """First configured selector holding enough text wins."""
    for selector in cfg.description_selectors:
        for node in dom.css(selector):
            text = node.text(separator="\n", strip=True)
            if text and len(normalise_text(text)) >= cfg.description_min_length:
                return text
    return None


def extract_description(
    sd: StructuredData, dom: HTMLParser, cfg: ExtractionConfig, max_length: int
) -> StrField:
    """Product.description -> og:description -> meta description -> DOM block."""

    if found := sd.product_str("description"):
        value, source = found
        text = clean_description(value, cfg, max_length)
        if len(text) >= cfg.description_min_length:
            return FieldValue.found(text, source, Confidence.HIGH)

    if found := (sd.og("og:description") or sd.tw("twitter:description")):
        value, source = found
        text = clean_description(value, cfg, max_length)
        if len(text) >= cfg.description_min_length:
            return FieldValue.found(text, source, Confidence.HIGH)

    if meta_description := sd.meta.get("description"):
        text = clean_description(meta_description, cfg, max_length)
        if len(text) >= cfg.description_min_length:
            return FieldValue.found(
                text,
                FieldSource.META,
                Confidence.MEDIUM,
                "From <meta name=description>, which is often written for search engines "
                "rather than for buyers.",
            )

    if block := _dom_description(dom, cfg):
        text = clean_description(block, cfg, max_length)
        if len(text) >= cfg.description_min_length:
            return FieldValue.found(
                text,
                FieldSource.HEURISTIC,
                Confidence.LOW,
                "Scraped from a page block; check for leftover site furniture.",
            )

    return FieldValue.missing(
        "No description found in structured data, og:description, meta, or a known "
        "description block."
    )
