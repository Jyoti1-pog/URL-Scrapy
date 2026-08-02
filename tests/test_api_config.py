"""v2 Phase 2: the API skeleton, and the two things it must not do.

This process holds image-host keys and an Anthropic key, and it fetches whatever
URL it is handed from inside the operator's network. So the tests that matter
here are not "does the route return 200" -- they are that a key never reaches
the wire, that a path cannot walk out of the static directory, and that binding
off loopback cannot happen without a token.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from haat_lister.api.app import create_app, new_token
from haat_lister.api.schemas import LOCKED_FIELDS
from haat_lister.config import Settings

SECRETS = {
    "cloudinary_api_key": "cloud-key-SHOULD-NEVER-APPEAR",
    "cloudinary_api_secret": "cloud-secret-SHOULD-NEVER-APPEAR",
    "cloudinary_url": "cloudinary://a:b@c-SHOULD-NEVER-APPEAR",
    "imgbb_api_key": "imgbb-key-SHOULD-NEVER-APPEAR",
    "imgur_client_id": "imgur-id-SHOULD-NEVER-APPEAR",
    "anthropic_api_key": "sk-ant-SHOULD-NEVER-APPEAR",
}


@pytest.fixture
def loaded(settings: Settings) -> Settings:
    """Settings with every credential populated, so a leak has something to find."""
    tuned = settings.model_copy(deep=True)
    tuned.secrets.cloudinary_cloud_name = "demo-cloud"
    for field, value in SECRETS.items():
        setattr(tuned.secrets, field, SecretStr(value))
    return tuned


@pytest.fixture
def client(loaded: Settings) -> TestClient:
    return TestClient(create_app(loaded))


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------


def test_config_endpoint_never_returns_keys(client: TestClient) -> None:
    """Asserted against the raw response text, not the parsed shape.

    A field-by-field check would pass a response that leaked a key through a
    nested model somebody added later; this fails on the substring wherever it
    appears.
    """
    body = client.get("/api/config").text
    for value in SECRETS.values():
        assert value not in body
    assert "SHOULD-NEVER-APPEAR" not in body


def test_no_endpoint_returns_keys(client: TestClient) -> None:
    """Every route this build serves, not just the one named in the brief."""
    for path in ("/api/config", "/api/health", "/api/openapi.json"):
        body = client.get(path).text
        assert "SHOULD-NEVER-APPEAR" not in body, f"{path} leaked a credential"


def test_config_reports_whether_a_host_is_usable_not_what_it_holds(
    client: TestClient,
) -> None:
    data = client.get("/api/config").json()
    hosts = {h["name"]: h["configured"] for h in data["image_hosts"]}

    assert hosts == {"cloudinary": True, "imgbb": True, "imgur": True}
    assert set(data["image_hosts"][0]) == {"name", "configured"}, "a host grew a field"


def test_an_unconfigured_host_says_so_rather_than_disappearing(settings: Settings) -> None:
    """A missing host must be visible as missing. Omitting it would look like a
    build that does not support it."""
    client = TestClient(create_app(settings))
    data = client.get("/api/config").json()

    assert [h["name"] for h in data["image_hosts"]] == ["cloudinary", "imgbb", "imgur"]
    assert not any(h["configured"] for h in data["image_hosts"])
    assert data["llm"]["configured"] is False


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


def test_health_is_config_check_as_json(client: TestClient) -> None:
    from haat_lister.config import collect_findings

    data = client.get("/api/health").json()
    expected = collect_findings(client.app.state.settings)

    assert len(data["findings"]) == len(expected)
    assert data["blocking"] == sum(1 for f in expected if f.level == "fail")
    assert data["ok"] is (data["blocking"] == 0)
    assert data["user_agent"].startswith("haat-lister/")


def test_health_surfaces_an_incomplete_taxonomy(
    settings: Settings, incomplete_taxonomy
) -> None:
    """The one setting that silently produces a bad CSV rather than an error, so
    the console has to say it before the first job."""
    tuned = settings.model_copy(deep=True)
    tuned.taxonomy = incomplete_taxonomy
    client = TestClient(create_app(tuned))

    data = client.get("/api/health").json()
    assert data["ok"] is False
    assert data["blocking"] >= 1
    assert any("taxonomy" in f["title"].lower() for f in data["findings"])


def test_config_marks_derived_slugs(client: TestClient) -> None:
    """The UI needs to warn about slugs inferred from haat's convention rather
    than read from haat."""
    data = client.get("/api/config").json()
    derived = [
        s for c in data["categories"] for s in c["subcategories"] if s["derived"]
    ]
    assert derived, "the shipped taxonomy has derived slugs; the API is hiding them"


def test_gi_region_is_advertised_as_locked(client: TestClient) -> None:
    """The UI reads this rather than hardcoding a rule it might later disagree
    with."""
    assert client.get("/api/config").json()["locked_fields"] == list(LOCKED_FIELDS)
    assert "gi_region" in LOCKED_FIELDS


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def test_no_token_means_no_auth_on_loopback(client: TestClient) -> None:
    assert client.get("/api/health").status_code == 200


def test_a_token_gates_every_api_route(loaded: Settings) -> None:
    token = new_token()
    client = TestClient(create_app(loaded, token=token))

    assert client.get("/api/health").status_code == 401
    assert client.get("/api/config").status_code == 401

    authed = client.get("/api/health", headers={"Authorization": f"Bearer {token}"})
    assert authed.status_code == 200
    # The query form exists because EventSource cannot set a header.
    assert client.get(f"/api/config?token={token}").status_code == 200


def test_a_wrong_token_is_refused(loaded: Settings) -> None:
    client = TestClient(create_app(loaded, token=new_token()))
    assert client.get("/api/health?token=" + new_token()).status_code == 401
    assert client.get("/api/health?token=").status_code == 401


def test_the_401_does_not_echo_the_token(loaded: Settings) -> None:
    client = TestClient(create_app(loaded, token="the-real-token"))
    response = client.get("/api/health?token=guess")
    assert "the-real-token" not in response.text
    assert "guess" not in response.text


def test_static_pages_are_not_behind_the_token(loaded: Settings) -> None:
    """Otherwise the browser cannot load the page that would let you supply it."""
    client = TestClient(create_app(loaded, token=new_token()))
    assert client.get("/").status_code in (200, 503)


# --------------------------------------------------------------------------
# Static serving
# --------------------------------------------------------------------------


def test_download_path_traversal_rejected(loaded: Settings, tmp_path: Path) -> None:
    """The static route takes a client-supplied path; it must not be able to
    leave the directory."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>console</h1>", encoding="utf-8")
    (tmp_path / "secret.env").write_text("ANTHROPIC_API_KEY=sk-ant-leak", encoding="utf-8")

    client = TestClient(create_app(loaded, dist=dist))

    for attempt in ("../secret.env", "..%2Fsecret.env", "a/../../secret.env"):
        response = client.get(f"/{attempt}")
        assert "sk-ant-leak" not in response.text, f"{attempt} escaped the dist directory"


