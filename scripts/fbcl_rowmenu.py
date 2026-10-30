"""Set date=Lifetime, then open the ... menu on the first sensitive row to see
available actions + platform. Inspect only (no clicking Delete)."""
from __future__ import annotations
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path("/opt/abuhafs/youtube-auto-uploader")
STATE = ROOT / "credentials" / "fb_storage_state.json"
SHOTS = ROOT / "logs"
URL = "https://www.facebook.com/professional_dashboard/content/content_library"

def shot(page, name):
    page.screenshot(path=str(SHOTS / f"fbcl_{name}.png")); print("shot:", name, flush=True)

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

        # Set Lifetime date range
        try:
            page.get_by_role("button", name="Last 28 days: May 24 - Jun 21").first.click()
            time.sleep(2)
            page.get_by_text("Lifetime", exact=True).first.click()
            time.sleep(1)
            # click Apply if present
            try:
                page.get_by_role("button", name="Apply").first.click()
            except Exception: pass
            time.sleep(5)
            print("date set to Lifetime", flush=True)
        except Exception as e:
            print("date set err", e, flush=True)
        shot(page, "5_lifetime")

        # count rows now
        rows = page.query_selector_all('[role="row"]')
        print("rows after Lifetime:", len(rows), flush=True)

        # Open the ... menu (aria-label "Actions for this post") on first content row
        acts = page.query_selector_all('[aria-label="Actions for this post"]')
        print("action-menu buttons found:", len(acts), flush=True)
        if acts:
            acts[0].click()
            time.sleep(3)
            shot(page, "6_rowmenu")
            print("=== menu items ===", flush=True)
            for el in page.query_selector_all('[role="menuitem"]'):
                tx = (el.inner_text() or "").strip()
                if tx: print("  item:", tx, flush=True)
        b.close()

if __name__ == "__main__":
    main()
