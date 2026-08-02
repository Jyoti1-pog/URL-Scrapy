"""URL canonicalisation and row keys.

Canonicalisation is what dedupe and resume both key on, so it has to be stable
across runs and conservative about what it throws away. Notably it does NOT sort
query parameters: tempting for dedupe, but plenty of storefronts treat parameter
order as meaningful and reordering them can change which product you get.
"""

from __future__ import annotations

import hashlib
import html
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from slugify import slugify

# Params that never identify a product, only where the click came from.
DEFAULT_TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
        "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "igshid", "_ga", "_gl",
        "ref", "referrer", "source", "spm", "srsltid",
    }
)

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def canonicalise(url: str, strip_params: frozenset[str] | set[str] | None = None) -> str:
    """A stable identity for a page URL.

    Lowercases scheme and host, drops the fragment, removes default ports and
    tracking params, and leaves everything else -- including parameter order --
    exactly as found.
    """
    strip = frozenset(strip_params) if strip_params is not None else DEFAULT_TRACKING_PARAMS
    parts = urlsplit(url.strip())

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    netloc = host
    if parts.port and str(parts.port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"

    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in strip
        ]
    )

    path = parts.path or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


def row_key(canonical_url: str) -> str:
    """Filesystem-safe, stable, and readable enough to grep for in a manifest.

    The hash suffix is what makes it unique; the slug prefix is what makes an
    operator able to find the right images/ folder by eye.
    """
    parts = urlsplit(canonical_url)
    readable = slugify(f"{parts.hostname or ''}{parts.path}", max_length=60, word_boundary=True)
    digest = hashlib.sha1(canonical_url.encode("utf-8")).hexdigest()[:8]
    return f"{readable}-{digest}" if readable else digest


def absolutise(base_url: str, candidate: str) -> str | None:
    """Resolve a possibly relative or protocol-relative URL against the page.

    Returns None for things that are not fetchable images: data: URIs, blank
    values, javascript: handlers.
    """
    if not candidate:
        return None
    value = html.unescape(candidate.strip())
    if not value or value.startswith(("data:", "javascript:", "blob:", "#", "about:")):
        return None
    if value.startswith("//"):
        value = f"{urlsplit(base_url).scheme or 'https'}:{value}"
    resolved = urljoin(base_url, value)
    return resolved if urlsplit(resolved).scheme in ("http", "https") else None


def strip_query_params(url: str, params: list[str] | set[str]) -> str:
    """Remove named query params, preserving the order of the rest."""
    lowered = {p.lower() for p in params}
    parts = urlsplit(url)
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in lowered
        ]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def path_and_query(url: str) -> str:
    """The part of a URL that describes the resource, excluding the host.

    Reject-substring matching runs against this rather than the whole URL: a
    shop at iconic-crafts.com should not have every image rejected for
    containing 'icon'.
    """
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}" if parts.query else parts.path


_SIZE_IN_URL = re.compile(r"(?<!\d)(\d{2,5})\s*[xX]\s*(\d{2,5})(?!\d)")
_WIDTH_PARAM = re.compile(r"(?:^|[?&_/-])(?:w|width|sw)[=_-](\d{2,5})(?!\d)")


def declared_dimensions(url: str) -> tuple[int | None, int | None]:
    """Dimensions a URL advertises about itself, e.g. `_1200x1200.jpg` or `?w=800`.

    A hint for ranking only -- never trusted as a substitute for actually
    decoding the image in predicate 6.
    """
    target = path_and_query(url)
    if m := _SIZE_IN_URL.search(target):
        return int(m.group(1)), int(m.group(2))
    if m := _WIDTH_PARAM.search(target):
        return int(m.group(1)), None
    return None, None
