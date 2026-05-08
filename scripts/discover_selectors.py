"""Interactive selector discovery.

Run locally (NOT in CI):
    POLYU_USERNAME=... POLYU_PASSWORD=... uv run python scripts/discover_selectors.py

Opens a visible Chromium, logs in, walks to the tennis search results page,
saves HTML + screenshots to artifacts/. Use the dumps to fill in
src/config.py:Selectors.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from playwright.async_api import async_playwright

from src.config import LOGIN_URL

ARTIFACTS = Path("artifacts")


async def dump(page, label: str) -> None:
    ARTIFACTS.mkdir(exist_ok=True)
    html = await page.content()
    (ARTIFACTS / f"{label}.html").write_text(html, encoding="utf-8")
    await page.screenshot(path=str(ARTIFACTS / f"{label}.png"), full_page=True)
    print(f"  saved artifacts/{label}.html and .png")


async def main() -> None:
    user = os.environ["POLYU_USERNAME"]
    pw = os.environ["POLYU_PASSWORD"]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=400)
        context = await browser.new_context()
        page = await context.new_page()

        print("Step 1: login page")
        await page.goto(LOGIN_URL)
        await dump(page, "01_login")

        print("Pause for manual inspection. Press Enter to attempt programmatic login,")
        print("or fill the form manually in the visible browser then press Enter.")
        input()

        # If the user filled it manually, this is a no-op-ish; if not, attempt
        # standard field names. Adjust here if it fails.
        try:
            await page.fill('input[name="username"]', user, timeout=2000)
            await page.fill('input[name="password"]', pw, timeout=2000)
            await page.click('input[type="submit"], button[type="submit"]')
        except Exception as e:
            print(f"  programmatic fill failed ({e}); assuming manual login")

        await page.wait_for_load_state("networkidle")
        await dump(page, "02_after_login")

        print("Step 2: navigate to Sports Facility -> Tennis -> Search.")
        print("Do this manually in the browser, then press Enter.")
        input()
        await dump(page, "03_search_results")

        print("Step 3: click an available 19:30 slot, then click Next.")
        print("(Pick any available slot for the discovery dump.)")
        print("Do it manually in the browser, then press Enter.")
        input()
        await dump(page, "04_pre_submit")

        print("DO NOT click Submit. Close the browser when done.")
        input("Press Enter to close.")
        await browser.close()

    print()
    print("Done. Open artifacts/*.html in a browser or editor.")
    print("Look for:")
    print("  - login form: input names for username/password, submit button selector")
    print("  - sports facility menu: link/button selector")
    print("  - activity dropdown: <select> name and the <option value> for Tennis")
    print("  - search button selector")
    print("  - results page: how cells are marked as available vs booked")
    print("    (class? data-attr? <a> vs <td>? what HTML wraps date/time?)")
    print("  - next button on results page")
    print("  - agreement checkbox name/id, submit button selector on make_book_submit.do")
    print()
    print("Fill these into src/config.py:Selectors and commit.")


if __name__ == "__main__":
    asyncio.run(main())
