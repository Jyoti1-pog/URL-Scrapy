"""Finding URLs in whatever an operator pasted, then giving them one identity.

Two jobs, in that order.

FINDING. `extract_urls` is the only place that turns a blob of text into links,
shared by the CLI's file reader, the API and the console's live preview. One
implementation, because three that agree today would disagree by Friday and the
operator would see a different count depending on where they looked.

IDENTITY. Canonicalisation is what dedupe and resume both key on, so it has to
be stable across runs and conservative about what it throws away. Notably it
does NOT sort query parameters: tempting for dedupe, but plenty of storefronts
treat parameter order as meaningful and reordering them can change which product
you get. The per-domain half lives in `canonical.py`.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from slugify import slugify

from .canonical import DEFAULT_IDENTITY, Identity, apply_rules

# Params that never identify a product, only where the click came from.
DEFAULT_TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
        "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "igshid", "_ga", "_gl",
        "ref", "referrer", "source", "spm", "srsltid",
    }
)

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def canonicalise(
    url: str,
    strip_params: frozenset[str] | set[str] | None = None,
    identity: Identity = DEFAULT_IDENTITY,
) -> str:
    """A stable identity for a page URL.

    Lowercases scheme and host, drops the fragment, removes default ports and
    tracking params, and leaves everything else -- including parameter order --
    exactly as found. Then applies the per-domain rule for that host, if there
    is one, which is what makes two tracking-laden links to one ASIN dedupe.

    `identity` is a parameter rather than a module global on purpose: an
    operator can extend the rule table in config.yaml, and every caller that
    computes an identity has to be using the same one or dedupe quietly stops
    working on exactly the sites the rules were added for.
    """
    strip = frozenset(strip_params) if strip_params is not None else DEFAULT_TRACKING_PARAMS
    parts = urlsplit(url.strip())

    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    # A leading `www.` is not part of a product's identity. This matters more
    # now that bare domains are accepted: an operator pasting `amazon.in/dp/X`
    # next to `https://www.amazon.in/dp/X` means one product, and without this
    # they get two rows and two fetches. Only the IDENTITY is normalised -- the
    # URL actually fetched is the one they pasted, so a shop that answers on
    # only one of the two is unaffected.
    if host.startswith("www.") and host.count(".") > 1:
        host = host[4:]
    netloc = host
    if parts.port and str(parts.port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"

    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in strip
        ],
        safe=",",
    )

    path = parts.path or "/"
    generic = urlunsplit((scheme, netloc, path, query, ""))
    return apply_rules(generic, identity)


# ---------------------------------------------------------------------------
# Finding links in a paste
# ---------------------------------------------------------------------------

# Invisible characters a spreadsheet or a chat client will happily put in the
# middle of a link. Written as escapes because the literals are, by definition,
# not visible in this file. Stripped before anything else looks at the text.
_ZERO_WIDTH = dict.fromkeys(
    map(ord, "​‌‍⁠﻿­")
)

# The rest of the unicode space zoo, which NFKC does not fold. NBSP does fold,
# so it is not listed.
_ODD_SPACES = re.compile("[  -   　]")

# Characters that cannot appear unencoded inside a URL, so a run ends at them.
# Note what is ABSENT: comma and semicolon. Both are legal in a path and a query
# string, and treating them as delimiters is the obvious approach that silently
# cuts real links in half -- `?ids=1,2,3` becomes three broken URLs.
_HARD_STOP = r"\s\"'<>`|\\^{}"

# A run starts at a scheme and continues until a hard stop OR the start of the
# next link. The lookahead is what makes `https://a/x,https://b/y` -- no space,
# which is what a spreadsheet export gives you -- two links rather than one.
_URL_RUN = re.compile(rf"https?://(?:(?!https?://)[^{_HARD_STOP}])+", re.IGNORECASE)

# Markdown and HTML wrappers, unwrapped before the scan so the label cannot be
# mistaken for a second link: `[amazon.in/dp/X](https://...)` would otherwise
# yield the bare-domain text as well as the real target.
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?\s*\)")
_HREF = re.compile(r"""\bhref\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

# A bare domain: at least one dot, a plausible TLD, and an optional path.
_BARE_DOMAIN = re.compile(
    r"(?<![\w@.\-/])((?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)+[a-z]{2,63})(/[^\s\"'<>`|\\]*)?",
    re.IGNORECASE,
)

# What a bare two-label token is, when it is not a domain. Only consulted when
# there is no path, so `shop.example/product.html` is unaffected.
_FILE_EXTENSIONS = frozenset(
    {
        "txt", "md", "csv", "tsv", "json", "xml", "yaml", "yml", "log", "ini", "cfg",
        "py", "js", "ts", "sh", "bat", "exe", "dll", "zip", "gz", "tar", "rar",
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
        "jpg", "jpeg", "png", "gif", "webp", "svg", "mp4", "mp3", "mov",
    }
)

# Delimiters, for splitting the leftovers only -- never for finding links.
# Spaces are absent: "hello world" is one thing an operator did not mean as a
# link, not two.
_RESIDUE_SPLIT = re.compile(r"[\n\r\t,;|]+")

# Trailing punctuation that ends a sentence far more often than it ends a URL.
_TRAILING_PUNCTUATION = ".,;:!?"

_WORD = re.compile(r"\w")

_BRACKETS = (("(", ")"), ("[", "]"), ("{", "}"))


@dataclass(frozen=True)
class FoundUrl:
    """One link, as found. Not yet canonicalised -- that is a separate decision."""

    url: str
    # True when the operator wrote `amazon.in/dp/X` and we supplied the scheme.
    # Surfaced rather than silent: assuming https for a shop that only answers
    # on http would otherwise fail the row for no visible reason.
    assumed_scheme: bool = False
    # 1-based, so a message can say which line to go and look at.
    line: int = 0


