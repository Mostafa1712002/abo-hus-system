"""Navigate Business Suite -> Content, set platform=Instagram if available, scroll,
and report whether IG reels (sensitive) appear. Inspect only."""
from __future__ import annotations
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path("/opt/abuhafs/youtube-auto-uploader")
STATE = ROOT / "credentials" / "fb_storage_state.json"
SHOTS = ROOT / "logs"
HOME = "https://business.facebook.com/latest/home"

def shot(page, name):
    try:
        page.screenshot(path=str(SHOTS / f"fbcl_{name}.png")); print("shot:", name, "url=", page.url[:90], flush=True)
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
        page.goto(HOME, wait_until="domcontentloaded"); time.sleep(8)
        print("home:", page.url[:90], flush=True)
        # Click "Content" in left nav
        clicked=False
        for name in ["Content","المحتوى"]:
            try:
                page.get_by_role("link", name=name, exact=True).first.click(timeout=8000)
                clicked=True; break
            except Exception: pass
        if not clicked:
            try:
                page.get_by_text("Content", exact=True).first.click(); clicked=True
            except Exception as e: print("content click err", e, flush=True)
        time.sleep(8)
        print("after content click:", page.url[:100], flush=True)
        shot(page, "biz_content")

        # Dump nav links + any filter buttons
        print("=== links/buttons ===", flush=True)
        seen=set()
        for el in page.query_selector_all('[role="button"], button, [role="tab"], a, [role="combobox"]'):
            try:
                tx=(el.inner_text() or "").strip(); al=el.get_attribute("aria-label") or ""
                k=(tx,al)
                if (tx or al) and k not in seen and len(tx)<45:
                    seen.add(k); print(f"  '{tx}' aria='{al}'", flush=True)
            except Exception: pass
        b.close()

if __name__ == "__main__":
    main()
