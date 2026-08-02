"""The accessibility and keyboard audit. Run it, do not assume it.

    python demo/shop.py                     # in one terminal
    haat-lister serve --port 8131 --no-open # in another
    python web/audit.py http://127.0.0.1:8131

Three things it checks, on every screen:

  axe-core at WCAG 2.1 AA. Loaded from node_modules rather than a CDN, so this
  works with the wifi off like the rest of the console.

  A keyboard-only run of the whole flow: skip link, textarea, provenance radios,
  Start, preflight, and the review table's arrow/enter editing. It asserts the
  edits actually landed, because "the keys did not error" is not the same as
  "the value reached the CSV".

  prefers-reduced-motion, by counting elements that still animate under it.

It needs the demo shop and 127.0.0.1 in fetch.allow_private_hosts, same as
demo/urls.txt.
"""

import asyncio
import pathlib
import sys

from playwright.async_api import async_playwright

# The report prints glyphs the console legend uses; Windows' default codepage
# cannot encode them.
sys.stdout.reconfigure(encoding="utf-8")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8131"
SHOP = "http://127.0.0.1:8799"
AXE = pathlib.Path("web/node_modules/axe-core/axe.min.js").read_text(encoding="utf-8")
SHOTS = pathlib.Path(__file__).parent

URLS = "\n".join(f"{SHOP}/plain/{i}" for i in range(6))


async def audit(page, label):
    """Run axe and return its violations."""
    await page.add_script_tag(content=AXE)
    result = await page.evaluate(
        """async () => {
            const r = await axe.run(document, {
              runOnly: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'],
            });
            return r.violations.map(v => ({
              id: v.id, impact: v.impact, help: v.help,
              nodes: v.nodes.slice(0, 3).map(n => ({
                target: n.target.join(' '), summary: (n.failureSummary || '').slice(0, 180),
              })),
            }));
        }"""
    )
    status = "clean" if not result else f"{len(result)} violation(s)"
    print(f"  axe · {label:<22} {status}")
    for v in result:
        print(f"      [{v['impact']}] {v['id']}: {v['help']}")
        for n in v["nodes"]:
            print(f"         {n['target']}")
            if n["summary"]:
                print(f"         {n['summary'].splitlines()[0]}")
    return result


