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
from .reasons import NoImageReason, explain
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


def _no_image(result: ImageResult, reason: NoImageReason, detail: str) -> ImageResult:
    """Finish a row with no photo, naming why in one word and in full.

    Every `method = NONE` in this module goes through here. That is the whole
    mechanism behind §4.6's never-silent rule: there is no way to set NONE
    without also setting the enum, so a new failure path cannot be added that
    forgets to say what happened. A test asserts the pairing across every path.
    """
    result.method = ImageMethod.NONE
    result.none_reason = reason
    result.reason = detail
    return result


def _best_low_res(results: list[ValidationResult]) -> ValidationResult | None:
    """The largest photo that failed ONLY on being under the listable standard.

    "Reject everything, ship nothing" is the wrong failure mode for a standard.
    An operator holding a 679x679 photo and a flag saying so can decide whether
    to use it, shoot a new one, or leave the row; an operator holding nothing
    cannot. So the standard does not move -- the FAILURE MODE does.

    Deliberately narrow. `unusably_small` is a separate predicate reason and is
    not eligible, and neither is anything that failed for any other cause: a URL
    that 404s or serves HTML is not a small photo, it is not a photo.
    """
    eligible = [
        r for r in results if r.reason == "below_min_dimensions" and r.width and r.height
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda r: (r.width or 0) * (r.height or 0))


def _flag_low_res(
    record: ProductRecord, width: int | None, height: int | None, settings: Settings
) -> None:
    """Say the size out loud. A flag reading "small" is not a decision anyone
    can make; "679x679, standard is 800x800" is."""
    cfg = settings.config.validator
    record.flag(
        f"The best photo on this page is {width}x{height}, below haat's {cfg.min_width}x"
        f"{cfg.min_height} standard. It has been used so the row is not empty -- replace it "
        f"with a larger shot before importing if you can."
    )


def _low_res_direct(
    result: ImageResult, record: ProductRecord, best: ValidationResult, summary: str
) -> ImageResult:
    result.url = best.url
    result.method = ImageMethod.DIRECT_LOW_RES
    result.reason = f"below_standard:{best.width}x{best.height} (others: {summary})"
    record.flag(
        f"The best photo on this page is {best.width}x{best.height}, below haat's standard. "
        f"It has been used so the row is not empty -- replace it with a larger shot before "
        f"importing if you can."
    )
    return result


