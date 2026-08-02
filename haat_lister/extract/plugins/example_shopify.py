"""A worked example: Shopify.

Copy this file, change the two methods, drop it in your `plugins_dir`. It is
about ninety lines and it is the whole interface.

Shopify is a good example because the generic path already half-works on it --
most themes emit decent JSON-LD -- and yet two things are reliably wrong:

  1. **Price.** Themes often omit `offers` from the JSON-LD, or emit the
     compare-at price. The truth is in `var meta = {...}`, in minor units.
  2. **The gallery.** A slider theme renders one `<img>` and loads the rest by
     script, so Stage A sees a single photo of a product that has eight. The
     same `meta` object lists every image.

Both are in a `<script>` tag that Stage A already downloaded, so this plugin
recovers them without a browser -- which on a large Shopify catalogue is the
difference between a batch that takes minutes and one that takes hours.

Note what it does NOT do: guess a category, invent a weight, or fill in an HS
code. It reports what the page states and nothing else, which is the same
standard the rest of the tool holds itself to.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ...models import Confidence, FieldSource, FieldValue
from ...utils.logging import get_logger
from ...utils.urls import absolutise
from . import PluginContext, PluginResult, register

log = get_logger(__name__)

# `var meta = {...};` -- Shopify's own theme object, present since forever.
_META = re.compile(r"var\s+meta\s*=\s*(\{.*?\});", re.DOTALL)

# Shopify serves resized variants as name_600x600.jpg; stripping the suffix
# gives the original upload, which is what a marketplace listing wants.
_SIZE_SUFFIX = re.compile(r"_(?:\d+x\d*|\d*x\d+|small|medium|large|grande|pico|icon)(?=\.\w+)")


class ShopifyPlugin:
    name = "example_shopify"

    def matches(self, url: str, html: str) -> bool:
        """Cheap: this runs on every page in the batch. Two substring checks."""
        return "cdn.shopify.com" in html or "Shopify.shop" in html

    def extract(self, ctx: PluginContext) -> PluginResult:
        result = PluginResult()
        meta = self._meta(ctx.html)
        if meta is None:
            return result

        product = meta.get("product") or {}

        # -- price ---------------------------------------------------------
        # Minor units: 249900 is Rs 2,499.00. Getting this wrong by a factor of
        # 100 on a price column is the single most expensive mistake available
        # here, so it is asserted rather than assumed.
        variants = product.get("variants") or []
        prices = [v["price"] for v in variants if isinstance(v.get("price"), int)]
        if prices:
            result.source_price = min(prices) / 100
            result.source_currency = str(meta.get("currency") or "") or None
            if len(set(prices)) > 1:
                result.flags.append(
                    f"This Shopify product has {len(set(prices))} variants at different prices; "
                    f"the lowest was taken. One CSV row cannot carry more than one price -- "
                    f"decide which variant this listing is."
                )

        # -- title ---------------------------------------------------------
        if title := product.get("title"):
            result.fields["title"] = FieldValue.found(
                str(title),
                FieldSource.PLUGIN,
                Confidence.HIGH,
                note="Shopify theme meta, which is the product's own title field.",
            )

        # -- gallery -------------------------------------------------------
        images = [
            full
            for raw in self._image_urls(product)
            if (full := absolutise(ctx.final_url, self._original_size(raw)))
        ]
        if len(images) > 1:
            result.image_candidates = images

        return result

    # -- helpers -----------------------------------------------------------

    def _meta(self, html: str) -> dict[str, Any] | None:
        match = _META.search(html)
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(1))
        except json.JSONDecodeError:
            # A theme has customised the object into something non-standard.
            # Not worth a flag: the generic path still ran.
            log.debug("Shopify meta object present but not JSON; ignoring")
            return None
        return parsed if isinstance(parsed, dict) else None

    def _image_urls(self, product: dict[str, Any]) -> list[str]:
        urls = [u for u in (product.get("images") or []) if isinstance(u, str)]
        for variant in product.get("variants") or []:
            if isinstance(image := variant.get("featured_image"), str):
                urls.append(image)
        return list(dict.fromkeys(urls))  # de-duplicated, order preserved

    def _original_size(self, url: str) -> str:
        return _SIZE_SUFFIX.sub("", url)


register(ShopifyPlugin())
