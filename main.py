"""YouTube Auto Uploader - Two Phase Flow.

Commands:
  upload <video>           - ارفع فيديو واحد
  upload-batch --series X  - ارفع batch
  process                  - عالج الـ pending
  status                   - شوف الحالة
  watch                    - مراقبة دورية
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from src.config import Config
from src.pending_tracker import PendingTracker
from src.pipeline import (
    _parse_publish_times,
    get_next_publish_time,
    get_pending_path,
    get_series_name,
    get_youtube,
    process_pending,
    upload_phase1,
)
from src.wave_planner import (
    _natural_key,
    classify_wave,
    find_videos_in_series,
    get_series_in_wave,
)

console = Console()


def setup_logging(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[
            RichHandler(console=console, rich_tracebacks=True, show_path=False),
            logging.FileHandler(log_dir / "uploader.log", encoding="utf-8"),
        ],
    )


def find_videos(input_dir: Path, scan_subfolders: bool = False) -> list:
    extensions = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".wmv", ".flv",
                  ".mpg", ".mpeg", ".ts", ".rmvb", ".rm", ".3gp", ".vob", ".ogv"}
    if scan_subfolders:
        files = [p for p in input_dir.rglob("*") if p.is_file() and p.suffix.lower() in extensions]
    else:
        files = [p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in extensions]
    # If both <stem>.rmvb (or .rm) and <stem>.mp4 exist, prefer the .mp4 —
    # we transcode .rmvb→.mp4 for quality and the .mp4 is the canonical version.
    by_stem: dict[tuple[str, str], Path] = {}
    for p in files:
        key = (str(p.parent), p.stem)
        old = by_stem.get(key)
        if old is None:
            by_stem[key] = p
            continue
        # Prefer .mp4 over legacy formats
        legacy = {".rmvb", ".rm"}
        if old.suffix.lower() in legacy and p.suffix.lower() not in legacy:
            by_stem[key] = p
        elif p.suffix.lower() in legacy and old.suffix.lower() not in legacy:
            pass  # keep old
    deduped = list(by_stem.values())
    return sorted(deduped, key=lambda p: (_natural_key(str(p.parent)), _natural_key(p.name)))


def transcode_to_mp4(src: Path, dst: Path) -> bool:
    """Transcode a low-quality source (e.g. .rmvb) to high-quality .mp4.

    Filters: hqdn3d denoising + slight unsharp mask + scale-and-pad to 1280x720.
    Encoding: libx264 medium / CRF 20 / High profile / 192k AAC.

    Returns True on success. The output is written atomically via a .tmp.mp4
    intermediate, so a Ctrl+C / crash won't leave a half-written .mp4 lying
    around to be uploaded.
    """
    import subprocess
    if dst.exists() and dst.stat().st_size > 1024 * 1024:
        return True
    tmp = dst.with_suffix(".tmp.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf", (
            "hqdn3d=4:3:6:4.5,"
            "unsharp=5:5:0.7:5:5:0.0,"
            "scale=1280:720:force_original_aspect_ratio=decrease,"
            "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,"
            "setsar=1"
        ),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
        "-f", "mp4",
        str(tmp),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode == 0 and tmp.exists() and tmp.stat().st_size > 1024 * 1024:
        tmp.replace(dst)
        return True
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass
    return False


def cmd_upload(cfg: Config, video_path: Path):
    if not video_path.exists():
        console.print(f"[red]الفيديو غير موجود: {video_path}[/]")
        sys.exit(1)
    series = ""
    try:
        input_root = Path(cfg.paths["videos_input"])
        series = get_series_name(video_path, input_root)
    except Exception:
        pass
    result = upload_phase1(video_path=video_path, cfg=cfg, series=series)
    console.print(f"[green]✓ تم رفع: {result['url']}[/]")
    console.print(f"   ينشر في: {result.get('publish_at') or 'غير مجدول'}")
    console.print("[yellow]→ بعد ~30-60 دقيقة شغل: python main.py process[/]")


def cmd_upload_batch(cfg: Config, series: str = "", limit: int = 1, day_offset: int = 0):
    input_dir = Path(cfg.paths["videos_input"])
    scan_sub = cfg.get("paths", "scan_subfolders", default=False)
    if series:
        target_dir = input_dir / series
        if not target_dir.exists() or not any(target_dir.iterdir()):
            # Workspace missing/empty — try to extract from cold-storage zip.
            from src.cold_storage import ColdStorage
            cold = ColdStorage.from_config(cfg)
            if cold.enabled and (cold.type or "").lower() == "zipped":
                try:
                    zip_path = cold._fetch_zip_if_needed(series)
                    target_dir.mkdir(parents=True, exist_ok=True)
                    import zipfile, shutil
                    with zipfile.ZipFile(zip_path, "r") as zf:
                        for member in zf.infolist():
                            if member.is_dir():
                                continue
                            name = Path(member.filename).name
                            dest = target_dir / name
                            if dest.exists():
                                continue
                            with zf.open(member) as src, dest.open("wb") as dst:
                                shutil.copyfileobj(src, dst, length=1024 * 1024)
                    console.print(f"[green]✓ تم استخراج {series} من الـ zip[/]")
                except Exception as e:
                    console.print(f"[red]السلسلة غير موجودة: {target_dir}[/]")
                    console.print(f"[red]وفشل استخراجها من cold-storage: {e}[/]")
                    sys.exit(1)
            else:
                console.print(f"[red]السلسلة غير موجودة: {target_dir}[/]")
                sys.exit(1)
        videos = find_videos(target_dir, scan_subfolders=False)
    else:
        videos = find_videos(input_dir, scan_subfolders=scan_sub)
    if not videos:
        console.print("[yellow]مفيش فيديوهات[/]")
        return
    tracker = PendingTracker(get_pending_path(cfg))
    uploaded_paths = {p.original_path for p in tracker.all()}
    # Also dedup against archived (completed/failed) entries by basename stem
    # so we don't re-upload the same lesson with a different extension/path
    # (e.g. "الرسالة 1.mp4" archived → skip "الرسالة 1.rmvb" extracted now).
    archived_stems: set[str] = set()
    archive_dir = Path(get_pending_path(cfg)).parent / "archive"
    if archive_dir.exists():
        import json as _json
        for f in archive_dir.glob("*.jsonl"):
            try:
                for line in f.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        d = _json.loads(line)
                        name = (d.get("original_name") or "").strip()
                        srs = (d.get("series") or "").strip()
                        if name and srs:
                            archived_stems.add(f"{srs}/{Path(name).stem}")
                    except _json.JSONDecodeError:
                        pass
            except OSError:
                pass

    def _is_already_uploaded(v: Path) -> bool:
        if str(v) in uploaded_paths:
            return True
        # Match by series+stem against archive
        try:
            v_series = v.parent.name
            stem_key = f"{v_series}/{v.stem}"
            if stem_key in archived_stems:
                return True
        except Exception:
            pass
        return False

    skipped = [v for v in videos if _is_already_uploaded(v)]
    if skipped:
        console.print(f"[yellow]هتم تخطي {len(skipped)} فيديو سبق رفعهم[/]")
        for v in skipped[:5]:
            console.print(f"  - {v.name}")
    videos = [v for v in videos if not _is_already_uploaded(v)]
    if limit:
        videos = videos[:limit]

    # Transcode .rmvb / .rm legacy formats to high-quality .mp4 before upload.
    # We pick MP4 because YouTube re-encodes from .rmvb sources poorly.
    legacy_exts = {".rmvb", ".rm"}
    transcoded_videos: list[Path] = []
    for v in videos:
        if v.suffix.lower() in legacy_exts:
            mp4 = v.with_suffix(".mp4")
            if not mp4.exists() or mp4.stat().st_size < 1024 * 1024:
                console.print(f"[cyan]→ transcoding {v.name} ({v.stat().st_size/1024/1024:.0f} MB) → {mp4.name}[/]")
                ok = transcode_to_mp4(v, mp4)
                if not ok:
                    console.print(f"[red]✗ فشل تحويل {v.name} — هترفعه كما هو[/]")
                    transcoded_videos.append(v)
                    continue
                console.print(f"[green]✓ تم التحويل: {mp4.stat().st_size/1024/1024:.0f} MB[/]")
            transcoded_videos.append(mp4)
        else:
            transcoded_videos.append(v)
    videos = transcoded_videos

    console.print(f"[green]هرفع {len(videos)} فيديو[/]")
    youtube = get_youtube(cfg)

    slots_per_day = max(1, len(_parse_publish_times(cfg.youtube)))
    existing_times: list[dt.datetime] = []
    for v in tracker.all():
        if v.publish_at and v.status not in ("completed", "failed"):
            try:
                existing_times.append(
                    dt.datetime.fromisoformat(str(v.publish_at).replace("Z", "+00:00"))
                )
            except Exception:
                pass

    for i, video in enumerate(videos):
        try:
            do = day_offset + (i // slots_per_day)
            si = i % slots_per_day
            publish_at = get_next_publish_time(
                cfg, day_offset=do, slot_index=si,
                existing_publish_times=existing_times,
            )
            existing_times.append(publish_at)
            console.print(f"\n[cyan]({i+1}/{len(videos)}) {video.name}[/]")
            console.print(f"   ينشر في: {publish_at.strftime('%Y-%m-%d %H:%M')}")
            series_name = get_series_name(video, input_dir)
            upload_phase1(video_path=video, cfg=cfg, publish_at=publish_at,
                          youtube=youtube, series=series_name)
        except Exception as e:
            console.print(f"[red]فشل: {e}[/]")
            logging.exception("تفاصيل:")


def cmd_upload_wave(cfg: Config, wave: int, videos_per_day: int = 1,
                    max_uploads: int = 5, start_day_offset: int = 0,
                    dry_run: bool = False):
    """Upload videos belonging to a given wave (1, 2, or 3).

    Series within the wave are iterated alphabetically; videos within each
    series are iterated naturally by filename. Already-uploaded videos
    (tracked via pending.json `original_path`) are skipped.
    """
    if wave not in (1, 2, 3):
        console.print(f"[red]wave لازم يكون 1 أو 2 أو 3 (وصل: {wave})[/]")
        sys.exit(1)
    if videos_per_day < 1:
        console.print("[red]videos-per-day لازم >= 1[/]")
        sys.exit(1)

    input_root = Path(cfg.paths["videos_input"])
    if not input_root.exists():
        console.print(f"[red]فولدر المرئيات غير موجود: {input_root}[/]")
        sys.exit(1)

    series_list = get_series_in_wave(input_root, wave)
    tracker = PendingTracker(get_pending_path(cfg))
    uploaded_paths = {p.original_path for p in tracker.all()}

    # Build a flat plan of (series, video) pairs in the iteration order.
    plan: list[tuple[Path, Path]] = []
    total_videos = 0
    already_uploaded = 0
    for series_path in series_list:
        for video in find_videos_in_series(series_path):
            total_videos += 1
            if str(video) in uploaded_paths:
                already_uploaded += 1
                continue
            plan.append((series_path, video))

    console.print(f"\n[bold cyan]Wave {wave}[/]")
    console.print(f"   عدد السلاسل في الـ wave: {len(series_list)}")
    console.print(f"   إجمالي الفيديوهات في السلاسل: {total_videos}")
    console.print(f"   اترفعت قبل كده: {already_uploaded}")

    remaining = len(plan)
    if max_uploads > 0:
        will_upload = min(max_uploads, remaining)
    else:
        will_upload = 0  # max=0 means just print the plan
    console.print(f"   باقي للرفع: {remaining}")
    console.print(f"   هيترفعوا في التشغيلة دي: {will_upload}\n")

    if not series_list:
        console.print("[yellow]مفيش سلاسل في الـ wave ده[/]")
        return

    # Print the schedule preview (limit preview length when not in dry-run).
    preview_count = will_upload if (max_uploads > 0 and not dry_run) else len(plan)
    if preview_count == 0 and dry_run:
        preview_count = len(plan)

    # Build list of currently-scheduled publish times so we don't double-book
    # any slot that's already taken by a previously-uploaded video.
    slots_per_day = max(1, len(_parse_publish_times(cfg.youtube)))
    existing_times: list[dt.datetime] = []
    for v in tracker.all():
        if v.publish_at and v.status not in ("completed", "failed"):
            try:
                existing_times.append(
                    dt.datetime.fromisoformat(str(v.publish_at).replace("Z", "+00:00"))
                )
            except Exception:
                pass

    # نخلّي الـ existing list منفصلة بين الـ preview و الـ upload عشان الـ preview
    # ما يأثرش على الترتيب الفعلي للرفع.
    preview_existing = list(existing_times)

    console.print("[bold]الخطة:[/]")
    last_series: Path | None = None
    shown = 0
    for idx, (series_path, video) in enumerate(plan):
        if shown >= preview_count and not dry_run:
            break
        if dry_run is False and max_uploads > 0 and shown >= max_uploads:
            break
        offset = start_day_offset + (idx // slots_per_day)
        slot_idx = idx % slots_per_day
        publish_at = get_next_publish_time(
            cfg, day_offset=offset, slot_index=slot_idx,
            existing_publish_times=preview_existing,
        )
        preview_existing.append(publish_at)
        if series_path != last_series:
            console.print(f"\n[magenta]► {series_path.name}[/]")
            last_series = series_path
        console.print(
            f"   [{publish_at.strftime('%Y-%m-%d %H:%M')}] {video.name}"
        )
        shown += 1

    if dry_run:
        console.print("\n[yellow]--dry-run: مفيش رفع فعلي[/]")
        return
    if max_uploads <= 0:
        console.print("\n[yellow]--max=0: مفيش رفع فعلي (خطة فقط)[/]")
        return
    if not plan:
        console.print("\n[yellow]مفيش فيديوهات جديدة للرفع[/]")
        return

    # Real upload phase.
    youtube = get_youtube(cfg)
    uploaded_count = 0
    for idx, (series_path, video) in enumerate(plan):
        if uploaded_count >= max_uploads:
            break
        offset = start_day_offset + (idx // slots_per_day)
        slot_idx = idx % slots_per_day
        try:
            publish_at = get_next_publish_time(
                cfg, day_offset=offset, slot_index=slot_idx,
                existing_publish_times=existing_times,
            )
            existing_times.append(publish_at)
            console.print(
                f"\n[cyan]({uploaded_count+1}/{will_upload}) {series_path.name} / {video.name}[/]"
            )
            console.print(f"   ينشر في: {publish_at.strftime('%Y-%m-%d %H:%M')}")
            upload_phase1(
                video_path=video, cfg=cfg, publish_at=publish_at,
                youtube=youtube, series=series_path.name,
            )
            uploaded_count += 1
        except Exception as e:
            console.print(f"[red]فشل: {e}[/]")
            logging.exception("تفاصيل:")

    console.print(f"\n[green]✓ اترفع {uploaded_count} فيديو من Wave {wave}[/]")


def cmd_process(cfg: Config, video_id: str = ""):
    result = process_pending(cfg, video_id=video_id or None)
    table = Table(title="نتائج المعالجة")
    table.add_column("الحالة")
    table.add_column("العدد")
    table.add_row("اتعالج", str(result.get("processed", 0)))
    table.add_row("مش جاهز لسه", str(result.get("not_ready", 0)))
    table.add_row("فشل", str(result.get("failed", 0)))
    console.print(table)


def cmd_status(cfg: Config):
    tracker = PendingTracker(get_pending_path(cfg))
    items = tracker.all()
    if not items:
        console.print("[yellow]مفيش فيديوهات في tracker[/]")
        return
    stats = tracker.stats()
    console.print(f"\n[bold cyan]الإحصائيات:[/] {stats}\n")
    table = Table(title="الفيديوهات")
    table.add_column("Status", width=12)
    table.add_column("Video ID", width=12)
    table.add_column("الاسم", width=40)
    table.add_column("checks")
    table.add_column("URL")
    for item in items[:50]:
        status_color = {
            "uploaded": "yellow", "processing": "cyan",
            "completed": "green", "failed": "red",
        }.get(item.status, "white")
        table.add_row(
            f"[{status_color}]{item.status}[/]",
            item.video_id,
            item.original_name[:40],
            str(item.captions_checked_count),
            f"https://youtu.be/{item.video_id}",
        )
    console.print(table)


def cmd_watch(cfg: Config):
    interval = int(cfg.scheduler.get("watch_folder_seconds", 300))
    console.print(f"[green]بدء المراقبة كل {interval}ث[/]")
    while True:
        try:
            console.print("\n[cyan]== فحص pending ==[/]")
            cmd_process(cfg)
            time.sleep(interval)
        except KeyboardInterrupt:
            console.print("[yellow]تم الإيقاف[/]")
            break


def main():
    parser = argparse.ArgumentParser(description="YouTube Auto Uploader (Two-Phase)")
    parser.add_argument("--config", default="config.json")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_up = sub.add_parser("upload", help="ارفع فيديو واحد")
    p_up.add_argument("video", help="مسار الفيديو")
    p_batch = sub.add_parser("upload-batch", help="ارفع batch")
    p_batch.add_argument("--series", default="")
    p_batch.add_argument("--limit", type=int, default=1)
    p_batch.add_argument("--day-offset", type=int, default=0)
    p_wave = sub.add_parser("upload-wave", help="ارفع موجة كاملة من السلاسل")
    p_wave.add_argument("--wave", type=int, required=True, choices=[1, 2, 3])
    p_wave.add_argument("--videos-per-day", type=int, default=1)
    p_wave.add_argument("--max", type=int, default=5,
                        help="أقصى عدد للرفع في التشغيلة دي (0 = خطة فقط)")
    p_wave.add_argument("--start-day-offset", type=int, default=0)
    p_wave.add_argument("--dry-run", action="store_true",
                        help="اطبع الخطة بس من غير رفع فعلي")
    p_proc = sub.add_parser("process", help="عالج الـ pending")
    p_proc.add_argument("video_id", nargs="?", default="")
    sub.add_parser("status", help="حالة الفيديوهات")
    sub.add_parser("watch", help="مراقبة دورية")

    args = parser.parse_args()
    try:
        cfg = Config(args.config)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/]")
        sys.exit(1)
    setup_logging(Path(cfg.project_root) / "logs")

    if args.cmd == "upload":
        cmd_upload(cfg, Path(args.video))
    elif args.cmd == "upload-batch":
        cmd_upload_batch(cfg, args.series, args.limit, args.day_offset)
    elif args.cmd == "upload-wave":
        cmd_upload_wave(
            cfg,
            wave=args.wave,
            videos_per_day=args.videos_per_day,
            max_uploads=args.max,
            start_day_offset=args.start_day_offset,
            dry_run=args.dry_run,
        )
    elif args.cmd == "process":
        cmd_process(cfg, args.video_id)
    elif args.cmd == "status":
        cmd_status(cfg)
    elif args.cmd == "watch":
        cmd_watch(cfg)


if __name__ == "__main__":
    main()
