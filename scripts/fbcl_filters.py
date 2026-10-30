"""Open the Filters panel + date dropdown to discover IG/platform + date options."""
from __future__ import annotations
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path("/opt/abuhafs/youtube-auto-uploader")
STATE = ROOT / "credentials" / "fb_storage_state.json"
SHOTS = ROOT / "logs"
URL = "https://www.facebook.com/professional_dashboard/content/content_library"

def shot(page, name):
    try:
        page.screenshot(path=str(SHOTS / f"fbcl_{name}.png"))
        print("shot:", name, flush=True)
    except Exception as e:
        print("shot err", e, flush=True)

def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox",
            "--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(storage_state=str(STATE),
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1600, "height": 1000}, locale="ar")
        page = ctx.new_page(); page.set_default_timeout(45000)
        page.goto(URL, wait_until="domcontentloaded"); time.sleep(8)
        if "login" in page.url or "checkpoint" in page.url:
            print("NOT_LOGGED_IN"); b.close(); return

        # 1) Open Filters
        try:
            page.get_by_role("button", name="Filters").first.click()
            time.sleep(3)
            shot(page, "3_filters_open")
            # dump the dialog/menu text
            body = page.inner_text("body")
            print("=== after Filters click, menu items ===", flush=True)
            for el in page.query_selector_all('[role="menuitem"],[role="option"],[role="checkbox"],[role="radio"], label'):
                tx = (el.inner_text() or "").strip()
                if tx and len(tx) < 60:
                    print("  filt:", tx, flush=True)
            page.keyboard.press("Escape"); time.sleep(1)
        except Exception as e:
            print("filters err", e, flush=True)

        # 2) Open date range dropdown
        try:
            page.get_by_role("button", name="Last 28 days: May 24 - Jun 21").first.click()
            time.sleep(3)
            shot(page, "4_date_open")
            print("=== date options ===", flush=True)
            for el in page.query_selector_all('[role="menuitem"],[role="option"],[role="radio"], li'):
                tx = (el.inner_text() or "").strip()
                if tx and len(tx) < 60:
                    print("  date:", tx, flush=True)
            page.keyboard.press("Escape"); time.sleep(1)
        except Exception as e:
            print("date err", e, flush=True)

        b.close()

if __name__ == "__main__":
    main()
