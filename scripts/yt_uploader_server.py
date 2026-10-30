"""Server-side YouTube Studio uploader (runs on the VPS, headless/xvfb).

Authenticates via YouTube session cookies imported from the user's browser
(Netscape cookies.txt — same export used for yt-dlp). Bypasses the Data API
quota by driving the Studio web UI.

Modes:
  check               — load cookies, open Studio, report whether we're logged in.
  upload <mp4> ...     — upload one video.

Cookies file: credentials/yt_cookies.txt  (Netscape format, must include the
google.com + youtube.com auth cookies: SID/HSID/SSID/SAPISID/__Secure-*).

Run under xvfb on the server:
  xvfb-run -a venv/bin/python yt_uploader_server.py check
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

ROOT = Path("/opt/abuhafs/youtube-auto-uploader")
STATE = ROOT / "credentials" / "yt_storage_state.json"
STUDIO = "https://studio.youtube.com"


def _new_ctx(p, headless: bool):
    browser = p.chromium.launch(
        headless=headless,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    ctx = browser.new_context(
        viewport={"width": 1366, "height": 900},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="en-US",
        storage_state=str(STATE),
    )
    return browser, ctx


def check(headless: bool = True) -> bool:
    with sync_playwright() as p:
        browser, ctx = _new_ctx(p, headless)
        page = ctx.new_page()
        page.goto(STUDIO, wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        url = page.url
        logged_in = "studio.youtube.com" in url and "accounts.google" not in url and "signin" not in url
        print(f"url={url}")
        print("LOGGED_IN" if logged_in else "NOT_LOGGED_IN")
        try:
            page.screenshot(path=str(ROOT / "logs" / "yt_studio_check.png"))
            print("screenshot:", ROOT / "logs" / "yt_studio_check.png")
        except Exception:
            pass
        browser.close()
        return logged_in


def _first(page, selectors: list[str], timeout: int = 15000):
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if loc.is_visible():
                    return loc
            except Exception:
                pass
        time.sleep(0.4)
    raise PWTimeout(f"none visible: {selectors}")


def upload_one(mp4: str, title: str, description: str, visibility: str = "private",
               headless: bool = True) -> str:
    mp4_path = str(Path(mp4).resolve())
    with sync_playwright() as p:
        browser, ctx = _new_ctx(p, headless)
        page = ctx.new_page()
        page.set_default_timeout(60000)
        page.goto(STUDIO, wait_until="domcontentloaded", timeout=60000)
        time.sleep(6)
        if "accounts.google" in page.url or "signin" in page.url:
            browser.close()
            raise RuntimeError("session invalid on this IP — cookies rejected")

        # open the upload dialog
        print("→ opening upload dialog…")
        _first(page, [
            "#upload-icon",
            "ytcp-button[aria-label='تحميل فيديوهات']",
            "[aria-label='تحميل فيديوهات']",
            "[aria-label='Upload videos']",
        ], timeout=20000).click()
        time.sleep(2)

        print("→ selecting file:", mp4_path)
        page.locator("input[type=file]").first.set_input_files(mp4_path, timeout=60000)

        _first(page, [
            "#title-textarea #textbox",
            "ytcp-social-suggestions-textbox#title-textarea #textbox",
            "div#textbox",
        ], timeout=120000)
        time.sleep(6)

        def js_click_text(label):
            """Click the last visible leaf element whose text == label (handles
            modal buttons the normal click can't reach due to backdrops)."""
            return page.evaluate(
                """(label) => {
                  const els=[...document.querySelectorAll('*')].filter(
                    e=>e.children.length===0 && e.textContent.trim()===label && e.offsetParent!==null);
                  if(!els.length) return 'none';
                  const t=els[els.length-1].closest('button,ytcp-button,[role=button]')||els[els.length-1];
                  t.click(); return 'clicked';
                }""", label)

        # identity interstitial — if it shows, the phone approval enables the button
        if page.locator("text=إثبات هويتك").count() > 0 or page.locator("text=Verify it").count() > 0:
            print("  ! identity dialog present — وافق على موبايلك دلوقتي")
            for _ in range(36):  # wait up to 3 min for phone approval to clear it
                time.sleep(5)
                if page.locator("text=إثبات هويتك").count() == 0 and page.locator("text=Verify it").count() == 0:
                    print("  ✓ identity cleared")
                    break
                js_click_text("التالي")  # nudge in case it just needs confirm

        # title via JS focus + keyboard
        tb = page.locator("#title-textarea #textbox, div#textbox").first
        tb.evaluate("el => el.focus()")
        page.keyboard.press("Control+A"); page.keyboard.press("Delete")
        page.keyboard.type(title[:100], delay=8)
        print("  ✓ title")

        try:
            db = page.locator("#description-textarea #textbox, #description-container #textbox").first
            db.evaluate("el => el.focus()")
            page.keyboard.type(description[:4900], delay=1)
            print("  ✓ description")
        except Exception:
            print("  ! description skipped")

        page.evaluate("""()=>{const r=document.querySelector("tp-yt-paper-radio-button[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']");if(r)r.click();}""")
        print("  ✓ not-for-kids")
        page.screenshot(path=str(ROOT / "logs" / "up_a_details.png"))

        # capture video id from the preview link in the dialog
        vid_id = ""
        try:
            href = page.locator("a[href*='/watch?v=']").first.get_attribute("href", timeout=8000) or ""
            if "watch?v=" in href:
                vid_id = href.split("watch?v=")[1][:11]
                print("  video id:", vid_id)
        except Exception:
            pass

        # Next × 3 via the #next-button id (JS click)
        for i in range(3):
            r = page.evaluate("""()=>{const b=document.querySelector('#next-button');if(b){b.click();return 'ok';}return 'none';}""")
            print(f"  → next {i+1}/3: {r}")
            time.sleep(2.5)
        page.screenshot(path=str(ROOT / "logs" / "up_b_visibility.png"))

        # visibility radio via JS
        vname = {"public": "PUBLIC", "unlisted": "UNLISTED", "private": "PRIVATE"}.get(visibility, "PRIVATE")
        page.evaluate("""(n)=>{const r=document.querySelector("tp-yt-paper-radio-button[name='"+n+"']");if(r)r.click();}""", vname)
        print(f"  ✓ visibility={visibility}")
        time.sleep(1)

        # publish/done via #done-button id (JS click)
        r = page.evaluate("""()=>{const b=document.querySelector('#done-button');if(b){b.click();return 'ok';}return 'none';}""")
        time.sleep(6)
        print(f"  ✓ published ({r})", f"https://youtu.be/{vid_id}" if vid_id else "")
        page.screenshot(path=str(ROOT / "logs" / "up_c_done.png"))

        browser.close()
        return vid_id


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--show", action="store_true", help="headed (needs xvfb)")
    up = sub.add_parser("upload")
    up.add_argument("mp4")
    up.add_argument("--title", required=True)
    up.add_argument("--description", default="")
    up.add_argument("--visibility", default="private", choices=["public", "unlisted", "private"])
    up.add_argument("--show", action="store_true")
    args = ap.parse_args()

    if args.cmd == "check":
        check(headless=not args.show)
    else:
        vid = upload_one(args.mp4, args.title, args.description, args.visibility, headless=not args.show)
        print("RESULT video_id:", vid or "(not captured)")


if __name__ == "__main__":
    main()
