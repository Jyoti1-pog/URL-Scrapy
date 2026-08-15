"""Section 10, swept in one place.

The individual guards are tested where they live -- the SSRF hook in
test_netguard, the token and traversal in test_api_config, the upload caps in
test_api_jobs. This file exists so that "is §10 actually satisfied?" has one
answer rather than six, and so a guard that gets quietly removed fails a test
whose name says which requirement it was.

Each test names its clause.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from haat_lister.api.app import DEV_ORIGIN, create_app, new_token
from haat_lister.config import Settings
from haat_lister.edits import LOCKED
from haat_lister.jobs import is_job_id

REPO = Path(__file__).resolve().parents[1]

CANARIES = {
    "cloudinary_api_key": "cloud-key-CANARY",
    "cloudinary_api_secret": "cloud-secret-CANARY",
    "cloudinary_url": "cloudinary://a:b@c-CANARY",
    "imgbb_api_key": "imgbb-CANARY",
    "imgur_client_id": "imgur-CANARY",
    "anthropic_api_key": "sk-ant-CANARY",
}


@pytest.fixture
def loaded(settings: Settings, tmp_path: Path) -> Settings:
    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    tuned.secrets.cloudinary_cloud_name = "demo-cloud"
    for field, value in CANARIES.items():
        setattr(tuned.secrets, field, SecretStr(value))
    return tuned


@pytest.fixture
def client(loaded: Settings) -> TestClient:
    return TestClient(create_app(loaded))


# --------------------------------------------------------------------------
# 10.1  Bind loopback. Off it needs a token. No CORS wildcard.
# --------------------------------------------------------------------------


def test_10_1_serve_refuses_a_public_bind_without_a_token() -> None:
    """The check is in the command, so the test reads the command.

    An integration test would have to bind a socket to 0.0.0.0 on the machine
    running the suite, which is precisely the thing this clause exists to stop.
    """
    from haat_lister.cli import serve

    source = inspect.getsource(serve)
    assert "not loopback and not token" in source
    assert "typer.Exit(code=2)" in source
    assert "--token" in source


def test_10_1_no_cors_wildcard_and_dev_is_one_origin(loaded: Settings) -> None:
    plain = TestClient(create_app(loaded))
    response = plain.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers

    dev = TestClient(create_app(loaded, dev=True))
    assert (
        dev.get("/api/health", headers={"Origin": DEV_ORIGIN}).headers[
            "access-control-allow-origin"
        ]
        == DEV_ORIGIN
    )
    assert (
        dev.get("/api/health", headers={"Origin": "https://evil.example"}).headers.get(
            "access-control-allow-origin"
        )
        != "https://evil.example"
    )


def test_10_1_a_token_gates_the_api_but_not_the_page(loaded: Settings) -> None:
    token = new_token()
    guarded = TestClient(create_app(loaded, token=token))

    assert guarded.get("/api/health").status_code == 401
    with_header = guarded.get("/api/health", headers={"Authorization": f"Bearer {token}"})
    assert with_header.status_code == 200
    # The query form exists because EventSource cannot set a header.
    assert guarded.get(f"/api/health?token={token}").status_code == 200
    # The page has to load, or there is nowhere to supply the token from.
    assert guarded.get("/").status_code in (200, 503)


# --------------------------------------------------------------------------
# 10.2  SSRF, re-checked after every redirect hop.
# --------------------------------------------------------------------------


async def test_10_2_private_ranges_and_metadata_are_refused() -> None:
    from haat_lister.utils.netguard import BlockedHost, check_url

    for url in (
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1/admin",
        "http://10.0.0.1/",
        "http://192.168.0.1/",
        "http://[::1]/",
        "file:///etc/passwd",
    ):
        with pytest.raises(BlockedHost):
            await check_url(url)


def test_10_2_the_guard_is_on_every_client_this_tool_builds() -> None:
    """Two clients exist: the fetcher's, and predicate 7's deliberately fresh
    session. Both carry the hook.

    The second one was missed at first: its whole point is a new session with no
    cookies, and it is fed URLs out of a shop's own HTML. The earlier predicates
    would usually block a private address first -- "usually" is not a security
    property.
    """
    for module in ("haat_lister/fetch/static.py", "haat_lister/images/validator.py"):
        source = (REPO / module).read_text(encoding="utf-8")
        assert "httpx.AsyncClient(" in source
        assert "request_hook(" in source, f"{module} builds a client with no SSRF hook"


def test_10_2_the_allowlist_is_config_only() -> None:
    """It can never arrive in a request body."""
    from haat_lister.api import schemas

    body = (REPO / "haat_lister/api/schemas.py").read_text(encoding="utf-8")
    assert "allow_private_hosts" not in body.split("class ConfigOut")[0], (
        "an input schema mentions the SSRF allowlist"
    )
    assert "allow_private_hosts" in schemas.ConfigOut.model_fields  # read-only, in the response


# --------------------------------------------------------------------------
# 10.3  Path traversal.
# --------------------------------------------------------------------------


def test_10_3_job_ids_are_a_fixed_shape() -> None:
    for bad in ("../../etc", "j_UPPER123", "j_short", "j_" + "a" * 9, "", "j_abc-1234", "."):
        assert not is_job_id(bad)


def test_10_3_artifacts_are_an_allowlist_not_a_filename(client: TestClient) -> None:
    from haat_lister.api.routes.downloads import ARTIFACTS

    # Exact, not a superset. An allowlist that grows without anyone noticing is
    # not an allowlist, so adding an artifact has to be a deliberate edit here
    # as well as there.
    assert set(ARTIFACTS) == {
        "listings",
        "listings_with_images",
        "review",
        "manifest",
        "failed",
        "zip",
    }
    for attempt in ("../../.env", "config.yaml", "job.json", "images", "%2e%2e%2f.env"):
        assert attempt not in ARTIFACTS


def test_10_3_no_route_serves_a_file_outside_its_directory(
    loaded: Settings, tmp_path: Path
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>console</h1>", encoding="utf-8")
    (tmp_path / "secret.env").write_text("ANTHROPIC_API_KEY=sk-ant-LEAK", encoding="utf-8")

    guarded = TestClient(create_app(loaded, dist=dist))
    for attempt in ("../secret.env", "..%2Fsecret.env", "a/../../secret.env", "%2e%2e/secret.env"):
        assert "sk-ant-LEAK" not in guarded.get(f"/{attempt}").text


# --------------------------------------------------------------------------
# 10.4  Upload caps, as a message rather than a 500.
# --------------------------------------------------------------------------


def test_10_4_caps_are_rejected_with_a_message(client: TestClient) -> None:
    from haat_lister.api.schemas import MAX_BYTES, MAX_URLS

    too_many = client.post(
        "/api/jobs",
        json={
            "urls": [f"https://s{i}.example/p" for i in range(MAX_URLS + 1)],
            "settings": {"provenance": "own"},
        },
    )
    assert too_many.status_code == 413
    assert "line limit" in too_many.text

    too_big = client.post(
        "/api/jobs",
        json={
            "urls": ["https://s.example/" + "x" * 4000 for _ in range(MAX_BYTES // 4000 + 2)],
            "settings": {"provenance": "own"},
        },
    )
    assert too_big.status_code == 413
    assert "KB limit" in too_big.text


# --------------------------------------------------------------------------
# 10.5  Never return secrets.
# --------------------------------------------------------------------------


def test_10_5_no_route_returns_a_credential(client: TestClient) -> None:
    """Against the raw text of every route this build serves. A field-by-field
    check would pass a response leaking through a model added later."""
    job = client.post(
        "/api/jobs", json={"urls": ["https://s.example/p/1"], "settings": {"provenance": "own"}}
    )
    job_id = job.json()["job_id"]

    paths = [
        "/api/health",
        "/api/config",
        "/api/openapi.json",
        "/api/jobs",
        f"/api/jobs/{job_id}",
        f"/api/jobs/{job_id}/rows",
    ]
    for path in paths:
        body = client.get(path).text
        assert "CANARY" not in body, f"{path} leaked a credential"


def test_10_5_secrets_do_not_leak_through_a_repr(loaded: Settings) -> None:
    """A traceback is a response surface too."""
    text = repr(loaded.secrets) + str(loaded.secrets.model_dump())
    assert "CANARY" not in text
    assert "**********" in text


def test_10_5_the_401_echoes_neither_token(loaded: Settings) -> None:
    guarded = TestClient(create_app(loaded, token="the-real-token"))
    response = guarded.get("/api/health?token=a-guess")
    assert "the-real-token" not in response.text
    assert "a-guess" not in response.text


# --------------------------------------------------------------------------
# 10.6  One job at a time.
# --------------------------------------------------------------------------


def test_10_6_the_runner_has_one_worker() -> None:
    """Three concurrent jobs over one shop would triple the load it never agreed
    to, because the rate limiter is per-domain and lives inside a run."""
    from haat_lister.api.runner import JobRunner

    source = inspect.getsource(JobRunner)
    assert source.count("asyncio.create_task(self._drain()") == 1
    assert "if self._worker is None or self._worker.done()" in source


# --------------------------------------------------------------------------
# Invariants that outrank all of the above
# --------------------------------------------------------------------------


def test_gi_region_cannot_be_written_by_any_path() -> None:
    """Three independent barriers, so removing one does not open it."""
    from haat_lister.models import ProductRecord
    from haat_lister.output.csv_writer import GI_REGION_ALWAYS_BLANK, HAAT_COLUMNS

    assert "gi_region" not in ProductRecord.model_fields  # nothing can hold it
    assert "gi_region" in HAAT_COLUMNS  # it is still a column
    assert GI_REGION_ALWAYS_BLANK == ""  # and the writer emits a constant
    assert "gi_region" in LOCKED  # and the API refuses to set it


def test_the_console_reaches_no_third_party_at_runtime() -> None:
    """Section 14: works with the wifi off, except the scraping itself."""
    dist = REPO / "web" / "dist"
    assert (dist / "index.html").exists(), "web/dist is meant to be committed"

    for path in [*dist.glob("*.html"), *dist.glob("assets/*.css"), *dist.glob("assets/*.js")]:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for offender in ("fonts.googleapis.com", "fonts.gstatic.com", "cdn.jsdelivr", "unpkg.com"):
            assert offender not in text, f"{path.name} reaches out to {offender}"

    assert len(list((dist / "fonts").glob("*.woff2"))) == 3, "fonts are meant to be self-hosted"


def test_gitignore_does_not_exclude_source_packages() -> None:
    """Caught by a fresh-clone check: `store/` unanchored matches at any depth,
    so it also excluded haat_lister/store/ and haat_lister/images/.

    A dev checkout never notices -- the files are already on disk -- and the
    first person to clone gets `No module named haat_lister.store`.
    """
    patterns = [
        line.strip()
        for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith(("#", "!"))
    ]
    package_dirs = {
        p.name for p in (REPO / "haat_lister").iterdir() if p.is_dir() and p.name != "__pycache__"
    }

    for pattern in patterns:
        bare = pattern.rstrip("/")
        if pattern.startswith("/") or "*" in pattern:
            continue  # anchored, or a glob that cannot match a bare directory name
        assert bare not in package_dirs, (
            f".gitignore excludes {bare!r} at any depth, which also excludes "
            f"haat_lister/{bare}/. Anchor it: /{bare}/"
        )
