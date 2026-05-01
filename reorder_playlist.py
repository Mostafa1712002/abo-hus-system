"""Reorder a YouTube playlist by natural sort of item titles.

Usage:
    python reorder_playlist.py "شرح الرسالة"
    python reorder_playlist.py --all   # both الرسالة + العبودية
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from typing import List

from src.config import Config
from src.youtube_uploader import get_youtube_service

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _natural_key(name: str):
    parts = re.split(r"(\d+)", name)
    out = []
    for p in parts:
        if p == "":
            continue
        if p.isdigit():
            out.append((0, int(p)))
        else:
            out.append((1, p.lower()))
    return tuple(out)


def find_playlist_id(yt, title_query: str) -> str | None:
    next_page = None
    while True:
        resp = yt.playlists().list(
            part="id,snippet", mine=True, maxResults=50, pageToken=next_page
        ).execute()
        for item in resp.get("items", []):
            if title_query in item["snippet"]["title"]:
                return item["id"]
        next_page = resp.get("nextPageToken")
        if not next_page:
            return None


def list_playlist_items(yt, playlist_id: str) -> List[dict]:
    items: List[dict] = []
    next_page = None
    while True:
        resp = yt.playlistItems().list(
            part="id,snippet",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=next_page,
        ).execute()
        items.extend(resp.get("items", []))
        next_page = resp.get("nextPageToken")
        if not next_page:
            break
    return items


def reorder(yt, playlist_id: str, dry_run: bool = False) -> None:
    items = list_playlist_items(yt, playlist_id)
    logger.info(f"Playlist {playlist_id}: {len(items)} items")

    sorted_items = sorted(items, key=lambda i: _natural_key(i["snippet"]["title"]))

    moves = 0
    for new_pos, item in enumerate(sorted_items):
        cur_pos = item["snippet"].get("position", -1)
        title = item["snippet"]["title"]
        if cur_pos == new_pos:
            logger.info(f"  [{new_pos:>3}] {title}  (no move)")
            continue
        moves += 1
        logger.info(f"  [{cur_pos:>3} → {new_pos:>3}] {title}")
        if dry_run:
            continue
        try:
            yt.playlistItems().update(
                part="snippet",
                body={
                    "id": item["id"],
                    "snippet": {
                        "playlistId": item["snippet"]["playlistId"],
                        "resourceId": item["snippet"]["resourceId"],
                        "position": new_pos,
                    },
                },
            ).execute()
        except Exception as e:
            logger.error(f"  ✗ failed: {e}")
    logger.info(f"Done. {moves} item(s) {'would be' if dry_run else 'were'} moved.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="?", default="", help="Playlist title substring")
    parser.add_argument("--all", action="store_true", help="Reorder both الرسالة + العبودية")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = Config("config.json")
    yt = get_youtube_service(
        cfg.youtube["client_secret_file"], cfg.youtube["token_file"]
    )

    queries: List[str]
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
        logger.info(f"\n=== {q} === ({pid})")
        reorder(yt, pid, dry_run=args.dry_run)

    return 0


if __name__ == "__main__":
    sys.exit(main())
