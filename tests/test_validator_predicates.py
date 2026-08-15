"""Phase 3: the nine Tier-1 predicates, both directions.

This is the module that keeps the expensive path shut, so it gets the closest
tests in the suite. Every predicate has at least one accept case and one reject
case, and the ordering guarantees are asserted by counting requests rather than
by reading the code.
"""

from __future__ import annotations

import io

import httpx
import pytest
import respx
from PIL import Image

from haat_lister.config import ValidatorConfig
from haat_lister.images.validator import (
    Tier1Validator,
    check_content_type,
    check_not_signed,
    check_size_floor,
    check_syntax,
    sniff_format,
    validate_all_candidates,
)
from haat_lister.store.ledger import Ledger

URL = "https://cdn.example/kurta.jpg"


def jpeg_bytes(width: int = 1200, height: int = 1200) -> bytes:
    """A JPEG of realistic weight.

    Deliberately noise rather than flat colour: a flat 400x400 JPEG compresses
    to about 3 KB and would trip the predicate-5 size floor, which would hide
    whatever the test was actually trying to check.
    """
    buffer = io.BytesIO()
    Image.merge("RGB", [Image.effect_noise((width, height), 120).convert("L")] * 3).save(
        buffer, format="JPEG", quality=95
    )
    return buffer.getvalue()


def image_response(
    body: bytes | None = None,
    status: int = 200,
    content_type: str = "image/jpeg",
    content_length: int | None = None,
) -> httpx.Response:
    body = jpeg_bytes() if body is None else body
    headers = {"content-type": content_type}
    if content_length is not None:
        headers["content-length"] = str(content_length)
    return httpx.Response(status, content=body, headers=headers)


def mock_image(url: str = URL, **kwargs) -> None:
    """A well-behaved image: HEAD, ranged GET and stranger GET all succeed."""
    body = kwargs.pop("body", None) or jpeg_bytes()
    respx.head(url).mock(
        return_value=httpx.Response(
            200,
            headers={
                "content-type": kwargs.get("content_type", "image/jpeg"),
                "content-length": str(len(body)),
            },
        )
    )
    respx.get(url).mock(return_value=image_response(body, **kwargs))


@pytest.fixture
def vcfg(app_config) -> ValidatorConfig:
    return app_config.validator


@pytest.fixture
def ledger():
    with Ledger(":memory:") as led:
        yield led


async def validate(vcfg: ValidatorConfig, url: str = URL, ledger=None, hotlink: bool = True):
    async with httpx.AsyncClient() as client:
        validator = Tier1Validator(client, vcfg, ledger, hotlink_test=hotlink)
        return await validator.validate(url)


# ---------------------------------------------------------------------------
# Predicate 1 -- syntax
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["", "not-a-url", "/relative/path.jpg", "ftp://cdn.example/a.jpg", "https:///no-host.jpg"],
)
def test_rejects_bad_syntax(url):
    assert check_syntax(url) == "bad_syntax"


def test_accepts_absolute_http_urls():
    assert check_syntax("https://cdn.example/a.jpg") is None
    assert check_syntax("http://cdn.example/a.jpg") is None


@respx.mock
async def test_bad_syntax_costs_no_network_call(vcfg):
    route = respx.route()
    result = await validate(vcfg, "not-a-url")
    assert result.reason == "bad_syntax"
    assert not route.called


# ---------------------------------------------------------------------------
# Predicate 8 -- signed / expiring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example/a.jpg?X-Amz-Signature=abc",
        "https://cdn.example/a.jpg?X-Amz-Expires=3600",
        "https://cdn.example/a.jpg?Expires=1699999999",
        "https://cdn.example/a.jpg?token=xyz",
        "https://cdn.example/a.jpg?sig=xyz",
        "https://cdn.example/a.jpg?Key-Pair-Id=K123",
        "https://cdn.example/tmp/a.jpg",
        "https://cdn.example/session/a.jpg",
        "https://cdn.example/preview/a.jpg",
    ],
)
def test_rejects_signed_url(vcfg, url):
    assert check_not_signed(url, vcfg.signed_url_tokens) == "signed_or_expiring_url"


def test_accepts_clean_cdn_url(vcfg):
    assert (
        check_not_signed("https://cdn.example/products/kurta.jpg?v=42", vcfg.signed_url_tokens)
        is None
    )


