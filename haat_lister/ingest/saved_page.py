"""§4.2 -- a page the operator saved from their own browser.

WHY THIS IS THE RIGHT ANSWER TO A BLOCKED SITE. The ladder measured it: the
hosts that matter most here refuse a correctly-identified client on every rung,
and a real headless Chromium gets the same protocol error. There is no rung
five. But the operator can open the page, and `Ctrl+S` costs them four seconds
and costs the site one request it was going to serve anyway.

So this is not a workaround for a block -- it is the operator lending us the
session they already legitimately have. Nothing here impersonates them, retries
on their behalf, or replays their cookies at the site.

WHAT ARRIVES. Three shapes, all of them things a browser actually produces:

    page.html                  "Webpage, HTML only"
    page.html + page_files/    "Webpage, complete" -- the common one
    page.mhtml                 "Single File" / Save as MHTML

WHAT IT COSTS. Zero network calls to the source host, ever. In manifest mode,
zero network calls at all -- which is what makes this usable on a site that has
already said no, and it is asserted rather than assumed.
"""

from __future__ import annotations

import email
import email.policy
from base64 import b64encode
from dataclasses import dataclass
from dataclasses import field as dc_field
from mimetypes import guess_type
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

from selectolax.parser import HTMLParser

from ..fetch.static import FetchResult
from ..models import FetchStage
from ..utils.logging import get_logger

log = get_logger(__name__)

# Everything a browser calls the sidecar folder, across locales and versions.
# Matched case-insensitively against `<stem><suffix>`.
_SIDECAR_SUFFIXES = ("_files", "_arquivos", "-Dateien", "_fichiers", "_archivos", ".files")

MAX_BYTES = 32 * 1024 * 1024

SUFFIXES = (".html", ".htm", ".mhtml", ".mht", ".xhtml")


class SavedPageError(Exception):
    """Bad input from a human, so the message is aimed at one.

    Never a stack trace and never a 500: a wrong file is the most likely thing
    to happen on this route, and it is not an error condition, it is Tuesday.
    """


@dataclass
class SavedPage:
    """A saved page, resolved and ready for the ordinary extractor."""

    html: str
    # Where the page was originally served from. This is what every relative
    # URL resolves against and what the row is keyed on, so a saved page and a
    # live fetch of the same product produce the same `row_key`.
    source_url: str
    path: Path
    assets: dict[str, Path]
    # The photographs the operator's browser actually saved, inlined. Kept
    # apart from the HTML rather than written into `<img src>`: a `data:` URI
    # on a live page is a placeholder pixel or a lazy-load spacer, never the
    # product, and `absolutise` is right to drop it. These are appended to the
    # candidate list AFTER extraction, ranked last -- a real URL that works is
    # worth more than bytes we would have to host ourselves.
    local_photos: list[str] = dc_field(default_factory=list)

    def as_fetch_result(self) -> FetchResult:
        """The same object a fetch produces, so the same code runs on it."""
        return FetchResult(
            url=self.source_url,
            final_url=self.source_url,
            status_code=200,
            html=self.html,
            stage=FetchStage.SAVED_PAGE,
            elapsed_ms=0,
            headers={"content-type": "text/html"},
        )


# ---------------------------------------------------------------------------
# Where the page came from
# ---------------------------------------------------------------------------


def _origin_from_html(dom: HTMLParser) -> str:
    """The URL the page was served from, in the order browsers record it.

    A saved page keeps its own address in several places and this order is not
    arbitrary -- `<base>` is what the browser itself would resolve against, and
    the canonical link is what the shop says the product's address is. Guessing
    from the filename comes last and is not done at all: an operator who saved
    `kurta (1).html` should be asked, not second-guessed.
    """
    if (base := dom.css_first("base[href]")) and (href := base.attributes.get("href")):
        if href.startswith(("http://", "https://")):
            return href
    for selector, attribute in (
        ("link[rel=canonical]", "href"),
        ("meta[property='og:url']", "content"),
        ("meta[name='twitter:url']", "content"),
    ):
        node = dom.css_first(selector)
        if node and (value := node.attributes.get(attribute)):
            if value.startswith(("http://", "https://")):
                return value
    # Chrome and Firefox both write this when saving.
    for comment_marker in ("saved from url=(", "originalurl:"):
        if (found := _from_comment(dom.html or "", comment_marker)) is not None:
            return found
    return ""


def _from_comment(html: str, marker: str) -> str | None:
    lowered = html[:4096].lower()
    at = lowered.find(marker)
    if at < 0:
        return None
    tail = html[at + len(marker) : at + len(marker) + 600]
    # Chrome's form is `(0044)https://...` -- a four-digit length, then the URL.
    if tail[:4].isdigit():
        tail = tail[4:]
    tail = tail.lstrip(") \t")
    end = min(
        (i for i in (tail.find(c) for c in (" ", "\n", "\r", "-->", '"')) if i > 0),
        default=len(tail),
    )
    candidate = tail[:end].strip()
    return candidate if candidate.startswith(("http://", "https://")) else None


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------


