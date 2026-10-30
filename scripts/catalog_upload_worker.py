"""Catalog → YouTube API uploader (runs on VPS).

Takes catalog_videos rows that are download_status='downloaded' and
process_status='pending', generates Arabic metadata with Gemini (using the
catalog's own description_ar as rich context when available), uploads the mp4
via the YouTube Data API, adds it to the series playlist, and marks the row
process_status='uploaded' with the new reupload_yt_id.

Quota-aware: stops when the estimated daily upload quota is exhausted
(QUOTA_PER_UPLOAD≈1600; DAILY_BUDGET leaves room for the existing pipeline).

Usage:
  python catalog_upload_worker.py --series <slug> --limit N
  python catalog_upload_worker.py --limit 3            # next 3 ready (any series)
  python catalog_upload_worker.py --dry-run
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/abuhafs/youtube-auto-uploader")
sys.stdout.reconfigure(encoding="utf-8")

import google.generativeai as genai
from src.youtube_uploader import (
    get_youtube_service, upload_video, get_or_create_playlist, add_video_to_playlist,
)
# prepend the standard 3-sec intro to every catalog video before upload
sys.path.insert(0, "/opt/abuhafs/youtube-auto-uploader/scripts")
from intro_prepend import prepend_intro

ROOT = Path("/opt/abuhafs/youtube-auto-uploader")
DB = ROOT / "data" / "abuhafs.db"
DESC_DIR = Path("/opt/abuhafs/catalog_meta")   # optional cached descriptions from catalog DB
DAILY_BUDGET = 8000          # leave ~2000 for the شرح الرسالة pipeline
QUOTA_PER_UPLOAD = 1600

HONORIFICS = [
    "فضيلة الشيخ المحدث أبو حفص الأثري",
    "فضيلة الشيخ العلامة أبو حفص بن العربي",
    "الشيخ المحدث الأثري سامي بن العربي",
    "فضيلة الشيخ سامي العربي الأثري",
]

PROMPT = """أنت كاتب SEO عربي متخصص في المحتوى الإسلامي لقناة الشيخ سامي العربي.
أمامك محاضرة من سلسلة "{series}". العنوان الأصلي: "{orig_title}".
{ctx_block}
ولّد JSON بالظبط (بدون أي نص قبله أو بعده):
{{
  "title": "عنوان جذاب موحٍ بمضمون المحاضرة + يذكر اسم السلسلة ورقمها لو واضح، 60-90 حرف، يذكر الشيخ بلقب معتمد. ممنوع 'الشيخ' مجردة",
  "description": "وصف 250-450 كلمة: ترحيب باللقب، ملخص، 3-5 نقاط، دعوة اشتراك، 6-8 هاشتاجات",
  "tags": ["10-15 وسم عربي قصير مناسب"]
}}
الألقاب المعتمدة: {honorifics}"""


def gen_meta(api_key, series, orig_title, ctx):
    genai.configure(api_key=api_key)
    m = genai.GenerativeModel("gemini-2.5-flash",
        generation_config={"response_mime_type": "application/json", "temperature": 0.7})
    ctx_block = f"\nملخص/محتوى مرجعي للاستئناس:\n---\n{ctx[:8000]}\n---\n" if ctx else ""
    r = m.generate_content(PROMPT.format(series=series, orig_title=orig_title,
        ctx_block=ctx_block, honorifics=" / ".join(HONORIFICS)))
    return json.loads(r.text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    gem_keys = cfg.get("gemini_api_keys_rotation") or [cfg["gemini_api_key"]]

    con = sqlite3.connect(str(DB)); con.row_factory = sqlite3.Row
    cur = con.cursor()

    q = ("SELECT * FROM catalog_videos WHERE download_status='downloaded' "
         "AND process_status='pending'")
    p = []
    if args.series:
        q += " AND series_slug=?"; p.append(args.series)
    q += " ORDER BY series_slug, ord, id"
    if args.limit:
        q += f" LIMIT {args.limit}"
    rows = cur.execute(q, p).fetchall()
    print(f"ready to upload: {len(rows)}")
    if args.dry_run:
        for r in rows[:30]:
            print(f"  [{r['series_slug']}] {r['youtube_id']}  {r['title'][:50]}")
        return
    if not rows:
        return

    yt = get_youtube_service(ROOT / "credentials" / "client_secret.json",
                             ROOT / "credentials" / "token.json")
    used = 0
    ok = fail = 0
    for r in rows:
        if used + QUOTA_PER_UPLOAD > DAILY_BUDGET:
            print(f"  budget reached (~{used}), stopping"); break
        mp4 = Path(r["mp4_path"])
        if not mp4.exists():
            print(f"  ! missing file {mp4}, marking failed")
            cur.execute("UPDATE catalog_videos SET process_status='failed' WHERE id=?", (r["id"],))
            con.commit(); continue

        # rich context from catalog description if cached locally
        ctx = ""
        df = DESC_DIR / f"{r['youtube_id']}.txt"
        if df.exists():
            ctx = df.read_text(encoding="utf-8", errors="ignore")

        meta = None
        for k in gem_keys:
            try:
                meta = gen_meta(k, r["series"], r["title"], ctx); break
            except Exception as e:
                print(f"    gemini key failed: {str(e)[:80]}")
        if not meta:
            print("  ! gemini exhausted, skipping"); fail += 1; continue

        # prepend the standard 3-sec intro; falls back to the original mp4 if
        # the prepend fails for any reason (better to upload without intro).
        intro_tmp = Path(f"/tmp/with_intro_{r['id']}.mp4")
        upload_path = Path(prepend_intro(str(mp4), str(intro_tmp)))

        try:
            new_id = upload_video(
                yt, video_path=upload_path, title=meta["title"],
                description=meta.get("description", ""), tags=meta.get("tags", []),
                category_id="27", privacy_status="private", default_language="ar",
            )
            used += QUOTA_PER_UPLOAD
            # add to series playlist
            try:
                pl = get_or_create_playlist(yt, r["series"][:150])
                if pl:
                    add_video_to_playlist(yt, new_id, pl)
            except Exception as e:
                print(f"    playlist warn: {str(e)[:60]}")
            cur.execute(
                "UPDATE catalog_videos SET process_status='uploaded', reupload_yt_id=?, "
                "download_status='archived', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (new_id, r["id"]))
            con.commit(); ok += 1
            print(f"  ✓ {r['youtube_id']} -> https://youtu.be/{new_id}  {meta['title'][:45]}")
            # free disk: delete the downloaded source once safely uploaded
            try:
                mp4.unlink()
                print(f"    cleaned {mp4.name}")
            except Exception as e:
                print(f"    cleanup warn: {str(e)[:60]}")
        except Exception as e:
            cur.execute("UPDATE catalog_videos SET process_status='failed', "
                        "download_error=? WHERE id=?", (str(e)[:300], r["id"]))
            con.commit(); fail += 1
            print(f"  ✗ {r['youtube_id']}: {str(e)[:90]}")
        finally:
            # remove the temp with-intro file (never the original downloaded mp4)
            if upload_path != mp4 and intro_tmp.exists():
                try:
                    intro_tmp.unlink()
                except Exception:
                    pass
        time.sleep(2)

    print(f"\ndone: ok={ok} fail={fail} quota_used~{used}")
    con.close()


if __name__ == "__main__":
    main()
