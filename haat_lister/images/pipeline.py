"""THE IMAGE PIPELINE -- the only orchestrator, and the gate from Rule 1.

    Tier 1 (validate the source's own direct URL) is attempted and validated on
    every single row, always. The Tier 2 upload to a third-party image host may
    execute only when Tier 1 has been PROVEN to fail AND the active output mode
    actually requires a URL. Never both. Never in parallel. Never "just to be
    safe."

That gate is written below as literal code rather than a comment, and asserted.
Every row records which tier produced its image and why, so a rising hosted
ratio can be diagnosed instead of shrugged at.

In `manifest` mode -- the default, and the right one for haat, whose uploader
takes files -- `_upload` is unreachable. A test proves it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx

from ..config import Settings
from ..models import (
    ImageFile,
    ImageMethod,
    ImageMode,
    ImageResult,
    ProductRecord,
    Provenance,
    ValidationResult,
)
from ..policy.provenance import hosting_allowed
from ..store.ledger import Ledger
from ..utils.logging import get_logger
from .downloader import download_candidates
from .hosts.base import ImageHost
from .optimiser import OptimiseError, optimise
from .validator import Tier1Validator, validate_all_candidates

log = get_logger(__name__)


def _content_hash(path: Path) -> str:
    """Content-addressed upload dedupe: the same photo under two source URLs
    should cost one upload, not two."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _summarise(results: list[ValidationResult]) -> str:
    """The failing predicates, so `image_reason` always names WHY we paid for Tier 2."""
    return ",".join(f"{r.reason}" for r in results if not r.ok) or "no_candidates"


