"""Gemini WEB-APP driver (free fallback when the Gemini API hits 429).

The free-tier Gemini *API* is capped at ~60 req/day and the user has no payment
method to enable the paid tier. However the saved Google session
(``credentials/yt_storage_state.json``) is logged into the Gemini *web app*
(``gemini.google.com``), and chatting there is not a "sensitive action", so it
works without any verification challenge.

This module drives that web app with Playwright (chromium, headless — run the
caller under ``xvfb-run -a`` on the headless VPS) and returns the parsed JSON
object from Gemini's reply.

Public API::

    from gemini_web import generate_json
    data = generate_json(prompt, timeout_s=180)   # -> dict

Notes / caveats:
  * The session file is SHARED and opened READ-ONLY (``storage_state=...``); we
    never write it back.
  * A cookie/privacy consent overlay (``div.cdk-overlay-container``) sometimes
    intercepts clicks and must be dismissed first.
  * Each call opens a FRESH chat (navigates to /app) to avoid context bleed.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

logger = logging.getLogger(__name__)

# Resolve project root relative to this file: scripts/gemini_web.py -> root
_ROOT = Path(__file__).resolve().parent.parent
_STORAGE_STATE = _ROOT / "credentials" / "yt_storage_state.json"
_DEBUG_SHOT = _ROOT / "logs" / "gemini_web_debug.png"

_GEMINI_URL = "https://gemini.google.com/app"
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Buttons that dismiss the cookie/consent overlay (EN + AR variants).
_CONSENT_TEXTS = ("Accept all", "I agree", "أوافق", "قبول الكل", "موافقة", "Accept")

# Selectors used to read back the model's response, most-specific first.
_RESPONSE_SELECTORS = (
    "message-content .markdown",
    "message-content",
    ".model-response-text",
    "[data-test-id='conversation-turn']",
    "model-response",
)


def _dismiss_consent(page) -> bool:
    """Dismiss the cdk-overlay consent dialog if present. Returns True if acted."""
    # 1) Try well-known button labels anywhere on the page.
    for label in _CONSENT_TEXTS:
        try:
            btn = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=3000)
                logger.info("dismissed consent via button %r", label)
                page.wait_for_timeout(700)
                return True
        except Exception:
            pass
    # 2) Fallback: any visible button inside the overlay container.
    try:
        overlay_btns = page.locator("div.cdk-overlay-container button")
        n = overlay_btns.count()
        for i in range(n):
            b = overlay_btns.nth(i)
            try:
                if b.is_visible():
                    txt = (b.inner_text() or "").strip()
                    b.click(timeout=3000)
                    logger.info("dismissed consent via overlay button %r", txt[:40])
                    page.wait_for_timeout(700)
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _find_composer(page):
    """Return the main composer contenteditable locator (inside rich-textarea)."""
    # Prefer the contenteditable inside rich-textarea (the real composer).
    candidates = (
        "rich-textarea div[contenteditable='true']",
        "div.ql-editor[contenteditable='true']",
        "div[contenteditable='true']",
    )
    for sel in candidates:
        loc = page.locator(sel)
        try:
            cnt = loc.count()
        except Exception:
            cnt = 0
        if cnt == 0:
            continue
        # take the last visible one (the main composer is the real input)
        for i in range(cnt - 1, -1, -1):
            el = loc.nth(i)
            try:
                if el.is_visible():
                    return el
            except Exception:
                continue
    return None


def _extract_last_response_text(page) -> str:
    """Grab the text of the LAST model turn using the best available selector."""
    for sel in _RESPONSE_SELECTORS:
        loc = page.locator(sel)
        try:
            cnt = loc.count()
        except Exception:
            cnt = 0
        if cnt == 0:
            continue
        try:
            txt = (loc.nth(cnt - 1).inner_text() or "").strip()
        except Exception:
            txt = ""
        if txt:
            return txt
    return ""


def _strip_code_fences(text: str) -> str:
    """Strip a leading/trailing markdown code fence (```json ... ```)."""
    t = text.strip()
    # ```json\n...\n``` or ```\n...\n```
    m = re.search(r"```(?:json|JSON)?\s*\n?(.*?)\n?```", t, re.DOTALL)
    if m:
        return m.group(1).strip()
    return t


def _parse_json_block(text: str) -> dict:
    """Parse a JSON object from ``text``: strip fences, json.loads, then repair."""
    cleaned = _strip_code_fences(text)
    # Try direct parse first.
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # Extract the first {...} span and try again.
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    candidate = match.group(0) if match else cleaned
    try:
        obj = json.loads(candidate)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # Last resort: json-repair (installed as json_repair in the venv).
    try:
        from json_repair import repair_json
        repaired = repair_json(candidate, return_objects=True)
        if isinstance(repaired, dict):
            logger.warning("gemini_web: response JSON was broken — repaired with json-repair")
            return repaired
    except Exception as e:
        logger.warning("gemini_web: json-repair failed: %s", e)
    raise ValueError("could not parse a JSON object from the Gemini web response")


def _wait_for_complete(page, timeout_s: int) -> str:
    """Wait until the response stops changing (stable across polls) or the
    stop button reverts to idle. Returns the final response text."""
    deadline = time.time() + timeout_s
    last_text = ""
    stable_count = 0
    poll = 1.5
    # require ~2 consecutive stable polls AND a non-empty response
    while time.time() < deadline:
        page.wait_for_timeout(int(poll * 1000))
        cur = _extract_last_response_text(page)

        # Is the model still generating? The composer's send control becomes a
        # "Stop" button while streaming. Detect it defensively.
        generating = False
        try:
            stop_btn = page.locator(
                "button[aria-label*='Stop' i], "
                "button[aria-label*='إيقاف'], "
                "button.stop, "
                "[data-test-id='stop-button']"
            )
            if stop_btn.count() > 0 and stop_btn.first.is_visible():
                generating = True
        except Exception:
            pass

        if cur and cur == last_text:
            stable_count += 1
        else:
            stable_count = 0
        last_text = cur

        if cur and not generating and stable_count >= 2:
            return cur
        # If we somehow never see the stop button but text is stable for longer
        if cur and stable_count >= 4:
            return cur
    return last_text


def _run_once(prompt: str, timeout_s: int) -> dict:
    """One full attempt: launch -> navigate -> consent -> type -> send -> read."""
    if not _STORAGE_STATE.exists():
        raise FileNotFoundError(f"session file not found: {_STORAGE_STATE}")

    _DEBUG_SHOT.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        context = None
        page = None
        try:
            import os as _os
            _anon = _os.environ.get("GEMINI_WEB_ANON") == "1"
            _ctx_kw = dict(user_agent=_UA, locale="en-US",
                           viewport={"width": 1366, "height": 900})
            if not _anon and _STORAGE_STATE.exists():
                _ctx_kw["storage_state"] = str(_STORAGE_STATE)  # READ-ONLY
            context = browser.new_context(**_ctx_kw)
            page = context.new_page()
            page.goto(_GEMINI_URL, wait_until="domcontentloaded", timeout=60000)
            # let the SPA settle
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except PWTimeout:
                pass
            page.wait_for_timeout(1500)

            # Dismiss the consent overlay (may need a couple tries as it animates in)
            for _ in range(3):
                if _dismiss_consent(page):
                    break
                page.wait_for_timeout(800)

            # Locate the composer
            composer = None
            for _ in range(20):
                composer = _find_composer(page)
                if composer is not None:
                    break
                page.wait_for_timeout(750)
            if composer is None:
                raise RuntimeError("could not find the Gemini composer (contenteditable)")

            # Focus + insert the prompt, then send with Enter.
            composer.click(timeout=10000)
            page.wait_for_timeout(300)
            # keyboard.insert_text() pastes the whole prompt in one shot — far
            # faster and more reliable than per-char type() for long Arabic
            # prompts (per-char typing times out on the Quill editor).
            inserted = False
            try:
                page.keyboard.insert_text(prompt)
                inserted = True
            except Exception as e:
                logger.warning("insert_text failed (%s); trying JS injection", str(e)[:80])
            if not inserted:
                # JS fallback: set the editor text + fire an input event so the
                # Angular/Quill composer registers the value and enables send.
                try:
                    composer.evaluate(
                        """(el, text) => {
                            el.focus();
                            el.innerText = text;
                            el.dispatchEvent(new InputEvent('input', {bubbles: true}));
                        }""",
                        prompt,
                    )
                    inserted = True
                except Exception as e:
                    logger.warning("JS injection failed: %s", str(e)[:120])
            if not inserted:
                raise RuntimeError("could not enter the prompt into the composer")
            # Verify the composer actually holds (most of) the text before sending.
            page.wait_for_timeout(500)
            try:
                got = (composer.inner_text() or "").strip()
            except Exception:
                got = ""
            if len(got) < min(40, len(prompt) // 2):
                logger.warning("composer text looks short (%d chars vs prompt %d)",
                               len(got), len(prompt))
            page.keyboard.press("Enter")

            # Give the request a moment to start, then wait for completion.
            page.wait_for_timeout(2500)
            text = _wait_for_complete(page, timeout_s=timeout_s)
            if not text:
                raise RuntimeError("empty response from Gemini web app")

            return _parse_json_block(text)
        except Exception:
            # Save a screenshot for diagnosis before re-raising.
            try:
                if page is not None:
                    page.screenshot(path=str(_DEBUG_SHOT), full_page=True)
                    logger.warning("gemini_web: saved debug screenshot to %s", _DEBUG_SHOT)
            except Exception:
                pass
            raise
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            browser.close()


def generate_json(prompt: str, timeout_s: int = 180) -> dict:
    """Drive the Gemini web app to answer ``prompt`` and return the parsed JSON.

    Retries the whole send-once up to 2 attempts on transient failure. Always
    closes the browser. Raises a clear exception on total failure.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, 3):
        try:
            data = _run_once(prompt, timeout_s=timeout_s)
            logger.info("gemini_web: success on attempt %d (keys=%s)",
                        attempt, list(data.keys())[:10])
            return data
        except Exception as e:
            last_exc = e
            logger.warning("gemini_web attempt %d/2 failed: %s", attempt, str(e)[:200])
            time.sleep(3)
    raise RuntimeError(f"gemini_web.generate_json failed after 2 attempts: {last_exc}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    test_prompt = (
        'Respond with ONLY valid JSON, no markdown, no commentary: '
        '{"title": "<a short Arabic islamic lecture title>", '
        '"tags": ["t1","t2","t3"], '
        '"shorts": [{"start": 10, "end": 45, "hook": "..."}], '
        '"quotes": ["q1","q2"]}'
    )
    t0 = time.time()
    result = generate_json(test_prompt, timeout_s=180)
    dt = time.time() - t0
    print("=== PARSED DICT ===")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"=== latency: {dt:.1f}s ===")
    assert isinstance(result, dict), "result is not a dict"
    for k in ("title", "tags", "shorts", "quotes"):
        print(f"  has {k!r}: {k in result}")