@respx.mock
async def test_signed_url_costs_no_network_call(vcfg):
    """Moved ahead of the network per the agreed ordering: a URL we can reject
    for free must not cost a round trip."""
    route = respx.route()
    result = await validate(vcfg, "https://cdn.example/a.jpg?X-Amz-Signature=abc")
    assert result.reason == "signed_or_expiring_url"
    assert result.predicate == 8
    assert not route.called


# ---------------------------------------------------------------------------
# Predicate 9 -- host reputation cache
# ---------------------------------------------------------------------------


@respx.mock
async def test_known_bad_host_costs_no_network_call(vcfg, ledger):
    for _ in range(vcfg.bad_host_failures_before_caching):
        ledger.record_hotlink_failure("cdn.example")

    route = respx.route()
    result = await validate(vcfg, URL, ledger)

    assert result.reason == "host_known_to_block"
    assert result.predicate == 9
    assert not route.called


@respx.mock
async def test_host_below_the_threshold_is_still_attempted(vcfg, ledger):
    ledger.record_hotlink_failure("cdn.example")  # 1 of 3
    mock_image()
    result = await validate(vcfg, URL, ledger)
    assert result.ok


def test_only_confirmed_blocks_populate_the_cache(vcfg, ledger):
    """Timeouts and undersized images describe one image, not a host."""
    assert not ledger.is_bad_hotlink_host("cdn.example", 3, 30)
    for _ in range(3):
        ledger.record_hotlink_failure("cdn.example")
    assert ledger.is_bad_hotlink_host("cdn.example", 3, 30)


@respx.mock
async def test_a_clean_hotlink_clears_the_host(vcfg, ledger):
    ledger.record_hotlink_failure("cdn.example")
    mock_image()
    await validate(vcfg, URL, ledger)
    assert ledger.bad_hosts() == []


# ---------------------------------------------------------------------------
# Predicate 2 -- reachable
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize("status", [404, 410, 500, 503])
async def test_rejects_unreachable(vcfg, status):
    respx.head(URL).mock(return_value=httpx.Response(status))
    result = await validate(vcfg)
    assert result.reason == f"http_{status}"
    assert result.predicate == 2


@respx.mock
async def test_falls_back_to_ranged_get_when_head_is_refused(vcfg):
    """Plenty of CDNs simply do not implement HEAD."""
    respx.head(URL).mock(return_value=httpx.Response(405))
    respx.get(URL).mock(return_value=image_response(content_length=len(jpeg_bytes())))
    result = await validate(vcfg)
    assert result.ok


@respx.mock
async def test_timeout_is_reported_not_swallowed(vcfg):
    respx.head(URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    result = await validate(vcfg)
    assert result.reason == "timeout"


@respx.mock
async def test_dns_failure_is_reported(vcfg):
    respx.head(URL).mock(side_effect=httpx.ConnectError("no such host"))
    result = await validate(vcfg)
    assert result.reason == "dns_error"


# ---------------------------------------------------------------------------
# Predicate 3 -- redirect sanity
# ---------------------------------------------------------------------------


@respx.mock
async def test_rejects_redirect_to_login_interstitial(vcfg):
    respx.head(URL).mock(
        return_value=httpx.Response(302, headers={"location": "https://cdn.example/login"})
    )
    respx.head("https://cdn.example/login").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"})
    )
    result = await validate(vcfg)
    assert result.reason == "redirect_to_interstitial"
    assert result.predicate == 3


@respx.mock
async def test_rejects_redirect_to_html(vcfg):
    respx.head(URL).mock(
        return_value=httpx.Response(302, headers={"location": "https://cdn.example/oops.html"})
    )
    respx.head("https://cdn.example/oops.html").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"})
    )
    result = await validate(vcfg)
    assert result.reason == "redirect_to_html"


# ---------------------------------------------------------------------------
# Predicate 4 -- content type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content_type", ["text/html", "text/html; charset=utf-8", "application/json", None, ""]
)
def test_rejects_wrong_content_type(content_type):
    assert check_content_type(content_type) == "wrong_content_type"


@pytest.mark.parametrize("content_type", ["image/jpeg", "image/png", "image/webp; charset=binary"])
def test_accepts_image_content_types(content_type):
    assert check_content_type(content_type) is None


@respx.mock
async def test_rejects_html_content_type(vcfg):
    """`text/html` on an image URL means a block page is being served."""
    respx.head(URL).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html", "content-length": "50000"}
        )
    )
    result = await validate(vcfg)
    assert result.reason == "wrong_content_type"
    assert result.predicate == 4


# ---------------------------------------------------------------------------
# Predicate 5 -- size floor
# ---------------------------------------------------------------------------


