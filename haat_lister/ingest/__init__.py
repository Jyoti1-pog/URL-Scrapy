"""Ways in that are not a fetch.

Every route here ends at `pipeline.process_page` or at the same field-by-field
record the fetcher builds. None of them re-implements extraction, image
validation, canonicalisation or CSV writing -- that is §7, and it is the reason
an imported row cannot quietly skip the provenance gate.

The routes exist because of a measurement rather than a preference: the hosts
this tool is pointed at most often refuse a correctly-identified client at every
rung, and the honest answer to that is not a cleverer fetcher. It is a door the
operator already has a key to -- their own seller panel, or their own browser.
"""

from __future__ import annotations

__all__ = ["saved_page", "seller_export"]
