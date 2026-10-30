"""Attempt a Google/YouTube login on the VPS via Playwright, capturing
screenshots at each step so we can see any challenge Google throws from the
datacenter IP. On success, saves the session to credentials/yt_storage_state.json.

Usage: xvfb-run -a venv/bin/python scripts/yt_login_server.py <email> <password>
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path("/opt/abuhafs/youtube-auto-uploader")
SHOTS = ROOT / "logs"
STATE = ROOT / "credentials" / "yt_storage_state.json"


def shot(page, name: str):
    try:
        page.screenshot(path=str(SHOTS / f"login_{name}.png"), full_page=False)
        print(f"  shot: login_{name}.png  url={page.url[:80]}")
    except Exception as e:
        print(f"  shot {name} failed: {e}")


def main() -> None:
    email, password = sys.argv[1], sys.argv[2]
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--start-maximized"],
        )
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="en-US",
        )
        page = ctx.new_page()
        page.set_default_timeout(40000)

        print("→ goto signin")
        page.goto("https://accounts.google.com/ServiceLogin?service=youtube", wait_until="domcontentloaded")
        time.sleep(3)
        shot(page, "1_start")

        # email
        try:
            page.fill("input[type=email]", email)
            shot(page, "2_email")
            page.click("#identifierNext, button:has-text('Next')")
            time.sleep(4)
        except Exception as e:
            print("  email step:", str(e)[:100])
        shot(page, "3_after_email")

        # password
        try:
            page.wait_for_selector("input[type=password]", timeout=20000)
            page.fill("input[type=password]", password)
            shot(page, "4_password")
            page.click("#passwordNext, button:has-text('Next')")
            time.sleep(6)
        except Exception as e:
            print("  password step:", str(e)[:100])
        shot(page, "5_after_password")

        # If a device-prompt challenge appears, surface the number and WAIT for
        # the user to approve on their phone (poll up to ~4 min).
        time.sleep(3)
        if "challenge" in page.url:
            # try to read the big number shown on the challenge page
            num = ""
            for sel in ["div[aria-live] strong", "samp", "div:has-text('Tap') strong"]:
                try:
                    t = page.locator(sel).first.inner_text(timeout=2000)
                    if t.strip().isdigit():
                        num = t.strip(); break
                except Exception:
                    pass
            if not num:
                # fallback: scan body text for a standalone 1-2 digit number near 'Tap'
                try:
                    body = page.inner_text("body")
                    import re
                    m = re.search(r"[Tt]ap\s+(\d{1,3})", body)
                    if m: num = m.group(1)
                except Exception:
                    pass
            print("\n" + "=" * 50)
            print(f">>> PHONE PROMPT NUMBER: {num or '(see screenshot)'}")
            print(">>> افتح موبايلك Galaxy A54، اضغط Yes واختار الرقم ده")
            print("=" * 50 + "\n")
            shot(page, "6_challenge")
            # poll for the challenge to clear
            for i in range(48):  # 48 * 5s = 240s
                time.sleep(5)
                if "challenge" not in page.url:
                    print(f"  ✓ challenge cleared after ~{(i+1)*5}s")
                    break
            else:
                print("  ! challenge not approved within 4 min")
        shot(page, "6_landed")

        # try to reach studio
        page.goto("https://studio.youtube.com", wait_until="domcontentloaded")
        time.sleep(5)
        shot(page, "7_studio")
        logged_in = "studio.youtube.com" in page.url and "signin" not in page.url and "accounts.google" not in page.url
        print("LOGGED_IN" if logged_in else "NOT_LOGGED_IN", "final_url=", page.url[:90])

        if logged_in:
            ctx.storage_state(path=str(STATE))
            print("✓ saved session:", STATE)
        browser.close()


if __name__ == "__main__":
    main()
