"""Stage B: a real Chromium, for pages that are a program rather than a document.

The expensive path, and structurally the last resort. `pipeline.process_url`
reaches it only when Stage A came back missing something a browser could
plausibly supply -- see `pipeline.incomplete_reasons`, which is where that
judgement lives, not here. This module knows how to render a page and nothing
about when it is worth doing.

Playwright is an optional dependency and is imported inside `start()`, so an
install without the `[render]` extra pays nothing: no import cost, and no
browser process unless a row actually needs one. A run that never meets an
incomplete page never launches Chromium at all.

What is deliberately NOT here, and will not be added: stealth plugins,
`navigator.webdriver` patching, fingerprint randomisation, canvas spoofing,
CAPTCHA solving. A browser is here to run a shop's own JavaScript, not to
pretend to be someone it is not. The User-Agent is the same honest, contactable
string httpx sends. If a site blocks that, the row fails and says so.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

from ..config import RenderConfig, Settings
from ..models import FetchStage
from ..utils.logging import get_logger
from .static import FetchError, FetchResult

log = get_logger(__name__)

INSTALL_HINT = (
    'Stage B needs Playwright, which is an optional extra:\n'
    '    pip install "haat-lister[render]"\n'
    "    playwright install chromium"
)


class RenderUnavailable(Exception):
    """Playwright is not installed, or its browser has not been downloaded.

    Not a row failure and not a crash: the run carries on with whatever Stage A
    produced, and the affected rows say why they are thin.
    """


class Renderer:
    """One browser for the whole run, started on first need.

    Launching Chromium costs roughly a second, so doing it per row on a batch
    would dominate. One browser, one context, a fresh page per row -- the page
    is the cheap part.
    """

    def __init__(self, settings: Settings, config: RenderConfig | None = None) -> None:
        self._settings = settings
        self._cfg = config or settings.config.render
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self.pages_rendered = 0
        self.unavailable_reason: str | None = None

    @property
    def started(self) -> bool:
        return self._context is not None

    async def start(self) -> None:
        """Idempotent. Raises RenderUnavailable rather than ImportError."""
        if self._context is not None:
            return

        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RenderUnavailable(f"{INSTALL_HINT}\n({exc})") from exc

        cfg = self._cfg
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 -- playwright's own error types vary
            await self._playwright.stop()
            self._playwright = None
            raise RenderUnavailable(
                f"Chromium could not be launched. If Playwright is installed but its browser "
                f"is not, run:  playwright install chromium\n({exc})"
            ) from exc

        self._context = await self._browser.new_context(
            user_agent=self._settings.user_agent,
            locale="en-IN",
            viewport={"width": cfg.viewport_width, "height": cfg.viewport_height},
        )

        if cfg.block_resource_types:
            blocked = set(cfg.block_resource_types)

            async def route_handler(route: Any, request: Any) -> None:
                # We want <img src>, not the pixels behind it -- the attribute is
                # in the DOM either way. Skipping them is faster for us and much
                # lighter on the shop.
                if request.resource_type in blocked:
                    await route.abort()
                else:
                    await route.continue_()

            await self._context.route("**/*", route_handler)

        log.info("Stage B browser started")

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                await closer.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._context = self._browser = self._playwright = None

    async def __aenter__(self) -> Renderer:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def fetch(self, url: str) -> FetchResult:
        """Render one page. Raises RenderUnavailable or FetchError, never junk."""
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import TimeoutError as PlaywrightTimeout

        await self.start()
        cfg = self._cfg
        assert self._context is not None

        page = await self._context.new_page()
        try:
            response = await page.goto(url, wait_until=cfg.wait_until, timeout=cfg.timeout_ms)
            if cfg.settle_ms:
                # Hydration and lazy-load galleries often land just after the
                # wait condition is satisfied.
                await page.wait_for_timeout(cfg.settle_ms)

            status = response.status if response is not None else 0
            if status >= 400:
                raise FetchError(f"http_{status}", url)

            html = await page.content()
            final_url = page.url
        except PlaywrightTimeout as exc:
            raise FetchError("render_timeout", str(exc)) from exc
        except PlaywrightError as exc:
            raise FetchError("render_error", str(exc)) from exc
        finally:
            await page.close()

        self.pages_rendered += 1
        max_bytes = self._settings.config.fetch.max_html_bytes
        return FetchResult(
            url=url,
            final_url=final_url,
            status_code=status,
            html=html[:max_bytes],
            stage=FetchStage.RENDERED,
            elapsed_ms=0,
            headers={"content-type": "text/html"},
        )


def build_renderer(settings: Settings, enabled: bool | None = None) -> Renderer | None:
    """None means Stage B is off for this run, and `process_url` will not reach it.

    Returning None rather than a disabled object is the point: with no renderer
    there is no code path to a browser at all, which is a stronger guarantee than
    a flag someone could later forget to check.
    """
    if enabled is None:
        enabled = settings.config.render.enabled
    return Renderer(settings) if enabled else None