def test_rejects_tracking_pixel(vcfg):
    assert check_size_floor(43, vcfg.min_bytes) == "too_small"


def test_accepts_real_image_size(vcfg):
    assert check_size_floor(250_000, vcfg.min_bytes) is None


def test_missing_content_length_defers_rather_than_rejecting(vcfg):
    """Chunked responses omit it; rejecting them would throw away good images."""
    assert check_size_floor(None, vcfg.min_bytes) is None


@respx.mock
async def test_tracking_pixel_is_rejected_end_to_end(vcfg):
    respx.head(URL).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "image/gif", "content-length": "43"}
        )
    )
    result = await validate(vcfg)
    assert result.reason == "too_small"
    assert result.predicate == 5


@respx.mock
async def test_small_file_without_content_length_is_caught_at_predicate_6(vcfg):
    tiny = jpeg_bytes(10, 10)
    respx.head(URL).mock(return_value=httpx.Response(200, headers={"content-type": "image/jpeg"}))
    respx.get(URL).mock(
        return_value=httpx.Response(200, content=tiny, headers={"content-type": "image/jpeg"})
    )
    result = await validate(vcfg)
    assert result.reason == "too_small"
    assert result.predicate == 5


# ---------------------------------------------------------------------------
# Predicate 6 -- decodable and big enough
# ---------------------------------------------------------------------------


def test_sniff_format_recognises_real_headers():
    assert sniff_format(jpeg_bytes()[:16]) == "jpeg"
    assert sniff_format(b"\x89PNG\r\n\x1a\n" + b"0" * 8) == "png"
    assert sniff_format(b"RIFF\x00\x00\x00\x00WEBP") == "webp"
    assert sniff_format(b"<!DOCTYPE html>") is None


@respx.mock
async def test_rejects_undecodable_bytes(vcfg):
    """An HTML error page wearing an image content-type."""
    body = b"<!DOCTYPE html><html><body>Forbidden</body></html>" * 500
    respx.head(URL).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "image/jpeg", "content-length": str(len(body))}
        )
    )
    respx.get(URL).mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})
    )
    result = await validate(vcfg)
    assert result.reason == "undecodable"
    assert result.predicate == 6


@respx.mock
async def test_accepts_webp_which_cannot_decode_from_a_partial_file(vcfg):
    """Pillow will not open a truncated WebP, so a header-sized probe always
    fails on one. Without the larger fallback read, every WebP on the web would
    fail Tier 1 -- and haat's own storage serves WebP."""
    buffer = io.BytesIO()
    Image.merge("RGB", [Image.effect_noise((1400, 1400), 120).convert("L")] * 3).save(
        buffer, format="WEBP", quality=90
    )
    body = buffer.getvalue()
    assert len(body) > vcfg.header_probe_bytes, "the probe must not cover the whole file"

    respx.head(URL).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "image/webp", "content-length": str(len(body))}
        )
    )
    respx.get(URL).mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "image/webp"})
    )

    result = await validate(vcfg)
    assert result.ok, f"webp rejected: {result.reason}"
    assert (result.width, result.height) == (1400, 1400)


@respx.mock
async def test_html_wearing_an_image_content_type_still_fails_fast(vcfg):
    """The larger fallback read must not turn a block page into a slow accept."""
    body = b"<!DOCTYPE html><html><body>Forbidden</body></html>" * 5000
    respx.head(URL).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "image/jpeg", "content-length": str(len(body))}
        )
    )
    respx.get(URL).mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})
    )
    result = await validate(vcfg)
    assert result.reason == "undecodable"


@respx.mock
async def test_rejects_below_min_dimensions(vcfg):
    """haat is a premium marketplace; a 400x400 thumbnail is not listable."""
    body = jpeg_bytes(400, 400)
    mock_image(body=body)
    result = await validate(vcfg)
    assert result.reason == "below_min_dimensions"
    assert result.predicate == 6
    assert (result.width, result.height) == (400, 400)


@respx.mock
async def test_accepts_a_large_clean_image(vcfg):
    mock_image(body=jpeg_bytes(1600, 1600))
    result = await validate(vcfg)
    assert result.ok
    assert result.reason == "direct_ok"
    assert (result.width, result.height) == (1600, 1600)


@respx.mock
async def test_exactly_at_the_minimum_is_accepted(vcfg):
    mock_image(body=jpeg_bytes(800, 800))
    assert (await validate(vcfg)).ok


# ---------------------------------------------------------------------------
# Predicate 7 -- hotlink test
# ---------------------------------------------------------------------------


