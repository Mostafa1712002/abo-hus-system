"""Reorder a YouTube playlist so MAIN lessons sit in natural order
(by lesson number in the source file's path) and each main is followed
by its associated Shorts.

We classify items by:
  - MAIN  : the video_id appears in the pending-tracker map (id -> path)
  - SHORT : the description's first youtu.be URL points to a known main
  - ?     : everything else (deleted videos, manual uploads predating
            the pipeline) — those are appended at the end in their
            current order.

Each `playlistItems.update` costs 50 quota; we skip items already in
their target position so the average pipeline run pays close to zero.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

_YT_URL_RE = re.compile(r"https?://youtu\.be/([A-Za-z0-9_-]{11})")


def natural_key(name: str):
    """Sort key that treats embedded digit runs as integers."""
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
    """video_id -> original_path, from the JSON pending-tracker file."""
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


def _list_items(yt, playlist_id: str) -> List[dict]:
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


def _fetch_descriptions(yt, video_ids: Iterable[str]) -> Dict[str, str]:
    ids = [v for v in video_ids if v]
    out: Dict[str, str] = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i : i + 50]
        resp = yt.videos().list(part="snippet", id=",".join(chunk)).execute()
        for item in resp.get("items", []):
            out[item["id"]] = item["snippet"].get("description", "")
    return out


def _parent_id(description: str) -> Optional[str]:
    m = _YT_URL_RE.search(description or "")
    return m.group(1) if m else None


def reorder_playlist(
    yt,
    playlist_id: str,
    main_paths: Dict[str, str],
    *,
    dry_run: bool = False,
) -> int:
    """Reorder ``playlist_id`` in place. Returns number of items moved.

    Order produced:
        [main_1] [shorts_of_main_1...] [main_2] [shorts_of_main_2...] ... [orphans...]
    where mains are sorted by ``natural_key(main_paths[video_id])``.
    """
    items = _list_items(yt, playlist_id)
    if not items:
        return 0

    mains = [
        it for it in items
        if it["snippet"]["resourceId"].get("videoId", "") in main_paths
    ]
    others = [
        it for it in items
        if it["snippet"]["resourceId"].get("videoId", "") not in main_paths
    ]

    mains.sort(key=lambda it: natural_key(
        main_paths.get(it["snippet"]["resourceId"]["videoId"], "")
    ))
    main_pos = {
        it["snippet"]["resourceId"]["videoId"]: i for i, it in enumerate(mains)
    }

    descs = _fetch_descriptions(
        yt, (it["snippet"]["resourceId"].get("videoId", "") for it in others)
    )

    by_parent: Dict[str, List[dict]] = {}
    orphans: List[dict] = []
    for it in others:
        vid = it["snippet"]["resourceId"].get("videoId", "")
        parent = _parent_id(descs.get(vid, ""))
        if parent and parent in main_pos:
            by_parent.setdefault(parent, []).append(it)
        else:
            orphans.append(it)

    # Stable order for shorts within a parent: keep current playlist order
    for k in by_parent:
        by_parent[k].sort(key=lambda it: it["snippet"].get("position", 9999))
    orphans.sort(key=lambda it: it["snippet"].get("position", 9999))

    final: List[dict] = []
    for main in mains:
        final.append(main)
        for short_it in by_parent.get(
            main["snippet"]["resourceId"]["videoId"], []
        ):
            final.append(short_it)
    final.extend(orphans)

    moves = 0
    for new_pos, item in enumerate(final):
        cur_pos = item["snippet"].get("position", -1)
        if cur_pos == new_pos:
            continue
        moves += 1
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
            logger.warning(
                f"playlist-reorder: failed to move item {item.get('id')}: {e}"
            )
    return moves
