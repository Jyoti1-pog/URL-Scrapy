"""Phase 8: Tier 2c/2d -- the host chain, failover, re-validation and dedupe.

The two named tests for this phase are `test_hotlink_blocked_falls_through_to_hosting`
and `test_host_chain_failover`. Both assert by counting calls.
"""

from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image

from haat_lister.config import Secrets
from haat_lister.images.hosts import build_hosts
from haat_lister.images.hosts.base import HostedImage, parse_retry_after
from haat_lister.images.hosts.cloudinary import CloudinaryHost
from haat_lister.images.hosts.imgbb import ImgbbHost
from haat_lister.images.hosts.imgur import ImgurHost
from haat_lister.images.pipeline import ImageResolver
from haat_lister.models import (
    FieldSource,
    FieldValue,
    ImageMethod,
    ImageMode,
    ProductRecord,
    Provenance,
)
from haat_lister.store.ledger import Ledger

HERO = "https://cdn.example/hero.jpg"


def jpeg_bytes(width: int = 1200, height: int = 1200) -> bytes:
    buffer = io.BytesIO()
    Image.merge("RGB", [Image.effect_noise((width, height), 120).convert("L")] * 3).save(
        buffer, format="JPEG", quality=95
    )
    return buffer.getvalue()


def mock_valid_image(url: str) -> None:
    body = jpeg_bytes()
    respx.head(url).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "image/jpeg", "content-length": str(len(body))}
        )
    )
    respx.get(url).mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})
    )


def mock_hotlink_protected(url: str) -> None:
    """200 with the product page as Referer, 403 for a stranger."""
    body = jpeg_bytes()

    def responder(request: httpx.Request) -> httpx.Response:
        if request.headers.get("referer"):
            return httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})
        return httpx.Response(403)

    respx.route(url=url).mock(side_effect=responder)


class FakeHost:
    """Counts uploads. `outcome` controls what the host does."""

    def __init__(self, name: str, outcome: str = "ok", url: str | None = None) -> None:
        self.name = name
        self.outcome = outcome
        self.url = url or f"https://{name}.example/hosted.jpg"
        self.uploads: list[Path] = []

    def is_configured(self) -> bool:
        return True

    async def upload(self, path: Path) -> HostedImage | None:
        self.uploads.append(path)
        if self.outcome == "fail":
            return None
        return HostedImage(url=self.url, host_name=self.name, delete_url=f"del-{self.name}")


def make_record(candidates: list[str]) -> ProductRecord:
    return ProductRecord(
        row_key="shop-example-kurta-abc12345",
        source_url="https://shop.example/products/kurta",
        canonical_url="https://shop.example/products/kurta",
        provenance=Provenance.OWN,
        title=FieldValue.found("Kurta", FieldSource.JSONLD),
        image_candidates=candidates,
    )


@pytest.fixture
def settings_in_tmp(settings, tmp_path):
    settings.root = tmp_path
    return settings


@pytest.fixture
def ledger():
    with Ledger(":memory:") as led:
        yield led


async def resolve(settings, hosts, candidates=None, ledger=None, mode=ImageMode.URL_COLUMNS):
    record = make_record(candidates if candidates is not None else [HERO])
    async with httpx.AsyncClient() as client:
        resolver = ImageResolver(settings, client, mode, hosts=hosts, ledger=ledger)
        result = await resolver.resolve(record)
    return record, result, resolver


# ---------------------------------------------------------------------------
# THE NAMED TESTS
# ---------------------------------------------------------------------------


@respx.mock
async def test_hotlink_blocked_falls_through_to_hosting(settings_in_tmp):
    """200 with Referer, 403 without, url_columns mode:
    exactly one download and exactly one upload."""
    mock_hotlink_protected(HERO)
    host = FakeHost("cloudinary")
    mock_valid_image(host.url)

    record, result, resolver = await resolve(settings_in_tmp, [host])

    assert result.tier1_passed is False
    assert "hotlink_blocked" in result.reason or "http_403" in result.reason

    assert resolver.downloads == 1, "exactly one download"
    assert len(host.uploads) == 1, "exactly one upload"

    assert result.method is ImageMethod.HOSTED
    assert result.url == host.url
    assert result.host_used == "cloudinary"
    assert result.upload_used is True
    # The reason must name the Tier-1 predicate that failed.
    assert "hosted_via:cloudinary" in result.reason
    assert "direct_failed:" in result.reason


@respx.mock
async def test_host_chain_failover(settings_in_tmp):
    """Cloudinary 429 (exhausted) -> ImgBB succeeds. Imgur is never touched."""
    mock_hotlink_protected(HERO)
    cloudinary = FakeHost("cloudinary", outcome="fail")
    imgbb = FakeHost("imgbb")
    imgur = FakeHost("imgur")
    mock_valid_image(imgbb.url)

    record, result, resolver = await resolve(settings_in_tmp, [cloudinary, imgbb, imgur])

    assert len(cloudinary.uploads) == 1
    assert len(imgbb.uploads) == 1
    assert imgur.uploads == [], "the chain must stop at the first success"

    assert result.method is ImageMethod.HOSTED
    assert result.host_used == "imgbb"
    # The failed first host stays visible in the reason.
    assert "cloudinary:upload_failed" in result.reason


