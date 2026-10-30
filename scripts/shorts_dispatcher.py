#!/usr/bin/env python3
"""Paced dispatcher for catalog shorts (decoupled from catalog_full_worker.py).

catalog_full_worker.py cuts the 3 shorts per MAIN video and ENQUEUES them in
the ``catalog_shorts`` sqlite table (status='pending') instead of posting them
inline. This script is the consumer: a cron runs it every hour and it posts a
small batch (default 3) of the oldest pending shorts to every platform, paced
across the day so we never burst-post and stay under Instagram's ~25 posts/24h
Content-Publishing cap.

For ONE short it replicates the EXACT per-platform sequence + metadata of the
live pipeline's ``src.pipeline._upload_shorts_for_video`` (reusing its helpers
verbatim), namely:
  1. YouTube Short  — upload_short(...) with parent-URL-first description so
     YouTube renders the parent chip; then link_short_to_main + short thumbnail.
  2. Facebook Reel  — pipeline._publish_short_to_fb_reel (brand hashtags, teaser).
  3. Instagram Reel — pipeline._publish_short_to_ig_reel — BUT gated by a daily
     cap: if >=24 IG reels were posted in the last 24h we SKIP IG (mark
     ig_attempted=1, leave ig_reel_id NULL) and still do YT+FB+TG.
  4. Telegram       — pipeline._publish_short_to_telegram.

Per-platform failures are caught and logged, never abort the whole short
(mirrors _upload_shorts_for_video). A short is marked 'posted' once at least
YouTube succeeds (the file is then deleted). If nothing posts it's marked
'failed' and the file is kept for a later retry.

Usage:
  python scripts/shorts_dispatcher.py --limit 3
  python scripts/shorts_dispatcher.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sqlite3
import sys
from pathlib import Path

ROOT = Path("/opt/abuhafs/youtube-auto-uploader")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.config import Config
from src import pipeline as P  # reuse _publish_* / _build_* / link_short_to_main
from src.youtube_uploader import (
    get_youtube_service,
    upload_short,
    set_thumbnail,
    add_video_to_playlist,
)

DB = ROOT / "data" / "abuhafs.db"
POOL_FACTOR = 5               # review this many times --limit, post the best
IG_DAILY_CAP = 24            # IG Content Publishing API caps ~25 posts / 24h
YT_SHORT_QUOTA = 1600        # ~ per short upload (insert)
DAILY_BUDGET = 100000        # YT quota/day (raised 2026-06-19) — not tight

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("shorts_dispatcher")


# ---------------------------------------------------------------------------
def ensure_shorts_table(con: sqlite3.Connection) -> None:
    """Create the catalog_shorts queue table if missing (idempotent)."""
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
    for ddl in ("ALTER TABLE catalog_shorts ADD COLUMN review_verdict TEXT",
                "ALTER TABLE catalog_shorts ADD COLUMN review_score REAL",
                "ALTER TABLE catalog_shorts ADD COLUMN review_json TEXT"):
        try:
            con.execute(ddl)
        except sqlite3.OperationalError:
            pass          # column already there
    con.commit()


def review_short(row) -> dict:
    """Score a queued clip before posting. Falls back to 'near' when unreviewable."""
    mp4 = Path(row["mp4_path"] or "")
    if not mp4.exists():
        return {"verdict": "under", "score": 0.0, "notes": ["mp4 missing"]}
    words = mp4.with_suffix("").parent / f"{mp4.stem}.words.json"
    if not words.exists():
        # cut before word captions existed - no alignment data to judge it by
        return {"verdict": "near", "score": 6.0, "notes": ["no words.json (pre-caption cut)"]}
    try:
        out = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "review_short.py"), str(mp4), str(words)],
            capture_output=True, text=True, timeout=300, check=True)
        return json.loads(out.stdout)
    except Exception as e:
        return {"verdict": "near", "score": 6.0, "notes": [f"review failed: {e}"]}


def ig_posts_last_24h(con: sqlite3.Connection) -> int:
    """Count IG reels actually posted in the last 24h (ig_reel_id set + recent).

    Uses sqlite datetime math against posted_at (stored as datetime('now')).
    """
    row = con.execute(
        "SELECT COUNT(*) FROM catalog_shorts "
        "WHERE ig_reel_id IS NOT NULL "
        "AND posted_at IS NOT NULL "
        "AND posted_at >= datetime('now', '-24 hours')"
    ).fetchone()
    return int(row[0]) if row else 0


def _safe_unlink(p) -> None:
    try:
        if p and Path(p).exists():
            Path(p).unlink()
    except Exception:
        pass


# ---------------------------------------------------------------------------
def post_one_short(cfg: Config, youtube, con: sqlite3.Connection, row,
                   *, ig_allowed: bool) -> dict:
    """Post one queued short to all platforms. Returns the produced ids.

    Replicates pipeline._upload_shorts_for_video for a SINGLE short. Per-platform
    errors are caught/logged; a per-platform failure never aborts the short.
    """
    yt_cfg = cfg.youtube
    branding = cfg.channel_branding
    speaker = branding.get("speaker_title", "فضيلة الشيخ أبو حفص بن العربي الأثري")
    channel_name = branding.get("channel_name", "نقال الخير")

    short_path = Path(row["mp4_path"])
    series = (row["series"] or "").strip()
    parent_url = (row["parent_url"] or "").strip()
    parent_yt_id = (row["parent_yt_id"] or "").strip()
    next_teaser = (row["next_teaser"] or "").strip()
    idx = row["clip_index"]

    clip_desc = (row["description"] or "").strip()
    if not clip_desc:
        series_part = f" من سلسلة {series}" if series else ""
        clip_desc = (
            f"{(row['title'] or '').strip()} — مقطع مختار{series_part}\n"
            f"لمشاهدة المحاضرة كاملةً يُرجى متابعة الرابط في الأسفل."
        )

    out = {"yt": None, "fb": None, "ig": None, "tg": None, "ig_attempted": 0}

    if not short_path.exists():
        raise FileNotFoundError(f"short mp4 missing: {short_path}")

    # ===== 1) YouTube Short ==============================================
    # title (leave room for " #Shorts")
    raw_title = (row["title"] or "").strip()
    short_title = raw_title[:90].rstrip()

    description_parts = [
        f"شاهد المحاضرة كاملة: {parent_url}",
        "",
        clip_desc,
        "",
        f"قناة {channel_name} — {speaker}",
    ]
    if series:
        description_parts.append(f"السلسلة: {series}")
    full_description = "\n".join(description_parts)

    try:
        clip_tags = json.loads(row["tags_json"] or "[]")
    except Exception:
        clip_tags = []
    default_tags = list(yt_cfg.get("default_tags", []) or [])
    if not clip_tags:
        clip_tags = default_tags[:8]
    merged = P.normalize_tags(clip_tags + default_tags, max_count=30)

    short_id = upload_short(
        youtube=youtube,
        file_path=short_path,
        title=short_title,
        description=full_description,
        tags=merged,
        privacy="public",
        publish_at=None,
        category_id=yt_cfg.get("default_category_id", "27"),
        made_for_kids=yt_cfg.get("made_for_kids", False),
    )
    out["yt"] = short_id
    log.info("✓ YouTube Short #%s: %s", idx, short_id)

    # link short to main (description chip already set; add owner comment)
    try:
        link_result = P.link_short_to_main(
            youtube, short_id=short_id, main_id=parent_yt_id, add_comment=True,
        )
        log.info("✓ link_short_to_main #%s: desc_updated=%s comment=%s", idx,
                 link_result.get("description_updated"),
                 "yes" if link_result.get("comment_id") else "no")
    except Exception as e:
        log.warning("link short to main #%s failed: %s", idx, e)

    # custom short thumbnail (logo only, no text)
    try:
        logo_path = cfg.thumbnail.get("logo_path", "") if hasattr(cfg, "thumbnail") else ""
        if not logo_path:
            logo_path = str(cfg.project_root / "assets" / "logo.png")
        else:
            lp = Path(logo_path)
            if not lp.is_absolute():
                logo_path = str(cfg.project_root / lp)
        short_thumb_path = Path(short_path).with_suffix(".jpg")
        P.make_short_thumbnail(
            short_video_path=short_path,
            output_path=short_thumb_path,
            logo_path=logo_path,
        )
        set_thumbnail(youtube, short_id, short_thumb_path)
        log.info("✓ short thumbnail #%s uploaded", idx)
    except Exception as e:
        log.warning("short thumbnail #%s failed: %s", idx, e)

    # add short to the series playlist if we know it (best-effort, same as inline)
    playlist_id = P._PLAYLIST_CACHE.get(series, "") if series else ""
    if playlist_id:
        try:
            add_video_to_playlist(youtube, short_id, playlist_id)
        except Exception as e:
            log.warning("playlist add #%s failed: %s", idx, e)

    # ===== 2) Facebook Reel ==============================================
    try:
        fb_id = P._publish_short_to_fb_reel(
            cfg=cfg, short_path=short_path, short_description=clip_desc,
            series=series, next_video_teaser=next_teaser,
        )
        out["fb"] = fb_id
    except Exception as e:
        log.warning("FB Reel #%s failed: %s", idx, e)

    # ===== 3) Instagram Reel (gated by daily cap) ========================
    if ig_allowed:
        try:
            ig_caption = P._build_reel_caption(base_text=clip_desc, parent_url=parent_url)
            ig_id = P._publish_short_to_ig_reel(cfg=cfg, short_path=short_path, caption=ig_caption)
            out["ig"] = ig_id
            out["ig_attempted"] = 1
        except Exception as e:
            out["ig_attempted"] = 1
            log.warning("IG Reel #%s failed: %s", idx, e)
    else:
        out["ig_attempted"] = 1
        log.info("⏭ IG skipped for short #%s — daily IG cap reached", idx)

    # ===== 4) Telegram ===================================================
    try:
        tg_caption = P._build_telegram_caption(
            base_text=clip_desc, parent_url=parent_url, is_short=True,
        )
        tg_msg_id = P._publish_short_to_telegram(cfg=cfg, short_path=short_path, caption=tg_caption)
        out["tg"] = tg_msg_id
    except Exception as e:
        log.warning("Telegram short #%s failed: %s", idx, e)

    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=3, help="max shorts to post this run")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what WOULD be posted (incl. IG-cap decision), no posting")
    args = ap.parse_args()

    cfg = Config(str(ROOT / "config.json"))

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    ensure_shorts_table(con)

    # Review a pool, then post the best of it. Posting the oldest N unseen was how a clip
    # with captions that do not match its audio went out like any other.
    pool = con.execute(
        "SELECT * FROM catalog_shorts WHERE status='pending' "
        "ORDER BY created_at ASC, id ASC LIMIT ?",
        (int(args.limit) * POOL_FACTOR,),
    ).fetchall()

    scored = []
    for r in pool:
        rv = review_short(r)
        con.execute("UPDATE catalog_shorts SET review_verdict=?, review_score=?, review_json=? "
                    "WHERE id=?",
                    (rv.get("verdict"), rv.get("score"), json.dumps(rv, ensure_ascii=False), r["id"]))
        if rv.get("verdict") == "under":
            con.execute("UPDATE catalog_shorts SET status='rejected' WHERE id=?", (r["id"],))
            print(f"  ✗ rejected id={r['id']} score={rv.get('score')} {rv.get('notes')}")
            continue
        scored.append((rv.get("score", 0.0), r))
    con.commit()

    scored.sort(key=lambda x: -x[0])
    rows = [r for _, r in scored[:int(args.limit)]]
    if scored:
        print("  reviewed pool: " + ", ".join(f"id={r['id']}:{s}" for s, r in scored))

    ig_used = ig_posts_last_24h(con)
    ig_budget = max(0, IG_DAILY_CAP - ig_used)
    print(f"pending picked: {len(rows)}  |  IG posted last 24h: {ig_used}/{IG_DAILY_CAP}  "
          f"(remaining IG slots this window: {ig_budget})")

    if args.dry_run:
        ig_left = ig_budget
        for r in rows:
            ig_decision = "IG:POST" if ig_left > 0 else "IG:SKIP(cap)"
            if ig_left > 0:
                ig_left -= 1
            exists = Path(r["mp4_path"]).exists()
            print(f"  WOULD POST id={r['id']} parent={r['parent_yt_id']} "
                  f"clip#{r['clip_index']} -> YT+FB+TG + {ig_decision}  "
                  f"file_exists={exists}  title={ (r['title'] or '')[:45]!r }")
        con.close()
        return

    if not rows:
        print("nothing to do")
        con.close()
        return

    youtube = get_youtube_service(
        ROOT / "credentials" / "client_secret.json",
        ROOT / "credentials" / "token.json")

    ig_left = ig_budget
    used_quota = 0
    posted = failed = 0
    for r in rows:
        if used_quota + YT_SHORT_QUOTA > DAILY_BUDGET:
            print(f"  YT budget reached (~{used_quota}), stopping")
            break
        ig_allowed = ig_left > 0
        try:
            out = post_one_short(cfg, youtube, con, r, ig_allowed=ig_allowed)
            used_quota += YT_SHORT_QUOTA
            if out.get("ig"):
                ig_left -= 1  # consumed one IG slot
            if out.get("yt"):
                # success: record ids + posted_at, delete the file
                con.execute(
                    "UPDATE catalog_shorts SET status='posted', "
                    "yt_short_id=?, fb_reel_id=?, ig_reel_id=?, tg_msg_id=?, "
                    "ig_attempted=?, posted_at=datetime('now'), last_error=NULL "
                    "WHERE id=?",
                    (out["yt"], out["fb"], out["ig"], out["tg"],
                     out["ig_attempted"], r["id"]),
                )
                con.commit()
                posted += 1
                _safe_unlink(r["mp4_path"])
                _safe_unlink(r["thumb_path"])
                log.info("✓ POSTED short id=%s (yt=%s fb=%s ig=%s tg=%s)",
                         r["id"], out["yt"], out["fb"], out["ig"], out["tg"])
            else:
                # YT itself didn't return an id — treat as failure, keep file
                con.execute(
                    "UPDATE catalog_shorts SET status='failed', "
                    "ig_attempted=?, last_error=? WHERE id=?",
                    (out["ig_attempted"], "no YouTube id returned", r["id"]),
                )
                con.commit()
                failed += 1
                log.error("✗ short id=%s: no YouTube id (kept file)", r["id"])
        except Exception as e:
            con.execute(
                "UPDATE catalog_shorts SET status='failed', last_error=? WHERE id=?",
                (str(e)[:300], r["id"]),
            )
            con.commit()
            failed += 1
            log.error("✗ short id=%s failed: %s", r["id"], str(e)[:160])

    print(f"\ndone: posted={posted} failed={failed} ig_slots_left~{ig_left} "
          f"yt_quota_used~{used_quota}")
    con.close()


if __name__ == "__main__":
    main()
