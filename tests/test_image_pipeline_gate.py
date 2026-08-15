"""Phase 7: the Rule 1 gate, the downloader and the optimiser.

These are the tests the whole design exists to satisfy. They assert the cheap
path stays cheap by COUNTING calls, not by reading the code -- a fake host that
records every invocation, and respx routes whose `.called` flag is checked.
"""

from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest
import respx
from PIL import Image, ImageFilter

from haat_lister.images.downloader import download_candidate, download_candidates
from haat_lister.images.hosts.base import HostedImage, ImageHost
from haat_lister.images.optimiser import OptimiseError, optimise
from haat_lister.images.pipeline import ImageResolver, apply_to_record
from haat_lister.models import (
    FieldSource,
    FieldValue,
    ImageMethod,
    ImageMode,
    ProductRecord,
    Provenance,
)

HERO = "https://cdn.example/hero.jpg"
SECOND = "https://cdn.example/second.jpg"


def jpeg_bytes(width: int = 1200, height: int = 1200, quality: int = 95) -> bytes:
    buffer = io.BytesIO()
    Image.merge("RGB", [Image.effect_noise((width, height), 120).convert("L")] * 3).save(
        buffer, format="JPEG", quality=quality
    )
    return buffer.getvalue()


def png_bytes(width: int = 1000, height: int = 1000, alpha: bool = False) -> bytes:
    buffer = io.BytesIO()
    mode = "RGBA" if alpha else "RGB"
    Image.new(mode, (width, height), (10, 120, 90, 128) if alpha else (10, 120, 90)).save(
        buffer, format="PNG"
    )
    return buffer.getvalue()


def mock_good_image(url: str, body: bytes | None = None) -> None:
    body = body or jpeg_bytes()
    respx.head(url).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "image/jpeg", "content-length": str(len(body))}
        )
    )
    respx.get(url).mock(
        return_value=httpx.Response(200, content=body, headers={"content-type": "image/jpeg"})
    )


def mock_hotlink_protected(url: str, body: bytes | None = None) -> None:
    """Serves anyone who arrives from the product page, 403s everyone else.

    This is the realistic shape of a Tier-1 failure that still downloads fine
    for us: the validator probes without a Referer (as a buyer's browser would
    when loading a haat listing) and is refused, while Tier 2a sends the product
    page as Referer and gets the bytes. Anything cruder -- a plain 403 on HEAD --
    would not actually fail Tier 1, because the validator falls back to a ranged
    GET on 403.
    """
    payload = body or jpeg_bytes()

    def responder(request: httpx.Request) -> httpx.Response:
        if request.headers.get("referer"):
            return httpx.Response(200, content=payload, headers={"content-type": "image/jpeg"})
        return httpx.Response(403)

    respx.route(url=url).mock(side_effect=responder)


class RecordingHost:
    """A host that fails the test simply by being used."""

    def __init__(self, name: str = "fake", succeeds: bool = True) -> None:
        self.name = name
        self.succeeds = succeeds
        self.calls: list[Path] = []

    @property
    def hosted_url(self) -> str:
        return f"https://{self.name}.example/x.jpg"

    def is_configured(self) -> bool:
        return True

    async def upload(self, path: Path) -> HostedImage | None:
        self.calls.append(path)
        if not self.succeeds:
            return None
        return HostedImage(url=self.hosted_url, host_name=self.name)


def make_record(candidates: list[str], provenance: Provenance = Provenance.OWN) -> ProductRecord:
    return ProductRecord(
        row_key="shop-example-kurta-abc12345",
        source_url="https://shop.example/products/kurta",
        canonical_url="https://shop.example/products/kurta",
        provenance=provenance,
        title=FieldValue.found("Kurta", FieldSource.JSONLD),
        image_candidates=candidates,
    )


async def resolve(settings, mode, candidates, hosts=None, provenance=Provenance.OWN, **kwargs):
    record = make_record(candidates, provenance)
    async with httpx.AsyncClient() as client:
        resolver = ImageResolver(settings, client, mode, hosts=hosts, **kwargs)
        result = await resolver.resolve(record)
    return record, result


@pytest.fixture
def settings_in_tmp(settings, tmp_path):
    """Keep downloads and images out of the repo during tests."""
    settings.root = tmp_path
    return settings


# ---------------------------------------------------------------------------
# THE HEADLINE TESTS
# ---------------------------------------------------------------------------


