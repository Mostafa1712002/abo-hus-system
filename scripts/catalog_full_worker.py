"""Catalog → FULL multi-platform worker (runs on VPS).

Gives every catalog video the SAME treatment the live pipeline gives شرح الرسالة
/ العبودية, but sourced from the catalog instead of YouTube captions (which are
BLOCKED from this VPS):

Per ready catalog_videos row (download_status='downloaded', process_status='pending'):
  1. Fetch the SRT from the catalog MySQL (tarweig) over SSH, cache it locally.
  2. Generate Arabic metadata + chapters + 3 shorts segments + 3-5 Telegram
     quotes with Gemini, USING THE SRT as the source text (reuses the proven
     ``GeminiGenerator.generate`` from the live pipeline).
  3. Prepend the standard 3-sec intro to the MAIN lecture (NOT to shorts).
  4. Upload the MAIN video to YouTube (private), add to series playlist, set thumb.
  5. Cut 3 shorts (9:16) into a PERSISTENT dir and ENQUEUE them in catalog_shorts
     (NOT posted inline). scripts/shorts_dispatcher.py posts them later, paced
     across the day (IG rate-limit friendly) and deletes each file after posting.
  6. Publish the MAIN video to Facebook + a Telegram post, and 3-5 text quotes
     to Telegram.
  7. Mark the row uploaded/archived, store produced ids, delete the MAIN mp4
     (short mp4s are kept until the dispatcher posts them).

Reuses the live pipeline's helpers verbatim (src.pipeline.*) so the branding,
captions, error-handling and posting sequence stay identical to شرح الرسالة.

A video with no SRT still gets its MAIN video uploaded (intro + YouTube +
playlist + thumbnail + FB + Telegram announcement) but skips shorts/quotes, and
is logged.

Quota-aware: now ~1600 units per video here (MAIN only; the 3 shorts spend
their own quota later in the dispatcher). Stops before exceeding DAILY_BUDGET
(matches scripts/catalog_upload_worker.py conventions).

Usage:
  python catalog_full_worker.py --series <slug> --limit N
  python catalog_full_worker.py --limit 1
  python catalog_full_worker.py --series <slug> --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/opt/abuhafs/youtube-auto-uploader")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---- live pipeline + helpers (reused verbatim) ----------------------------
from src.config import Config
from src.ai_generator import GeminiGenerator, metadata_to_dict
from src.shorts_cutter import cut_all_shorts
from src.thumbnail import make_thumbnail
from src.youtube_uploader import (
    add_video_to_playlist,
    get_or_create_playlist,
    get_youtube_service,
    set_thumbnail,
    upload_video,
)
from src import pipeline as P  # _publish_*, _build_*, _compute_next_teaser, ...

# intro prepend (scripts/) — applied to MAIN only
from intro_prepend import prepend_intro

# Branded thumbnail generator (scripts/) — professional fixed template with the
# title rendered into navy/green boxes. Preferred over the frame-grab thumbnail.
try:
    from branded_thumbnail import make_branded_thumbnail  # scripts/branded_thumbnail.py
except Exception:  # pragma: no cover - keep worker importable even if missing
    make_branded_thumbnail = None

# Gemini WEB-APP fallback (free, drives gemini.google.com via Playwright) used
# only when the Gemini *API* exhausts its rate limit. Imported lazily-safe.
try:
    import gemini_web  # scripts/gemini_web.py (on sys.path via ROOT/scripts)
except Exception:  # pragma: no cover - keep worker importable even if missing
    gemini_web = None

# ---------------------------------------------------------------------------
DB = ROOT / "data" / "abuhafs.db"
SRT_CACHE = Path("/opt/abuhafs/catalog_meta")     # cache fetched SRT here
DESC_DIR = SRT_CACHE                              # optional cached descriptions
# Persistent home for cut shorts AWAITING the paced dispatcher. NOT a temp dir:
# the worker only cuts+enqueues here; scripts/shorts_dispatcher.py posts each
# short later (spread across the day for IG rate limits) and deletes its file.
CATALOG_SHORTS_DIR = Path("/opt/abuhafs/catalog_shorts")
# سلاسل حساسة: الفيديو الكامل فقط، بدون عمل shorts (مواضيع قد تثير فتنة)
NO_SHORTS_SLUGS = {"slsl-alghzo-alfkry"}
DAILY_BUDGET = 90000          # YT quota raised to 100k/day (2026-06-19); leave ~10k for شرح الرسالة
QUOTA_PER_UPLOAD = 1600      # main + each short ~1600 each
# main only now (shorts are decoupled into the dispatcher); keep headroom margin
QUOTA_PER_FULL = QUOTA_PER_UPLOAD * 2

# Site catalog MySQL (subtitle_srt lives here; YouTube captions are blocked).
# abuhafs.info moved to this box on 2026-10-01 - read the local database instead of tarweig.
CATALOG_MYSQL = ["mysql", "--defaults-file=/root/.my.cnf", "abuhafs_info", "-N", "-e"]
CATALOG_QUERY = "SELECT subtitle_srt FROM videos WHERE video_id='{yt}' LIMIT 1;"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("catalog_full_worker")


class _Parent:
    """Lightweight stand-in for the pipeline's PendingVideo.

    The reused pipeline helpers (``_upload_shorts_for_video``,
    ``_compute_next_teaser``) only read these attributes off the parent/item:
    ``video_id``, ``series``, ``original_path``, ``playlist_id``,
    ``original_name``. We expose exactly those so we can call them unchanged.
    """

    def __init__(self, video_id, series, original_path, playlist_id, original_name):
        self.video_id = video_id
        self.series = series
        self.original_path = original_path
        self.playlist_id = playlist_id
        self.original_name = original_name
        self.metadata = {}


def _normalize_srt(text):
    """VTT dot-ms (00:00:02.399) -> SRT comma-ms (00:00:02,399)."""
    if not text:
        return text
    return re.sub(r'(\d{2}:\d{2}:\d{2})\.(\d{3})', r'\1,\2', text)


def fetch_srt(yt_id: str) -> str:
    """Fetch subtitle_srt for a YouTube id from the site catalog MySQL.

    Cached to /opt/abuhafs/catalog_meta/<yt>.srt. Returns '' if none.
    """
    SRT_CACHE.mkdir(parents=True, exist_ok=True)
    cache = SRT_CACHE / f"{yt_id}.srt"
    if cache.exists() and cache.stat().st_size > 50:
        return _normalize_srt(cache.read_text(encoding="utf-8", errors="replace"))
    cmd = CATALOG_MYSQL + [CATALOG_QUERY.format(yt=yt_id)]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=120).stdout
    except Exception as e:
        log.warning("SRT fetch failed for %s: %s", yt_id, e)
        return ""
    text = out.decode("utf-8", errors="replace").strip()
    # mysql -N prints literal "\n"/"\r" escapes for newlines in the column —
    # unescape them so we get a real multi-line SRT.
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    if not text or text.upper() in ("NULL", ""):
        return ""
    # sanity: an SRT has at least one "-->" timing line
    if "-->" not in text:
        log.warning("fetched text for %s has no SRT timings; ignoring", yt_id)
        return ""
    text = _normalize_srt(text)
    cache.write_text(text, encoding="utf-8")
    return text


def _safe_unlink(p: Path) -> None:
    try:
        if p and Path(p).exists():
            Path(p).unlink()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# catalog_shorts queue (Part A): the worker cuts the 3 shorts then enqueues them
# here; scripts/shorts_dispatcher.py posts them later, paced across the day.
# ---------------------------------------------------------------------------
def ensure_shorts_table(con: sqlite3.Connection) -> None:
    """Create the catalog_shorts queue table if it doesn't exist (idempotent)."""
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS catalog_shorts (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_catalog_id INTEGER,
            parent_yt_id      TEXT,
            parent_url        TEXT,
            series            TEXT,
            next_teaser       TEXT,
            clip_index        INTEGER,
            mp4_path          TEXT,
            thumb_path        TEXT,
            title             TEXT,
            description       TEXT,
            hook              TEXT,
            tags_json         TEXT,
            status            TEXT DEFAULT 'pending',
            yt_short_id       TEXT,
            fb_reel_id        TEXT,
            ig_reel_id        TEXT,
            tg_msg_id         INTEGER,
            ig_attempted      INTEGER DEFAULT 0,
            last_error        TEXT,
            created_at        TEXT,
            posted_at         TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_catalog_shorts_status ON catalog_shorts(status);
        CREATE INDEX IF NOT EXISTS ix_catalog_shorts_posted_at ON catalog_shorts(posted_at);
        """
    )
    con.commit()


def enqueue_shorts(con: sqlite3.Connection, *, parent_catalog_id: int,
                   parent_yt_id: str, parent_url: str, series: str,
                   next_teaser: str, clips, cut_paths) -> int:
    """Insert one catalog_shorts row per cut short. Returns count enqueued.

    Stores everything the dispatcher needs to rebuild the SAME per-platform
    metadata that pipeline._upload_shorts_for_video builds (title, description,
    hook, tags, parent_url, series, next_teaser). cut_all_shorts walks clips
    1..N in order, so clip_index lines up with clips[idx-1].
    """
    cur = con.cursor()
    n = 0
    for idx, short_path in enumerate(cut_paths, 1):
        clip = clips[idx - 1] if idx - 1 < len(clips) else None
        if clip is None:
            log.warning("no clip metadata for short #%d — skipping enqueue", idx)
            continue
        title = (getattr(clip, "suggested_short_title", "") or "").strip()
        description = (getattr(clip, "description", "") or "").strip()
        hook = (getattr(clip, "hook", "") or "").strip()
        tags = list(getattr(clip, "tags", []) or [])
        thumb_path = str(Path(short_path).with_suffix(".jpg"))
        cur.execute(
            "INSERT INTO catalog_shorts ("
            " parent_catalog_id, parent_yt_id, parent_url, series, next_teaser,"
            " clip_index, mp4_path, thumb_path, title, description, hook,"
            " tags_json, status, ig_attempted, created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending', 0, datetime('now'))",
            (parent_catalog_id, parent_yt_id, parent_url, series, next_teaser,
             idx, str(short_path), thumb_path, title, description, hook,
             json.dumps(tags, ensure_ascii=False)),
        )
        n += 1
    con.commit()
    log.info("✓ enqueued %d shorts into catalog_shorts for parent %s", n, parent_yt_id)
    return n


# Substrings that indicate the Gemini API is rate-limited / quota-exhausted
# (mirrors the rotate_triggers in GeminiGenerator + our own RuntimeError text).
_RATE_LIMIT_SIGNATURES = (
    "429", "quota", "rate limit", "rate-limit", "ratelimit", "rate ",
    "resourceexhausted", "resource_exhausted", "resource exhausted",
    "all keys rate limited", "كل المفاتيح",  # ar message from generate()
)


def _is_rate_limit_error(exc: Exception) -> bool:
    s = (str(exc) or "").lower()
    s += " " + type(exc).__name__.lower()
    return any(sig in s for sig in _RATE_LIMIT_SIGNATURES)


def _generate_via_web_fallback(gemini, cfg, *, with_ts, plain, duration):
    """Free fallback: build the SAME prompt the Gemini API uses, send it through
    the Gemini WEB APP (gemini_web.generate_json), then adapt the returned dict
    into a VideoMetadata using the SAME parser the API path uses.

    Reuses ``GeminiGenerator._build_prompt`` and ``._parse_response`` so the
    prompt text and downstream metadata shape are byte-for-byte identical to the
    API path — only the transport (web app vs API) differs.

    Returns a VideoMetadata, or raises on failure (caller keeps current
    behavior so the main video still uploads).
    """
    if gemini_web is None:
        raise RuntimeError("gemini_web module unavailable")

    max_clips = cfg.shorts.get("max_clips_per_video", 3)
    min_clip = cfg.shorts.get("min_clip_seconds", 25)
    max_clip = cfg.shorts.get("max_clip_seconds", 58)
    max_title = cfg.ai_prompts.get("title_max_length", 75)
    max_hashtags = cfg.ai_prompts.get("max_hashtags", 10)
    include_chapters = cfg.ai_prompts.get("include_chapters", True)
    honorifics = (cfg.channel_branding or {}).get("speaker_honorifics", []) or []

    # Cache honorifics on the instance (the API path's generate() does this too,
    # though _build_prompt also takes them explicitly).
    gemini._speaker_honorifics = list(honorifics)

    prompt = gemini._build_prompt(
        with_ts, plain, duration,
        max_clips, min_clip, max_clip, max_title, max_hashtags,
        include_chapters,
        cfg.ai_prompts.get("content_context", ""),
        cfg.ai_prompts.get("description_template_hint", ""),
        cfg.ai_prompts.get("shorts_selection_hint", ""),
        speaker_honorifics=honorifics,
    )

    log.warning("Gemini API exhausted — falling back to Gemini WEB APP "
                "(gemini.google.com) for metadata generation")
    data = gemini_web.generate_json(prompt, timeout_s=240)

    # Adapt via the SAME parser used by the API path (no OpenAI verifier here:
    # the web fallback is the rate-limit escape hatch; we keep it simple and
    # robust — clips/quotes still go through identical normalization).
    md = gemini._parse_response(data, duration, max_clip, min_clip, max_clips)
    log.info("✓ Gemini WEB fallback produced metadata: title=%r chapters=%d "
             "shorts=%d quotes=%d", md.title, len(md.chapters),
             len(md.important_clips), len(md.text_quotes))
    return md


def _make_split_ask(gemini, *, allow_web=True):
    """Build the ``ask(prompt) -> dict`` callable used by ``generate_split``.

    Each sub-prompt goes through the Gemini API first (with the generator's own
    key-rotation). If a sub-call is rate-limited/quota-exhausted OR returns an
    empty dict, we transparently fall back to the Gemini WEB APP for THAT
    sub-call only. This keeps the split partial-tolerant per slice: a
    rate-limited shorts sub-call can still succeed via the web while the others
    used the API (or vice-versa). The 3 prompts are individually small, so no
    single response is large enough to hit the 8192-token truncation.
    """
    def ask(prompt: str) -> dict:
        try:
            data = gemini._ask_via_api(prompt)
        except Exception as e:
            if allow_web and gemini_web is not None and _is_rate_limit_error(e):
                log.warning("split sub-call API rate-limited (%s) — web fallback",
                            type(e).__name__)
                try:
                    return gemini_web.generate_json(prompt, timeout_s=240) or {}
                except Exception as we:
                    log.warning("split sub-call web fallback failed: %s", str(we)[:160])
                    return {}
            raise
        if not data and allow_web and gemini_web is not None:
            # Empty/unrecoverable API JSON for this slice — try the web app once.
            log.warning("split sub-call returned empty JSON — trying web fallback")
            try:
                return gemini_web.generate_json(prompt, timeout_s=240) or {}
            except Exception as we:
                log.warning("split sub-call web fallback failed: %s", str(we)[:160])
                return {}
        return data
    return ask


def _generate_split(gemini, cfg, *, with_ts, plain, duration):
    """Anti-truncation generator: run ``GeminiGenerator.generate_split`` with the
    3-sub-prompt splitting logic, using a per-sub-call ask that prefers the API
    and falls back to the Gemini WEB APP on rate-limit/empty.

    Same VideoMetadata shape as ``_generate_with_retry``; the worker can use
    either. Raises only when the CORE call can't produce a title.
    """
    ask = _make_split_ask(gemini)
    md = gemini.generate_split(
        ask,
        srt_text_with_timestamps=with_ts,
        plain_text=plain,
        video_duration_seconds=duration,
        max_clips=cfg.shorts.get("max_clips_per_video", 3),
        min_clip_seconds=cfg.shorts.get("min_clip_seconds", 25),
        max_clip_seconds=cfg.shorts.get("max_clip_seconds", 58),
        max_title_length=cfg.ai_prompts.get("title_max_length", 75),
        max_hashtags=cfg.ai_prompts.get("max_hashtags", 10),
        include_chapters=cfg.ai_prompts.get("include_chapters", True),
        content_context=cfg.ai_prompts.get("content_context", ""),
        description_template_hint=cfg.ai_prompts.get("description_template_hint", ""),
        shorts_selection_hint=cfg.ai_prompts.get("shorts_selection_hint", ""),
        speaker_honorifics=(cfg.channel_branding or {}).get("speaker_honorifics", []) or [],
    )
    log.info("✓ Gemini SPLIT produced metadata: title=%r chapters=%d shorts=%d quotes=%d",
             md.title, len(md.chapters), len(md.important_clips), len(md.text_quotes))
    return md


def _generate_with_retry(gemini, cfg, *, with_ts, plain, duration, max_tries=3):
    """Call ``GeminiGenerator.generate`` with the live-pipeline params, retrying
    when the response comes back without any clips AND without any quotes
    (the telltale sign of an 8192-token truncation that json-repair salvaged
    only partially). Returns the best ``VideoMetadata`` we managed to get.

    We accept the first response that has at least one clip OR one quote; on
    full failure (or a raised ValueError such as an empty title) we retry, and
    if every try is degraded we return the best partial we saw so the MAIN
    video still gets a proper title/description even if shorts are skipped.
    """
    best = None
    last_exc = None
    for attempt in range(1, max_tries + 1):
        try:
            md = gemini.generate(
                srt_text_with_timestamps=with_ts,
                plain_text=plain,
                video_duration_seconds=duration,
                max_clips=cfg.shorts.get("max_clips_per_video", 3),
                min_clip_seconds=cfg.shorts.get("min_clip_seconds", 25),
                max_clip_seconds=cfg.shorts.get("max_clip_seconds", 58),
                max_title_length=cfg.ai_prompts.get("title_max_length", 75),
                max_hashtags=cfg.ai_prompts.get("max_hashtags", 10),
                include_chapters=cfg.ai_prompts.get("include_chapters", True),
                content_context=cfg.ai_prompts.get("content_context", ""),
                description_template_hint=cfg.ai_prompts.get("description_template_hint", ""),
                shorts_selection_hint=cfg.ai_prompts.get("shorts_selection_hint", ""),
                speaker_honorifics=(cfg.channel_branding or {}).get("speaker_honorifics", []) or [],
            )
        except Exception as e:  # empty title, unrepairable JSON, etc.
            last_exc = e
            log.warning("Gemini generate attempt %d/%d failed: %s",
                        attempt, max_tries, str(e)[:120])
            time.sleep(2)
            continue
        # keep the richest result seen so far (more clips, then more quotes)
        if best is None or (
            (len(md.important_clips), len(md.text_quotes))
            > (len(best.important_clips), len(best.text_quotes))
        ):
            best = md
        # Happy path: accept immediately only if the result is actually usable
        # — at least one clip OR quote AND a non-blank title. A result with a
        # blank/empty title is treated as degraded so it falls through to the
        # web-app fallback trigger below (we still keep it as `best`).
        if (md.important_clips or md.text_quotes) and (md.title or "").strip():
            if attempt > 1:
                log.info("Gemini recovered on attempt %d", attempt)
            return md
        log.warning("Gemini attempt %d/%d returned no usable result "
                    "(clips=%d quotes=%d title=%r) — retrying", attempt,
                    max_tries, len(md.important_clips), len(md.text_quotes),
                    (md.title or "")[:40])
        time.sleep(2)
    # ===== Web-app fallback trigger =====
    # The fast (happy) path already returned above the moment any API attempt
    # produced at least one clip OR one quote. We only reach here when the API
    # gave us nothing usable. Trigger the free Gemini WEB APP fallback when:
    #   (1) the failure looks like a rate-limit / quota exhaustion, OR
    #   (2) we have no API metadata at all (every attempt raised), OR
    #   (3) the best API result we got is effectively empty/degraded:
    #       a falsy/blank title, OR zero clips AND zero quotes.
    # The clips/quotes emptiness is the strongest signal of a degraded result.
    title = (getattr(best, "title", "") or "").strip() if best is not None else ""
    degraded = (
        best is None
        or not title
        or (len(best.important_clips) == 0 and len(best.text_quotes) == 0)
    )
    want_web_fallback = (
        (last_exc is not None and _is_rate_limit_error(last_exc))
        or degraded
    )

    if want_web_fallback:
        try:
            web_md = _generate_via_web_fallback(
                gemini, cfg, with_ts=with_ts, plain=plain, duration=duration)
        except Exception as web_exc:
            log.warning("Gemini WEB fallback also failed: %s", str(web_exc)[:200])
        else:
            # Prefer the richer result (more clips, then more quotes). Never
            # downgrade a good API result with a worse web result.
            if best is None or (
                (len(web_md.important_clips), len(web_md.text_quotes))
                > (len(best.important_clips), len(best.text_quotes))
            ):
                log.info("Using Gemini WEB fallback result over API "
                         "(web clips=%d quotes=%d vs api clips=%s quotes=%s)",
                         len(web_md.important_clips), len(web_md.text_quotes),
                         (len(best.important_clips) if best is not None else "-"),
                         (len(best.text_quotes) if best is not None else "-"))
                return web_md
            log.info("Gemini WEB fallback not richer than API result — "
                     "keeping API result")

    # Web fallback was not wanted, failed, or was not richer: fall back to the
    # best API partial we saw so the MAIN video still uploads. Only if we truly
    # have nothing do we raise.
    if best is not None:
        log.warning("Gemini degraded after %d tries — using best partial "
                    "(clips=%d quotes=%d)", max_tries,
                    len(best.important_clips), len(best.text_quotes))
        return best

    raise RuntimeError(f"Gemini failed after {max_tries} tries: {last_exc}")


def process_video(cfg, youtube, gemini, row, dry_run=False) -> dict:
    """Run the full multi-platform flow for one catalog row.

    Returns a result dict {ok, main_id, shorts, fb_main, tg_main, quotes, ...}.
    Raises on a fatal main-upload failure (so the row is marked failed and the
    local mp4 is NOT deleted).
    """
    yt_id = row["youtube_id"]
    series = (row["series"] or "").strip()
    title_orig = row["title"]
    mp4 = Path(row["mp4_path"])
    base = yt_id
    log.info("=== %s [%s] %s", yt_id, row["series_slug"], title_orig[:50])

    if not mp4.exists():
        raise FileNotFoundError(f"missing mp4 {mp4}")

    # ----- 1. SRT --------------------------------------------------------
    srt_text = fetch_srt(yt_id)
    has_srt = bool(srt_text)
    if not has_srt:
        log.warning("no SRT for %s — main video only (no shorts/quotes)", yt_id)

    # ----- 2. Gemini metadata + chapters + shorts + quotes ---------------
    # NOTE: gemini-2.5-flash caps output at 8192 tokens. For long lectures
    # (e.g. 80+ min) the full JSON (description + chapters + N clips + quotes)
    # sometimes overflows and gets truncated; json-repair then recovers only
    # the early fields (title) and silently drops clips/quotes/chapters.
    # generate() itself only retries on key-rotation errors, so we retry the
    # whole call here until we get a usable result (clips OR quotes present),
    # keeping the best partial result as a fallback.
    md = None
    if has_srt:
        duration = P._get_duration_from_srt(srt_text)
        plain = P._srt_plain_text(srt_text)
        with_ts = P._srt_with_simple_timestamps(srt_text)
        # SPLIT generation: 3 small Gemini calls (core / shorts / quotes) so no
        # single response hits the 8192-token cap and drops shorts/quotes on
        # long lectures. Falls back to the legacy single-call retry only if the
        # split itself can't even produce a title.
        try:
            md = _generate_split(
                gemini, cfg, with_ts=with_ts, plain=plain, duration=duration)
        except Exception as e:
            log.warning("split generation failed (%s) — falling back to "
                        "single-call retry", str(e)[:160])
            md = _generate_with_retry(
                gemini, cfg, with_ts=with_ts, plain=plain, duration=duration)
        # cap shorts at 3 (Gemini sometimes returns more); keeps quota ~6400/video
        _cap = cfg.shorts.get("max_clips_per_video", 3)
        if md and md.important_clips and len(md.important_clips) > _cap:
            md.important_clips = md.important_clips[:_cap]
        log.info("Gemini: title=%r  chapters=%d  shorts=%d  quotes=%d",
                 md.title, len(md.chapters), len(md.important_clips), len(md.text_quotes))

    if dry_run:
        return {
            "ok": True, "dry_run": True, "has_srt": has_srt,
            "title": (md.title if md else title_orig),
            "n_shorts": (len(md.important_clips) if md else 0),
            "n_quotes": (len(md.text_quotes) if md else 0),
        }

    paths = cfg.paths

    # If no SRT, fall back to a minimal title/description from the catalog title.
    if md is None:
        yt_title = title_orig[:100]
        full_description = title_orig
        tags = P.normalize_tags(list(cfg.youtube.get("default_tags", []) or []), max_count=30)
        raw_body_for_fb = title_orig
    else:
        yt_title = md.title
        full_description = md.description_with_chapters()
        yt_cta_footer = (
            "\n\n━━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔔 اشترك في قناتنا وفعّل الجرس ليصلك كل جديد:\n"
            "https://www.youtube.com/@abohafs.elaraby?sub_confirmation=1\n\n"
            "📱 تابعنا على باقي المنصات:\n"
            "▪ Facebook: https://www.facebook.com/profile.php?id=100910031570983\n"
            "▪ Instagram: https://www.instagram.com/abohafs.elaraby/\n"
            "▪ Telegram: https://t.me/abohafs_elaraby"
        )
        full_description = (full_description.rstrip() + yt_cta_footer)[:4900]
        tags = P.normalize_tags(
            md.hashtags + list(cfg.youtube.get("default_tags", []) or []), max_count=30)
        raw_body_for_fb = md.description

    # ----- 3. Intro prepend (MAIN only) ----------------------------------
    intro_tmp = Path(f"/tmp/with_intro_{row['id']}.mp4")
    upload_path = Path(prepend_intro(str(mp4), str(intro_tmp)))
    log.info("intro prepend -> %s", upload_path.name)

    # ----- 4. Upload MAIN to YouTube (private) ---------------------------
    main_id = upload_video(
        youtube, video_path=upload_path, title=yt_title,
        description=full_description, tags=tags,
        category_id=cfg.youtube.get("default_category_id", "27"),
        privacy_status="private", default_language="ar",
        made_for_kids=cfg.youtube.get("made_for_kids", False),
    )
    log.info("✓ MAIN uploaded: https://youtu.be/%s", main_id)
    _safe_unlink(intro_tmp)  # free the temp with-intro file

    # series playlist
    playlist_id = ""
    if series:
        try:
            pl_desc = f"سلسلة {series} — قناة نقال الخير"
            playlist_id = P._PLAYLIST_CACHE.get(series) or get_or_create_playlist(
                youtube, series[:150], pl_desc) or ""
            if playlist_id:
                P._PLAYLIST_CACHE[series] = playlist_id
                add_video_to_playlist(youtube, main_id, playlist_id)
                log.info("✓ playlist: %s (%s)", series, playlist_id)
        except Exception as e:
            log.warning("playlist warn: %s", e)

    parent = _Parent(video_id=main_id, series=series,
                     original_path=str(mp4), playlist_id=playlist_id,
                     original_name=mp4.name)

    yt_url = f"https://youtu.be/{main_id}"
    next_teaser = P._compute_next_teaser(cfg=cfg, current_video_path=mp4, series=series)

    result = {
        "ok": True, "has_srt": has_srt, "main_id": main_id,
        "title": yt_title, "playlist_id": playlist_id,
        "shorts": [], "fb_short_ids": [], "ig_short_ids": [],
        "tg_short_ids": [], "tg_quote_ids": [],
        "fb_main_id": None, "tg_main_id": None,
    }

    # ----- main thumbnail -------------------------------------------------
    try:
        thumb_path = Path(paths["output_thumbnails"]) / f"{base}.jpg"
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        # Prefer the branded professional template (fixed background + logo, only
        # the title changes). Fall back to the frame-grab thumbnail if it fails.
        try:
            if make_branded_thumbnail is None:
                raise RuntimeError("branded_thumbnail unavailable")
            make_branded_thumbnail(yt_title, str(thumb_path))
        except Exception as be:
            log.warning("branded thumbnail failed (%s) — falling back to frame grab", be)
            make_thumbnail(video_path=mp4, title=yt_title, output_path=thumb_path,
                           config=cfg.thumbnail)
        set_thumbnail(youtube, main_id, thumb_path)
        log.info("✓ thumbnail uploaded")
        result["_thumb_path"] = str(thumb_path)
    except Exception as e:
        log.warning("thumbnail warn: %s", e)

    # ----- 5/6 main video to Facebook ------------------------------------
    try:
        result["fb_main_id"] = P._publish_full_video_to_fb(
            cfg=cfg, video_path=mp4, title=yt_title, body=raw_body_for_fb,
            series=series, next_video_teaser=next_teaser)
    except Exception as e:
        log.warning("FB main warn: %s", e)

    # Final visibility of the long lecture. Default "public"; set
    # youtube.long_video_privacy="unlisted" to keep long re-uploads off the
    # public feed (they get ~0 views and drag channel signals) while still
    # reachable by link so the Shorts' "watch full lecture" still works.
    _long_priv = cfg.youtube.get("long_video_privacy", "public")
    if _long_priv not in ("public", "unlisted"):
        _long_priv = "public"
    try:
        youtube.videos().update(
            part="status",
            body={"id": main_id, "status": {
                "privacyStatus": _long_priv, "selfDeclaredMadeForKids": False}},
        ).execute()
        log.info("✓ MAIN set %s", _long_priv)
    except Exception as e:
        log.warning("set visibility warn: %s", e)

    # main Telegram announcement (text + YouTube link)
    try:
        result["tg_main_id"] = P._publish_full_video_to_telegram(
            cfg=cfg, video_path=mp4, title=yt_title,
            description=full_description, yt_url=yt_url,
            series=series, next_video_teaser=next_teaser)
    except Exception as e:
        log.warning("TG main warn: %s", e)

    # ----- 5. Shorts: cut + ENQUEUE (no inline posting) ------------------
    # DECOUPLED: we cut the 3 shorts into a PERSISTENT dir and enqueue them in
    # the catalog_shorts table. scripts/shorts_dispatcher.py posts them later,
    # paced across the day (IG-capped). We do NOT call _upload_shorts_for_video
    # here anymore. The actual INSERT happens in main() (which owns the db conn);
    # here we record the cut metadata into the result.
    _slug = (row.get("series_slug") if isinstance(row, dict) else row["series_slug"]) if row is not None else ""
    if _slug in NO_SHORTS_SLUGS:
        log.info("shorts skipped for sensitive series %s (full video only)", _slug)
        md.important_clips = [] if md is not None else None
    if md is not None and cfg.shorts.get("enabled", True) and md.important_clips and _slug not in NO_SHORTS_SLUGS:
        # write the SRT to disk so burn-in / clip-cropping can use it
        srt_path = Path(paths["output_srt"]) / f"{base}.srt"
        srt_path.parent.mkdir(parents=True, exist_ok=True)
        srt_path.write_text(srt_text, encoding="utf-8")
        # persistent per-parent dir; files kept until the dispatcher posts them
        shorts_dir = CATALOG_SHORTS_DIR / main_id
        shorts_dir.mkdir(parents=True, exist_ok=True)
        cut_paths = []
        try:
            cut_paths = cut_all_shorts(
                video_path=mp4, clips=md.important_clips, output_dir=shorts_dir,
                base_name=base, srt_path=srt_path,
                aspect=cfg.shorts.get("output_aspect", "9:16"),
                burn_subtitles=cfg.shorts.get("burn_subtitles", False),
                subtitle_font_size=cfg.shorts.get("subtitle_font_size", 48),
                word_level_captions=cfg.shorts.get("word_level_captions", False),
                caption_font_name=cfg.shorts.get("caption_font_name", "Noto Sans Arabic"),
            )
            log.info("✓ cut %d shorts -> %s", len(cut_paths), shorts_dir)
        except Exception as e:
            log.warning("shorts cut warn: %s", e)

        # Hand the cut data back to main() to enqueue (main() owns the db conn).
        result["_shorts_to_enqueue"] = {
            "clips": md.important_clips,
            "cut_paths": [str(p) for p in cut_paths],
            "parent_url": yt_url,
            "series": series,
            "next_teaser": next_teaser,
        }
        result["_shorts_dir"] = str(shorts_dir)
        result["_srt_path"] = str(srt_path)

    # ----- 6. Telegram text quotes (3-5) ---------------------------------
    if md is not None and md.text_quotes:
        try:
            tg_cfg = cfg.get("telegram", default={}) or {}
            if tg_cfg.get("enabled", False) and tg_cfg.get("publish_text_quotes", True):
                target = int(tg_cfg.get("quotes_count", 4))
                quotes = list(md.text_quotes)[:max(0, target)]
                delay = float(tg_cfg.get("delay_between_posts_seconds", 3))
                speaker = (cfg.channel_branding.get("speaker_short")
                           or cfg.channel_branding.get("speaker_title") or "")
                for i, q in enumerate(quotes, 1):
                    mid = P._publish_quote_to_telegram(
                        cfg=cfg, quote=q, series=series,
                        parent_yt_url=yt_url, speaker=speaker)
                    if mid:
                        result["tg_quote_ids"].append(mid)
                    if i < len(quotes) and delay > 0:
                        time.sleep(delay)
                log.info("✓ posted %d TG quotes", len(result["tg_quote_ids"]))
        except Exception as e:
            log.warning("TG quotes warn: %s", e)

    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", help="series_slug")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = Config(str(ROOT / "config.json"))
    rotation_keys = cfg.get("gemini_api_keys_rotation") or []
    gemini = GeminiGenerator(
        api_key=cfg.gemini_api_key,
        api_keys=rotation_keys if rotation_keys else None,
    )

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    ensure_shorts_table(con)  # catalog_shorts queue (Part A)

    q = ("SELECT * FROM catalog_videos WHERE download_status='downloaded' "
         "AND process_status='pending'")
    p: list = []
    if args.series:
        q += " AND series_slug=?"
        p.append(args.series)
    q += " ORDER BY series_slug, ord, id"
    if args.limit:
        q += f" LIMIT {int(args.limit)}"
    rows = cur.execute(q, p).fetchall()
    print(f"ready: {len(rows)}")

    if args.dry_run:
        for r in rows[:30]:
            print(f"  [{r['series_slug']}] ord={r['ord']} {r['youtube_id']}  {r['title'][:50]}")
        # show one Gemini sample if there is at least one row with SRT
        if rows:
            r = rows[0]
            res = process_video(cfg, None, gemini, r, dry_run=True)
            print(f"  sample: srt={res['has_srt']} title={res.get('title','')[:60]!r} "
                  f"shorts={res.get('n_shorts')} quotes={res.get('n_quotes')}")
        con.close()
        return
    if not rows:
        con.close()
        return

    youtube = get_youtube_service(
        ROOT / "credentials" / "client_secret.json",
        ROOT / "credentials" / "token.json")

    used = 0
    ok = fail = 0
    for r in rows:
        if used + QUOTA_PER_FULL > DAILY_BUDGET:
            print(f"  budget reached (~{used}), stopping")
            break
        try:
            res = process_video(cfg, youtube, gemini, r)
            # shorts are no longer posted inline — only the MAIN upload consumed
            # quota here (each short consumes its own quota in the dispatcher).
            used += QUOTA_PER_UPLOAD

            # Enqueue the cut shorts for the paced dispatcher (Part A).
            enq = res.get("_shorts_to_enqueue")
            n_enqueued = 0
            if enq and enq.get("cut_paths"):
                try:
                    n_enqueued = enqueue_shorts(
                        con,
                        parent_catalog_id=r["id"],
                        parent_yt_id=res.get("main_id"),
                        parent_url=enq["parent_url"],
                        series=enq["series"],
                        next_teaser=enq["next_teaser"],
                        clips=enq["clips"],
                        cut_paths=enq["cut_paths"],
                    )
                except Exception as e:
                    log.warning("enqueue shorts warn: %s", e)
            res["n_enqueued_shorts"] = n_enqueued

            # persist produced ids in a compact JSON blob in download_error
            # (re-purposed as a free-form notes column) + reupload_yt_id
            ids_blob = json.dumps({
                "main_id": res.get("main_id"),
                "shorts_enqueued": n_enqueued,
                "fb_main_id": res.get("fb_main_id"),
                "tg_main_id": res.get("tg_main_id"),
                "tg_quote_ids": res.get("tg_quote_ids"),
                "has_srt": res.get("has_srt"),
            }, ensure_ascii=False)[:1000]

            cur.execute(
                "UPDATE catalog_videos SET process_status='uploaded', "
                "download_status='archived', reupload_yt_id=?, download_error=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (res.get("main_id"), ids_blob, r["id"]))
            con.commit()
            ok += 1
            log.info("✓ DONE %s -> %s (shorts_enqueued=%d quotes=%d)",
                     r["youtube_id"], res.get("main_id"),
                     n_enqueued, len(res.get("tg_quote_ids", [])))

            # ----- 7. cleanup local files (only after full success) -------
            # Delete the big MAIN mp4 to free disk. KEEP the small short mp4s in
            # CATALOG_SHORTS_DIR — the dispatcher deletes each after posting it.
            _safe_unlink(Path(r["mp4_path"]))
            _safe_unlink(Path(res["_thumb_path"])) if res.get("_thumb_path") else None
        except Exception as e:
            cur.execute(
                "UPDATE catalog_videos SET process_status='failed', "
                "download_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(e)[:300], r["id"]))
            con.commit()
            fail += 1
            log.error("✗ %s: %s", r["youtube_id"], str(e)[:120])
        time.sleep(2)

    print(f"\ndone: ok={ok} fail={fail} quota_used~{used}")
    con.close()


if __name__ == "__main__":
    main()
