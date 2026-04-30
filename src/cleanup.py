"""
Daily cleanup script — حذف الفيديوهات المكتملة + الملفات الوسيطة لتوفير مساحة القرص.

الـ behavior:
  - completed videos: لو publish_at عدى → احذف الـ original + intermediate files → archive
  - failed videos: لو > age_days → archive فقط (مش حذف للأصل)
  - الملفات اللي بنحتفظ بيها: output/metadata/<base>.json + output/thumbnails/<base>.jpg
  - أرشفة: output/archive/completed-YYYY-MM.jsonl (شهر لكل ملف)
  - بعد الأرشفة، الـ entry بيتشال من pending.json
"""
from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .cold_storage import ColdStorage
from .config import Config
from .pending_tracker import PendingTracker, PendingVideo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _project_root(cfg: Config) -> Path:
    return cfg.project_root


def _pending_path(cfg: Config) -> Path:
    return _project_root(cfg) / "output" / "pending.json"


def _archive_dir(cfg: Config) -> Path:
    p = _project_root(cfg) / "output" / "archive"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _archive_file_for(now: datetime, cfg: Config) -> Path:
    """ملف الأرشيف للشهر الحالي: completed-YYYY-MM.jsonl"""
    return _archive_dir(cfg) / f"completed-{now.strftime('%Y-%m')}.jsonl"


def _resolve_original_path(cfg: Config, original_path: str) -> Path | None:
    """يرجّع Path للفيديو الأصلي. لو absolute بيستخدمه كما هو، لو relative
    بيتحط جوة paths['videos_input']."""
    if not original_path:
        return None
    p = Path(original_path)
    if p.is_absolute():
        return p
    videos_root = cfg.paths.get("videos_input")
    if not videos_root:
        return p
    return Path(videos_root) / p


def _safe_size(p: Path) -> int:
    """حجم الملف بالبايت، أو 0 لو الملف مش موجود/مش قابل للوصول."""
    try:
        if p.exists() and p.is_file():
            return p.stat().st_size
    except OSError:
        return 0
    return 0


def _safe_delete(p: Path, dry_run: bool) -> tuple[bool, int]:
    """يحذف ملف لو موجود. بيرجع (deleted, size_bytes).
    لو الملف مش موجود → (False, 0) من غير error.
    لو الـ drive مش متاحة (E:\\) → (False, 0) + warning."""
    try:
        if not p.exists():
            return False, 0
        if not p.is_file():
            return False, 0
        size = _safe_size(p)
        if dry_run:
            return True, size
        p.unlink()
        return True, size
    except (OSError, PermissionError) as e:
        logger.warning(f"تعذر حذف الملف {p}: {e}")
        return False, 0


def _safe_iter_files(d: Path, patterns: Iterable[str]) -> list[Path]:
    """يجمع الملفات اللي ماتش الـ patterns جوة directory.
    بيرجع [] لو الـ directory مش موجودة أو مش قابلة للوصول."""
    out: list[Path] = []
    try:
        if not d.exists() or not d.is_dir():
            return []
        for pat in patterns:
            try:
                out.extend(p for p in d.glob(pat) if p.is_file())
            except OSError as e:
                logger.warning(f"تعذر استعراض {d} بـ {pat}: {e}")
    except OSError as e:
        logger.warning(f"تعذر الوصول لـ {d}: {e}")
    return out


def _publish_at_in_past(publish_at_iso: str) -> bool:
    """يرجّع True لو publish_at موجود وعدى. لو فاضي/غلط → False (احتياطاً)."""
    if not publish_at_iso:
        return False
    try:
        dt = datetime.fromisoformat(publish_at_iso)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= datetime.now(timezone.utc)


def _append_archive(cfg: Config, item: PendingVideo, reason: str,
                    dry_run: bool) -> Path:
    """يضيف entry لملف الأرشيف الشهري. بيرجع مسار الـ archive file."""
    now = datetime.now(timezone.utc)
    archive_path = _archive_file_for(now, cfg)
    if dry_run:
        return archive_path
    record = asdict(item)
    record["archived_at"] = now.isoformat()
    record["archive_reason"] = reason  # 'completed' | 'failed_old'
    line = json.dumps(record, ensure_ascii=False)
    with archive_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return archive_path


