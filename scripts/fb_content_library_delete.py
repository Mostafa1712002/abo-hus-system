"""Delete sensitive Instagram reels via the Facebook Content Library
(facebook.com/professional_dashboard/content/content_library) using injected
Facebook cookies. Runs headless on the VPS under xvfb. Screenshot-driven so we
can iterate on the (obfuscated) DOM.

Usage on VPS:
  xvfb-run -a venv/bin/python fb_content_library_delete.py [--apply]
Without --apply it only screenshots + reports what it sees (dry run).
Cookies: credentials/fb_storage_state.json (converted from facebook.com cookies.txt).
"""
from __future__ import annotations
import sys, time, json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path("/opt/abuhafs/youtube-auto-uploader")
STATE = ROOT / "credentials" / "fb_storage_state.json"
SHOTS = ROOT / "logs"
URL = "https://www.facebook.com/professional_dashboard/content/content_library"

SENSITIVE = ["كنيس","كنائس","أقباط","نصار","يهود","علمان","ماسون","تنصير",
             "استشراق","بهائي","قاديان","نصيري","دروز","ديمقراط","حزب",
             "البعث","مؤامر","أعداء","إضراب","المهجر","العهر","فرويد","التطور"]


def shot(page, name):
    try:
        page.screenshot(path=str(SHOTS / f"fbcl_{name}.png"))
        print("shot:", name, "url=", page.url[:70], flush=True)
    except Exception as e:
        print("shot err", e, flush=True)


def main():
    apply = "--apply" in sys.argv
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox",
            "--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(storage_state=str(STATE),
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1500, "height": 950}, locale="ar")
        page = ctx.new_page(); page.set_default_timeout(45000)
        page.goto(URL, wait_until="domcontentloaded"); time.sleep(8)
        print("landed:", page.url[:80], flush=True)
        shot(page, "1_landing")
        if "login" in page.url or "checkpoint" in page.url:
            print("NOT_LOGGED_IN — FB cookies invalid/expired"); b.close(); return
        # scroll to load content grid
        for _ in range(5):
            page.mouse.wheel(0, 3000); time.sleep(2)
        shot(page, "2_grid")
        # report visible text snippets to map the DOM
        body = page.inner_text("body")
        hits = sorted({k for k in SENSITIVE if k in body})
        print("sensitive terms visible on page:", hits, flush=True)
        print("(dry-run: review logs/fbcl_*.png to tune selectors; rerun with --apply)" if not apply
              else "(--apply mode)", flush=True)
        b.close()


if __name__ == "__main__":
    main()
