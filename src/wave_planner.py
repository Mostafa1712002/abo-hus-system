"""Wave-based upload planner.

Classifies series folders into upload waves based on folder modification time
so the user can roll out a backlog in chronologically meaningful chunks.

Wave boundaries (folder mtime):
  Wave 1: pre-2025  (oldest backlog, mostly pre-2018 material)
  Wave 2: 2025-01-01 .. 2025-09-30
  Wave 3: 2025-10-01 onwards (newest)
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import List

from .cold_storage import ColdStorage

logger = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".wmv", ".flv", ".mov", ".m4v", ".webm",
                    ".mpg", ".mpeg", ".ts", ".rmvb", ".rm", ".3gp", ".vob", ".ogv"}

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


def get_series_in_wave_with_cold_fallback(cfg, wave: int) -> List[str]:
    """Return series names belonging to the given wave.

    Tries the local ``videos_input`` folder first; if it is empty or
    missing, falls back to listing the cold-storage server via SSH.

    Returns a list of *series names* (str). Local hits return the folder
    basename; cold-storage hits return the top-level remote directory
    name. When falling back to cold storage, wave classification is
    skipped (we can't easily get folder mtimes over ssh) — all series
    are returned and the caller is expected to filter or process all.
    """
    try:
        local_root = Path(cfg.paths["videos_input"])
    except Exception:
        local_root = None

    local_results: List[Path] = []
    if local_root and local_root.exists():
        local_results = get_series_in_wave(local_root, wave)
    if local_results:
        return [p.name for p in local_results]

    cold = ColdStorage.from_config(cfg)
    if not cold.enabled:
        return []
    try:
        remote_series = cold.list_remote_series()
    except Exception as e:
        logger.warning(f"cold-storage: list_remote_series failed: {e}")
        return []
    if not remote_series:
        return []
    logger.info(
        f"wave-planner: local empty, falling back to cold storage "
        f"({len(remote_series)} series)"
    )
    return sorted(remote_series)


def find_videos_in_series_with_cold_fallback(cfg, series_name: str) -> List[str]:
    """Return paths (str) for videos in a series, falling back to cold storage.

    Local paths are returned as absolute strings; cold-storage paths are
    returned as the remote absolute path so the pipeline can later call
    ``ColdStorage.ensure_local`` on them.
    """
    try:
        local_root = Path(cfg.paths["videos_input"])
    except Exception:
        local_root = None

    if local_root and (local_root / series_name).exists():
        return [str(p) for p in find_videos_in_series(local_root / series_name)]

    cold = ColdStorage.from_config(cfg)
    if not cold.enabled:
        return []
    rels = cold.list_remote_videos(series=series_name)
    if not rels:
        return []
    rels.sort(key=lambda r: (_natural_key(str(Path(r).parent)),
                             _natural_key(Path(r).name)))
    root = (cold.remote_root or "").rstrip("/")
    return [f"{root}/{rel}" for rel in rels]
