from __future__ import annotations
import time
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT = Path("/opt/abuhafs/youtube-auto-uploader")
STATE = ROOT / "credentials" / "fb_storage_state.json"
URL = "https://www.facebook.com/professional_dashboard/content/content_library"
with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--no-sandbox","--disable-blink-features=AutomationControlled"])
    ctx = b.new_context(storage_state=str(STATE),
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        viewport={"width":1600,"height":1000}, locale="ar")
    page = ctx.new_page(); page.set_default_timeout(45000)
    page.goto(URL, wait_until="domcontentloaded"); time.sleep(9)
    print("URL:", page.url, flush=True)
    page.screenshot(path=str(ROOT/"logs"/"fbcl_check.png"))
    b.close()