def sidecar_for(path: Path) -> Path | None:
    """The `_files` folder a browser wrote next to this page, if there is one."""
    for suffix in _SIDECAR_SUFFIXES:
        candidate = path.with_name(path.stem + suffix)
        if candidate.is_dir():
            return candidate
    # Locale we do not know: any single sibling directory named after the page.
    matches = [
        child
        for child in path.parent.iterdir()
        if child.is_dir() and child.name.lower().startswith(path.stem.lower())
    ]
    return matches[0] if len(matches) == 1 else None


def _index_assets(folder: Path | None) -> dict[str, Path]:
    """Every file the browser saved, by name and by name-without-query.

    Indexed both ways because browsers rewrite `photo.jpg?v=3` to `photo.jpg`
    in the folder while leaving the query in the `src` attribute, and matching
    on only one of those loses roughly every image on a CDN-backed shop.
    """
    if folder is None or not folder.is_dir():
        return {}
    assets: dict[str, Path] = {}
    for child in sorted(folder.rglob("*")):
        if not child.is_file():
            continue
        name = unquote(child.name)
        assets.setdefault(name, child)
        assets.setdefault(name.split("?")[0], child)
        assets.setdefault(name.lower(), child)
    return assets


def _as_data_uri(path: Path) -> str | None:
    """A local image, inlined, so the ordinary image chain can read it.

    Inlined rather than passed as a `file://` URL on purpose: `file://` reaching
    the image validator would be a URL scheme that bypasses the SSRF guard, and
    the guard exists precisely so that no part of this program can be talked
    into reading the local disk over the network layer.
    """
    try:
        if path.stat().st_size > MAX_BYTES:
            return None
        blob = path.read_bytes()
    except OSError:
        return None
    mime = guess_type(path.name)[0] or "image/jpeg"
    if not mime.startswith("image/"):
        return None
    return f"data:{mime};base64,{b64encode(blob).decode('ascii')}"


def _local_photos(assets: dict[str, Path], limit: int = 12) -> list[str]:
    """Every image in the sidecar folder, inlined, largest first.

    Largest first because a saved page's folder holds the gallery alongside the
    icons, the logo and the payment-method sprites, and file size is the one
    signal available before decoding. Tier 1 still rejects whatever is not a
    listable photograph -- this only decides what it looks at first.
    """
    seen: set[Path] = set()
    photos: list[tuple[int, Path]] = []
    for path in assets.values():
        if path in seen:
            continue
        seen.add(path)
        if (guess_type(path.name)[0] or "").startswith("image/"):
            try:
                photos.append((path.stat().st_size, path))
            except OSError:  # pragma: no cover -- vanished between index and read
                continue
    photos.sort(key=lambda pair: -pair[0])
    return [uri for _, path in photos[:limit] if (uri := _as_data_uri(path)) is not None]


def _resolve_images(dom: HTMLParser, assets: dict[str, Path], origin: str) -> int:
    """Make every image reference mean something, or mean nothing on purpose.

    A saved page's `src` has usually been rewritten by the browser to
    `page_files/x.jpg`. Two wrong things could be done with that, and both were
    tried before this:

      - Resolve it against the origin. That INVENTS `shop.example/p/
        page_files/x.jpg`, a URL which has never existed, and sends Tier 1 to
        ask a blocked host about it.
      - Leave it. The extractor resolves it against the base URL and invents
        the same thing.

    So a reference with a local copy is replaced by that copy inlined -- which
    `absolutise` correctly drops, because a `data:` URI on a page is a
    placeholder pixel and not a product. The photograph itself reaches the
    candidate list through `local_photos`, ranked after whatever the page named
    for real.

    A reference with NO local copy is resolved against the origin, which is the
    case the rewriting is genuinely for: a page saved as "HTML only", where the
    CDN is still reachable even though the HTML endpoint was not.
    """
    resolved = 0
    for node in dom.css("img, source"):
        for attribute in ("src", "data-src", "srcset", "data-srcset"):
            value = node.attributes.get(attribute)
            if not value or value.startswith("data:"):
                continue
            first = value.split(",")[0].strip().split(" ")[0] if "srcset" in attribute else value
            local = _lookup(first, assets)
            if local is not None and (uri := _as_data_uri(local)) is not None:
                node.attrs[attribute] = uri
                resolved += 1
            elif origin and not first.startswith(("http://", "https://", "//")):
                node.attrs[attribute] = urljoin(origin, first)
                resolved += 1
    return resolved


def _lookup(reference: str, assets: dict[str, Path]) -> Path | None:
    if not assets:
        return None
    name = unquote(urlsplit(reference).path.rsplit("/", 1)[-1])
    for key in (name, name.split("?")[0], name.lower()):
        if (hit := assets.get(key)) is not None:
            return hit
    return None


# ---------------------------------------------------------------------------
# MHTML
# ---------------------------------------------------------------------------