def _remove_from_pending(cfg: Config, video_ids: list[str], dry_run: bool) -> None:
    """يشيل الـ entries من pending.json بعد الأرشفة."""
    if dry_run or not video_ids:
        return
    tracker = PendingTracker(_pending_path(cfg))
    for vid in video_ids:
        tracker.remove(vid)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def cleanup_completed_videos(cfg: Config, dry_run: bool = False) -> dict:
    """
    لكل فيديو في pending.json بـ status='completed':
      - لو cleanup.delete_after_completion + publish_at عدى:
          احذف الفيديو الأصلي.
      - لو cleanup.delete_intermediate_files:
          احذف output/srt/<base>*.srt + output/shorts/<base>/*.{mp4,jpg}
          مع الاحتفاظ بـ output/metadata/<base>.json و output/thumbnails/<base>.jpg.
      - حرّك الـ entry لـ output/archive/completed-YYYY-MM.jsonl + شيله من pending.json.

    Returns: {'videos_deleted', 'space_freed_mb', 'archived', 'errors'}.
    """
    cleanup_cfg = cfg.get("cleanup", default={}) or {}
    delete_original = bool(cleanup_cfg.get("delete_after_completion", False))
    delete_intermediate = bool(cleanup_cfg.get("delete_intermediate_files", False))

    paths = cfg.paths
    srt_dir = Path(paths.get("output_srt", "output/srt"))
    shorts_dir = Path(paths.get("output_shorts", "output/shorts"))
    videos_input = Path(paths.get("videos_input", "")) if paths.get(
        "videos_input"
    ) else None

    tracker = PendingTracker(_pending_path(cfg))
    completed = [i for i in tracker.all() if i.status == "completed"]

    # Build a ColdStorage so we can delegate zip-cache cleanup back to it.
    cold = ColdStorage.from_config(cfg)
    is_zipped_mode = cold.enabled and (cold.type or "raw").lower() == "zipped"
    series_seen: set[str] = set()

    videos_deleted = 0
    bytes_freed = 0
    errors: list[str] = []
    archived_ids: list[str] = []

    for item in completed:
        try:
            base = Path(item.original_name).stem if item.original_name else ""
            local_freed = 0
            local_deleted_original = False

            # ===== الفيديو الأصلي =====
            if delete_original and _publish_at_in_past(item.publish_at):
                src = _resolve_original_path(cfg, item.original_path)
                if src is not None:
                    deleted, size = _safe_delete(src, dry_run=dry_run)
                    if deleted:
                        local_deleted_original = True
                        local_freed += size
                    elif src.exists() is False:
                        # الملف مش موجود — مفيش error، بس ما خصمناش حجم
                        pass

            # ===== الملفات الوسيطة =====
            if delete_intermediate and base:
                # SRT: <base>.srt + <base>.clip_*.srt
                for f in _safe_iter_files(srt_dir, [f"{base}.srt", f"{base}.clip_*.srt"]):
                    deleted, size = _safe_delete(f, dry_run=dry_run)
                    if deleted:
                        local_freed += size

                # Shorts: output/shorts/<base>/*.mp4 + *.jpg
                shorts_sub = shorts_dir / base
                for f in _safe_iter_files(shorts_sub, ["*.mp4", "*.jpg"]):
                    deleted, size = _safe_delete(f, dry_run=dry_run)
                    if deleted:
                        local_freed += size
                # حاول تشيل الـ subfolder الفاضية (لو خلت فاضية)
                if not dry_run:
                    try:
                        if shorts_sub.exists() and not any(shorts_sub.iterdir()):
                            shorts_sub.rmdir()
                    except OSError:
                        pass

            # ===== الأرشفة =====
            archive_path = _append_archive(cfg, item, reason="completed",
                                           dry_run=dry_run)
            archived_ids.append(item.video_id)

            if local_deleted_original:
                videos_deleted += 1
            bytes_freed += local_freed

            # ===== Zipped cold-storage: drop empty series dir + cached zip =====
            # We do this once per series (the zip is a single shared cache for
            # all videos in the series, so checking after every video would be
            # wasteful). We only act when the series dir has no remaining
            # video files — that's the strongest signal nothing else needs it.
            if is_zipped_mode and item.series and item.series not in series_seen:
                series_seen.add(item.series)
                if videos_input is not None:
                    series_dir = videos_input / item.series
                    if series_dir.exists() and series_dir.is_dir():
                        remaining_videos = [
                            p for p in series_dir.rglob("*")
                            if p.is_file() and p.suffix.lower() in (
                                ".mp4", ".mkv", ".avi", ".wmv",
                                ".flv", ".mov", ".m4v", ".webm",
                                ".mpg", ".mpeg", ".ts",
                                ".rmvb", ".rm", ".3gp", ".vob", ".ogv",
                            )
                        ]
                        if not remaining_videos and not dry_run:
                            # Try to drop the empty extracted dir.
                            try:
                                shutil_rm = __import__("shutil").rmtree
                                shutil_rm(series_dir)
                                logger.info(
                                    f"cleanup zipped: removed empty series "
                                    f"dir {series_dir}"
                                )
                            except OSError as e:
                                logger.warning(
                                    f"couldn't remove series dir {series_dir}: {e}"
                                )
                # Drop the cached zip if no pending work is left for the series.
                if not dry_run:
                    try:
                        cold.cleanup_zip_if_series_done(item.series)
                    except Exception as e:
                        logger.warning(
                            f"zip-cache cleanup failed for {item.series}: {e}"
                        )

            logger.info(
                f"cleanup completed: video_id={item.video_id} "
                f"deleted_original={local_deleted_original} "
                f"freed_bytes={local_freed} archive={archive_path}"
            )
        except Exception as e:  # noqa: BLE001
            msg = f"خطأ في {item.video_id}: {e}"
            logger.error(msg, exc_info=True)
            errors.append(msg)

    # شيل الـ entries المؤرشفة من pending.json
    _remove_from_pending(cfg, archived_ids, dry_run=dry_run)

    space_freed_mb = round(bytes_freed / (1024 * 1024), 2)
    return {
        "videos_deleted": videos_deleted,
        "space_freed_mb": space_freed_mb,
        "archived": len(archived_ids),
        "errors": errors,
        "dry_run": dry_run,
    }


