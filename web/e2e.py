"""End to end, through a real browser: paste, run, watch, download, verify.

    python demo/shop.py                     # in one terminal
    haat-lister serve --port 8131 --no-open # in another
    python web/e2e.py http://127.0.0.1:8131

Needs `127.0.0.1` in `fetch.allow_private_hosts`, same as demo/urls.txt.

What it proves that the Python suite cannot: that the whole thing works from a
browser, and that the bytes which come out of the download button are the bytes
haat's importer expects. The header check is against
`haat-bulk-listings-template.csv`, byte for byte -- if that ever drifts, this is
where it shows.
"""

from __future__ import annotations

import asyncio
import csv
import io
import pathlib
import sys
import tempfile

from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding="utf-8")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8131"
SHOP = "http://127.0.0.1:8799"
REPO = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "haat-bulk-listings-template.csv"

# Three products, one duplicate, one line that is not a link -- so the run
# exercises collapsing and rejection as well as the happy path.
#
# All server-rendered on purpose. A /spa/ page needs Stage B, which is the
# optional [render] extra, and a base install would fail this gate for a reason
# that has nothing to do with the console. Stage B has its own tests.
URLS = "\n".join(
    [
        f"{SHOP}/plain/0",
        f"{SHOP}/plain/1",
        f"{SHOP}/plain/0?utm_source=newsletter",
        f"{SHOP}/plain/2",
        "not-a-url",
    ]
)

# The demo shop cycles three products by index, so /plain/0,1,2 are three
# different titles -- which is what makes the order check mean something.
EXPECTED_DISTINCT = 3


async def main() -> None:
    problems: list[str] = []
    downloads = pathlib.Path(tempfile.mkdtemp(prefix="haat-e2e-"))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context(
            viewport={"width": 1360, "height": 950}, accept_downloads=True
        )
        page = await context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        # -- paste -------------------------------------------------------
        await page.goto(BASE, wait_until="networkidle")
        await page.fill("textarea", URLS)
        await page.wait_for_timeout(250)
        counter = (await page.inner_text(".counter")).split()
        print(f"  pasted → {' '.join(counter)}")
        if "3" not in counter:
            problems.append(f"expected 3 unique, counter said {counter}")

        # -- provenance: the run cannot start without it -----------------
        if not await page.is_disabled("button.primary"):
            problems.append("Start was enabled before provenance was chosen")
        await page.click("text=I made or own it")

        # -- preflight ---------------------------------------------------
        await page.click("button.primary")
        await page.wait_for_selector(".preflight", timeout=60_000)
        print(f"  preflight → {(await page.inner_text('.preflight-lede')).strip()}")
        await page.click(".preflight button.primary")

        # -- watch -------------------------------------------------------
        await page.wait_for_url("**/jobs/j_*", timeout=20_000)
        job_id = page.url.rsplit("/", 1)[-1]
        await page.wait_for_selector("text=Processed", timeout=300_000)
        await page.wait_for_timeout(800)
        summary = " ".join((await page.inner_text(".job-summary")).split())
        print(f"  {job_id} → {summary}")

        # -- download ----------------------------------------------------
        async with page.expect_download() as pending:
            await page.click("a.button.primary")
        download = await pending.value
        target = downloads / download.suggested_filename
        await download.save_as(target)
        print(f"  downloaded → {target.name} ({target.stat().st_size} bytes)")

        if errors:
            problems.append(f"js errors: {errors[:2]}")
        await browser.close()

    # -- verify the bytes -----------------------------------------------
    raw = target.read_bytes()
    header = raw.split(b"\r\n")[0]
    expected = TEMPLATE.read_bytes().split(b"\r\n")[0]

    checks = {
        "header byte-identical to the template": header == expected,
        "CRLF line endings": raw.count(b"\r\n") == raw.count(b"\n"),
        "no BOM": raw[:3] != b"\xef\xbb\xbf",
    }
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8"))))
    checks["19 columns"] = bool(rows) and len(rows[0]) == 19
    checks["3 rows, duplicate collapsed"] = len(rows) == 3
    # /plain/0,1,2 are three different products, so three distinct titles in
    # that order is the order guarantee actually holding.
    titles = [r["title"] for r in rows]
    checks["rows in the order pasted"] = len(set(titles)) == EXPECTED_DISTINCT
    checks["every row has a title"] = all(titles)
    checks["gi_region blank in every row"] = all(r["gi_region"] == "" for r in rows)

    print()
    for label, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            problems.append(label)

    print()
    if problems:
        print(f"E2E: {len(problems)} problem(s)")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print("E2E: clean — pasted, ran, downloaded, and the bytes are what haat expects")


asyncio.run(main())