@respx.mock
async def test_tier1_pass_prevents_download_and_upload(settings_in_tmp):
    """A valid direct URL in url_columns mode: zero downloads, zero host calls.

    This is the test the whole design exists to satisfy.
    """
    mock_good_image(HERO)
    host = RecordingHost()

    record, result = await resolve(settings_in_tmp, ImageMode.URL_COLUMNS, [HERO], hosts=[host])

    assert result.method is ImageMethod.DIRECT
    assert result.url == HERO
    assert result.reason == "direct_ok"

    assert host.calls == [], "a valid Tier-1 URL must never reach an image host"
    assert result.download_used is False
    assert result.upload_used is False
    assert result.bytes_downloaded == 0
    assert result.files == []
    # ...and Tier 1 is still recorded as attempted, on every row, always.
    assert result.tier1_attempted is True
    assert result.tier1_passed is True


@respx.mock
async def test_manifest_mode_never_calls_any_host(settings_in_tmp):
    """The default mode cannot contact a host, even when Tier 1 fails outright."""
    mock_hotlink_protected(HERO)
    hosts = [RecordingHost("cloudinary"), RecordingHost("imgbb"), RecordingHost("imgur")]

    record, result = await resolve(settings_in_tmp, ImageMode.MANIFEST, [HERO], hosts=hosts)

    for host in hosts:
        assert host.calls == [], f"{host.name} was called in manifest mode"
    assert result.upload_used is False
    assert result.method is ImageMethod.LOCAL
    assert result.files, "manifest mode still has to produce the files"


@respx.mock
async def test_manifest_mode_downloads_even_when_tier1_passes(settings_in_tmp):
    """Files are the deliverable here, so a passing URL does not skip the download."""
    mock_good_image(HERO)
    host = RecordingHost()

    record, result = await resolve(settings_in_tmp, ImageMode.MANIFEST, [HERO], hosts=[host])

    assert result.tier1_passed is True
    assert result.download_used is True
    assert result.files
    assert host.calls == []


@respx.mock
async def test_both_mode_keeps_the_direct_url_and_the_files(settings_in_tmp):
    mock_good_image(HERO)
    host = RecordingHost()

    record, result = await resolve(settings_in_tmp, ImageMode.BOTH, [HERO], hosts=[host])

    assert result.method is ImageMethod.DIRECT
    assert result.reason == "direct_ok+files"
    assert result.url == HERO
    assert result.files
    assert host.calls == []


@respx.mock
async def test_third_party_provenance_blocks_hosting(settings_in_tmp):
    """Rule 2.2: re-uploading photos the operator does not own is not something
    this tool does on their behalf."""
    mock_hotlink_protected(HERO)
    host = RecordingHost()

    record, result = await resolve(
        settings_in_tmp,
        ImageMode.URL_COLUMNS,
        [HERO],
        hosts=[host],
        provenance=Provenance.THIRD_PARTY,
    )

    assert host.calls == []
    assert result.method is ImageMethod.NONE
    assert "hosting_blocked:third_party_provenance" in result.reason


@respx.mock
async def test_force_rehost_warns_and_takes_the_expensive_path(settings_in_tmp):
    """It exists for manual use, and must be visibly abnormal."""
    mock_good_image(HERO)
    host = RecordingHost()
    mock_good_image(host.hosted_url)

    record, result = await resolve(
        settings_in_tmp, ImageMode.URL_COLUMNS, [HERO], hosts=[host], force_rehost=True
    )

    assert result.tier1_passed is False
    assert result.download_used is True
    # The whole point: it discards a perfectly good direct URL and pays for a host.
    assert host.calls, "--force-rehost must actually reach the host"
    assert result.method is ImageMethod.HOSTED


# ---------------------------------------------------------------------------
# Tier 3 -- failing honestly
# ---------------------------------------------------------------------------


@respx.mock
async def test_all_tiers_fail_writes_needs_review_row(settings_in_tmp):
    respx.head(HERO).mock(return_value=httpx.Response(404))
    respx.get(HERO).mock(return_value=httpx.Response(404))

    record, result = await resolve(settings_in_tmp, ImageMode.MANIFEST, [HERO])
    apply_to_record(record, result)

    assert result.method is ImageMethod.NONE
    assert result.url == ""
    assert "http_404" in result.reason
    assert record.status.value == "needs_review"
    # The flag says what happened in a sentence, not in predicate soup. The
    # detail is still on the row -- `image_reason` in review.csv -- but
    # "direct_failed:http_404 -> nothing_downloaded" is a true, complete and
    # useless thing to put in front of a seller.
    assert result.none_reason is not None
    assert any(
        "failed a listability check" in note or "none of them could be fetched" in note
        for note in record.notes
    ), record.notes


