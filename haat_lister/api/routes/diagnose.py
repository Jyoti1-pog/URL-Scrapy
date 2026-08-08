"""GET /api/diagnose -- the "Why no photo?" report, for a browser.

Thin, like every route here: it validates the query, calls `diagnose_url`, and
returns what that returns. The report model is reused rather than copied into
`schemas.py` because it is not a core model -- it was built to be read, carries
no FieldValue and no ImageResult, and a parallel wire copy would be one more
thing to keep in step for no gain.

TWO THINGS THIS ROUTE OWNS, because the core function cannot know them:

  * a failed diagnosis is still a 200. "The fetch was refused" is the answer,
    not an error -- turning it into a 500 would put the reason in a server log
    instead of in front of the person who asked.
  * one at a time. The core is happy to run concurrently; a console that lets
    someone open twenty rows at once would be twenty simultaneous crawls of one
    shop, which is exactly what the per-domain limiter exists to prevent.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from ...config import Settings
from ...diagnose import Diagnosis, diagnose_url
from ...utils.logging import get_logger
from ...utils.urls import host_of

router = APIRouter(tags=["diagnose"])

log = get_logger(__name__)

# Long enough for a marketplace URL with its whole tracking tail, short enough
# that the query string is not a payload.
MAX_URL_CHARS = 2048

# How long a request will wait for the one slot before giving up. A diagnosis is
# seconds, not minutes; anything past this means something is stuck and the
# caller deserves to be told rather than held.
QUEUE_TIMEOUT_S = 60.0

_gate = asyncio.Semaphore(1)


@router.get("/api/diagnose", response_model=Diagnosis)
async def diagnose(
    request: Request,
    url: Annotated[str, Query(max_length=MAX_URL_CHARS)],
    check_all: Annotated[bool, Query(alias="all")] = False,
    render: Annotated[bool | None, Query()] = None,
) -> Diagnosis:
    settings: Settings = request.app.state.settings

    cleaned = url.strip()
    if not cleaned.lower().startswith(("http://", "https://")) or not host_of(cleaned):
        raise HTTPException(
            status_code=400,
            detail="Give me a full http(s) product page URL, including the scheme.",
        )

    try:
        await asyncio.wait_for(_gate.acquire(), timeout=QUEUE_TIMEOUT_S)
    except TimeoutError:
        raise HTTPException(
            status_code=503,
            detail="Another diagnosis is still running. Try again in a moment.",
        ) from None

    try:
        return await diagnose_url(cleaned, settings, render=render, check_all=check_all)
    finally:
        _gate.release()