def test_an_unknown_path_falls_back_to_the_console(loaded: Settings, tmp_path: Path) -> None:
    """So /jobs/j_7fk2m9qa survives a refresh -- the job id belongs in the URL."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>console</h1>", encoding="utf-8")
    client = TestClient(create_app(loaded, dist=dist))

    response = client.get("/jobs/j_7fk2m9qa")
    assert response.status_code == 200
    assert "console" in response.text


def test_a_missing_build_explains_itself(loaded: Settings, tmp_path: Path) -> None:
    """A developer who cloned and did not build should be told which command to
    run, not shown a 404."""
    client = TestClient(create_app(loaded, dist=tmp_path / "nothing-here"))
    response = client.get("/")

    assert response.status_code == 503
    assert "npm run build" in response.text
    assert "/api/health" in response.text


def test_the_shipped_console_is_present_and_self_contained() -> None:
    """It must work with the wifi off: no CDN, no Google Fonts link."""
    index = Path(__file__).resolve().parents[1] / "web" / "dist" / "index.html"
    assert index.exists(), "web/dist/index.html is meant to be committed"

    html = index.read_text(encoding="utf-8")
    for offender in ("https://cdn.", "fonts.googleapis.com", "unpkg.com", "jsdelivr"):
        assert offender not in html, f"the console reaches out to {offender}"


# --------------------------------------------------------------------------
# CORS
# --------------------------------------------------------------------------


def test_there_is_no_cors_wildcard(loaded: Settings) -> None:
    """The console is same-origin, so no CORS header is needed at all."""
    client = TestClient(create_app(loaded))
    response = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert response.headers.get("access-control-allow-origin") != "*"
    assert "access-control-allow-origin" not in response.headers


def test_dev_mode_allows_exactly_one_origin(loaded: Settings) -> None:
    client = TestClient(create_app(loaded, dev=True))

    allowed = client.get("/api/health", headers={"Origin": "http://127.0.0.1:5173"})
    assert allowed.headers.get("access-control-allow-origin") == "http://127.0.0.1:5173"

    denied = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert denied.headers.get("access-control-allow-origin") != "https://evil.example"


# --------------------------------------------------------------------------
# The shape the UI codes against
# --------------------------------------------------------------------------


def test_config_is_stable_enough_to_code_against(client: TestClient) -> None:
    data = client.get("/api/config").json()
    assert set(data) == {
        "version",
        "taxonomy_complete",
        "fallback_category",
        "categories",
        "enums",
        "image_hosts",
        "llm",
        "defaults",
        "locked_fields",
        "allow_private_hosts",
    }
    assert set(data["enums"]) == {
        "provenance",
        "image_mode",
        "price_strategy",
        "description_mode",
        "availability",
    }
    assert "third-party" in data["enums"]["provenance"]
    assert data["defaults"]["image_mode"] == "manifest"
    assert json.dumps(data)  # serialises without a custom encoder