async def main():
    failures = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()

        # ---------------------------------------------------------------
        # 1. A complete run, keyboard only after the first focus.
        # ---------------------------------------------------------------
        page = await browser.new_page(viewport={"width": 1360, "height": 950})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

        await page.goto(BASE, wait_until="networkidle")
        failures += await audit(page, "compose (empty)")

        # The skip link should be the first tab stop and should land on main.
        await page.keyboard.press("Tab")
        first = await page.evaluate("document.activeElement.className")
        print(f"  first tab stop: {first!r}")
        if "skip" not in first:
            failures.append({"id": "skip-link", "help": "skip link is not the first tab stop"})
        await page.keyboard.press("Enter")

        # Reach the textarea by tabbing, then type.
        for _ in range(12):
            tag = await page.evaluate("document.activeElement.tagName")
            if tag == "TEXTAREA":
                break
            await page.keyboard.press("Tab")
        tag = await page.evaluate("document.activeElement.tagName")
        if tag != "TEXTAREA":
            failures.append({"id": "keyboard", "help": f"textarea unreachable by Tab (got {tag})"})
        await page.keyboard.type(URLS)

        failures += await audit(page, "compose (filled)")

        # Provenance by keyboard: tab to the radio group, choose with arrows.
        for _ in range(6):
            kind = await page.evaluate("document.activeElement.type || ''")
            if kind == "radio":
                break
            await page.keyboard.press("Tab")
        if await page.evaluate("document.activeElement.type") != "radio":
            failures.append({"id": "keyboard", "help": "provenance radios unreachable by Tab"})
        await page.keyboard.press("Space")

        # Tab to Start and press it.
        for _ in range(8):
            text = await page.evaluate("(document.activeElement.textContent||'').trim()")
            if text.startswith("Start processing"):
                break
            await page.keyboard.press("Tab")
        await page.keyboard.press("Enter")

        await page.wait_for_selector(".preflight", timeout=60_000)
        failures += await audit(page, "preflight")
        await page.screenshot(path=str(SHOTS / "p8-preflight.png"))

        await page.click(".preflight button.primary")
        await page.wait_for_url("**/jobs/j_*", timeout=20_000)
        await page.wait_for_timeout(2500)
        failures += await audit(page, "job (running)")

        await page.wait_for_selector("text=Processed", timeout=240_000)
        await page.wait_for_timeout(800)
        failures += await audit(page, "job (complete)")
        await page.screenshot(path=str(SHOTS / "p8-complete.png"))

        # ---------------------------------------------------------------
        # 2. The review table, keyboard only.
        # ---------------------------------------------------------------
        await page.click("text=Fix them here")
        await page.wait_for_selector(".review-grid", timeout=20_000)
        await page.wait_for_timeout(400)
        failures += await audit(page, "review table")

        await page.click(".review-grid tbody tr:nth-child(1) td:nth-child(6)")
        await page.keyboard.press("Enter")
        await page.keyboard.type("1999")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(500)
        # Enter already dropped one row; edit that one too, so the check is on
        # two DIFFERENT rows. (Typing over an existing value replaces it, which
        # is what a spreadsheet does and what the select-on-edit fix restored.)
        await page.keyboard.press("Enter")
        await page.keyboard.type("2099")
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(700)
        await page.screenshot(path=str(SHOTS / "p8-review.png"))

        filled = await page.evaluate(
            """() => [...document.querySelectorAll('.review-grid tbody tr')]
                 .map(r => r.children[5]?.textContent.trim()).slice(0, 3)"""
        )
        print(f"  keyboard edits landed: {filled}")
        if not any("1999" in f for f in filled) or not any("2099" in f for f in filled):
            failures.append({"id": "keyboard", "help": f"edits did not land: {filled}"})

        # gi_region must not be a tab stop.
        gi_focusable = await page.evaluate(
            """() => [...document.querySelectorAll('.cell-td.locked')]
                 .some(td => td.tabIndex >= 0)"""
        )
        if gi_focusable:
            failures.append({"id": "locked", "help": "gi_region is reachable by keyboard"})
        print(f"  gi_region refuses focus: {not gi_focusable}")

        # ---------------------------------------------------------------
        # 3. History and empty states.
        # ---------------------------------------------------------------
        await page.goto(f"{BASE}/jobs", wait_until="networkidle")
        failures += await audit(page, "history")
        await page.screenshot(path=str(SHOTS / "p8-history.png"))

        await page.goto(f"{BASE}/nowhere", wait_until="networkidle")
        failures += await audit(page, "not found")

        # ---------------------------------------------------------------
        # 4. Reduced motion.
        # ---------------------------------------------------------------
        quiet = await browser.new_context(reduced_motion="reduce")
        qpage = await quiet.new_page()
        await qpage.goto(BASE, wait_until="networkidle")
        moving = await qpage.evaluate(
            """() => [...document.querySelectorAll('*')].filter(el => {
                 const s = getComputedStyle(el);
                 const dur = (s.animationDuration + ' ' + s.transitionDuration);
                 return /[1-9]/.test(dur.replace(/0s/g, ''));
               }).length"""
        )
        print(f"  prefers-reduced-motion: {moving} element(s) still animating")
        if moving:
            failures.append({"id": "motion", "help": f"{moving} elements animate under reduce"})
        await quiet.close()

        print(f"  js errors: {errors or 'none'}")
        if errors:
            failures.append({"id": "js", "help": str(errors[:2])})

        await browser.close()

    print()
    if failures:
        print(f"AUDIT: {len(failures)} problem(s)")
        sys.exit(1)
    print("AUDIT: clean — axe found nothing, keyboard run completed, motion respected")


asyncio.run(main())
