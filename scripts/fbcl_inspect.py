"""Inspect-only: map the content library rows, captions, platform, and the ... menu.
Widens date range if possible. No deletion."""
from __future__ import annotations
import sys, time, json
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path("/opt/abuhafs/youtube-auto-uploader")
STATE = ROOT / "credentials" / "fb_storage_state.json"
SHOTS = ROOT / "logs"
URL = "https://www.facebook.com/professional_dashboard/content/content_library"

def shot(page, name):
    try:
        page.screenshot(path=str(SHOTS / f"fbcl_{name}.png"), full_page=True)
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
        print("landed:", page.url[:80], flush=True)
        if "login" in page.url or "checkpoint" in page.url:
            print("NOT_LOGGED_IN"); b.close(); return

        # Look for the Filters button text and any platform/IG filter
        # Dump all rows: each row is a table row; capture caption + any aria-labels
        for _ in range(8):
            page.mouse.wheel(0, 3000); time.sleep(1.5)
        time.sleep(2)

        # Try to find grid rows via role
        rows = page.query_selector_all('[role="row"]')
        print("role=row count:", len(rows), flush=True)
        out = []
        for i, r in enumerate(rows):
            try:
                t = r.inner_text().replace("\n", " | ")[:200]
                out.append(f"[{i}] {t}")
            except Exception:
                pass
        for line in out[:80]:
            print(line, flush=True)

        # Dump filter buttons / links text to understand IG filtering
        print("\n--- BUTTONS ---", flush=True)
        btns = page.query_selector_all('[role="button"], button, [role="tab"], a')
        seen = set()
        for el in btns:
            try:
                tx = (el.inner_text() or "").strip()
                al = el.get_attribute("aria-label") or ""
                key = (tx, al)
                if (tx or al) and key not in seen and len(tx) < 40:
                    seen.add(key)
                    print(f"btn text='{tx}' aria='{al}'", flush=True)
            except Exception:
                pass
        b.close()

if __name__ == "__main__":
    main()
