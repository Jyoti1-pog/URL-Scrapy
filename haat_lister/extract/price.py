"""Price and currency detection.

This module DETECTS. It does not decide. The default `--price-strategy blank`
emits nothing into `price_inr` and records the source amount and currency in
review.csv for a human to act on, because haat asks for the maker's INR price --
a business decision -- not the scraped retail price of some other shop.

The one case where copying is safe is a source already priced in INR under an
explicit `copy` strategy, and even that is the operator's choice, not a default.
Conversion needs an FX rate and arrives in Phase 6; asked for it here, we record
the source and flag rather than inventing a number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from selectolax.parser import HTMLParser

from ..config import CurrencyConfig
from ..models import Confidence, FieldSource, FieldValue, IntField, PriceStrategy
from .structured import StructuredData, scalar
from .title import normalise_text

# Price-looking text in the DOM, as a last resort.
_PRICE_SELECTORS = (
    "[itemprop='price']",
    "[data-price]",
    ".price",
    ".product-price",
    ".price-item--regular",
    ".product__price",
    "#price",
)

_AMOUNT = re.compile(r"\d[\d.,\s]*\d|\d")


def parse_amount(text: str) -> float | None:
    """`"₹2,499.00"` -> 2499.0, `"1.234,56"` -> 1234.56.

    Both thousand-separator conventions appear on Indian storefronts selling
    internationally, so the separator that appears LAST is treated as the
    decimal point -- which is true for both `1,234.56` and `1.234,56`.
    """
    if not text:
        return None
    match = _AMOUNT.search(text.replace(" ", " "))
    if not match:
        return None

    raw = match.group(0).replace(" ", "")
    last_comma, last_dot = raw.rfind(","), raw.rfind(".")

    if last_comma >= 0 and last_dot >= 0:
        decimal_sep, thousands_sep = (",", ".") if last_comma > last_dot else (".", ",")
        raw = raw.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif last_comma >= 0:
        # Two digits after the separator means it is a decimal point ("70,97");
        # three means thousands ("2,499"). Anything else, drop it.
        raw = raw.replace(",", "." if len(raw) - last_comma - 1 == 2 else "")
    elif last_dot >= 0 and len(raw) - last_dot - 1 == 3:
        # "2.499" -- three trailing digits is a thousands separator in most of
        # the world, and Indian sites write "2,499" anyway.
        raw = raw.replace(".", "")

    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def detect_currency(text: str, cfg: CurrencyConfig) -> tuple[str | None, bool]:
    """Returns (currency code, is_ambiguous)."""
    if not text:
        return None, False
    lowered = text.lower()

    for symbol, code in cfg.symbols.items():
        if symbol in text:
            return code, False

    # Longest prefix first: "us$" must beat "$", "inr" must beat nothing.
    for prefix in sorted(cfg.prefixes, key=len, reverse=True):
        if prefix in lowered:
            return cfg.prefixes[prefix], False

    if "$" in text:
        # USD, CAD, AUD, SGD and others all use it.
        return cfg.ambiguous_dollar_default, True

    return None, False


# Exported so `pipeline` can register it as a retractable gap note rather than
# matching on a duplicated literal. See ProductRecord.note_gap.
NO_PRICE_NOTE = "No source price was found; price_inr must be set by hand."


@dataclass
class PriceResult:
    price_inr: IntField
    source_amount: float | None = None
    source_currency: str | None = None
    currency_ambiguous: bool = False
    # `notes` are expected; `flags` are judgement calls a human should overturn
    # if we got them wrong.
    notes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def note(self, text: str) -> None:
        self.notes.append(text)

    def flag(self, text: str) -> None:
        self.flags.append(text)


def _from_structured(
    sd: StructuredData, cfg: CurrencyConfig
) -> tuple[float, str | None, bool] | None:
    """JSON-LD offers.price / priceCurrency, then og:price / product:price."""
    if sd.product:
        offers = sd.product.get("offers")
        for offer in offers if isinstance(offers, list) else [offers]:
            if not isinstance(offer, dict):
                continue
            raw = scalar(offer.get("price") or offer.get("lowPrice") or offer.get("highPrice"))
            if raw is None:
                continue
            amount = parse_amount(raw)
            if amount is None:
                continue
            code = scalar(offer.get("priceCurrency"))
            return amount, (code.upper() if code else None), False

    for amount_key, currency_key in (
        ("og:price:amount", "og:price:currency"),
        ("product:price:amount", "product:price:currency"),
    ):
        raw = sd.opengraph.get(amount_key)
        if raw and (amount := parse_amount(raw)) is not None:
            code = sd.opengraph.get(currency_key)
            return amount, (code.upper() if code else None), False

    return None


def _from_dom(dom: HTMLParser, cfg: CurrencyConfig) -> tuple[float, str | None, bool] | None:
    for selector in _PRICE_SELECTORS:
        for node in dom.css(selector):
            text = normalise_text(
                node.attributes.get("content")
                or node.attributes.get("data-price")
                or node.text(strip=True)
            )
            amount = parse_amount(text)
            if amount is None:
                continue
            code, ambiguous = detect_currency(text, cfg)
            return amount, code, ambiguous
    return None


def extract_price(
    sd: StructuredData,
    dom: HTMLParser,
    cfg: CurrencyConfig,
    strategy: PriceStrategy = PriceStrategy.BLANK,
) -> PriceResult:
    found = _from_structured(sd, cfg)
    source = FieldSource.JSONLD if sd.product else FieldSource.OG
    if found is None:
        found = _from_dom(dom, cfg)
        source = FieldSource.HEURISTIC

    if found is None:
        result = PriceResult(price_inr=FieldValue.missing("No price found on the page."))
        result.note(NO_PRICE_NOTE)
        return result

    amount, currency, ambiguous = found
    result = PriceResult(
        price_inr=FieldValue.missing(),
        source_amount=amount,
        source_currency=currency,
        currency_ambiguous=ambiguous,
    )

    if ambiguous:
        result.flag(
            f"Source price used '$', which is ambiguous; recorded as {currency}. "
            "Confirm before converting."
        )
    if currency is None:
        result.flag(f"Found the amount {amount} but no currency; confirm before using it.")

    if strategy is PriceStrategy.BLANK:
        result.note(
            f"price_inr left blank by policy. Source: {amount} {currency or 'unknown currency'}. "
            "haat wants the maker's INR price, which is a business decision."
        )
        return result

    if strategy is PriceStrategy.COPY:
        if currency == "INR":
            result.price_inr = FieldValue.found(
                int(round(amount)), source, Confidence.HIGH, "Copied from an INR source price."
            )
        else:
            result.note(
                f"--price-strategy copy only applies to INR sources; this page is in "
                f"{currency or 'an unknown currency'}, so price_inr is blank."
            )
        return result

    # convert / markup need an FX rate, which lands in Phase 6.
    result.note(
        f"--price-strategy {strategy.value} needs an FX rate (enrich/fx.py, Phase 6). "
        f"Source recorded as {amount} {currency or 'unknown'}; price_inr left blank."
    )
    return result
