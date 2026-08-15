"""GET /api/diagnose -- the route, not the report.

The report has its own file. What is checked here is what the route adds: a
refused fetch is still a 200 with the reason in it, a junk URL is a message
rather than a 500, and the SSRF guard applies to a URL typed into a browser
exactly as it applies to one in a file.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import respx
from fastapi.testclient import TestClient

from haat_lister.api.app import create_app
from haat_lister.config import Settings


def _client(settings: Settings, tmp_path: Path) -> TestClient:
    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    tuned.config.render.enabled = False
    tuned.config.fetch.respect_robots = False
    return TestClient(create_app(tuned))


@respx.mock
def test_a_page_with_no_images_answers_with_the_reason(settings: Settings, tmp_path: Path) -> None:
    respx.get("https://shop.example/p/1").mock(
        return_value=httpx.Response(
            200,
            html="<html><body><h1>Kurta</h1></body></html>",
            headers={"content-type": "text/html"},
        )
    )

    response = _client(settings, tmp_path).get(
        "/api/diagnose", params={"url": "https://shop.example/p/1"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["images"]["reason"] == "no_image_candidates"
    assert body["images"]["explanation"]
    assert body["title"]["value"] == "Kurta"
    # Every rule is reported, including the ones that found nothing -- that is
    # the whole difference between this and "image: none".
    assert len(body["images"]["rules"]) == 7


@respx.mock
def test_a_refused_fetch_is_a_200_carrying_the_refusal(settings: Settings, tmp_path: Path) -> None:
    """A 500 would put the answer in a server log instead of in front of the
    person who asked the question."""
    respx.get("https://shop.example/p/1").mock(return_value=httpx.Response(403))

    response = _client(settings, tmp_path).get(
        "/api/diagnose", params={"url": "https://shop.example/p/1"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["fetch"]["ok"] is False
    # The transport word here, the shared enum below. Both, on purpose:
    # `http_403` is what the wire said and `blocked_403` is what it means, and
    # a report that shows only one of them is missing half the answer.
    assert body["fetch"]["error_reason"] == "http_403"
    # The transport cause, not the category. v4 §11: no result anywhere says
    # `page_fetch_failed` -- the enum still groups these rows for the UI, but
    # what a person reads is what actually happened on the wire.
    assert body["images"]["reason"] == "blocked_403"


def test_a_private_address_is_refused_by_the_same_guard(settings: Settings, tmp_path: Path) -> None:
    """§10.2. A URL typed into a browser is not more trustworthy than one in a
    file, and this route is a fetcher an anonymous caller aims."""
    response = _client(settings, tmp_path).get(
        "/api/diagnose", params={"url": "http://169.254.169.254/latest/meta-data/"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["fetch"]["error_reason"] == "blocked_address"
    assert "metadata" in body["fetch"]["error_detail"]


def test_junk_is_a_message_not_a_stack_trace(settings: Settings, tmp_path: Path) -> None:
    client = _client(settings, tmp_path)
    for bad in ("", "   ", "shop.example/p/1", "file:///etc/passwd", "javascript:alert(1)"):
        response = client.get("/api/diagnose", params={"url": bad})
        assert response.status_code == 400, bad
        assert "http" in response.json()["detail"]


def test_an_overlong_url_is_rejected_by_the_schema(settings: Settings, tmp_path: Path) -> None:
    from haat_lister.api.routes.diagnose import MAX_URL_CHARS

    response = _client(settings, tmp_path).get(
        "/api/diagnose", params={"url": "https://shop.example/" + "x" * MAX_URL_CHARS}
    )
    assert response.status_code == 422


def test_the_route_never_returns_a_secret(settings: Settings, tmp_path: Path) -> None:
    """§10.5, held at every route rather than at most of them."""
    from pydantic import SecretStr

    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    tuned.config.render.enabled = False
    tuned.config.fetch.respect_robots = False
    tuned.secrets.anthropic_api_key = SecretStr("sk-ant-CANARY")
    tuned.secrets.imgbb_api_key = SecretStr("imgbb-CANARY")

    with respx.mock:
        respx.get("https://shop.example/p/1").mock(return_value=httpx.Response(404))
        response = TestClient(create_app(tuned)).get(
            "/api/diagnose", params={"url": "https://shop.example/p/1"}
        )

    assert "CANARY" not in response.text
