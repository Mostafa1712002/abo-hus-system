"""For each content row (Lifetime), grab caption + permalink via Copy link ->
clipboard read, to determine FB vs IG and map sensitive rows. Inspect only."""
from __future__ import annotations
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path("/opt/abuhafs/youtube-auto-uploader")
STATE = ROOT / "credentials" / "fb_storage_state.json"
SHOTS = ROOT / "logs"
URL = "https://www.facebook.com/professional_dashboard/content/content_library"

SENSITIVE = ["كنيس","كنائس","أقباط","نصار","يهود","علمان","ماسون","تنصير",
             "استشراق","بهائي","قاديان","نصيري","دروز","ديمقراط","حزب",
             "البعث","مؤامر","أعداء","إضراب","المهجر","العهر","فرويد","التطور",
             "رأس الكفر","المواطنة"]

def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox",
            "--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(storage_state=str(STATE),
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1600, "height": 1000}, locale="ar",
            permissions=["clipboard-read","clipboard-write"])
        page = ctx.new_page(); page.set_default_timeout(45000)
        page.goto(URL, wait_until="domcontentloaded"); time.sleep(8)
        if "login" in page.url or "checkpoint" in page.url:
            print("NOT_LOGGED_IN"); b.close(); return
        # Lifetime
        try:
            page.get_by_role("button", name="Last 28 days: May 24 - Jun 21").first.click(); time.sleep(2)
            page.get_by_text("Lifetime", exact=True).first.click(); time.sleep(1)
            try: page.get_by_role("button", name="Apply").first.click()
            except Exception: pass
            time.sleep(5)
        except Exception as e:
            print("date err", e, flush=True)

        # Scroll to load ALL rows; collect unique captions
        seen_caps = {}
        for s in range(25):
            rows = page.query_selector_all('[role="row"]')
            for r in rows:
                try:
                    tx = r.inner_text().replace("\n"," ").strip()
                    if "Published" in tx or "•" in tx:
                        cap = tx[:120]
                        seen_caps[cap] = r
                except Exception: pass
            page.mouse.wheel(0, 2500); time.sleep(1.2)
        print("total unique content rows seen:", len(seen_caps), flush=True)

        # For the first sensitive caption, get the permalink via Copy link
        targets = [cap for cap in seen_caps if any(k in cap for k in SENSITIVE)]
        print("sensitive captions found:", len(targets), flush=True)
        for cap in targets:
            print("  SENS:", cap[:90], flush=True)

        # Get permalink of first sensitive row
        if targets:
            r = seen_caps[targets[0]]
            r.scroll_into_view_if_needed(); time.sleep(1)
            btn = r.query_selector('[aria-label="Actions for this post"]')
            if btn:
                btn.click(); time.sleep(2)
                cl = page.get_by_role("menuitem", name="Copy link").first
                cl.click(); time.sleep(1.5)
                link = page.evaluate("navigator.clipboard.readText()")
                print("FIRST SENSITIVE PERMALINK:", link, flush=True)
        b.close()

if __name__ == "__main__":
    main()