class ImageResolver:
    """Resolves one record's images. Holds the validator, downloader and hosts."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        mode: ImageMode,
        hosts: list[ImageHost] | None = None,
        ledger: Ledger | None = None,
        hotlink_test: bool | None = None,
        force_rehost: bool = False,
    ) -> None:
        self._settings = settings
        self._client = client
        self._mode = mode
        self._hosts = hosts or []
        self._ledger = ledger
        self._force_rehost = force_rehost
        self._validator = Tier1Validator(
            client,
            settings.config.validator,
            ledger,
            hotlink_test=hotlink_test,
            allow_private_hosts=settings.config.fetch.allow_private_hosts,
        )

        # Counters for the run summary.
        self.host_calls = 0
        self.downloads = 0

    async def resolve(self, record: ProductRecord) -> ImageResult:
        candidates = record.image_candidates

        need_url = self._mode.need_url
        need_file = self._mode.need_file

        # ------------------------------------------------------------------
        # TIER 1 -- always, every row, never skipped.
        # ------------------------------------------------------------------
        winner, results = await validate_all_candidates(candidates, self._validator)
        tier1_passed = winner is not None and not self._force_rehost

        if self._force_rehost and winner is not None:
            log.warning(
                "--force-rehost is discarding a VALID direct URL (%s) and taking the expensive "
                "path. This is not normal operation.",
                winner.url,
            )

        # ------------------------------------------------------------------
        # THE GATE (Rule 1). Read this before changing anything below it.
        # ------------------------------------------------------------------
        do_download = need_file or (need_url and not tier1_passed)
        do_upload = need_url and not tier1_passed

        assert not (do_upload and tier1_passed), (
            "Tier 2 upload reached with a valid Tier-1 URL"
        )

        result = ImageResult(
            tier1_attempted=True,
            tier1_passed=tier1_passed,
            candidate_results=results,
        )

        # Tier 1 passed and no local file is wanted: this row is finished.
        # No download, no host call, no bytes.
        if tier1_passed and not do_download:
            assert winner is not None
            result.url = winner.url
            result.method = ImageMethod.DIRECT
            result.reason = "direct_ok"
            return result

        if not do_download:
            result.method = ImageMethod.NONE
            result.reason = f"direct_failed:{_summarise(results)}"
            return result

        # ------------------------------------------------------------------
        # TIER 2a + 2b -- download and normalise.
        # ------------------------------------------------------------------
        files = await self._download_and_normalise(record)
        result.files = files
        result.download_used = True
        result.bytes_downloaded = sum(f.bytes for f in files)

        if tier1_passed:
            assert winner is not None
            result.url = winner.url
            result.method = ImageMethod.DIRECT
            result.reason = "direct_ok+files"
            return result

        if not do_upload:
            # manifest mode: local files ARE the deliverable.
            result.method = ImageMethod.LOCAL if files else ImageMethod.NONE
            result.reason = (
                f"local_only:{_summarise(results)}" if files else f"no_image:{_summarise(results)}"
            )
            return result

        # ------------------------------------------------------------------
        # TIER 2c/2d -- gated, last resort. Adapters arrive in Phase 8.
        # ------------------------------------------------------------------
        return await self._upload(record, result, files, results)

    # -- Tier 2a/2b --------------------------------------------------------

    async def _download_and_normalise(self, record: ProductRecord) -> list[ImageFile]:
        cfg = self._settings.config
        root = self._settings.root
        raw_dir = root / cfg.paths.downloads_dir / record.row_key
        out_dir = root / cfg.paths.images_dir / record.row_key

        downloaded = await download_candidates(
            self._client,
            record.image_candidates,
            raw_dir,
            cfg.images,
            cfg.fetch,
            referer=record.source_url,
        )
        self.downloads += len(downloaded)

        files: list[ImageFile] = []
        for source in downloaded:
            try:
                optimised = optimise(source.path, out_dir, len(files) + 1, cfg.images)
            except OptimiseError as exc:
                log.warning("Could not normalise %s: %s", source.source_url, exc.reason)
                record.note(f"Image {source.source_url} was dropped: {exc.reason}.")
                continue

            # The same floor Tier 1 applies to direct URLs. A candidate whose
            # size only becomes visible after decoding would otherwise slip a
            # thumbnail into the deliverables, and haat is a premium marketplace.
            if (
                optimised.width < cfg.validator.min_width
                or optimised.height < cfg.validator.min_height
            ):
                log.debug(
                    "Dropping %s: %dx%d is below the listable minimum",
                    source.source_url,
                    optimised.width,
                    optimised.height,
                )
                record.note(
                    f"Image {source.source_url} was dropped: {optimised.width}x"
                    f"{optimised.height} is below the {cfg.validator.min_width}x"
                    f"{cfg.validator.min_height} minimum for a listable photo."
                )
                optimised.path.unlink(missing_ok=True)
                continue

            files.append(
                ImageFile(
                    order=len(files) + 1,
                    local_path=str(optimised.path.relative_to(root)),
                    original_source_url=source.source_url,
                    bytes=optimised.bytes,
                    width=optimised.width,
                    height=optimised.height,
                )
            )

        return files

    # -- Tier 2c/2d --------------------------------------------------------

    async def _upload(
        self,
        record: ProductRecord,
        result: ImageResult,
        files: list[ImageFile],
        results: list[ValidationResult],
    ) -> ImageResult:
        """Entry precondition asserted in code: never guess a way into a host call."""
        assert self._mode.need_url, "upload reached in a mode that needs no URL"
        assert not result.tier1_passed or self._force_rehost, (
            "upload reached with a passing Tier-1 URL"
        )

        reasons = _summarise(results)

        if not hosting_allowed(record.provenance):
            # Rule 2.2: re-uploading photographs the operator does not own would
            # be us making a copy of someone else's work on their behalf.
            result.method = ImageMethod.NONE
            result.reason = f"direct_failed:{reasons} -> hosting_blocked:third_party_provenance"
            record.flag(
                "Tier 1 failed and provenance is third-party, so the image was not re-hosted. "
                "This row has no image URL."
            )
            return result

        if not files or not self._hosts:
            result.method = ImageMethod.NONE
            result.reason = (
                f"direct_failed:{reasons} -> no_host_configured"
                if files
                else f"direct_failed:{reasons} -> nothing_downloaded"
            )
            return result

        hero = files[0]
        hero_path = self._settings.root / hero.local_path
        content_hash = _content_hash(hero_path)

        # Identical bytes already hosted: reuse, and make no second call.
        if self._ledger is not None and (seen := self._ledger.find_upload(content_hash)):
            url, host_name, _ = seen
            hero.hosted_url = url
            result.url = url
            result.method = ImageMethod.HOSTED
            result.host_used = host_name
            result.upload_used = False
            result.reason = f"direct_failed:{reasons} -> reused_existing_upload:{host_name}"
            return result

        host_failures: list[str] = []

        for host in self._hosts:
            self.host_calls += 1
            hosted = await host.upload(hero_path)

            if hosted is None:
                host_failures.append(f"{host.name}:upload_failed")
                continue

            # TIER 2d -- do not trust the host's own answer. The same nine
            # predicates, hotlink test included. A hosted URL that fails is not
            # a success.
            check = await self._validator.validate(hosted.url)
            if not check.ok:
                log.warning(
                    "%s returned a URL that fails Tier 1 (%s); trying the next host",
                    host.name,
                    check.reason,
                )
                host_failures.append(f"{host.name}:revalidation_{check.reason}")
                continue

            if self._ledger is not None:
                self._ledger.record_upload(
                    content_hash, hosted.host_name, hosted.url, hosted.delete_url
                )

            hero.hosted_url = hosted.url
            result.url = hosted.url
            result.method = ImageMethod.HOSTED
            result.host_used = hosted.host_name
            result.upload_used = True
            # Rejected hosts stay in the reason. A host that hands back a dead
            # URL is worth seeing even when a later one saved the row.
            rejected = "".join(f"{failure} -> " for failure in host_failures)
            result.reason = (
                f"direct_failed:{reasons} -> {rejected}hosted_via:{hosted.host_name}"
            )
            return result

        # TIER 3 -- every host failed. Never fabricate a URL.
        result.method = ImageMethod.NONE
        result.upload_used = True
        result.reason = f"direct_failed:{reasons} -> all_hosts_failed:{','.join(host_failures)}"
        return result


def apply_to_record(record: ProductRecord, result: ImageResult) -> None:
    """Attach the outcome and flag the row when there is no usable image."""
    record.image = result

    if result.method is ImageMethod.NONE:
        record.flag(
            f"No usable image for this listing ({result.reason}). haat requires at least one "
            "photo, so add one by hand before importing."
        )
    elif result.method is ImageMethod.HOSTED and record.provenance is Provenance.THIRD_PARTY:
        raise AssertionError("hosted image produced for a third-party row")


def unused_paths(settings: Settings) -> tuple[Path, Path]:
    """Where intermediate and deliverable images live, for the summary text."""
    return (
        settings.root / settings.config.paths.downloads_dir,
        settings.root / settings.config.paths.images_dir,
    )
