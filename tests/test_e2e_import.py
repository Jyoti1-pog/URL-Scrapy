"""§8 test 17 -- the import screen, driven by a browser, end to end.

WHY A BROWSER AND NOT A CLIENT. Everything below is already covered against the
API: the mapper, the profile round-trip, the row build. What is NOT covered
anywhere else is whether the two-step shape §4.1 requires actually holds in the
thing an operator touches -- whether the columns are visible before anything is
committed, whether `provenance` is genuinely unskippable when the button is
right there, and whether a saved profile reappears in the dropdowns rather than
merely in the response body.

Those are properties of the screen. A test that asserted them against the API
would be asserting them about the wrong artefact.

WRITTEN IN PYTHON rather than with `@playwright/test`, and that is a deliberate
economy: Playwright is already a dependency here for Stage B rendering, so this
costs no new toolchain, no second config file and no second CI step. The
trade-off is no auto-waiting fixtures, which is why the waits below are
explicit.

SKIPPED, NOT FAILED, when the browser or the built console is missing. A
contributor without `playwright install chromium` should not see a red suite for
a dependency the rest of the tool does not need.
"""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from haat_lister.api.app import create_app
from haat_lister.config import Settings

EXPORT = (
    "Seller SKU,Product Name,Product URL,MRP,Net Weight,HSN Code,Category,Internal Ref\n"
    "KRT-1,Indigo block-print kurta,https://shop.example/p/kurta-1,2499,320 g,6206,apparel,X-99\n"
    "STL-2,Handwoven silk stole,https://shop.example/p/stole-2,3750,180 g,6214,apparel,X-100\n"
)

DIST = Path(__file__).resolve().parents[1] / "web" / "dist"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api", reason="Playwright not installed")
    with playwright.sync_playwright() as p:
        try:
            launched = p.chromium.launch()
        except Exception as exc:  # noqa: BLE001 -- an absent browser is a skip
            pytest.skip(f"Chromium not installed: run `playwright install chromium` ({exc})")
        yield launched
        launched.close()


@pytest.fixture
def console(settings: Settings, tmp_path: Path):
    """The real API, serving the real built console, on a real port.

    Not the TestClient: the whole point is the browser, and a browser needs a
    socket. Threaded rather than a subprocess so the settings object under test
    is the one the server uses -- `tmp_path` for the root means the profile this
    test writes cannot leak into the operator's own `profiles/`.
    """
    if not (DIST / "index.html").is_file():
        pytest.skip("web/dist is not built: run `npm run build` in web/")

    import uvicorn

    tuned = settings.model_copy(deep=True, update={"root": tmp_path})
    tuned.config.render.enabled = False

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(tuned), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:  # pragma: no cover -- a wedged event loop
        pytest.skip("the API did not start in time")

    yield f"http://127.0.0.1:{port}", tuned

    server.should_exit = True
    thread.join(timeout=10)


def _upload(page, path: Path) -> None:
    page.set_input_files("input[type=file]", str(path))
    page.wait_for_selector("table.mapper", timeout=15_000)


def test_import_a_seller_export_map_save_run_and_reimport(browser, console, tmp_path) -> None:
    """§8 test 17, as one journey because that is how it is used.

    Split into five tests it would need five uploads and five servers to assert
    the one thing that matters most -- that the profile saved in step three is
    still there in step five.
    """
    base, tuned = console
    export = tmp_path / "catalogue.csv"
    export.write_text(EXPORT, encoding="utf-8")

    page = browser.new_page()
    page.goto(f"{base}/import", wait_until="networkidle")

    # --- 1. the file, and the columns before anything is committed ---------
    _upload(page, export)

    body = page.inner_text("table.mapper")
    assert "Product Name" in body
    assert "Internal Ref" in body, "a column we did not understand was hidden"
    # §4.1: shown, and shown as a choice rather than as a default that happened.
    assert "not used" in page.inner_text("body")
    # Recognised-but-unwritable reads differently from unrecognised.
    assert "no haat column for it" in page.inner_text("body")

    # Nothing has been built. The rows only exist after the second call.
    assert "written" not in page.inner_text("body").lower().split("Import")[0]

    # --- 2. the auto-map is right, and editable ---------------------------
    selects = page.locator("table.mapper select")
    values = [selects.nth(i).input_value() for i in range(selects.count())]
    assert "source_url" in values, "the URL column was not recognised"
    assert "gi_region" not in values

    # And there is no way to choose it either -- the options come from the
    # server, which does not send it (§7).
    assert "gi_region" not in page.inner_html("table.mapper")

    # --- 3. provenance is unskippable, even with the button right there ----
    page.fill("input[placeholder*='nilaya']", "Nilaya Panel")
    run = page.get_by_role("button", name="Import")
    assert run.is_disabled(), "the run button was live before provenance was chosen"

    page.get_by_role("radio", name="My own shop").check()
    assert run.is_enabled()

    # --- 4. run ------------------------------------------------------------
    run.click()
    page.wait_for_selector("text=/need a human/", timeout=60_000)

    summary = page.inner_text("body")
    assert "Indigo block-print kurta" in summary
    assert "Saved the mapping as" in summary

    # The profile is on disk under the test's own root, not the operator's.
    saved = tuned.root / "profiles" / "nilaya-panel.yaml"
    assert saved.is_file(), f"no profile at {saved}"

    # --- 5. re-import: the profile comes back ------------------------------
    page.goto(f"{base}/import", wait_until="networkidle")
    _upload(page, export)

    assert "nilaya-panel" in page.inner_text("body"), "the saved profile did not auto-apply"
    reselects = page.locator("table.mapper select")
    again = [reselects.nth(i).input_value() for i in range(reselects.count())]
    assert again == values, "the profile applied a different mapping to the one it saved"

    page.close()


def test_the_import_screen_is_reachable_and_named(browser, console) -> None:
    """A route nobody can find is a route nobody uses, and this one is the
    answer to the failure mode the rest of v5 is about."""
    base, _ = console
    page = browser.new_page()
    page.goto(base, wait_until="networkidle")

    page.get_by_role("link", name="Import").click()
    page.wait_for_selector("input[type=file]", timeout=10_000)

    assert "Import a file" in page.inner_text("h1")
    page.close()


def test_a_wrong_file_says_what_to_do_instead(browser, console, tmp_path) -> None:
    """The most likely thing to happen on this screen. It is not an error
    condition, it is Tuesday, and the message has to name the next action."""
    base, _ = console
    junk = tmp_path / "holiday.png"
    junk.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    page = browser.new_page()
    page.goto(f"{base}/import", wait_until="networkidle")
    page.set_input_files("input[type=file]", str(junk))
    page.wait_for_selector(".error", timeout=15_000)

    assert "Ctrl+S" in page.inner_text(".error")
    page.close()
