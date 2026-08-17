"""Structured data first, always.

Since the target set is "any product page anywhere", schema.org coverage is what
makes a site-agnostic extractor viable at all. CSS selectors are the fallback,
not the plan.

Preference order per §5.2: JSON-LD -> microdata -> RDFa -> OpenGraph/Twitter ->
DOM heuristics.

extruct handles the first three. OpenGraph and Twitter tags are read straight
from the DOM here rather than through extruct, because a handful of meta tags is
completely predictable to parse and extruct's opengraph shape varies between
versions -- a dependency's output format is not worth the coupling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import extruct
from selectolax.parser import HTMLParser

from ..models import FieldSource
from ..utils.logging import get_logger

log = get_logger(__name__)

# Types that mean "this node describes the thing being sold".
PRODUCT_TYPES = {"product", "productgroup", "individualproduct", "productmodel", "vehicle"}

_SYNTAX_SOURCE = {
    "json-ld": FieldSource.JSONLD,
    "microdata": FieldSource.MICRODATA,
    "rdfa": FieldSource.RDFA,
}


def _types_of(node: dict[str, Any]) -> set[str]:
    raw = node.get("@type") or node.get("type") or []
    values = raw if isinstance(raw, list) else [raw]
    return {str(v).rsplit("/", 1)[-1].lower() for v in values if v}


def _find_product(node: Any, depth: int = 0) -> dict[str, Any] | None:
    """Depth-first search for a Product node.

    Real pages nest these in @graph, in arrays, inside ItemList and BreadcrumbList,
    and occasionally inside an Offer. Depth is capped because some CMSs emit
    deeply self-referential graphs.
    """
    if depth > 8:
        return None
    if isinstance(node, list):
        for item in node:
            if found := _find_product(item, depth + 1):
                return found
        return None
    if not isinstance(node, dict):
        return None

    if _types_of(node) & PRODUCT_TYPES:
        return node

    for key in ("@graph", "mainEntity", "itemListElement", "item", "hasVariant"):
        if key in node and (found := _find_product(node[key], depth + 1)):
            return found

    for value in node.values():
        if isinstance(value, list | dict) and (found := _find_product(value, depth + 1)):
            return found
    return None


def scalar(value: Any) -> str | None:
    """Flatten a JSON-LD value to a plain string.

    JSON-LD lets a field be a string, a list, or a {"@value": ...} wrapper, and
    real sites use all three for the same property.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, dict):
        for key in ("@value", "value", "name", "@id"):
            if key in value:
                return scalar(value[key])
        return None
    if isinstance(value, list):
        for item in value:
            if (found := scalar(item)) is not None:
                return found
    return None


@dataclass
class StructuredData:
    """Everything machine-readable the page told us about itself."""

    product: dict[str, Any] | None = None
    product_source: FieldSource | None = None
    opengraph: dict[str, str] = field(default_factory=dict)
    twitter: dict[str, str] = field(default_factory=dict)
    meta: dict[str, str] = field(default_factory=dict)
    syntaxes_found: list[str] = field(default_factory=list)
    # The shop's own shelf for this product: its breadcrumb trail, plus any
    # `category` the Product node declares. Evidence rather than inference --
    # "Home > Sarees > Silk Sarees" is the shop telling us where it files this,
    # which beats guessing from a product title every time.
    trail: list[str] = field(default_factory=list)

    def product_field(self, *keys: str) -> tuple[Any, FieldSource] | None:
        """First present key from the Product node, with its syntax as source."""
        if not self.product or self.product_source is None:
            return None
        for key in keys:
            if key in self.product and self.product[key] not in (None, "", [], {}):
                return self.product[key], self.product_source
        return None

    def product_str(self, *keys: str) -> tuple[str, FieldSource] | None:
        found = self.product_field(*keys)
        if not found:
            return None
        text = scalar(found[0])
        return (text, found[1]) if text else None

    def og(self, *keys: str) -> tuple[str, FieldSource] | None:
        for key in keys:
            if value := self.opengraph.get(key):
                return value, FieldSource.OG
        return None

    def tw(self, *keys: str) -> tuple[str, FieldSource] | None:
        for key in keys:
            if value := self.twitter.get(key):
                return value, FieldSource.TWITTER
        return None


