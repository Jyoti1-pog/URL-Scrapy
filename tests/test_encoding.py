"""Never ask a server for a codec we cannot read.

THE DEFECT, and it is the worst kind: silent, and it blamed the shop.

`browser_headers` hardcoded `Accept-Encoding: gzip, deflate, br` while `brotli`
was not a dependency. So on a normal install we asked every site for Brotli,
got it, and handed the raw compressed bytes to the parser. A 200 came back, the
body was 191 KB of binary, and the row reported `no_title` and
`no_image_candidates` -- as though the page had no title and no photographs.
The same page, decoded, has 37 images and a title.

Only sites that happened to serve gzip worked. From the outside that looked
like "the tool only supports Amazon", which is exactly how it was reported.
"""

from __future__ import annotations

import httpx

from haat_lister.config import Settings
from haat_lister.fetch.ladder import _decodable_encodings, browser_headers, missing_codecs


def test_we_only_advertise_codecs_we_can_decode(settings: Settings) -> None:
    """The header is derived, never written down.

    Asserted against httpx's own value because httpx builds it from the codecs
    actually importable -- it is the one answer that cannot drift from the
    truth as dependencies come and go.
    """
    _decodable_encodings.cache_clear()
    advertised = browser_headers(settings)["Accept-Encoding"]
    honest = httpx.Client().headers.get("accept-encoding")

    assert advertised == honest, "we are claiming a codec httpx cannot decode"


def test_brotli_is_installed(settings: Settings) -> None:
    """Not merely handled -- present.

    Most CDNs serve Brotli when offered, so an install without it degrades to
    "no photographs anywhere". It is a hard dependency for that reason, and
    this test is what stops it drifting back into an extra.
    """
    import brotli  # noqa: F401

    assert "br" in browser_headers(settings)["Accept-Encoding"]


def test_a_missing_codec_is_reportable(settings: Settings) -> None:
    """One line of `config-check` beats losing a catalogue and not knowing why."""
    _decodable_encodings.cache_clear()
    assert missing_codecs() == [], f"cannot decode: {missing_codecs()}"
