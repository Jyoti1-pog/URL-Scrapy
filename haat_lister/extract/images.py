"""Image candidate collection, normalisation and ranking.

Collect everything, then dedupe and rank. This module produces the ordered
`candidates` list that Tier 1 walks; it never fetches anything itself.

The ranking matters more than it looks. Tier 1 validates candidates in order and
stops at the first pass, so a good ordering means one HEAD request per row and a
bad one means six.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum

from selectolax.parser import HTMLParser

from ..config import ImagesConfig, ValidatorConfig
from ..models import FieldSource
from ..utils.urls import absolutise, declared_dimensions, path_and_query, strip_query_params
from .structured import StructuredData, scalar

# Where a candidate was found. Lower is preferred, all else being equal.
_SOURCE_RANK = {
    FieldSource.JSONLD: 0,
    FieldSource.MICRODATA: 1,
    FieldSource.RDFA: 2,
    FieldSource.OG: 3,
    FieldSource.TWITTER: 4,
    FieldSource.HEURISTIC: 5,
}


class Tier(IntEnum):
    """Ranking buckets, best first."""

    UPSIZED = 0        # a known-safe full-size variant we derived
    BIG_DECLARED = 1   # URL states dimensions and they clear the minimum
    BIG_SRCSET = 2     # srcset width descriptor clears the minimum
    UNKNOWN = 3        # no size information either way
    TOO_SMALL = 4      # URL states dimensions and they are below the minimum


@dataclass
class ImageCandidate:
    url: str
    source: FieldSource
    dom_index: int
    declared_w: int | None = None
    declared_h: int | None = None
    srcset_width: int | None = None
    upsized_from: str | None = None

    @property
    def declared_max(self) -> int:
        return max(self.declared_w or 0, self.declared_h or 0)


# ---------------------------------------------------------------------------
# Known-safe full-size CDN variants
# ---------------------------------------------------------------------------

# Shopify: files/kurta_300x300.jpg, kurta_1024x1024@2x.jpg, kurta_grande.jpg
_SHOPIFY_SIZE = re.compile(
    r"_(?:\d{2,5}x\d{0,5}|x\d{2,5}|pico|icon|thumb|small|compact|medium|large|grande|master)"
    r"(?:@[23]x)?(?=\.(?:jpe?g|png|webp|avif)\b)",
    re.IGNORECASE,
)
# WooCommerce / WordPress: kurta-300x300.jpg
_WORDPRESS_SIZE = re.compile(r"-\d{2,5}x\d{2,5}(?=\.(?:jpe?g|png|webp|avif)\b)", re.IGNORECASE)


def _is_shopify(url: str) -> bool:
    parts = url.lower()
    return "shopify" in parts or "/cdn/shop/" in parts or "/s/files/" in parts


def _is_wordpress(url: str) -> bool:
    return "/wp-content/uploads/" in url.lower()


def full_size_variant(url: str) -> str | None:
    """Derive the full-size original, but only on CDNs whose pattern we know.

    The `_800x800.jpg` convention is Shopify's; the `-800x800.jpg` one is
    WordPress's. Applying either to an arbitrary host would be inventing a URL,
    and every wrong guess costs a HEAD request on every row -- which is exactly
    the expense the cheap-path-first design exists to avoid. So the rewrite is
    gated on evidence that we are actually looking at that CDN.

    Returned as an ADDITIONAL candidate, never as a replacement.
    """
    if _is_shopify(url) and _SHOPIFY_SIZE.search(url):
        if (candidate := _SHOPIFY_SIZE.sub("", url)) != url:
            return candidate
    if _is_wordpress(url) and _WORDPRESS_SIZE.search(url):
        if (candidate := _WORDPRESS_SIZE.sub("", url)) != url:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _parse_srcset(value: str) -> list[tuple[str, int | None]]:
    """"a.jpg 320w, b.jpg 2x" -> [(a.jpg, 320), (b.jpg, None)]"""
    out: list[tuple[str, int | None]] = []
    for entry in value.split(","):
        parts = entry.strip().split()
        if not parts:
            continue
        width: int | None = None
        if len(parts) > 1 and parts[-1].endswith("w") and parts[-1][:-1].isdigit():
            width = int(parts[-1][:-1])
        out.append((parts[0], width))
    return out


_BACKGROUND_URL = re.compile(r"background-image\s*:\s*url\(\s*['\"]?([^'\")]+)", re.IGNORECASE)


def _collect_raw(sd: StructuredData, dom: HTMLParser, cfg: ImagesConfig) -> list[ImageCandidate]:
    """Every plausible image reference on the page, in discovery order."""
    found: list[ImageCandidate] = []
    index = 0

    def add(raw: str | None, source: FieldSource, srcset_width: int | None = None) -> None:
        nonlocal index
        if raw:
            found.append(
                ImageCandidate(url=raw, source=source, dom_index=index, srcset_width=srcset_width)
            )
        index += 1

    # 1. Structured data. Product.image is a string, a list, or a list of
    #    ImageObjects depending on who generated it.
    if sd.product and sd.product_source:
        raw_images = sd.product.get("image") or sd.product.get("images")
        items = raw_images if isinstance(raw_images, list) else [raw_images]
        for item in items:
            if isinstance(item, dict):
                add(scalar(item.get("contentUrl") or item.get("url") or item), sd.product_source)
            else:
                add(scalar(item), sd.product_source)

    # 2. OpenGraph, then Twitter.
    for key in ("og:image:secure_url", "og:image", "og:image:url"):
        add(sd.opengraph.get(key), FieldSource.OG)
    for key in ("twitter:image", "twitter:image:src"):
        add(sd.twitter.get(key), FieldSource.TWITTER)

    # 3. DOM images: srcset entries, src, then lazy attributes.
    for node in dom.css("img"):
        attrs = node.attributes
        for attr in ("srcset", "data-srcset"):
            if value := attrs.get(attr):
                for url, width in _parse_srcset(value):
                    add(url, FieldSource.HEURISTIC, width)
        add(attrs.get("src"), FieldSource.HEURISTIC)
        for attr in cfg.lazy_attributes:
            if attr != "data-srcset":
                add(attrs.get(attr), FieldSource.HEURISTIC)

    # 4. Elements carrying a background image, which is how a lot of themes do
    #    gallery zoom.
    for node in dom.css("[style*='background-image']"):
        if style := node.attributes.get("style"):
            for match in _BACKGROUND_URL.finditer(style):
                add(match.group(1), FieldSource.HEURISTIC)

    return found


# ---------------------------------------------------------------------------
# Normalisation and ranking
# ---------------------------------------------------------------------------


def _is_rejected(url: str, cfg: ImagesConfig) -> bool:
    """Obvious non-products: sprites, icons, payment badges, tracking pixels.

    Matched against the path and query only. A shop at iconic-crafts.com should
    not have every image rejected for containing 'icon'.
    """
    target = path_and_query(url).lower()
    return any(token.lower() in target for token in cfg.reject_url_substrings)


def _tier(c: ImageCandidate, vcfg: ValidatorConfig) -> Tier:
    if c.upsized_from:
        return Tier.UPSIZED
    if c.declared_w and c.declared_h:
        big_enough = c.declared_w >= vcfg.min_width and c.declared_h >= vcfg.min_height
        return Tier.BIG_DECLARED if big_enough else Tier.TOO_SMALL
    if c.declared_max:
        return Tier.BIG_DECLARED if c.declared_max >= vcfg.min_width else Tier.TOO_SMALL
    if c.srcset_width:
        return Tier.BIG_SRCSET if c.srcset_width >= vcfg.min_width else Tier.TOO_SMALL
    return Tier.UNKNOWN


def _sort_key(c: ImageCandidate, vcfg: ValidatorConfig) -> tuple[int, int, int, int]:
    return (
        _tier(c, vcfg),
        -max(c.declared_max, c.srcset_width or 0),
        _SOURCE_RANK.get(c.source, 9),
        c.dom_index,
    )


def _normalise(
    raw: list[ImageCandidate], base_url: str, cfg: ImagesConfig
) -> list[ImageCandidate]:
    """Resolve, clean and filter, keeping the first sighting of each URL."""
    out: list[ImageCandidate] = []
    seen: set[str] = set()

    for candidate in raw:
        resolved = absolutise(base_url, candidate.url)
        if not resolved:
            continue
        resolved = strip_query_params(resolved, cfg.strip_query_params)
        if _is_rejected(resolved, cfg) or resolved in seen:
            continue

        seen.add(resolved)
        candidate.url = resolved
        candidate.declared_w, candidate.declared_h = declared_dimensions(resolved)
        out.append(candidate)

    return out


@dataclass
class _Group:
    """One product photo, and the URLs we know for it.

    Grouping is what makes `max_images_per_product` mean "six photos" rather
    than "six URLs". Without it, a page whose images all have derivable
    full-size variants spends the whole budget on guesses and keeps none of the
    originals to fall back to.
    """

    primary: ImageCandidate
    fallbacks: list[ImageCandidate]


def _group_by_photo(
    candidates: list[ImageCandidate], cfg: ImagesConfig, vcfg: ValidatorConfig
) -> list[_Group]:
    groups: dict[str, _Group] = {}

    for candidate in candidates:
        full = full_size_variant(candidate.url)
        if full and not _is_rejected(full, cfg):
            key = full
            derived = ImageCandidate(
                url=full,
                source=candidate.source,
                dom_index=candidate.dom_index,
                upsized_from=candidate.url,
            )
        else:
            key, derived = candidate.url, candidate

        if key not in groups:
            groups[key] = _Group(primary=derived, fallbacks=[])

        if candidate.url != key:
            # An original whose URL admits it is below the listable minimum can
            # never pass predicate 6, so keeping it as a fallback would only buy
            # a wasted request.
            if _tier(candidate, vcfg) is not Tier.TOO_SMALL:
                groups[key].fallbacks.append(candidate)
        elif candidate is not derived:
            # The full-size URL was also listed directly; it is not a guess.
            groups[key].primary = candidate

    return list(groups.values())


def collect_candidates(
    sd: StructuredData,
    dom: HTMLParser,
    base_url: str,
    cfg: ImagesConfig,
    vcfg: ValidatorConfig,
) -> list[ImageCandidate]:
    """The ordered candidate list Tier 1 will walk. Hero first.

    Each photo contributes its best URL followed immediately by its fallbacks,
    so a full-size guess that 404s drops straight through to the URL the page
    actually published.
    """
    normalised = _normalise(_collect_raw(sd, dom, cfg), base_url, cfg)
    groups = _group_by_photo(normalised, cfg, vcfg)
    groups.sort(key=lambda g: _sort_key(g.primary, vcfg))

    ordered: list[ImageCandidate] = []
    for group in groups[: cfg.max_images_per_product]:
        ordered.append(group.primary)
        ordered.extend(sorted(group.fallbacks, key=lambda c: _sort_key(c, vcfg)))
    return ordered
