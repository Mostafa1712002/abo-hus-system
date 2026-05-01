"""Reorder a YouTube playlist so main lessons are in natural order
(by lesson number in the source file path) and each main is followed
by its associated Shorts.

Main lessons are identified via the pending tracker (output/pending.json).
Shorts are linked to their parent via the URL the linker bot puts as the
first line of the description: "شاهد المحاضرة كاملة: https://youtu.be/<MAIN_ID>".
Any item we can't classify is appended at the end, preserving its current order.

Usage:
    python reorder_playlist.py "شرح الرسالة"
    python reorder_playlist.py --all
    python reorder_playlist.py --all --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

from src.config import Config
from src.youtube_uploader import get_youtube_service

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

_YT_URL_RE = re.compile(r"https?://youtu\.be/([A-Za-z0-9_-]{11})")


def _natural_key(name: str):
    parts = re.split(r"(\d+)", name or "")
    out = []
    for p in parts:
        if p == "":
            continue
        if p.isdigit():
            out.append((0, int(p)))
        else:
            out.append((1, p.lower()))
    return tuple(out)


def load_main_paths(tracker_path: Path) -> Dict[str, str]:
    """video_id -> original_path (only main videos in the tracker)."""
    if not tracker_path.exists():
        return {}
    with tracker_path.open(encoding="utf-8") as f:
        data = json.load(f)
    out: Dict[str, str] = {}
    for v in data:
        vid = v.get("video_id") or v.get("youtube_video_id")
        path = v.get("original_path", "")
        if vid:
            out[vid] = path
    return out


def find_playlist_id(yt, title_query: str) -> Optional[str]:
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
            part="id,snippet,contentDetails",
            playlistId=playlist_id,
            maxResults=50,
            pageToken=next_page,
        ).execute()
        items.extend(resp.get("items", []))
        next_page = resp.get("nextPageToken")
        if not next_page:
            break
    return items


def fetch_descriptions(yt, video_ids: List[str]) -> Dict[str, str]:
    """video_id -> description (batched, 50/call)."""
    out: Dict[str, str] = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i : i + 50]
        resp = yt.videos().list(part="snippet", id=",".join(chunk)).execute()
        for item in resp.get("items", []):
            out[item["id"]] = item["snippet"].get("description", "")
    return out


def parse_parent_id(description: str) -> Optional[str]:
    """First youtu.be URL in description = parent video (linker convention)."""
    m = _YT_URL_RE.search(description or "")
    return m.group(1) if m else None


def reorder(yt, playlist_id: str, main_paths: Dict[str, str],
            dry_run: bool = False) -> None:
    items = list_playlist_items(yt, playlist_id)
    logger.info(f"Playlist {playlist_id}: {len(items)} items")

    # Classify: main vs short.  Mains we already know via the tracker map.
    mains: List[dict] = []          # items whose video_id is a main
    others: List[dict] = []         # everything else (shorts, deleted, ...)
    for it in items:
        vid = it["snippet"]["resourceId"].get("videoId", "")
        if vid in main_paths:
            mains.append(it)
        else:
            others.append(it)

    # Sort mains by natural key on their original_path.
    mains.sort(key=lambda it: _natural_key(
        main_paths.get(it["snippet"]["resourceId"]["videoId"], "")
    ))
    main_id_to_pos = {
        it["snippet"]["resourceId"]["videoId"]: idx
        for idx, it in enumerate(mains)
    }

    # Resolve each "other"'s parent main, fall back to current position.
    other_ids = [it["snippet"]["resourceId"]["videoId"] for it in others]
    descs = fetch_descriptions(yt, other_ids) if other_ids else {}

    def others_key(it: dict):
        vid = it["snippet"]["resourceId"].get("videoId", "")
        parent = parse_parent_id(descs.get(vid, ""))
        # Sort by (parent's main index, then current position so order is stable)
        if parent and parent in main_id_to_pos:
            return (0, main_id_to_pos[parent], it["snippet"].get("position", 9999))
        # Unknown parent → push to very end, preserve current order
        return (1, it["snippet"].get("position", 9999), 0)

    others.sort(key=others_key)

    # Interleave: each main, then any "others" whose parent == that main.
    by_parent: Dict[str, List[dict]] = {}
    orphans: List[dict] = []
    for it in others:
        vid = it["snippet"]["resourceId"].get("videoId", "")
        parent = parse_parent_id(descs.get(vid, ""))
        if parent and parent in main_id_to_pos:
            by_parent.setdefault(parent, []).append(it)
        else:
            orphans.append(it)

    final_order: List[dict] = []
    for main in mains:
        final_order.append(main)
        mid = main["snippet"]["resourceId"]["videoId"]
        for short_it in by_parent.get(mid, []):
            final_order.append(short_it)
    final_order.extend(orphans)

    # Apply position updates.
    moves = 0
    for new_pos, item in enumerate(final_order):
        cur_pos = item["snippet"].get("position", -1)
        title = item["snippet"]["title"]
        vid = item["snippet"]["resourceId"].get("videoId", "")
        kind = "MAIN" if vid in main_paths else (
            "SHORT" if parse_parent_id(descs.get(vid, "")) else "?"
        )
        if cur_pos == new_pos:
            logger.info(f"  [{new_pos:>3}] {kind:5} {title}  (no move)")
            continue
        moves += 1
        logger.info(f"  [{cur_pos:>3} → {new_pos:>3}] {kind:5} {title}")
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
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = Config("config.json")
    yt = get_youtube_service(
        cfg.youtube["client_secret_file"], cfg.youtube["token_file"]
    )

    main_paths = load_main_paths(Path("output/pending.json"))
    logger.info(f"Loaded {len(main_paths)} main videos from tracker.")

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
        reorder(yt, pid, main_paths, dry_run=args.dry_run)

    return 0


if __name__ == "__main__":
    sys.exit(main())
