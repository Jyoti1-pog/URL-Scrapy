"""The provenance gate (Rule 2.2).

`own` and `authorised` proceed normally. `third-party` still produces output --
refusing outright would just push the operator toward a worse tool -- but every
row is forced to needs_review, images may not be re-hosted, and descriptions are
forced through a rewrite.

Why re-hosting specifically is blocked: uploading someone else's photographs to
an image host is us making a copy of their work on the operator's behalf. Linking
to a URL the source already publishes is the operator's call and is recorded;
making a new copy is not something this tool will do on their behalf.
"""

from __future__ import annotations

from ..models import DescriptionMode, ImageMethod, ProductRecord, Provenance, RowStatus

THIRD_PARTY_NOTICE = (
    "Provenance is third-party: this content was not made by, and is not licensed to, the "
    "seller. Every row is marked needs_review, images will not be re-hosted, and the "
    "description must be rewritten in the seller's own words before listing. Check haat's "
    "rules on resale, dropshipping and counterfeits before importing."
)


def effective_description_mode(
    requested: DescriptionMode, provenance: Provenance
) -> DescriptionMode:
    """Third-party copy is never passed through verbatim."""
    if provenance is Provenance.THIRD_PARTY:
        return DescriptionMode.REWRITE
    return requested


def hosting_allowed(provenance: Provenance) -> bool:
    """Whether Tier 2c may ever run for this row. Read by images/pipeline.py."""
    return provenance is not Provenance.THIRD_PARTY


def apply_gate(record: ProductRecord, requested_mode: DescriptionMode) -> None:
    """Enforce Rule 2.2 on a finished record.

    Called last, so nothing downstream can undo it.
    """
    if record.provenance is not Provenance.THIRD_PARTY:
        return

    if record.status is RowStatus.OK:
        record.status = RowStatus.NEEDS_REVIEW
    record.note(THIRD_PARTY_NOTICE)

    if record.image.method is ImageMethod.HOSTED:
        # Defence in depth: the image pipeline is gated too, but this is the
        # invariant that must hold no matter which path produced the record.
        raise AssertionError(
            "Tier 2 hosting produced an image for a third-party-provenance row. "
            "images/pipeline.py must not upload when hosting_allowed() is False."
        )

    if requested_mode is DescriptionMode.RAW and record.description.is_present:
        record.flag(
            "Description was taken verbatim from a source the seller does not own. It must be "
            "rewritten before listing -- run with --description-mode rewrite --llm, or write it "
            "by hand."
        )
