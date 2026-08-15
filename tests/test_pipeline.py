"""Phase 2: end-to-end per-URL orchestration, with all HTTP mocked.

Nothing in the suite touches the network.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from haat_lister.fetch.static import build_client
from haat_lister.models import FetchStage, Provenance, RowStatus
from haat_lister.pipeline import process_url
from haat_lister.utils.robots import RobotsCache

PRODUCT_URL = "https://shop.example/products/mirror-kurta"

PRODUCT_HTML = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product",
 "name":"Hand-embroidered cotton kurta with mirror work",
 "description":"Hand-embroidered in Kutch on handloom cotton, with a mirror-work yoke.",
 "image":["https://cdn.shop.example/kurta_1600x1600.jpg"]}
</script>
<meta property="og:image" content="https://cdn.shop.example/og-kurta.jpg">
</head><body>
<h1>Hand-embroidered cotton kurta</h1>
<img src="/img/gallery-2.jpg">
</body></html>
"""


def html_response(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, text=body, headers={"content-type": "text/html; charset=utf-8"})


async def run(settings, url: str = PRODUCT_URL, robots: bool = False):
    async with build_client(settings) as client:
        cache = RobotsCache(client, settings.user_agent) if robots else None
        return await process_url(url, Provenance.OWN, settings, client, cache)


@respx.mock
async def test_happy_path_builds_a_full_record(settings):
    respx.get(PRODUCT_URL).mock(return_value=html_response(PRODUCT_HTML))

    record = await run(settings)

    assert record.status is RowStatus.OK
    assert record.fetch_stage is FetchStage.STATIC
    assert record.title.value == "Hand-embroidered cotton kurta with mirror work"
    assert "Kutch" in record.description.value
    assert "json-ld" in record.structured_syntaxes
    assert record.image_candidates
    assert record.row_key.startswith("shop-example-products-mirror-kurta-")
    assert record.canonical_url == PRODUCT_URL
    assert record.fetched_at is not None


@respx.mock
async def test_record_serialises_to_json_with_confidence(settings):
    respx.get(PRODUCT_URL).mock(return_value=html_response(PRODUCT_HTML))
    record = await run(settings)

    payload = record.model_dump(mode="json")
    assert payload["title"]["confidence"] == "high"
    assert payload["title"]["source"] == "jsonld"
    assert "gi_region" not in payload


@respx.mock
async def test_no_image_column_no_image_work_yet(settings):
    """Phase 2 collects candidates and stops. Nothing is fetched or validated."""
    respx.get(PRODUCT_URL).mock(return_value=html_response(PRODUCT_HTML))
    image_route = respx.get(url__startswith="https://cdn.shop.example")

    record = await run(settings)

    assert not image_route.called
    assert record.image.tier1_attempted is True
    assert record.image.tier1_passed is False


@respx.mock
async def test_http_error_fails_the_row_with_a_named_reason(settings):
    respx.get(PRODUCT_URL).mock(return_value=httpx.Response(404))
    record = await run(settings)
    assert record.status is RowStatus.FAILED
    assert record.failure_reason == "not_a_product_page"
    assert record.fetch_stage is FetchStage.FAILED


@respx.mock
async def test_non_html_response_fails_the_row(settings):
    respx.get(PRODUCT_URL).mock(
        return_value=httpx.Response(200, json={"nope": 1}),
    )
    record = await run(settings)
    assert record.status is RowStatus.FAILED
    assert record.failure_reason == "not_a_product_page"


@respx.mock
async def test_timeout_fails_the_row_rather_than_retrying_forever(settings):
    respx.get(PRODUCT_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    record = await run(settings)
    assert record.status is RowStatus.FAILED
    # `timeout`, split by v4 §2.5. Which end timed out matters: a connect
    # timeout is a host that is not answering, a read timeout is one that
    # accepted the connection and then went quiet -- which is what a bot wall
    # black-holing a request looks like.
    assert record.failure_reason == "timeout_connect"


@respx.mock
async def test_page_without_a_title_is_a_row_failure(settings):
    respx.get(PRODUCT_URL).mock(return_value=html_response("<html><body><p>hi</p></body></html>"))
    record = await run(settings)
    assert record.status is RowStatus.FAILED
    assert record.failure_reason == "no_title"


@respx.mock
async def test_thin_page_is_flagged_not_silently_accepted(settings):
    respx.get(PRODUCT_URL).mock(
        return_value=html_response("<html><body><h1>A Kurta</h1></body></html>")
    )
    record = await run(settings)
    assert record.status is RowStatus.NEEDS_REVIEW
    assert any("description" in n.lower() for n in record.notes)
    assert any("image candidates" in n.lower() for n in record.notes)


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------


@respx.mock
async def test_robots_disallow_stops_us_before_the_page_is_fetched(settings):
    respx.get("https://shop.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /products/")
    )
    page = respx.get(PRODUCT_URL).mock(return_value=html_response(PRODUCT_HTML))

    record = await run(settings, robots=True)

    assert record.status is RowStatus.FAILED
    assert record.failure_reason == "robots_disallowed"
    assert not page.called


@respx.mock
async def test_robots_allow_lets_the_fetch_proceed(settings):
    respx.get("https://shop.example/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\nDisallow: /admin")
    )
    respx.get(PRODUCT_URL).mock(return_value=html_response(PRODUCT_HTML))

    record = await run(settings, robots=True)
    assert record.status is RowStatus.OK


@respx.mock
async def test_missing_robots_txt_means_allowed(settings):
    respx.get("https://shop.example/robots.txt").mock(return_value=httpx.Response(404))
    respx.get(PRODUCT_URL).mock(return_value=html_response(PRODUCT_HTML))

    record = await run(settings, robots=True)
    assert record.status is RowStatus.OK


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
async def test_robots_txt_behind_auth_means_disallowed(settings, status):
    """Refused sight of the rules is not the same as being granted permission."""
    respx.get("https://shop.example/robots.txt").mock(return_value=httpx.Response(status))
    page = respx.get(PRODUCT_URL).mock(return_value=html_response(PRODUCT_HTML))

    record = await run(settings, robots=True)
    assert record.status is RowStatus.FAILED
    assert not page.called