# ---------------------------------------------------------------------------
# TIER 2d -- re-validation
# ---------------------------------------------------------------------------


@respx.mock
async def test_hosted_url_revalidated(settings_in_tmp):
    """A host that hands back a 404ing URL has not succeeded."""
    mock_hotlink_protected(HERO)
    liar = FakeHost("cloudinary", url="https://cloudinary.example/gone.jpg")
    respx.head(liar.url).mock(return_value=httpx.Response(404))
    respx.get(liar.url).mock(return_value=httpx.Response(404))

    honest = FakeHost("imgbb")
    mock_valid_image(honest.url)

    record, result, resolver = await resolve(settings_in_tmp, [liar, honest])

    assert len(liar.uploads) == 1
    assert result.method is ImageMethod.HOSTED
    assert result.host_used == "imgbb"
    assert "revalidation_http_404" in result.reason


@respx.mock
async def test_hosted_url_must_survive_the_hotlink_test_too(settings_in_tmp):
    """§6.5: do not trust the host's response, hotlink test included."""
    mock_hotlink_protected(HERO)
    host = FakeHost("cloudinary")
    mock_hotlink_protected(host.url)  # serves us, 403s a stranger

    record, result, resolver = await resolve(settings_in_tmp, [host])

    assert result.method is ImageMethod.NONE
    assert "revalidation_" in result.reason


@respx.mock
async def test_all_hosts_failing_never_fabricates_a_url(settings_in_tmp):
    mock_hotlink_protected(HERO)
    hosts = [FakeHost(n, outcome="fail") for n in ("cloudinary", "imgbb", "imgur")]

    record, result, resolver = await resolve(settings_in_tmp, hosts)

    assert result.url == ""
    assert result.method is ImageMethod.NONE
    assert "all_hosts_failed" in result.reason
    assert all(len(h.uploads) == 1 for h in hosts)
    # The files are still kept: bytes we already paid for are not thrown away.
    assert result.files


# ---------------------------------------------------------------------------
# Upload dedupe
# ---------------------------------------------------------------------------


@respx.mock
async def test_identical_bytes_are_uploaded_once(settings_in_tmp, ledger):
    """The same photo under two source URLs should cost one upload."""
    mock_hotlink_protected(HERO)
    host = FakeHost("cloudinary")
    mock_valid_image(host.url)

    _, first, _ = await resolve(settings_in_tmp, [host], ledger=ledger)
    assert len(host.uploads) == 1
    assert first.upload_used is True

    _, second, _ = await resolve(settings_in_tmp, [host], ledger=ledger)
    assert len(host.uploads) == 1, "a second upload of identical bytes is waste"
    assert second.method is ImageMethod.HOSTED
    assert second.url == host.url
    assert second.upload_used is False
    assert "reused_existing_upload" in second.reason


def test_ledger_stores_delete_urls(ledger):
    """Cleanup will be wanted; every delete handle is kept."""
    ledger.record_upload("abc123", "imgbb", "https://i.ibb.co/x.jpg", "https://ibb.co/del/x")
    assert ledger.find_upload("abc123") == (
        "https://i.ibb.co/x.jpg",
        "imgbb",
        "https://ibb.co/del/x",
    )
    assert ledger.find_upload("nothing") is None


# ---------------------------------------------------------------------------
# Host construction
# ---------------------------------------------------------------------------


def test_manifest_mode_builds_no_hosts_at_all(settings):
    """The strongest form of "never contacts a host" is having none to contact."""
    hosts, skipped = build_hosts(settings, httpx.AsyncClient(), ImageMode.MANIFEST)
    assert hosts == []
    assert skipped == []


def test_unconfigured_hosts_are_skipped_at_startup(settings):
    hosts, skipped = build_hosts(settings, httpx.AsyncClient(), ImageMode.URL_COLUMNS)
    assert hosts == []
    assert {s.split(" ")[0] for s in skipped} == {"cloudinary", "imgbb", "imgur"}
    assert all("no credentials" in s for s in skipped)


def test_configured_hosts_appear_in_chain_order(settings):
    settings.secrets = Secrets(
        haat_contact="ops@a-real-domain.in",
        imgbb_api_key="key-1",
        imgur_client_id="client-1",
    )
    hosts, skipped = build_hosts(settings, httpx.AsyncClient(), ImageMode.URL_COLUMNS)
    assert [h.name for h in hosts] == ["imgbb", "imgur"]
    assert skipped == ["cloudinary (no credentials)"]