@respx.mock
async def test_no_candidates_is_reported_not_invented(settings_in_tmp):
    record, result = await resolve(settings_in_tmp, ImageMode.MANIFEST, [])
    assert result.method is ImageMethod.NONE
    assert result.url == ""
    assert "no_candidates" in result.reason


@respx.mock
async def test_image_reason_names_the_failing_predicate(settings_in_tmp):
    """A rising hosted ratio must be diagnosable."""
    signed = "https://cdn.example/a.jpg?X-Amz-Signature=deadbeef"
    respx.get(signed).mock(
        return_value=httpx.Response(
            200, content=jpeg_bytes(), headers={"content-type": "image/jpeg"}
        )
    )
    record, result = await resolve(settings_in_tmp, ImageMode.MANIFEST, [signed])
    assert "signed_or_expiring_url" in result.reason


# ---------------------------------------------------------------------------
# Tier 2a -- downloader
# ---------------------------------------------------------------------------


@respx.mock
async def test_download_sends_referer_and_browser_agent(settings, tmp_path):
    """Hotlink-blocked URLs usually download fine FOR US, with the page as Referer."""
    captured: dict[str, str] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        captured.update(request.headers)
        return httpx.Response(200, content=jpeg_bytes(), headers={"content-type": "image/jpeg"})

    respx.get(HERO).mock(side_effect=responder)

    async with httpx.AsyncClient() as client:
        result = await download_candidate(
            client,
            HERO,
            tmp_path,
            1,
            settings.config.images,
            settings.config.fetch,
            referer="https://shop.example/products/kurta",
        )

    assert result is not None
    assert captured["referer"] == "https://shop.example/products/kurta"
    assert "Mozilla" in captured["user-agent"]


@respx.mock
async def test_download_rejects_non_image_bytes(settings, tmp_path):
    respx.get(HERO).mock(
        return_value=httpx.Response(
            200, content=b"<html>nope</html>" * 100, headers={"content-type": "image/jpeg"}
        )
    )
    async with httpx.AsyncClient() as client:
        result = await download_candidate(
            client, HERO, tmp_path, 1, settings.config.images, settings.config.fetch, referer="x"
        )
    assert result is None
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_download_respects_the_size_cap(settings, tmp_path):
    settings.config.images.max_download_mb = 1
    huge = b"\xff\xd8\xff" + b"0" * (2 * 1024 * 1024)
    respx.get(HERO).mock(
        return_value=httpx.Response(200, content=huge, headers={"content-type": "image/jpeg"})
    )
    async with httpx.AsyncClient() as client:
        result = await download_candidate(
            client, HERO, tmp_path, 1, settings.config.images, settings.config.fetch, referer="x"
        )
    assert result is None


@respx.mock
async def test_download_skips_failures_and_keeps_going(settings, tmp_path):
    respx.get(HERO).mock(return_value=httpx.Response(404))
    respx.get(SECOND).mock(
        return_value=httpx.Response(
            200, content=jpeg_bytes(), headers={"content-type": "image/jpeg"}
        )
    )
    async with httpx.AsyncClient() as client:
        files = await download_candidates(
            client, [HERO, SECOND], tmp_path, settings.config.images, settings.config.fetch, "x"
        )
    assert len(files) == 1
    assert files[0].source_url == SECOND


# ---------------------------------------------------------------------------
# Tier 2b -- optimiser
# ---------------------------------------------------------------------------


def test_optimiser_names_files_hero_first(settings, tmp_path):
    source = tmp_path / "raw.jpg"
    source.write_bytes(jpeg_bytes())
    out = optimise(source, tmp_path / "out", 1, settings.config.images)
    assert out.path.name == "01.jpg"

    out2 = optimise(source, tmp_path / "out", 2, settings.config.images)
    assert out2.path.name == "02.jpg"


def test_optimiser_downscales_but_never_upscales(settings, tmp_path):
    settings.config.images.max_edge_px = 800

    big = tmp_path / "big.jpg"
    big.write_bytes(jpeg_bytes(2400, 1200))
    out = optimise(big, tmp_path / "out", 1, settings.config.images)
    assert max(out.width, out.height) == 800
    assert out.width / out.height == pytest.approx(2.0, rel=0.01)

    small = tmp_path / "small.jpg"
    small.write_bytes(jpeg_bytes(400, 400))
    out = optimise(small, tmp_path / "out", 2, settings.config.images)
    assert (out.width, out.height) == (400, 400)


