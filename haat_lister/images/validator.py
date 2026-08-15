"""TIER 1 -- validate the source page's own direct image URL.

This is the gate that keeps the expensive path shut, so it is written to be
boring and read often. Nothing here downloads a file to keep, and nothing here
can reach an image host.

Tier 1 runs on every single row, always. It is never skipped.

PREDICATE ORDER
---------------
The nine predicates keep their spec numbers -- `reason` strings and
`ValidationResult.predicate` map straight back to the spec -- but they are
EVALUATED cheapest-first:

    1. syntax              pure string
    8. signed / expiring   pure string
    9. host reputation     local cache lookup
    2. reachable           <-- first network call
    3. redirect sanity
    4. content-type
    5. size floor
    6. decodable + dimensions
    7. hotlink test        <-- second network call

Predicates 8 and 9 were specified last. Running them there means a URL we can
reject for free still costs up to seven checks and two round trips first, which
is backwards for a tool whose whole design is cheap-path-first: on a 5,000-URL
batch against one blocking CDN that is thousands of avoidable requests. Moving
them ahead of the network preserves every stated guarantee -- `tier1_attempted`
is still True on every row, the reasons are unchanged, and predicate 9 remains a
cache rather than a bypass.

Evaluation short-circuits on the first failure: predicate 6 never runs if
predicate 4 already failed.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import httpx
from PIL import Image, UnidentifiedImageError

from ..config import ValidatorConfig
from ..models import ValidationResult
from ..store.ledger import Ledger
from ..utils.logging import get_logger
from ..utils.netguard import BlockedHost, request_hook
from ..utils.urls import host_of

log = get_logger(__name__)

# The order `validate` actually runs the predicates in, with their spec numbers.
# Written down because `diagnose` reconstructs the walk from the one predicate a
# ValidationResult names -- "stopped at 6" only means "1, 8, 9, 2, 3, 4, 5 passed"
# if something states that sequence. Reading it off a comment would let the two
# drift; a test asserts this matches `validate`.
EVALUATION_ORDER: tuple[tuple[int, str], ...] = (
    (1, "syntax"),
    (8, "not signed or expiring"),
    (9, "host reputation"),
    (2, "reachable"),
    (3, "redirect chain"),
    (4, "content-type"),
    (5, "size floor"),
    (6, "decodable, dimensions"),
    (7, "hotlink test"),
)

# Magic bytes for the formats a marketplace photo could plausibly be. Checked
# before Pillow so that an HTML block page served with `Content-Type: image/jpeg`
# is rejected as undecodable rather than tying up the decoder.
_MAGIC = {
    b"\xff\xd8\xff": "jpeg",
    b"\x89PNG\r\n\x1a\n": "png",
    b"GIF87a": "gif",
    b"GIF89a": "gif",
    b"BM": "bmp",
}

# Paths that mean "you have been redirected to a page, not an image".
_INTERSTITIAL_MARKERS = (
    "/login",
    "/signin",
    "/sign-in",
    "/auth",
    "/consent",
    "/challenge",
    "/captcha",
    "/blocked",
    "/access-denied",
)


def sniff_format(head: bytes) -> str | None:
    for magic, name in _MAGIC.items():
        if head.startswith(magic):
            return name
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp":  # avif / heic share the ISO-BMFF container
        return "avif"
    return None


# ---------------------------------------------------------------------------
# Pure predicates -- no network, trivially testable
# ---------------------------------------------------------------------------


def _data_uri_mime(url: str) -> str:
    head = url[5 : url.find(",")] if "," in url else ""
    return head.split(";")[0] or "application/octet-stream"


def _decode_data_uri(url: str) -> bytes:
    """The bytes out of a `data:` candidate. Never raises for junk.

    Inlined images from a saved page arrive this way. `file://` was the obvious
    alternative and is deliberately not used: a scheme that reads local disk
    through the image chain would be a hole straight past the SSRF guard, and
    the guard is the reason nothing in this program can be talked into fetching
    `169.254.169.254`.
    """
    from base64 import b64decode
    from urllib.parse import unquote_to_bytes

    comma = url.find(",")
    if comma < 0:
        return b""
    header, payload = url[5:comma], url[comma + 1 :]
    try:
        if "base64" in header:
            return b64decode(payload, validate=False)
        return unquote_to_bytes(payload)
    except (ValueError, TypeError):
        return b""


def check_syntax(url: str) -> str | None:
    """Predicate 1. Absolute http(s) with a host -- or an inlined image.

    `data:` is admitted here and nowhere else. It carries its own bytes, so it
    has no host to resolve and nothing for the SSRF guard to be asked about;
    every remaining predicate still runs against it unchanged.
    """
    if url.startswith("data:"):
        return None if _data_uri_mime(url).startswith("image/") else "bad_syntax"
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError, TypeError):
        return "bad_syntax"
    if parsed.scheme not in ("http", "https") or not parsed.host:
        return "bad_syntax"
    return None


def check_not_signed(url: str, tokens: list[str]) -> str | None:
    """Predicate 8. A URL that works today and 404s in six hours is worthless
    in a live listing, so we would rather host our own copy."""
    for token in tokens:
        # Mixed-case tokens (X-Amz-Signature, Key-Pair-Id) are matched as given;
        # lowercase ones are matched case-insensitively.
        if token.lower() == token:
            if token in url.lower():
                return "signed_or_expiring_url"
        elif token in url:
            return "signed_or_expiring_url"
    return None


def check_host_reputation(url: str, ledger: Ledger | None, cfg: ValidatorConfig) -> str | None:
    """Predicate 9. A cache, not a bypass: the attempt still counts as made, it
    just resolves instantly from evidence we already paid for."""
    if ledger is None:
        return None
    host = host_of(url)
    if host and ledger.is_bad_hotlink_host(
        host, cfg.bad_host_failures_before_caching, cfg.bad_host_cache_ttl_days
    ):
        return "host_known_to_block"
    return None


def check_content_type(content_type: str | None) -> str | None:
    """Predicate 4. `text/html` here means a block page is being served."""
    if not content_type or not content_type.split(";")[0].strip().lower().startswith("image/"):
        return "wrong_content_type"
    return None


# The smallest well-formed image there is: a 26-byte 1x1 GIF. Below that, no
# byte sequence can encode a picture in any format, so a Content-Length under
# it is not describing a small image -- it is not describing an image.
#
# Tight on purpose. An earlier attempt used 64, which swallowed the genuine
# 43-byte tracking pixel this floor exists to kill -- caught by the test that
# asserts exactly that, which is why the number is the real minimum rather than
# a comfortable round one.
SMALLEST_POSSIBLE_IMAGE = 26


def check_size_floor(content_length: int | None, min_bytes: int) -> str | None:
    """Predicate 5. Kills tracking pixels and placeholders.

    A missing Content-Length does NOT fail here. Chunked responses and plenty of
    CDNs omit it, and rejecting those would throw away good images; predicate 6
    enforces the floor on bytes actually read instead.

    An IMPOSSIBLE Content-Length is treated the same way, and that is not a
    loosening. Flipkart's CDN answers HEAD with `Content-Length: 20` and
    `image/webp` for a 1500x1500 photograph -- a stub for HEAD, the real file
    for GET. Twenty bytes cannot encode any image in any format, so believing
    it rejected every photograph on the site as `too_small`. Below this
    threshold the header is not evidence about the picture; predicate 6 reads
    actual bytes and enforces the floor on those, which is where a genuine
    43-byte tracking pixel dies anyway.
    """
    if content_length is None or content_length < SMALLEST_POSSIBLE_IMAGE:
        return None
    if content_length < min_bytes:
        return "too_small"
    return None


def check_redirect_chain(response: httpx.Response, max_hops: int) -> str | None:
    """Predicate 3. At most `max_hops`, and the destination must be an image.

    Only applies when a redirect actually happened. A URL that serves HTML
    without redirecting is predicate 4's business, and reporting it here would
    tell the operator to look for a redirect that does not exist.
    """
    if not response.history:
        return None
    if len(response.history) > max_hops:
        return "too_many_redirects"
    final_path = (response.url.path or "").lower()
    if any(marker in final_path for marker in _INTERSTITIAL_MARKERS):
        return "redirect_to_interstitial"
    if "text/html" in response.headers.get("content-type", "").lower():
        return "redirect_to_html"
    return None


def check_dimensions(image: Image.Image, cfg: ValidatorConfig) -> str | None:
    """Predicate 6, second half. haat is a premium marketplace; thumbnails are
    not listable.

    Two reasons, not one. `below_min_dimensions` means "under the standard but
    usable, and an operator may want it anyway"; `unusably_small` means the
    answer is no whatever they think. Only the first is eligible for the low-res
    tier, and keeping them apart is what stops that tier turning the standard
    into a suggestion.
    """
    width, height = image.size
    if width < cfg.hard_min_width or height < cfg.hard_min_height:
        return "unusably_small"
    if width < cfg.min_width or height < cfg.min_height:
        return "below_min_dimensions"
    return None


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------


@dataclass
class _Probe:
    response: httpx.Response
    content_type: str | None
    content_length: int | None


class Tier1Validator:
    """Runs the nine predicates against one candidate URL."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        cfg: ValidatorConfig,
        ledger: Ledger | None = None,
        hotlink_test: bool | None = None,
        allow_private_hosts: list[str] | None = None,
    ) -> None:
        self._client = client
        self._cfg = cfg
        self._ledger = ledger
        self._hotlink_test = cfg.hotlink_test if hotlink_test is None else hotlink_test
        # Defaults to empty rather than to fetch.allow_private_hosts: a caller
        # that forgets to pass it gets the guarded behaviour, not the permissive
        # one. The resolver passes the real list.
        self._allow_hosts = allow_private_hosts or []

    async def validate(self, url: str) -> ValidationResult:
        """True only if all nine predicates pass. Never raises."""
        cfg = self._cfg

        # --- free checks, before any network call --------------------------
        if reason := check_syntax(url):
            return ValidationResult(url=url, ok=False, reason=reason, predicate=1)
        if reason := check_not_signed(url, cfg.signed_url_tokens):
            return ValidationResult(url=url, ok=False, reason=reason, predicate=8)
        if reason := check_host_reputation(url, self._ledger, cfg):
            return ValidationResult(url=url, ok=False, reason=reason, predicate=9)

        # --- predicate 2: reachable ---------------------------------------
        try:
            probe = await self._probe(url)
        except _ProbeFailed as exc:
            return ValidationResult(url=url, ok=False, reason=exc.reason, predicate=2)

        def result(
            ok: bool,
            reason: str,
            predicate: int | None = None,
            width: int | None = None,
            height: int | None = None,
        ) -> ValidationResult:
            return ValidationResult(
                url=url,
                ok=ok,
                reason=reason,
                predicate=predicate,
                content_type=probe.content_type,
                content_length=probe.content_length,
                width=width,
                height=height,
            )

        if reason := check_redirect_chain(probe.response, cfg.max_redirect_hops):
            return result(False, reason, 3)
        if reason := check_content_type(probe.content_type):
            return result(False, reason, 4)
        if reason := check_size_floor(probe.content_length, cfg.min_bytes):
            return result(False, reason, 5)

        # --- predicate 6: decode far enough to read the real dimensions ----
        try:
            image, bytes_read = await self._open_header(url)
        except _ProbeFailed as exc:
            return result(False, exc.reason, 6)

        if (
            probe.content_length is None or probe.content_length < SMALLEST_POSSIBLE_IMAGE
        ) and bytes_read < cfg.min_bytes:
            # Deferred from predicate 5: the header was absent or impossible,
            # and the bytes we actually read came in under the floor. This is
            # where a real tracking pixel dies.
            return result(False, "too_small", 5)

        width, height = image.size
        if reason := check_dimensions(image, cfg):
            return result(False, reason, 6, width, height)

        # --- predicate 7: would a third party get this image? --------------
        #
        # An inlined image has no third party to ask. It is not "passed" -- it
        # is not applicable, and a saved-page row therefore cannot end up
        # hotlinking anything, because there is no remote URL to hotlink.
        if url.startswith("data:"):
            return result(True, "direct_ok", None, width, height)
        if self._hotlink_test and not await self._hotlink_ok(url):
            self._record_hotlink_failure(url)
            return result(False, "hotlink_blocked", 7, width, height)

        if self._ledger is not None:
            self._ledger.clear_bad_host(host_of(url))
        return result(True, "direct_ok", None, width, height)

    # -- network helpers ---------------------------------------------------

    async def _probe(self, url: str) -> _Probe:
        """HEAD, falling back to a ranged GET where HEAD is not honoured."""
        if url.startswith("data:"):
            # v5 §4.2. A photo the operator's own browser already saved. It
            # goes through all nine predicates like any other candidate -- the
            # bytes are simply already here, so predicates 2 and 3 are answered
            # without asking anyone. Handled at this seam rather than by
            # skipping the validator, because "imported images are trusted" is
            # how an unlistable 90x90 thumbnail reaches a live listing.
            blob = _decode_data_uri(url)
            return _Probe(
                response=httpx.Response(200, request=httpx.Request("GET", "https://saved.local/")),
                content_type=_data_uri_mime(url),
                content_length=len(blob),
            )
        response = await self._request_with_fallback(url)
        if response.status_code >= 400:
            raise _ProbeFailed(f"http_{response.status_code}")
        return _Probe(
            response=response,
            content_type=response.headers.get("content-type"),
            content_length=_int_or_none(response.headers.get("content-length")),
        )

    async def _request_with_fallback(self, url: str) -> httpx.Response:
        try:
            response = await self._client.head(url, follow_redirects=True)
            # Plenty of CDNs simply do not implement HEAD.
            if response.status_code in (403, 405, 501):
                return await self._client.get(
                    url, headers={"Range": "bytes=0-2047"}, follow_redirects=True
                )
            return response
        except httpx.TooManyRedirects as exc:
            raise _ProbeFailed("too_many_redirects") from exc
        except httpx.TimeoutException as exc:
            raise _ProbeFailed("timeout") from exc
        except httpx.ConnectError as exc:
            raise _ProbeFailed("dns_error") from exc
        except httpx.HTTPError as exc:
            raise _ProbeFailed("request_failed") from exc

    async def _open_header(self, url: str) -> tuple[Image.Image, int]:
        """Stream only as far as Pillow needs to read the dimensions.

        JPEG and PNG give up their size in the first few KB, which is the whole
        point of probing rather than downloading. WebP and AVIF do not -- Pillow
        will not open a partial file of either -- so when the probe fails we
        keep reading up to `max_probe_bytes` before calling it undecodable.
        Without that, every WebP image on the web would fail Tier 1, and haat's
        own storage serves WebP.
        """
        if url.startswith("data:"):
            blob = _decode_data_uri(url)
            # The magic-byte sniff still applies. A saved page can perfectly
            # well carry an inlined tracking pixel or an SVG, and the operator
            # having supplied it does not make it a product photograph.
            if not sniff_format(blob[:16]):
                raise _ProbeFailed("undecodable")
            if (opened := _try_open(blob)) is None:
                raise _ProbeFailed("undecodable")
            return opened, len(blob)

        buffer = bytearray()
        threshold = self._cfg.header_probe_bytes
        hard_cap = max(self._cfg.max_probe_bytes, threshold)
        image: Image.Image | None = None

        try:
            async with self._client.stream("GET", url, follow_redirects=True) as response:
                if response.status_code >= 400:
                    raise _ProbeFailed(f"http_{response.status_code}")

                async for chunk in response.aiter_bytes(8192):
                    buffer.extend(chunk)
                    if len(buffer) < threshold:
                        continue

                    if len(buffer) >= 16 and not sniff_format(bytes(buffer[:16])):
                        # Not an image at all -- an HTML block page wearing an
                        # image content-type. No amount of extra bytes helps.
                        raise _ProbeFailed("undecodable")

                    if (image := _try_open(bytes(buffer))) is not None:
                        break
                    if len(buffer) >= hard_cap:
                        break
                    threshold = min(threshold * 4, hard_cap)
        except _ProbeFailed:
            raise
        except httpx.TimeoutException as exc:
            raise _ProbeFailed("timeout") from exc
        except httpx.HTTPError as exc:
            raise _ProbeFailed("request_failed") from exc

        if not buffer or not sniff_format(bytes(buffer[:16])):
            raise _ProbeFailed("undecodable")

        # The stream may have ended before we reached a threshold -- a small
        # file, which is legitimate and handled by the size floor.
        image = image or _try_open(bytes(buffer))
        if image is None:
            raise _ProbeFailed("undecodable")
        return image, len(buffer)

    async def _hotlink_ok(self, url: str) -> bool:
        """Predicate 7. Re-request as a stranger would: no Referer, neutral UA,
        a brand-new session with no cookies.

        Many CDNs serve a browser sitting on the product page and 403 everyone
        else, which is exactly what a haat listing would be.

        The fresh session is the point, but it must not also be a fresh hole:
        image URLs come out of a shop's own HTML, so a compromised page could
        point one at a private address. The SSRF hook rides along -- it is about
        WHERE we connect, not about session state. The earlier predicates would
        usually block first; "usually" is not a security property.
        """
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=self._client.timeout,
            headers={"User-Agent": self._cfg.hotlink_neutral_user_agent},
            cookies=None,
            event_hooks={"request": [request_hook(self._allow_hosts)]},
        ) as stranger:
            try:
                response = await stranger.get(url, headers={"Range": "bytes=0-2047"})
            except BlockedHost:
                return False
            except httpx.HTTPError:
                return False
        if response.status_code >= 400:
            return False
        return check_content_type(response.headers.get("content-type")) is None

    def _record_hotlink_failure(self, url: str) -> None:
        if self._ledger is None:
            return
        if host := host_of(url):
            count = self._ledger.record_hotlink_failure(host)
            log.debug("Hotlink failure %d for %s", count, host)


def _try_open(data: bytes) -> Image.Image | None:
    """Pillow reads the header only, which is all we need for `.size`."""
    try:
        return Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, OSError, ValueError):
        return None


class _ProbeFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


async def validate_all_candidates(
    candidates: list[str],
    validator: Tier1Validator,
    *,
    stop_at_first: bool = True,
) -> tuple[ValidationResult | None, list[ValidationResult]]:
    """Walk candidates in rank order. Returns (winner or None, every result).

    `stop_at_first` is the default because a JOB needs one hero photo, and
    stopping early is what makes Tier 1 a single HEAD request on a healthy
    page -- across a 200-URL catalogue that economy is the whole design.

    Find photos passes False, because its subtitle promises "every photo for
    every product" and it was delivering one. That costs up to
    `max_images_per_product` HEAD requests for a page instead of one, which is
    the trade that screen exists to make: it writes nothing and creates no
    listing, so the only thing it spends is time.
    """
    results: list[ValidationResult] = []
    winner: ValidationResult | None = None
    for url in candidates:
        result = await validator.validate(url)
        results.append(result)
        if result.ok:
            if winner is None:
                winner = result
            if stop_at_first:
                return winner, results
    return winner, results
