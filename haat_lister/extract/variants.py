"""Sizes, availability and stock quantity.

The rule that shapes this module: never default. An unknown availability is
blank and flagged, not `stock`; vague copy like "only a few left" is blank, not
a number someone made up. A wrong `stock_qty` oversells a piece that does not
exist, and haat's whole model is one-of-a-kind craft.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

from ..config import ExtractionConfig, FieldsConfig
from ..models import Confidence, FieldSource, FieldValue, IntField, StrField
from .specs import spec_pairs
from .structured import StructuredData, scalar
from .title import normalise_text

# schema.org ItemAvailability values, lowercased leaf names.
_IN_STOCK = {"instock", "instoreonly", "limitedavailability", "onlineonly"}
_MADE_TO_ORDER = {"madetoorder", "preorder", "presale", "backorder"}
_OUT_OF_STOCK = {"outofstock", "soldout", "discontinued"}

_STOCK_COUNT = re.compile(r"(\d{1,6})\s*(?:in stock|available|left|pieces?|units?)", re.IGNORECASE)


# Exported so `pipeline` can register it as a retractable gap note rather than
# matching on a duplicated literal. See ProductRecord.note_gap.
NO_AVAILABILITY_NOTE = (
    "Could not determine availability; left blank rather than defaulting to stock."
)


@dataclass
class VariantResult:
    sizes: StrField = field(default_factory=FieldValue)
    availability: StrField = field(default_factory=FieldValue)
    stock_qty: IntField = field(default_factory=FieldValue)
    # `notes` are expected gaps; `flags` are judgement calls to overturn.
    notes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Sizes
# ---------------------------------------------------------------------------


def _clean_sizes(raw: list[str], cfg: ExtractionConfig) -> list[str]:
    """Preserve source order, drop prompts, dedupe case-insensitively.

    Casing is normalised only for the standard letter sizes; numeric and free
    sizes ("38", "Free Size", "One size") are kept as written, because that is
    what the seller's own labelling says.
    """
    noise = {n.lower() for n in cfg.size_noise}
    seen: set[str] = set()
    out: list[str] = []

    for value in raw:
        text = normalise_text(value)
        if not text or text.lower() in noise or len(text) > 24:
            continue
        if re.fullmatch(r"(?i)(xs|s|m|l|xl|xxl|xxxl|[2-5]xl)", text):
            text = text.upper()
        if text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)

    return out


def extract_sizes(sd: StructuredData, dom: HTMLParser, cfg: ExtractionConfig) -> StrField:
    # JSON-LD variants carry the cleanest answer when they exist.
    if sd.product:
        variants = sd.product.get("hasVariant")
        found: list[str] = []
        for variant in variants if isinstance(variants, list) else [variants]:
            if isinstance(variant, dict):
                value = scalar(variant.get("size") or variant.get("name"))
                if value:
                    found.append(value)
        if sizes := _clean_sizes(found, cfg):
            return FieldValue.found(
                ",".join(sizes), sd.product_source or FieldSource.JSONLD, Confidence.HIGH
            )

    for selector in cfg.size_selectors:
        nodes = dom.css(selector)
        if not nodes:
            continue
        raw = [
            n.attributes.get("data-value") or n.attributes.get("value") or n.text(strip=True) or ""
            for n in nodes
        ]
        if sizes := _clean_sizes(raw, cfg):
            return FieldValue.found(",".join(sizes), FieldSource.VARIANTS, Confidence.MEDIUM)

    # Non-apparel products legitimately have none; that is not a problem.
    return FieldValue.missing("No size variants found.")


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def _schema_availability(sd: StructuredData) -> str | None:
    if not sd.product:
        return None
    offers = sd.product.get("offers")
    for offer in offers if isinstance(offers, list) else [offers]:
        if isinstance(offer, dict):
            if raw := scalar(offer.get("availability")):
                return raw.rsplit("/", 1)[-1].rsplit("#", 1)[-1].lower()
    return None


def extract_availability(
    sd: StructuredData, dom: HTMLParser, fields: FieldsConfig
) -> tuple[StrField, list[str], list[str]]:
    """Map to haat's vocabulary, or leave blank. Never default to in-stock.

    Returns (field, notes, flags). Plenty of product pages simply never state
    availability, so failing to find one is a note; a page that states something
    we deliberately refuse to act on -- out of stock, or made-to-order while its
    wire value is unconfigured -- is a flag.
    """
    notes: list[str] = []
    flags: list[str] = []
    state = _schema_availability(sd)

    if state is None:
        text = " ".join(
            normalise_text(n.text(strip=True)) for n in dom.css("[itemprop='availability'], .stock")
        ).lower()
        if "out of stock" in text or "sold out" in text:
            state = "outofstock"
        elif "made to order" in text or "made-to-order" in text:
            state = "madetoorder"
        elif "in stock" in text or "available" in text:
            state = "instock"

    if state in _IN_STOCK:
        value = fields.availability_in_stock_value
        if value and value in fields.availability_values:
            return FieldValue.found(value, FieldSource.JSONLD, Confidence.HIGH), notes, flags

    if state in _MADE_TO_ORDER:
        value = fields.availability_made_to_order_value
        if value:
            return FieldValue.found(value, FieldSource.JSONLD, Confidence.HIGH), notes, flags
        flags.append(
            "Page says made-to-order, but haat's wire value for that state is not configured "
            "(config.yaml -> fields.availability_made_to_order_value). Left blank rather than "
            "guessing an enum the importer would reject."
        )
        return FieldValue.missing("Made-to-order value not configured."), notes, flags

    if state in _OUT_OF_STOCK:
        flags.append(
            "Page says out of stock. availability left blank -- decide whether to list this "
            "piece at all."
        )
        return FieldValue.missing("Source is out of stock."), notes, flags

    notes.append(NO_AVAILABILITY_NOTE)
    return FieldValue.missing("No availability signal on the page."), notes, flags


# ---------------------------------------------------------------------------
# Stock quantity
# ---------------------------------------------------------------------------


def extract_stock_qty(
    sd: StructuredData, dom: HTMLParser, cfg: ExtractionConfig
) -> tuple[IntField, list[str]]:
    notes: list[str] = []

    if sd.product:
        offers = sd.product.get("offers")
        for offer in offers if isinstance(offers, list) else [offers]:
            if not isinstance(offer, dict):
                continue
            level = offer.get("inventoryLevel")
            raw = scalar(level.get("value")) if isinstance(level, dict) else scalar(level)
            if raw and raw.strip().isdigit():
                return (
                    FieldValue.found(
                        int(raw), sd.product_source or FieldSource.JSONLD, Confidence.HIGH
                    ),
                    notes,
                )

    pairs = spec_pairs(dom)
    for key in ("stock", "quantity", "quantity in stock", "available quantity"):
        if value := pairs.get(key):
            if match := _STOCK_COUNT.search(value) or re.fullmatch(r"\s*(\d{1,6})\s*", value):
                return (
                    FieldValue.found(
                        int(match.group(1)), FieldSource.SPEC_TABLE, Confidence.MEDIUM
                    ),
                    notes,
                )

    body = normalise_text(dom.body.text(separator=" ", strip=True)) if dom.body else ""
    if match := _STOCK_COUNT.search(body):
        return FieldValue.found(int(match.group(1)), FieldSource.HEURISTIC, Confidence.LOW), notes

    lowered = body.lower()
    if any(phrase in lowered for phrase in cfg.vague_stock_phrases):
        notes.append(
            "The page hints at stock levels in words rather than numbers, so stock_qty is blank. "
            "It is never defaulted to 1 or 10."
        )

    return FieldValue.missing("No stock count found."), notes


def extract_variants(
    sd: StructuredData, dom: HTMLParser, cfg: ExtractionConfig, fields: FieldsConfig
) -> VariantResult:
    result = VariantResult(sizes=extract_sizes(sd, dom, cfg))
    result.availability, availability_notes, availability_flags = extract_availability(
        sd, dom, fields
    )
    result.stock_qty, stock_notes = extract_stock_qty(sd, dom, cfg)
    result.notes.extend([*availability_notes, *stock_notes])
    result.flags.extend(availability_flags)
    return result