def test_optimiser_keeps_webp_because_haat_accepts_it(settings, tmp_path):
    assert settings.config.images.keep_webp is True
    source = tmp_path / "raw.webp"
    Image.new("RGB", (1000, 1000), (30, 60, 90)).save(source, format="WEBP")
    out = optimise(source, tmp_path / "out", 1, settings.config.images)
    assert out.image_format == "WEBP"
    assert out.path.suffix == ".webp"


def test_optimiser_converts_webp_when_asked(settings, tmp_path):
    settings.config.images.keep_webp = False
    source = tmp_path / "raw.webp"
    Image.new("RGB", (1000, 1000), (30, 60, 90)).save(source, format="WEBP")
    out = optimise(source, tmp_path / "out", 1, settings.config.images)
    assert out.image_format == "JPEG"


def test_optimiser_converts_unaccepted_formats_to_jpeg(settings, tmp_path):
    source = tmp_path / "raw.bmp"
    Image.new("RGB", (900, 900), (10, 20, 30)).save(source, format="BMP")
    out = optimise(source, tmp_path / "out", 1, settings.config.images)
    assert out.image_format == "JPEG"


def test_optimiser_flattens_transparency_onto_white(settings, tmp_path):
    """JPEG has no alpha; a black halo would look like a defect to a buyer."""
    settings.config.images.accepted_formats = ["jpeg"]
    source = tmp_path / "raw.png"
    source.write_bytes(png_bytes(alpha=True))
    out = optimise(source, tmp_path / "out", 1, settings.config.images)
    assert out.image_format == "JPEG"
    with Image.open(out.path) as opened:
        assert opened.mode == "RGB"


def test_optimiser_strips_exif(settings, tmp_path):
    source = tmp_path / "raw.jpg"
    image = Image.merge("RGB", [Image.effect_noise((900, 900), 120).convert("L")] * 3)
    exif = Image.Exif()
    exif[271] = "TestCamera"
    image.save(source, format="JPEG", exif=exif)
    assert Image.open(source).getexif().get(271) == "TestCamera"

    out = optimise(source, tmp_path / "out", 1, settings.config.images)
    with Image.open(out.path) as opened:
        assert opened.getexif().get(271) is None


def test_optimiser_steps_quality_down_to_meet_the_size_ceiling(settings, tmp_path):
    cfg = settings.config.images
    cfg.max_file_mb = 2
    cfg.max_edge_px = 4000
    # Chosen so the first pass is definitely over and the step is definitely
    # under, rather than relying on where a real quality curve happens to land.
    cfg.jpeg_quality = 100
    cfg.jpeg_quality_steps = [40]

    # A blurred noise field, not raw noise: pure noise is incompressible, so a
    # quality dial would have nothing to work with and the test would prove
    # nothing about the stepping.
    photo = Image.merge("RGB", [Image.effect_noise((3000, 3000), 90).convert("L")] * 3).filter(
        ImageFilter.GaussianBlur(1.2)
    )
    source = tmp_path / "raw.jpg"
    photo.save(source, format="JPEG", quality=100)

    ceiling = cfg.max_file_mb * 1024 * 1024
    assert source.stat().st_size > ceiling, "the input must exceed the ceiling"

    out = optimise(source, tmp_path / "out", 1, cfg)
    assert out.bytes <= ceiling


def test_optimiser_fails_by_name_when_it_cannot_shrink_enough(settings, tmp_path):
    settings.config.images.max_file_mb = 0
    source = tmp_path / "raw.jpg"
    source.write_bytes(jpeg_bytes(1200, 1200))

    with pytest.raises(OptimiseError) as exc:
        optimise(source, tmp_path / "out", 1, settings.config.images)
    assert exc.value.reason == "too_large_after_optimisation"


def test_optimiser_reports_corrupt_input_by_name(settings, tmp_path):
    source = tmp_path / "raw.jpg"
    source.write_bytes(b"\xff\xd8\xff" + b"garbage")
    with pytest.raises(OptimiseError) as exc:
        optimise(source, tmp_path / "out", 1, settings.config.images)
    assert exc.value.reason.startswith("optimise_failed")


# ---------------------------------------------------------------------------
# The protocol
# ---------------------------------------------------------------------------


def test_recording_host_satisfies_the_protocol():
    """If this drifts, the manifest-mode test would stop proving anything."""
    assert isinstance(RecordingHost(), ImageHost)