def test_cloudinary_accepts_either_credential_form(settings):
    client = httpx.AsyncClient()
    cfg = settings.config.hosts

    url_form = CloudinaryHost(
        client, Secrets(cloudinary_url="cloudinary://key:secret@my-cloud"), cfg
    )
    assert url_form.is_configured()

    three_part = CloudinaryHost(
        client,
        Secrets(
            cloudinary_cloud_name="my-cloud",
            cloudinary_api_key="key",
            cloudinary_api_secret="secret",
        ),
        cfg,
    )
    assert three_part.is_configured()

    assert not CloudinaryHost(client, Secrets(), cfg).is_configured()


def test_unconfigured_hosts_refuse_to_upload(settings, tmp_path):
    client = httpx.AsyncClient()
    cfg = settings.config.hosts
    for host in (
        CloudinaryHost(client, Secrets(), cfg),
        ImgbbHost(client, Secrets(), cfg),
        ImgurHost(client, Secrets(), cfg),
    ):
        assert not host.is_configured()


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


@respx.mock
async def test_imgbb_parses_url_and_delete_url(settings, tmp_path):
    path = tmp_path / "01.jpg"
    path.write_bytes(jpeg_bytes(900, 900))

    respx.post("https://api.imgbb.com/1/upload").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "url": "https://i.ibb.co/abc/01.jpg",
                    "delete_url": "https://ibb.co/delete/abc",
                }
            },
        )
    )
    async with httpx.AsyncClient() as client:
        host = ImgbbHost(client, Secrets(imgbb_api_key="k"), settings.config.hosts)
        hosted = await host.upload(path)

    assert hosted is not None
    assert hosted.url == "https://i.ibb.co/abc/01.jpg"
    assert hosted.delete_url == "https://ibb.co/delete/abc"


@respx.mock
async def test_imgur_parses_link_and_deletehash(settings, tmp_path):
    path = tmp_path / "01.jpg"
    path.write_bytes(jpeg_bytes(900, 900))

    respx.post("https://api.imgur.com/3/image").mock(
        return_value=httpx.Response(
            200, json={"data": {"link": "https://i.imgur.com/abc.jpg", "deletehash": "xyz"}}
        )
    )
    async with httpx.AsyncClient() as client:
        host = ImgurHost(client, Secrets(imgur_client_id="c"), settings.config.hosts)
        hosted = await host.upload(path)

    assert hosted is not None
    assert hosted.url == "https://i.imgur.com/abc.jpg"
    assert hosted.delete_url == "xyz"


@respx.mock
async def test_cloudinary_signs_and_parses(settings, tmp_path):
    path = tmp_path / "01.jpg"
    path.write_bytes(jpeg_bytes(900, 900))
    captured: dict[str, str] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode("utf-8", "replace")
        return httpx.Response(
            200, json={"secure_url": "https://res.cloudinary.com/x/01.jpg", "public_id": "x/01"}
        )

    respx.post(url__regex=r"https://api\.cloudinary\.com/.*").mock(side_effect=responder)

    async with httpx.AsyncClient() as client:
        host = CloudinaryHost(
            client, Secrets(cloudinary_url="cloudinary://key:secret@my-cloud"),
            settings.config.hosts,
        )
        hosted = await host.upload(path)

    assert hosted is not None
    assert hosted.url == "https://res.cloudinary.com/x/01.jpg"
    assert hosted.delete_url == "x/01"
    assert "signature" in captured["body"]
    # The secret itself must never be transmitted.
    assert "secret" not in captured["body"]


@respx.mock
async def test_retries_are_bounded_and_then_give_up(settings, tmp_path):
    """A host that keeps 500ing must not stall the run."""
    settings.config.hosts.max_attempts_per_host = 2
    settings.config.hosts.backoff_initial_s = 0.001
    settings.config.hosts.backoff_max_s = 0.002

    path = tmp_path / "01.jpg"
    path.write_bytes(jpeg_bytes(900, 900))
    route = respx.post("https://api.imgbb.com/1/upload").mock(
        return_value=httpx.Response(500)
    )

    async with httpx.AsyncClient() as client:
        host = ImgbbHost(client, Secrets(imgbb_api_key="k"), settings.config.hosts)
        hosted = await host.upload(path)

    assert hosted is None
    assert route.call_count == 2


@respx.mock
async def test_bad_credentials_are_not_retried(settings, tmp_path):
    """403 does not improve with waiting."""
    settings.config.hosts.max_attempts_per_host = 3
    path = tmp_path / "01.jpg"
    path.write_bytes(jpeg_bytes(900, 900))
    route = respx.post("https://api.imgbb.com/1/upload").mock(
        return_value=httpx.Response(403, json={"error": "bad key"})
    )

    async with httpx.AsyncClient() as client:
        host = ImgbbHost(client, Secrets(imgbb_api_key="k"), settings.config.hosts)
        hosted = await host.upload(path)

    assert hosted is None
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("header", "expected"),
    [({"retry-after": "5"}, 5.0), ({}, None), ({"retry-after": "nonsense"}, None)],
)
def test_retry_after_parsing(header, expected):
    response = httpx.Response(429, headers=header)
    assert parse_retry_after(response) == expected
