"""Amazon: the gallery is not in `<img src>`, so the generic path finds nothing.

This is the plugin system's first real customer, and it exists because of a
concrete observed failure: a product page with fourteen photos on it produced
`image: none`. The generic collector reads JSON-LD, og:image, srcset, src, lazy
attributes and background-image, and an Amazon detail page has the gallery in
none of them -- it is a JSON blob in an attribute and a JavaScript object in an
inline script.

FOUR RULES, IN ORDER OF HOW MUCH THEY CAN BE TRUSTED:

  1. `#landingImage[data-a-dynamic-image]` -- a JSON map of {url: [w, h]}. The
     page tells us the dimensions, so the largest can be picked without a
     request. Highest yield of the four by a distance.
  2. `#landingImage[data-old-hires]` -- the full-resolution original, when the
     theme includes it.
  3. `colorImages.initial[]` in the ImageBlockATF script -- the whole gallery,
     in the order Amazon shows it, each entry with `hiRes` and `large`. This is
     how you get eight photos rather than the hero alone.
  4. `#imgTagWrapperId img`, `#altImages img` -- last resort, thumbnails.

THEN THE SIZE MODIFIER, which matters more than it looks:

    .../images/I/61abcDEF._AC_SX679_.jpg   679x679 -- fails the 800x800 floor
    .../images/I/61abcDEF.jpg              the original, usually 1500x1500

Stripping `._AC_SX679_` is not a guess about an arbitrary CDN: it is a
documented, stable convention on one host we can identify by name, which is the
same standard `full_size_variant` already applies to Shopify and WordPress. The
stripped URL is added as an ADDITIONAL, higher-ranked candidate and the modified
one is kept behind it -- never a blind replacement, so a wrong guess costs one
extra HEAD rather than the row's only photo.

WHAT THIS PLUGIN DOES NOT DO. It does not touch price, weight, dimensions or
category, and it does not help with a bot check. If Amazon serves a captcha, the
page-shape check fails the row before this ever runs.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ...utils.logging import get_logger
from ...utils.urls import absolutise
from . import PluginContext, PluginResult, register

log = get_logger(__name__)

# `"colorImages":{"initial":[...]}` inside an inline script. Matched rather than
# parsed as JavaScript because the surrounding object is not JSON -- it is a
# script body with function calls in it.
_COLOR_IMAGES = re.compile(r"['\"]colorImages['\"]\s*:\s*\{\s*['\"]initial['\"]\s*:\s*(\[)")

# The size modifier Amazon appends before the extension:
#   ._AC_SX679_    ._SL1600_    ._AC_UL320_    ._SX38_SY50_CR,0,0,38,50_
#
# One `._..._` segment sitting immediately before the extension. Underscores
# are INSIDE the class, not separators between repeated groups -- an earlier
# version treated them as separators and matched nothing at all, which is a
# quiet failure this whole phase exists to stop happening.
#
# The leading dot is what keeps it safe: a filename like `61abc_DEF_.jpg` has
# no dot before its underscore and is left alone.
_SIZE_MODIFIER = re.compile(
    r"\._[A-Za-z0-9_,]+_(?=\.(?:jpe?g|png|webp|gif)\b)", re.IGNORECASE
)

_MEDIA_HOSTS = ("media-amazon.com", "ssl-images-amazon.com", "images-amazon.com")

# Amazon's own furniture, served from the same CDN as the product photos.
#
# LOWERCASE, because `_junk` matches against a lowercased URL. `/G/01/` was in
# this tuple with a capital G and therefore never matched anything -- a sprite
# sheet went through as a product photo until a test looked. Kept lowercase at
# the definition rather than lowered at the comparison, so the next entry cannot
# make the same mistake.
_NOT_PRODUCTS = (
    "/captcha/",
    "transparent-pixel",
    "grey-pixel",
    "/sprites/",
    "sprite-",
    "-sprite",
    "/g/01/",  # Amazon's static asset directory: icons, buttons, chrome.
    "player-thumb",
    "play-icon",
)

assert all(token == token.lower() for token in _NOT_PRODUCTS), (
    "_NOT_PRODUCTS is matched against a lowercased URL; an uppercase entry is dead code"
)


def is_amazon_media(url: str) -> bool:
    return any(host in url for host in _MEDIA_HOSTS)


def strip_size_modifier(url: str) -> str | None:
    """The original upload, or None when the URL carries no modifier.

    Gated on the host: this convention belongs to Amazon's image CDN, and
    applying it to an arbitrary shop would be inventing a URL -- one wasted HEAD
    request on every row for the rest of time.
    """
    if not is_amazon_media(url):
        return None
    stripped = _SIZE_MODIFIER.sub("", url)
    return stripped if stripped != url else None


def _balanced_array(text: str, start: int) -> str | None:
    """The JSON array beginning at `start`, tracking string state.

    A depth counter alone gets this wrong: Amazon's alt text contains brackets,
    and one `[` inside a caption would leave the array unterminated forever.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, min(len(text), start + 2_000_000)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


