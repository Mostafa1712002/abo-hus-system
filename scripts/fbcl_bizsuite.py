"""Try Meta Business Suite content library (which spans FB+IG) with same session.
Inspect only: find platform/IG filter + whether IG reels appear."""
from __future__ import annotations
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path("/opt/abuhafs/youtube-auto-uploader")
STATE = ROOT / "credentials" / "fb_storage_state.json"
SHOTS = ROOT / "logs"
CANDIDATES = [
    "https://business.facebook.com/latest/content/posts",
    "https://business.facebook.com/latest/content_library",
]

def shot(page, name):
    try:
        page.screenshot(path=str(SHOTS / f"fbcl_{name}.png")); print("shot:", name, "url=", page.url[:80], flush=True)
    except Exception as e: print("shot err", e, flush=True)

def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--no-sandbox",
            "--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(storage_state=str(STATE),
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1600, "height": 1000}, locale="en")
        page = ctx.new_page(); page.set_default_timeout(45000)
        for i, url in enumerate(CANDIDATES):
            try:
                page.goto(url, wait_until="domcontentloaded"); time.sleep(10)
                print(f"--- candidate {i}: {url}", flush=True)
                print("  landed:", page.url[:100], flush=True)
                shot(page, f"biz_{i}")
                if "login" in page.url or "checkpoint" in page.url:
                    print("  -> redirected to login/checkpoint", flush=True)
                    continue
                # look for instagram indicators
                body = page.inner_text("body")[:4000]
                for kw in ["Instagram","instagram","Facebook","Reel","reel","Platform","platform"]:
                    if kw in body: print("   body has:", kw, flush=True)
            except Exception as e:
                print("  err", e, flush=True)
        b.close()

if __name__ == "__main__":
    main()