def _candidate_reason(candidates: list[str], fallback: NoImageReason) -> NoImageReason:
    """No candidates at all is a different problem from candidates that failed.

    The first sends an operator to look at the page or write a plugin; the
    second sends them to look at the photos. Collapsing the two was most of what
    made `image: none` unactionable.
    """
    return NoImageReason.NO_IMAGE_CANDIDATES if not candidates else fallback


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

        # A photo that failed ONLY on being under the listable standard, and is
        # still above the unusable floor. Not a Tier-1 pass -- it is below the
        # standard and the row will say so -- but a real, fetchable photo, and
        # shipping it flagged beats shipping nothing.
        low_res = None if tier1_passed else _best_low_res(results)

        # ------------------------------------------------------------------
        # THE GATE (Rule 1). Read this before changing anything below it.
        # ------------------------------------------------------------------
        #
        # `low_res` appears here in one direction only: it can stop an upload,
        # never start one. A usable-if-small direct URL means there is nothing
        # to pay a host for, so both flags can only move towards False.
        do_download = need_file or (need_url and not tier1_passed and low_res is None)
        do_upload = need_url and not tier1_passed and low_res is None

        assert not (do_upload and tier1_passed), (
            "Tier 2 upload reached with a valid Tier-1 URL"
        )
        assert not (do_upload and low_res is not None), (
            "Tier 2 upload reached with a usable low-resolution URL in hand"
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

        if low_res is not None and not do_download:
            return _low_res_direct(result, record, low_res, _summarise(results))

        if not do_download:
            return _no_image(
                result,
                _candidate_reason(candidates, NoImageReason.ALL_CANDIDATES_REJECTED),
                f"direct_failed:{_summarise(results)}",
            )

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
            if files:
                small = all(f.low_res for f in files)
                result.method = ImageMethod.LOCAL_LOW_RES if small else ImageMethod.LOCAL
                result.reason = f"local_only:{_summarise(results)}"
                if small:
                    _flag_low_res(record, files[0].width, files[0].height, self._settings)
                return result
            if low_res is not None:
                # The bytes could not be kept, but the URL is still live and
                # still a photo. Better than an empty column.
                return _low_res_direct(result, record, low_res, _summarise(results))
            return _no_image(
                result,
                _candidate_reason(candidates, NoImageReason.ALL_CANDIDATES_REJECTED),
                f"no_image:{_summarise(results)}",
            )

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

            # The same two floors Tier 1 applies to direct URLs. Below the
            # hard floor the file is genuinely unusable and is deleted; between
            # the two it is kept and marked, because a small photo an operator
            # can see and replace beats an empty row they have to investigate.
            if (
                optimised.width < cfg.validator.hard_min_width
                or optimised.height < cfg.validator.hard_min_height
            ):
                log.debug(
                    "Dropping %s: %dx%d is unusably small",
                    source.source_url,
                    optimised.width,
                    optimised.height,
                )
                record.note(
                    f"Image {source.source_url} was dropped: {optimised.width}x"
                    f"{optimised.height} is below the {cfg.validator.hard_min_width}x"
                    f"{cfg.validator.hard_min_height} floor for a usable photo."
                )
                optimised.path.unlink(missing_ok=True)
                continue

            low_res = (
                optimised.width < cfg.validator.min_width
                or optimised.height < cfg.validator.min_height
            )

            files.append(
                ImageFile(
                    order=len(files) + 1,
                    local_path=str(optimised.path.relative_to(root)),
                    original_source_url=source.source_url,
                    bytes=optimised.bytes,
                    width=optimised.width,
                    height=optimised.height,
                    low_res=low_res,
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
            _no_image(
                result,
                NoImageReason.HOSTING_BLOCKED,
                f"direct_failed:{reasons} -> hosting_blocked:third_party_provenance",
            )
            record.flag(
                "Tier 1 failed and provenance is third-party, so the image was not re-hosted. "
                "This row has no image URL."
            )
            return result

        if not files or not self._hosts:
            if files:
                return _no_image(
                    result,
                    NoImageReason.HOST_UPLOAD_FAILED,
                    f"direct_failed:{reasons} -> no_host_configured",
                )
            return _no_image(
                result,
                _candidate_reason(record.image_candidates, NoImageReason.ALL_CANDIDATES_REJECTED),
                f"direct_failed:{reasons} -> nothing_downloaded",
            )

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
        result.upload_used = True
        return _no_image(
            result,
            NoImageReason.HOST_UPLOAD_FAILED,
            f"direct_failed:{reasons} -> all_hosts_failed:{','.join(host_failures)}",
        )


def apply_to_record(record: ProductRecord, result: ImageResult) -> None:
    """Attach the outcome and flag the row when there is no usable image."""
    record.image = result

    if result.method is ImageMethod.NONE:
        assert result.none_reason is not None, (
            "a row finished with no image and no reason -- see _no_image"
        )
        # The plain sentence, not the predicate soup. `image_reason` in
        # review.csv still carries the detail for whoever wants it.
        record.flag(
            f"{explain(result.none_reason)} haat requires at least one photo, so add one by "
            "hand before importing."
        )
    elif result.method is ImageMethod.HOSTED and record.provenance is Provenance.THIRD_PARTY:
        raise AssertionError("hosted image produced for a third-party row")


def unused_paths(settings: Settings) -> tuple[Path, Path]:
    """Where intermediate and deliverable images live, for the summary text."""
    return (
        settings.root / settings.config.paths.downloads_dir,
        settings.root / settings.config.paths.images_dir,
    )