def cleanup_failed_old_videos(cfg: Config, age_days: int = 14,
                              dry_run: bool = False) -> dict:
    """
    أرشفة الـ entries بـ status='failed' الأقدم من age_days.
    ما بنحذفش الفيديو الأصلي — المستخدم ممكن يحاول retry يدوياً.
    """
    tracker = PendingTracker(_pending_path(cfg))
    failed = [i for i in tracker.all() if i.status == "failed"]
    cutoff = datetime.now(timezone.utc).timestamp() - (age_days * 86400)

    archived_ids: list[str] = []
    errors: list[str] = []

    for item in failed:
        try:
            # نستخدم uploaded_at كـ "آخر نشاط" (مفيش failed_at field)
            ts_iso = item.uploaded_at or ""
            try:
                dt = datetime.fromisoformat(ts_iso) if ts_iso else None
            except ValueError:
                dt = None
            if dt is None:
                # مفيش timestamp واضح — نأرشف بدل ما نسيبه يكبر
                pass
            else:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt.timestamp() > cutoff:
                    continue  # لسه جديد

            archive_path = _append_archive(cfg, item, reason="failed_old",
                                           dry_run=dry_run)
            archived_ids.append(item.video_id)
            logger.info(
                f"cleanup failed: video_id={item.video_id} "
                f"archive={archive_path}"
            )
        except Exception as e:  # noqa: BLE001
            msg = f"خطأ في {item.video_id}: {e}"
            logger.error(msg, exc_info=True)
            errors.append(msg)

    _remove_from_pending(cfg, archived_ids, dry_run=dry_run)

    return {
        "archived": len(archived_ids),
        "errors": errors,
        "dry_run": dry_run,
        "age_days": age_days,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg = Config("config.json")
    if not cfg.get("cleanup", "enabled", default=False):
        print("cleanup disabled in config")
        sys.exit(0)
    result = cleanup_completed_videos(cfg)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    fail_result = cleanup_failed_old_videos(cfg, age_days=14)
    print("failed cleanup:", json.dumps(fail_result, ensure_ascii=False))
