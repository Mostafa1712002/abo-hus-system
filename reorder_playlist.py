"""Manual reorder for an existing playlist (CLI shim).

The pipeline runs this automatically after every main upload, so this
script is only needed for ad-hoc backfills or for playlists not yet
under pipeline control.

Usage:
    python reorder_playlist.py "شرح الرسالة"
    python reorder_playlist.py --all
    python reorder_playlist.py --all --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.config import Config
from src.playlist_reorder import load_main_paths, reorder_playlist
from src.youtube_uploader import get_youtube_service

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def find_playlist_id(yt, title_query: str) -> str | None:
    next_page = None
    while True:
        resp = yt.playlists().list(
            part="id,snippet", mine=True, maxResults=50, pageToken=next_page,
        ).execute()
        for item in resp.get("items", []):
            if title_query in item["snippet"]["title"]:
                return item["id"]
        next_page = resp.get("nextPageToken")
        if not next_page:
            return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = Config("config.json")
    yt = get_youtube_service(
        cfg.youtube["client_secret_file"], cfg.youtube["token_file"],
    )

    main_paths = load_main_paths(Path("output/pending.json"))
    logger.info(f"Loaded {len(main_paths)} main videos from tracker.")

    if args.all:
        queries = ["الرسالة", "العبودية"]
    elif args.query:
        queries = [args.query]
    else:
        parser.error("provide query or --all")
        return 2

    for q in queries:
        pid = find_playlist_id(yt, q)
        if not pid:
            logger.warning(f"⚠ playlist for '{q}' not found")
            continue
        moves = reorder_playlist(yt, pid, main_paths, dry_run=args.dry_run)
        verb = "would be" if args.dry_run else "were"
        logger.info(f"=== {q} ({pid}): {moves} item(s) {verb} moved ===")

    return 0


if __name__ == "__main__":
    sys.exit(main())