class AmazonPlugin:
    name = "amazon"

    def matches(self, url: str, html: str) -> bool:
        """Cheap: this runs on every page in the batch."""
        lowered = url.lower()
        return "amazon." in lowered and ("/dp/" in lowered or "/gp/product/" in lowered)

    def extract(self, ctx: PluginContext) -> PluginResult:
        result = PluginResult()

        found: list[str] = []
        for rule in (self._dynamic_image, self._old_hires, self._colour_images, self._fallback_dom):
            for raw in rule(ctx):
                if (url := absolutise(ctx.final_url, raw)) and not self._junk(url):
                    found.append(url)

        if not found:
            # Silent rather than flagged. A plugin that matched and found
            # nothing is a page shape we have not seen; the generic result --
            # and the row's own "no candidates" reason -- already say so, and a
            # second voice saying it would just be noise.
            log.debug("Amazon plugin matched %s but found no gallery", ctx.url)
            return result

        result.image_candidates = self._rank(found)
        result.notes.append(
            f"Amazon's gallery is not in plain <img> tags; the amazon plugin read "
            f"{len(result.image_candidates)} photo URL(s) out of the page's own image data."
        )
        # A page whose product name is only in the DOM: og:title on an Amazon
        # detail page is the full SEO string, and so is <title>. Left to the
        # generic extractor and to Phase 6's title cleaning -- this plugin's job
        # is the gallery.
        return result

    # -- the four rules ----------------------------------------------------

    def _dynamic_image(self, ctx: PluginContext) -> list[str]:
        """Rule 1. `{"url": [width, height], ...}` -- largest by area first."""
        node = ctx.dom.css_first("#landingImage[data-a-dynamic-image]")
        raw = node.attributes.get("data-a-dynamic-image") if node else None
        if not raw:
            return []
        try:
            sizes = json.loads(raw)
        except json.JSONDecodeError:
            log.debug("data-a-dynamic-image present but not JSON on %s", ctx.url)
            return []
        if not isinstance(sizes, dict):
            return []

        def area(item: tuple[str, Any]) -> int:
            dims = item[1]
            if isinstance(dims, list) and len(dims) == 2:
                try:
                    return int(dims[0]) * int(dims[1])
                except (TypeError, ValueError):
                    return 0
            return 0

        return [url for url, _ in sorted(sizes.items(), key=area, reverse=True)]

    def _old_hires(self, ctx: PluginContext) -> list[str]:
        """Rule 2. Often the full-resolution original, stated outright."""
        node = ctx.dom.css_first("#landingImage[data-old-hires]")
        value = node.attributes.get("data-old-hires") if node else None
        return [value] if value else []

    def _colour_images(self, ctx: PluginContext) -> list[str]:
        """Rule 3. The whole gallery, in the order Amazon shows it.

        `hiRes` where present, `large` otherwise. This is the only rule that
        gets more than the hero shot, which is what makes a listing look like a
        listing rather than a placeholder.
        """
        match = _COLOR_IMAGES.search(ctx.html)
        if match is None:
            return []
        blob = _balanced_array(ctx.html, match.start(1))
        if blob is None:
            log.debug("colorImages array on %s never closed; skipping", ctx.url)
            return []
        try:
            entries = json.loads(blob)
        except json.JSONDecodeError:
            log.debug("colorImages array on %s is not JSON; skipping", ctx.url)
            return []

        urls: list[str] = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            for key in ("hiRes", "large", "mainUrl"):
                if isinstance(value := entry.get(key), str) and value:
                    urls.append(value)
                    break
        return urls

    def _fallback_dom(self, ctx: PluginContext) -> list[str]:
        """Rule 4. Thumbnails, which at least prove the page had photos."""
        urls: list[str] = []
        for selector in ("#imgTagWrapperId img", "#altImages img", "#main-image-container img"):
            for node in ctx.dom.css(selector):
                for attr in ("data-old-hires", "src", "data-src"):
                    if value := node.attributes.get(attr):
                        urls.append(value)
                        break
        return urls

    # -- ranking -----------------------------------------------------------

    def _rank(self, found: list[str]) -> list[str]:
        """Each photo's stripped original first, then the URL as published.

        Order within a photo, not across the gallery: the hero's original, the
        hero as published, then the second photo, and so on. A stripped URL that
        404s therefore drops straight through to the one the page actually
        used, which is the same shape as `collect_candidates`' own fallbacks.
        """
        ordered: list[str] = []
        seen: set[str] = set()
        for url in found:
            for candidate in (strip_size_modifier(url), url):
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    ordered.append(candidate)
        return ordered

    def _junk(self, url: str) -> bool:
        lowered = url.lower()
        return not is_amazon_media(lowered) or any(bad in lowered for bad in _NOT_PRODUCTS)


# Built in, so it loads on every run. That is right for this one: `matches` is
# two substring checks on a URL, and the difference it makes is between a
# working row and `image: none`.
register(AmazonPlugin())