@dataclass(frozen=True)
class Unparsed:
    """Something in the paste that was not a link, kept exactly as written."""

    text: str
    line: int = 0


@dataclass
class Extraction:
    urls: list[FoundUrl] = field(default_factory=list)
    # Everything that was not a link. Never dropped silently: a typo an operator
    # cannot see is a row they will not know is missing.
    unparsed: list[Unparsed] = field(default_factory=list)


def _trim(run: str) -> str:
    """Strip wrappers and sentence punctuation a URL almost never ends with.

    Bracket-aware, so `.../dp/B0FT?a=(1)` keeps its parenthesis and
    `(https://example.com/x)` loses it. Loops because a run can end with several
    at once -- `see (https://a.example/x).` leaves `x).` behind.
    """
    while run:
        if run[-1] in _TRAILING_PUNCTUATION:
            run = run[:-1]
            continue
        for opener, closer in _BRACKETS:
            if run.endswith(closer) and run.count(closer) > run.count(opener):
                run = run[:-1]
                break
        else:
            break
    return run


def _looks_like_a_file(host: str, path: str) -> bool:
    if path:
        return False
    labels = host.split(".")
    return len(labels) == 2 and labels[-1].lower() in _FILE_EXTENSIONS


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    """Erase the matched spans, keeping the offsets of everything else.

    By span rather than by `str.replace`, which is O(n) per call and turns a
    single line holding five thousand links into a quadratic one.
    """
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        for index in range(start, end):
            chars[index] = " "
    return "".join(chars)


def extract_urls(blob: str) -> Extraction:
    """Find every link in a paste, in the order they appear.

    Extracts rather than splits. Splitting on commas is the obvious approach and
    it is wrong -- commas are legal in URLs and appear in real query strings --
    so this scans for URL-shaped runs and treats everything else as leftovers.

    Handles, mixed in one paste: newlines, commas, semicolons, tabs, pipes, runs
    of spaces, markdown links, `href="..."`, angle brackets, quotes, trailing
    sentence punctuation, and bare domains.

    Leftovers are reported, not judged. A line of prose with no link in it comes
    back in `unparsed` exactly as written, which is noisier than dropping it and
    is the point: the alternative is an operator whose typo vanished.
    """
    if not blob:
        return Extraction()

    text = _ODD_SPACES.sub(" ", unicodedata.normalize("NFKC", blob).translate(_ZERO_WIDTH))
    text = _MARKDOWN_LINK.sub(lambda m: f" {m.group(1)} ", text)
    text = _HREF.sub(lambda m: f" {m.group(1)} ", text)

    found = Extraction()

    # Per line, so leftovers can be reported next to the line they came from and
    # a line that did produce a link is not also reported as junk.
    for number, line in enumerate(text.splitlines(), start=1):
        hits: list[tuple[int, str, bool]] = []
        spans: list[tuple[int, int]] = []

        for match in _URL_RUN.finditer(line):
            if url := _trim(match.group(0)):
                hits.append((match.start(), url, False))
            spans.append(match.span())

        # Bare domains, from what is left -- so a full URL's own host is never
        # also read as a bare domain.
        #
        # The dot check is not a micro-optimisation. On a paste that is all
        # well-formed links the residue is pure whitespace, and running the
        # bare-domain pattern over every line of it was most of the time a
        # 5,000-link paste took.
        residue = _blank(line, spans) if spans else line
        if "." in residue:
            spans = []
            for match in _BARE_DOMAIN.finditer(residue):
                host, path = match.group(1), match.group(2) or ""
                if _looks_like_a_file(host, path):
                    continue
                if url := _trim(f"https://{host}{path}"):
                    hits.append((match.start(), url, True))
                spans.append(match.span())
            residue = _blank(residue, spans) if spans else residue

        # Positional order, so the list reads the way the paste does regardless
        # of which rule found which link.
        #
        # Repeats are NOT collapsed here. Deciding that two links are the same
        # product needs the canonical form and belongs to `plan_urls`, which
        # also reports the count -- collapsing them at this level would delete
        # the evidence before anything could show it, and "48 pasted, 6
        # duplicate" is exactly what the operator asked to see.
        for _, url, assumed in sorted(hits, key=lambda hit: hit[0]):
            found.urls.append(FoundUrl(url=url, assumed_scheme=assumed, line=number))

        # What counts as worth reporting depends on whether this line worked.
        # A line that produced no link is reported whole -- that is the typo
        # case, and the whole reason leftovers are kept at all. A line that DID
        # produce a link only reports leftovers that could plausibly have been
        # meant as links themselves, because otherwise pasting a chat thread
        # buries the one real typo under a page of ordinary sentences.
        worked = bool(hits)
        if not residue.strip():
            continue
        for fragment in _RESIDUE_SPLIT.split(residue):
            cleaned = fragment.strip()
            if not cleaned or not _WORD.search(cleaned):
                continue
            if worked and "." not in cleaned and "/" not in cleaned:
                continue
            found.unparsed.append(Unparsed(text=cleaned, line=number))

    return found


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
    """Remove named query params, preserving the order of the rest.

    `safe=","` for the same reason as `canonicalise`, but with more at stake:
    this result is the image URL we actually request. A CDN taking
    `?tx=w_800,h_600` gets what its own page published rather than `%2C`, which
    a correct server decodes identically and a careless one does not.
    """
    lowered = {p.lower() for p in params}
    parts = urlsplit(url)
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in lowered
        ],
        safe=",",
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
