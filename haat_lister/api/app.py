"""The FastAPI app. A thin layer over the package, and nothing more.

Every route calls the same functions the CLI calls. Nothing here reimplements
pipeline logic, and nothing shells out to the CLI -- if a behaviour is needed by
both, it belongs in the core, which is why `jobs.py` and `batch.py` grew rather
than `api/`.

Three security properties are decided in this file rather than left to a
deployment note:

  It binds 127.0.0.1 unless told otherwise, and being told otherwise requires a
  token. This process holds image-host keys and an Anthropic key, and it fetches
  arbitrary URLs from inside the operator's network.

  There is no CORS wildcard. The console is served by this same app, so
  same-origin is the normal case and no CORS header is needed at all. `--dev`
  adds exactly one origin: the Vite dev server.

  A missing `web/dist/` is a page explaining how to build it, not a 404. The
  brief ships the built assets so a user without Node can still run `serve`;
  a developer who has just cloned and not built should be told which command to
  run, not shown a broken app.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ..config import Settings
from ..utils.logging import get_logger
from .events import EventBroker
from .routes import config as config_routes
from .routes import diagnose as diagnose_routes
from .routes import downloads as download_routes
from .routes import find as find_routes
from .routes import ingest as ingest_routes
from .routes import jobs as job_routes
from .routes import rows as row_routes
from .routes import sheet as sheet_routes
from .runner import JobRunner

log = get_logger(__name__)

WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"
DEV_ORIGIN = "http://127.0.0.1:5173"

NO_BUILD_PAGE = """<!doctype html>
<meta charset="utf-8"><title>haat-lister — console not built</title>
<body style="font:15px/1.6 system-ui,sans-serif;max-width:44rem;margin:12vh auto;padding:0 1.5rem">
<h1 style="font-size:1.4rem">The console has not been built yet.</h1>
<p>The API is running and answering. The browser front-end lives in
<code>web/</code> and needs building once:</p>
<pre style="background:#f1f3f6;padding:1rem;overflow-x:auto">cd web
npm install
npm run build</pre>
<p>Then restart <code>haat-lister serve</code>. Meanwhile the API is live:
<a href="/api/health">/api/health</a> · <a href="/api/config">/api/config</a></p>
</body>"""


def new_token() -> str:
    return secrets.token_urlsafe(24)


def create_app(
    settings: Settings,
    *,
    token: str | None = None,
    dev: bool = False,
    dist: Path | None = None,
    process: Any = None,
) -> FastAPI:
    from .. import __version__

    broker = EventBroker()
    runner = JobRunner(settings, broker, process=process)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """The runner's worker task needs a running loop, so it starts here
        rather than at import, and is asked to wind down on the way out."""
        runner.start()
        try:
            yield
        finally:
            await runner.stop()

    app = FastAPI(
        lifespan=lifespan,
        title="haat-lister",
        description="Local console for turning product pages into a haat bulk-listing CSV.",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.token = token
    app.state.dist = dist or WEB_DIST
    app.state.broker = broker
    app.state.runner = runner
    # One broker and one runner per app. The runner's asyncio task is created
    # on first submit rather than at import, so building an app in a test costs
    # nothing and needs no running loop.
    if dev:
        # Exactly one origin, and only when asked. A wildcard here would mean
        # any page in the operator's browser could drive a tool that holds API
        # keys and fetches from inside their network.
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=[DEV_ORIGIN],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
        log.warning("Dev mode: allowing cross-origin requests from %s", DEV_ORIGIN)

    _install_auth(app)
    app.include_router(config_routes.router)
    app.include_router(job_routes.router)
    app.include_router(row_routes.router)
    app.include_router(download_routes.router)
    app.include_router(sheet_routes.router)
    app.include_router(diagnose_routes.router)
    app.include_router(find_routes.router)
    app.include_router(ingest_routes.router)
    _install_static(app)
    return app


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _install_auth(app: FastAPI) -> None:
    """Token check, when a token is set. Loopback-only runs have none.

    The query-parameter form exists for one real reason: the browser's
    EventSource cannot set an Authorization header, and the whole running screen
    is an SSE stream. Accepting it in the header too means everything except SSE
    can use the tidier form.
    """

    @app.middleware("http")
    async def require_token(request: Request, call_next: Any) -> Response:
        expected: str | None = request.app.state.token
        if expected is None or not request.url.path.startswith("/api/"):
            return await call_next(request)

        header = request.headers.get("authorization", "")
        supplied = header.removeprefix("Bearer ").strip() if header else (
            request.query_params.get("token") or ""
        )
        # Constant-time: this is a bearer token over a socket someone chose to
        # expose to their network.
        if not secrets.compare_digest(supplied, expected):
            return JSONResponse(
                {"detail": "This console requires the token printed when it started."},
                status_code=401,
            )
        return await call_next(request)


# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------


def _install_static(app: FastAPI) -> None:
    dist: Path = app.state.dist

    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    # response_model=None: the return is a union of Response subclasses, and
    # without this FastAPI tries to build a pydantic model out of them.
    @app.get("/{path:path}", include_in_schema=False, response_model=None)
    async def spa(path: str) -> HTMLResponse | FileResponse:
        """Serve the console, with SPA fallback.

        `path` never reaches the filesystem as-is: it is resolved and then
        checked to be inside dist, so `/../../.env` cannot walk out. Anything
        that is not a real file falls back to index.html, which is what makes
        /jobs/j_7fk2m9qa survive a refresh.
        """
        # An /api/ path that matched no route is a 404, never the console. It
        # is safe either way -- index.html is not a file off disk -- but a
        # client asking for JSON and getting HTML with a 200 has been told the
        # wrong thing, and a traversal attempt that "succeeds" reads as one.
        if path.startswith("api/"):
            raise HTTPException(status_code=404, detail="No such endpoint.")

        index = dist / "index.html"
        if not index.exists():
            return HTMLResponse(NO_BUILD_PAGE, status_code=503)

        if path:
            candidate = (dist / path).resolve()
            if not candidate.is_relative_to(dist.resolve()):
                raise HTTPException(status_code=404)
            if candidate.is_file():
                return FileResponse(candidate)

        return FileResponse(index)
