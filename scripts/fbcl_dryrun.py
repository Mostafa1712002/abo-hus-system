"""DRY-RUN: list FB content-library rows whose caption matches a sensitive keyword,
printing the FULL caption + which keyword matched + permalink. NO deletion."""
from __future__ import annotations
import time, json
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
        except Exception as e: print("date err", e, flush=True)

        # collect unique rows by caption text
        rows_by_cap = {}
        for _ in range(30):
            for r in page.query_selector_all('[role="row"]'):
                try:
                    tx = r.inner_text().replace("\n"," ").strip()
                    if "Published" in tx or "•" in tx:
                        rows_by_cap[tx[:300]] = True
                except Exception: pass
            page.mouse.wheel(0, 2500); time.sleep(1.0)
        print("TOTAL unique rows:", len(rows_by_cap), flush=True)
        print("=== SENSITIVE MATCHES (full caption) ===", flush=True)
        n=0
        for cap in rows_by_cap:
            hits=[k for k in SENSITIVE if k in cap]
            if hits:
                n+=1
                print(f"\n#{n} MATCH {hits}", flush=True)
                print("   ", cap[:280], flush=True)
        print(f"\nTOTAL sensitive = {n}", flush=True)
        b.close()

if __name__ == "__main__":
    main()
