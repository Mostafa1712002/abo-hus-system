"""Backfill: ربط الـ Shorts المرفوعة سابقاً بالفيديو الأم.

YouTube Data API v3 لا يدعم endScreens/cards. الطريقة المتاحة:
  1. تعديل الوصف ليبدأ برابط الفيديو الأم (يظهر كـ chip في Shorts).
  2. نشر تعليق من صاحب القناة فيه نفس الرابط.

السكربت بيقرأ الـ shorts من قاعدة البيانات (metadata.short_video_ids)،
وبيمرّ على كل short ويستدعي link_short_to_main.

Usage:
    python fix_shorts_link_parent.py            # all shorts in DB
    python fix_shorts_link_parent.py --dry-run  # show plan only
    python fix_shorts_link_parent.py --main R3b57poudYE  # one parent
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import Config
from src.youtube_uploader import get_youtube_service, link_short_to_main

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("fix_shorts_link_parent")


def collect_shorts_from_db(db_path: Path) -> list[dict]:
    """Return [{'main_id', 'main_title', 'short_ids': [...]}, ...]"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT video_id, title, series, metadata_json "
        "FROM videos WHERE metadata_json IS NOT NULL"
    ).fetchall()
    conn.close()

    out = []
    for r in rows:
        try:
            md = json.loads(r["metadata_json"])
        except Exception:
            continue
        sids = md.get("short_video_ids") or []
        if sids:
            out.append({
                "main_id": r["video_id"],
                "main_title": (r["title"] or "")[:80],
                "series": r["series"] or "",
                "short_ids": list(sids),
            })
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="لا تعدّل، اعرض الخطة فقط.")
    parser.add_argument("--main", default="",
                        help="افلتر على main_id واحد فقط.")
    parser.add_argument("--no-comment", action="store_true",
                        help="لا تنشر تعليق، حدّث الوصف فقط.")
    args = parser.parse_args()

    cfg = Config(str(PROJECT_ROOT / "config.json"))
    db_rel = cfg.get("persistence", "db_path", default="data/abuhafs.db")
    db_path = PROJECT_ROOT / db_rel
    if not db_path.exists():
        logger.error("مفيش قاعدة بيانات في: %s", db_path)
        return 1

    groups = collect_shorts_from_db(db_path)
    if args.main:
        groups = [g for g in groups if g["main_id"] == args.main]

    total_shorts = sum(len(g["short_ids"]) for g in groups)
    logger.info("هيتم ربط %d short لـ %d فيديو أم", total_shorts, len(groups))
    for g in groups:
        logger.info(
            "  - الأم %s | %s | shorts=%s",
            g["main_id"], g["main_title"], g["short_ids"],
        )

    if args.dry_run:
        logger.info("--dry-run، خرجنا.")
        return 0

    client_secret = PROJECT_ROOT / cfg.youtube.get(
        "client_secret_file", "credentials/client_secret.json"
    )
    token_file = PROJECT_ROOT / cfg.youtube.get(
        "token_file", "credentials/token.json"
    )

    youtube = get_youtube_service(client_secret, token_file)

    success = 0
    failed = 0
    for g in groups:
        for sid in g["short_ids"]:
            try:
                res = link_short_to_main(
                    youtube, short_id=sid, main_id=g["main_id"],
                    add_comment=not args.no_comment,
                )
                if res["description_updated"] or res["comment_id"]:
                    success += 1
                    logger.info(
                        "✓ %s -> %s | desc=%s | comment=%s",
                        sid, g["main_id"],
                        "updated" if res["description_updated"]
                        else f"skipped({res['description_skipped_reason']})",
                        res["comment_id"] or f"none({res['comment_error']})",
                    )
                else:
                    success += 1  # no-op = already linked, still count
                    logger.info(
                        "= %s -> %s | desc=%s | comment=%s (already linked)",
                        sid, g["main_id"],
                        res["description_skipped_reason"],
                        res["comment_error"] or "skipped",
                    )
            except Exception as e:
                failed += 1
                logger.error("✗ فشل %s: %s", sid, e)

    logger.info("خلصنا. نجح=%d فشل=%d", success, failed)
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