def _read_mhtml(raw: bytes) -> tuple[str, dict[str, bytes], str]:
    """Unpack a single-file archive into its HTML part and its images.

    MHTML is MIME, so this is the stdlib's job rather than a parser of our own.
    """
    message = email.message_from_bytes(raw, policy=email.policy.default)
    html = ""
    origin = ""
    blobs: dict[str, bytes] = {}

    for part in message.walk():
        if part.is_multipart():
            continue
        location = str(part.get("Content-Location", "")).strip()
        ctype = part.get_content_type()
        try:
            payload = part.get_payload(decode=True)
        except (KeyError, TypeError):  # pragma: no cover -- malformed archive
            continue
        if not isinstance(payload, bytes):
            continue
        if ctype in ("text/html", "application/xhtml+xml") and not html:
            charset = part.get_content_charset() or "utf-8"
            html = payload.decode(charset, "replace")
            origin = location
        elif ctype.startswith("image/") and location:
            blobs[location] = payload

    if not html:
        raise SavedPageError(
            "That .mhtml file has no HTML part. Re-save the page with "
            '"Webpage, Single File" and try again.'
        )
    return html, blobs, origin


def _inline_mhtml_images(dom: HTMLParser, blobs: dict[str, bytes], origin: str) -> int:
    """Point every `src` at the absolute URL the archive recorded for it.

    MHTML keys its parts by `Content-Location`, which IS the original absolute
    URL -- so unlike a `_files` folder, the archive tells us what each image was
    called on the web. That is worth restoring: it gives Tier 1 a real URL to
    try before it falls back to the bytes.
    """
    resolved = 0
    for node in dom.css("img, source"):
        for attribute in ("src", "data-src"):
            value = node.attributes.get(attribute)
            if not value or value.startswith("data:"):
                continue
            absolute = urljoin(origin, value) if origin else value
            if absolute in blobs and absolute != value:
                node.attrs[attribute] = absolute
                resolved += 1
    return resolved


def _mhtml_photos(blobs: dict[str, bytes], limit: int = 12) -> list[str]:
    """The archive's own images, inlined, largest first. Same rule as a folder."""
    ordered = sorted(blobs.items(), key=lambda pair: -len(pair[1]))
    photos = []
    for location, blob in ordered[:limit]:
        mime = guess_type(location)[0] or "image/jpeg"
        if mime.startswith("image/"):
            photos.append(f"data:{mime};base64,{b64encode(blob).decode('ascii')}")
    return photos


# ---------------------------------------------------------------------------
# The one entry point
# ---------------------------------------------------------------------------


def load(path: Path, source_url: str = "") -> SavedPage:
    """Read a saved page from disk. Raises `SavedPageError` for bad input.

    `source_url` overrides what the file says about itself, for the case where
    the browser recorded nothing useful. It is not guessed from the filename.
    """
    if path.is_dir():
        # An operator who drags the folder rather than the file. Common enough
        # to handle rather than lecture about.
        candidates = sorted(
            child for child in path.iterdir() if child.suffix.lower() in SUFFIXES
        )
        if not candidates:
            raise SavedPageError(f"No .html or .mhtml file inside {path.name}.")
        path = candidates[0]

    if not path.is_file():
        raise SavedPageError(f"{path} is not a file.")
    if path.suffix.lower() not in SUFFIXES:
        raise SavedPageError(
            f"{path.name} is not a saved page. Save the product page from your browser "
            'with Ctrl+S ("Webpage, complete") and choose the .html file.'
        )
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise SavedPageError(
            f"{path.name} is {size / 1_048_576:.0f} MB, over the {MAX_BYTES // 1_048_576} MB "
            "limit. That is usually a whole-site save rather than one page."
        )

    raw = path.read_bytes()
    assets: dict[str, Path] = {}

    if path.suffix.lower() in (".mhtml", ".mht"):
        html, blobs, recorded = _read_mhtml(raw)
        dom = HTMLParser(html)
        origin = source_url or recorded or _origin_from_html(dom)
        inlined = _inline_mhtml_images(dom, blobs, origin)
        photos = _mhtml_photos(blobs)
    else:
        html = _decode(raw)
        dom = HTMLParser(html)
        origin = source_url or _origin_from_html(dom)
        folder = sidecar_for(path)
        assets = _index_assets(folder)
        inlined = _resolve_images(dom, assets, origin)
        photos = _local_photos(assets)

    if not origin:
        raise SavedPageError(
            f"{path.name} does not record which URL it was saved from, so the row would "
            "have no source. Pass the product's URL alongside the file."
        )

    log.info(
        "Loaded %s (%d reference(s) resolved, %d local photo(s)) as %s",
        path.name, inlined, len(photos), origin,
    )
    return SavedPage(
        html=dom.html or html,
        source_url=origin,
        path=path,
        assets=assets,
        local_photos=photos,
    )


def _decode(raw: bytes) -> str:
    """Browsers save in the page's own encoding, which is often not UTF-8."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")  # pragma: no cover -- latin-1 never fails