@respx.mock
async def test_rejects_hotlink_blocked(vcfg, ledger):
    """200 with the product page as Referer, 403 for a stranger. Common on CDNs,
    and fatal for a live listing."""
    body = jpeg_bytes()
    respx.head(URL).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "image/jpeg", "content-length": str(len(body))}
        )
    )
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # The stranger client is the second GET: fresh session, neutral UA.
        if calls["n"] > 1:
            return httpx.Response(403)
        return httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})

    respx.get(URL).mock(side_effect=responder)

    result = await validate(vcfg, URL, ledger)
    assert result.reason == "hotlink_blocked"
    assert result.predicate == 7
    # The failure is evidence about the host, so it feeds predicate 9.
    assert ledger.bad_hosts() == [("cdn.example", 1)]


@respx.mock
async def test_hotlink_test_can_be_skipped(vcfg):
    body = jpeg_bytes()
    respx.head(URL).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "image/jpeg", "content-length": str(len(body))}
        )
    )
    route = respx.get(URL).mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})
    )
    result = await validate(vcfg, URL, hotlink=False)
    assert result.ok
    assert route.call_count == 1  # only the decode read, no stranger request


# ---------------------------------------------------------------------------
# Ordering and short-circuiting
# ---------------------------------------------------------------------------


@respx.mock
async def test_predicates_short_circuit_on_first_failure(vcfg):
    """Predicate 6 must never run when predicate 4 already failed."""
    respx.head(URL).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html", "content-length": "99999"}
        )
    )
    body_route = respx.get(URL)
    result = await validate(vcfg)
    assert result.reason == "wrong_content_type"
    assert not body_route.called


@respx.mock
async def test_first_passing_candidate_wins_and_the_rest_are_untried(vcfg):
    """The whole point of Tier 1: a healthy page costs one probe."""
    good = "https://cdn.example/good.jpg"
    mock_image(good)
    later = respx.head("https://cdn.example/later.jpg")

    async with httpx.AsyncClient() as client:
        validator = Tier1Validator(client, vcfg, None)
        winner, results = await validate_all_candidates(
            [good, "https://cdn.example/later.jpg"], validator
        )

    assert winner is not None and winner.url == good
    assert len(results) == 1
    assert not later.called


@respx.mock
async def test_falls_through_to_the_next_candidate(vcfg):
    first = "https://cdn.example/first.jpg"
    second = "https://cdn.example/second.jpg"
    respx.head(first).mock(return_value=httpx.Response(404))
    mock_image(second)

    async with httpx.AsyncClient() as client:
        validator = Tier1Validator(client, vcfg, None)
        winner, results = await validate_all_candidates([first, second], validator)

    assert winner is not None and winner.url == second
    assert [r.reason for r in results] == ["http_404", "direct_ok"]


@respx.mock
async def test_all_candidates_failing_returns_no_winner(vcfg):
    urls = ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"]
    for url in urls:
        respx.head(url).mock(return_value=httpx.Response(403))
        respx.get(url).mock(return_value=httpx.Response(403))

    async with httpx.AsyncClient() as client:
        validator = Tier1Validator(client, vcfg, None)
        winner, results = await validate_all_candidates(urls, validator)

    assert winner is None
    assert len(results) == 2
    assert all(not r.ok for r in results)


# --------------------------------------------------------------------------
# Two real CDNs that this floor used to reject entirely
# --------------------------------------------------------------------------


def test_an_impossible_content_length_is_not_believed(vcfg) -> None:
    """Flipkart answers HEAD with `Content-Length: 20` and `image/webp` for a
    1500x1500 photograph -- a stub for HEAD, the real file for GET.

    Twenty bytes cannot encode an image in any format. Believing the header
    rejected every photograph on the site as `too_small`, so below the smallest
    possible encoding the header stops being evidence and predicate 6 reads
    actual bytes instead.
    """
    assert check_size_floor(20, vcfg.min_bytes) is None
    assert check_size_floor(0, vcfg.min_bytes) is None


def test_a_real_pixel_is_still_rejected_for_free(vcfg) -> None:
    """The line sits at the true minimum so this keeps costing zero requests.

    43 bytes is a genuine 1x1 GIF, and it must die at predicate 5 rather than
    earning a ranged GET -- a page with ten pixels would otherwise cost ten.
    """
    assert check_size_floor(43, vcfg.min_bytes) == "too_small"
    assert check_size_floor(26, vcfg.min_bytes) == "too_small"