def _read_meta(dom: HTMLParser) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    og: dict[str, str] = {}
    twitter: dict[str, str] = {}
    other: dict[str, str] = {}

    for node in dom.css("meta"):
        content = (node.attributes.get("content") or "").strip()
        if not content:
            continue
        key = (node.attributes.get("property") or node.attributes.get("name") or "").strip().lower()
        if not key:
            continue
        # First occurrence wins: og:image repeats for a gallery, and the first
        # is conventionally the hero.
        if key.startswith("og:"):
            og.setdefault(key, content)
        elif key.startswith("twitter:"):
            twitter.setdefault(key, content)
        else:
            other.setdefault(key, content)

    return og, twitter, other


def extract_structured(html: str, base_url: str, dom: HTMLParser | None = None) -> StructuredData:
    """Parse all machine-readable product data out of a page.

    Never raises: a page with malformed JSON-LD is common and must degrade to
    the DOM path, not fail the row.
    """
    dom = dom or HTMLParser(html)
    og, twitter, meta = _read_meta(dom)
    data = StructuredData(opengraph=og, twitter=twitter, meta=meta)

    try:
        parsed = extruct.extract(
            html,
            base_url=base_url,
            syntaxes=["json-ld", "microdata", "rdfa"],
            uniform=True,
        )
    except Exception as exc:  # extruct raises a wide variety on malformed markup
        log.debug("Structured-data parse failed for %s: %s", base_url, exc)
        return data

    for syntax, source in _SYNTAX_SOURCE.items():
        items = parsed.get(syntax) or []
        if items:
            data.syntaxes_found.append(syntax)
        if data.product is None and (product := _find_product(items)):
            data.product = product
            data.product_source = source
        if not data.trail:
            data.trail = _breadcrumb_trail(items)

    if not data.trail:
        data.trail = _dom_breadcrumbs(dom)
    if data.product:
        declared = data.product.get("category")
        if isinstance(declared, str) and declared.strip():
            # Shopify and WooCommerce both put a slash- or angle-separated path
            # here. Split rather than kept whole: "Sarees > Silk" should score
            # for both words.
            data.trail.extend(_split_path(declared))

    return data


_TRAIL_SEPARATORS = re.compile(r"\s*(?:>|/|»|›|\|)\s*")

# Words every shop's trail begins with. They separate nothing, and left in they
# would let "Home" score against a `home-textiles` shelf on every page.
_TRAIL_NOISE = frozenset(
    {"home", "shop", "all", "products", "product", "catalog", "catalogue", "index", "collections"}
)


def _split_path(value: str) -> list[str]:
    parts = [p.strip() for p in _TRAIL_SEPARATORS.split(value) if p.strip()]
    return [p for p in parts if _is_shelf(p)]


def _breadcrumb_trail(items: list[dict[str, Any]]) -> list[str]:
    """The names in a schema.org BreadcrumbList, in order, minus the noise."""
    out: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if "breadcrumb" in str(item.get("@type", "")).lower():
            for element in item.get("itemListElement") or []:
                if not isinstance(element, dict):
                    continue
                name = element.get("name")
                if not name and isinstance(element.get("item"), dict):
                    name = element["item"].get("name")
                if isinstance(name, str) and _is_shelf(name.strip()):
                    out.append(name.strip())
    return out


# A shelf name is short. A product title is not, and the last crumb of a trail
# is usually the product itself -- which is the title again, and injecting it
# at a HIGHER weight than the title would let one long product name outvote
# every other signal on the page.
MAX_CRUMB_CHARS = 40


def _is_shelf(name: str) -> bool:
    return bool(name) and len(name) <= MAX_CRUMB_CHARS and name.lower() not in _TRAIL_NOISE


def _dom_breadcrumbs(dom: HTMLParser) -> list[str]:
    """The visible trail, for shops that render one without marking it up.

    Best-effort and deliberately narrow: only elements that say `breadcrumb` in
    their own class or aria-label. Anything looser matches a site's main
    navigation, which names every shelf the shop has and would score for all of
    them at once.
    """
    for selector in (
        "[class*='breadcrumb'] a",
        "[id*='breadcrumb'] a",
        "nav[aria-label*='readcrumb'] a",
    ):
        names = [n.text(strip=True) for n in dom.css(selector)]
        names = [n for n in names if _is_shelf(n)]
        if names:
            return names[:6]
    return []
