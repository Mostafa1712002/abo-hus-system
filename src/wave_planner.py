"""Wave-based upload planner.

Classifies series folders into upload waves based on folder modification time
so the user can roll out a backlog in chronologically meaningful chunks.

Wave boundaries (folder mtime):
  Wave 1: pre-2025  (oldest backlog, mostly pre-2018 material)
  Wave 2: 2025-01-01 .. 2025-09-30
  Wave 3: 2025-10-01 onwards (newest)
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import List

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".wmv", ".flv", ".mov", ".m4v", ".webm"}

# Boundary timestamps used to classify folders into waves.
WAVE2_START = datetime(2025, 1, 1)
WAVE3_START = datetime(2025, 10, 1)


def classify_wave(folder_path: Path) -> int:
    """Return the wave (1, 2, or 3) for a series folder based on its mtime.

    - mtime year < 2025                    -> Wave 1
    - 2025-01-01 <= mtime <  2025-10-01    -> Wave 2
    - mtime >= 2025-10-01                  -> Wave 3
    """
    mtime = datetime.fromtimestamp(folder_path.stat().st_mtime)
    if mtime < WAVE2_START:
        return 1
    if mtime < WAVE3_START:
        return 2
    return 3


def get_series_in_wave(input_root: Path, wave: int) -> List[Path]:
    """Return subfolders of `input_root` whose wave matches, sorted alphabetically."""
    if not input_root.exists():
        return []
    matching: List[Path] = []
    for child in input_root.iterdir():
        if not child.is_dir():
            continue
        try:
            if classify_wave(child) == wave:
                matching.append(child)
        except OSError:
            # If we can't stat the folder for any reason, skip it.
            continue
    matching.sort(key=lambda p: p.name)
    return matching


def _natural_key(name: str):
    """Split a name into a tuple of (kind, value) pairs for natural sort.

    Each chunk is tagged (0, int) for digit runs and (1, str) for text so the
    key remains comparable across names whose digit/text patterns differ.
    """
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


def find_videos_in_series(series_path: Path) -> List[Path]:
    """Return video files inside `series_path`, sorted naturally by filename.

    Natural sort means "01", "02", ..., "10" sort in numeric order rather than
    lexicographic order ("1", "10", "2", ...). Subfolders are walked recursively
    so multi-part series with nested folders are handled too.
    """
    if not series_path.exists():
        return []
    videos = [
        p for p in series_path.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
    ]
    videos.sort(key=lambda p: (_natural_key(str(p.parent)), _natural_key(p.name)))
    return videos
