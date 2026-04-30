"""Transcode all .rmvb/.rm files in a series folder to .mp4.

Standalone CLI tool. Walks ``E:\\فضيلة الشيخ أبي حفص\\مرئيات\\<series>\\``
recursively and converts every ``.rmvb``/``.rm`` to a 1280x720 H.264 + AAC
``.mp4`` next to the source. Idempotent: any file that already has a sibling
``.mp4`` with the same stem is skipped.

Usage::

    python transcode_rmvb.py --series "شرح الرسالة"
    python transcode_rmvb.py --all
    python transcode_rmvb.py --series "شرح الرسالة" --dry-run
    python transcode_rmvb.py --series "شرح الرسالة" --delete-rmvb
    python transcode_rmvb.py --file "E:\\...\\الرسالة 7.rmvb"
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from tqdm import tqdm

# Ensure UTF-8 stdout/stderr on Windows so Arabic filenames print correctly.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

# Make `from src...` imports work when run from project root.
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.log_setup import setup_logging  # noqa: E402

setup_logging("transcode.log")

# ---------------------------------------------------------------- config
LOCAL_ROOT = Path(r"E:\فضيلة الشيخ أبي حفص\مرئيات")
SOURCE_EXTS = {".rmvb", ".rm"}
TARGET_EXT = ".mp4"

# ffmpeg / ffprobe resolution. Prefer PATH; fall back to the well-known
# winget install location (Gyan.FFmpeg).
_WINGET_FFMPEG_DIR = (
    Path(os.environ.get("LOCALAPPDATA", r"C:\Users\aldaa\AppData\Local"))
    / "Microsoft" / "WinGet" / "Packages"
    / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
    / "ffmpeg-8.1-full_build" / "bin"
)


def _resolve_tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    candidate = _WINGET_FFMPEG_DIR / f"{name}.exe"
    if candidate.exists():
        return str(candidate)
    raise RuntimeError(
        f"{name} not found on PATH or in {_WINGET_FFMPEG_DIR}. "
        f"Install ffmpeg (e.g. `winget install Gyan.FFmpeg`)."
    )


FFMPEG = None  # resolved lazily so --help works without ffmpeg installed
FFPROBE = None


def ffmpeg() -> str:
    global FFMPEG
    if FFMPEG is None:
        FFMPEG = _resolve_tool("ffmpeg")
    return FFMPEG


def ffprobe() -> str:
    global FFPROBE
    if FFPROBE is None:
        FFPROBE = _resolve_tool("ffprobe")
    return FFPROBE


logger = logging.getLogger("transcode_rmvb")


# ---------------------------------------------------------------- helpers
def discover_sources(root: Path) -> list[Path]:
    """All .rmvb/.rm files under ``root`` (recursive), sorted by name."""
    if not root.exists():
        return []
    out = [
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in SOURCE_EXTS
    ]
    out.sort(key=lambda p: (str(p.parent), p.name.lower()))
    return out


def already_transcoded(src: Path) -> Path | None:
    """Return the sibling .mp4 with the same stem if it exists, else None."""
    target = src.with_suffix(TARGET_EXT)
    return target if target.exists() else None


def transcode_one(src: Path, dst: Path) -> tuple[bool, str, int]:
    """Run ffmpeg to produce ``dst`` from ``src`` atomically.

    Writes to ``dst.tmp`` first, then ``os.replace`` to ``dst`` on success.
    Returns ``(ok, message, returncode)``. The returncode is 0 on success
    or whatever ffmpeg exited with on failure; ``-1`` is reserved for
    pre-flight failures (binary missing, output empty, rename fails).
    """
    tmp = dst.with_suffix(dst.suffix + ".tmp")
    if tmp.exists():
        try:
            tmp.unlink()
        except OSError:
            pass

    vf = (
        "scale=1280:720:force_original_aspect_ratio=decrease:flags=lanczos,"
        "pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    )
    cmd = [
        ffmpeg(),
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-stats",
        "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "22",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        "-f", "mp4",  # tmp filename ends in .tmp; force mp4 muxer explicitly
        str(tmp),
    ]
    logger.debug("ffmpeg cmd: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            check=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as e:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        # Last 500 chars of merged stderr/stdout — enough to see the
        # actual ffmpeg error without spamming the log.
        full = (e.stdout or "")
        tail500 = full[-500:]
        return False, "ffmpeg failed (rc=%d): %s" % (e.returncode, tail500), e.returncode
    except FileNotFoundError as e:
        return False, str(e), -1

    if not tmp.exists() or tmp.stat().st_size == 0:
        return False, "ffmpeg produced no output", -1

    # Atomic publish.
    try:
        os.replace(tmp, dst)
    except OSError as e:
        return False, f"rename failed: {e}", -1

    _ = proc  # quiet linters
    return True, "ok", 0


def ffprobe_summary(path: Path) -> str:
    """One-line summary: WxH codec audio_codec faststart?"""
    try:
        out = subprocess.run(
            [
                ffprobe(),
                "-v", "error",
                "-show_entries",
                "stream=index,codec_type,codec_name,width,height,sample_rate:format=format_name",
                "-of", "default=noprint_wrappers=1",
                str(path),
            ],
            check=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ).stdout
    except subprocess.CalledProcessError as e:
        return f"ffprobe failed: {e.stdout}"
    return out.strip()


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--series", help="Transcode files inside this series folder")
    g.add_argument("--all", action="store_true",
                   help="Transcode .rmvb/.rm across ALL series under LOCAL_ROOT")
    g.add_argument("--file", help="Transcode just this single .rmvb/.rm file")
    ap.add_argument("--dry-run", action="store_true",
                    help="Only list what would be transcoded; don't run ffmpeg")
    ap.add_argument("--delete-rmvb", action="store_true",
                    help="Delete the .rmvb/.rm source after a successful transcode")
    ap.add_argument("--keep-rmvb", action="store_true",
                    help="(default) Keep the original .rmvb/.rm next to the .mp4")
    args = ap.parse_args()

    # Logging is initialized at import time via setup_logging().
    logger.info(
        "args: series=%s all=%s file=%s dry_run=%s delete_rmvb=%s",
        args.series, args.all, args.file, args.dry_run, args.delete_rmvb,
    )

    # --- discover sources
    if args.file:
        src_path = Path(args.file)
        if not src_path.exists():
            logger.error("file not found: %s", src_path)
            return 2
        if src_path.suffix.lower() not in SOURCE_EXTS:
            logger.error("not a .rmvb/.rm file: %s", src_path)
            return 2
        sources = [src_path]
    elif args.series:
        target = LOCAL_ROOT / args.series
        if not target.exists():
            logger.error("series not found: %s", target)
            return 2
        sources = discover_sources(target)
    else:  # --all
        if not LOCAL_ROOT.exists():
            logger.error("local root not found: %s", LOCAL_ROOT)
            return 2
        sources = discover_sources(LOCAL_ROOT)

    if not sources:
        logger.info("No .rmvb/.rm files found.")
        return 0

    # --- plan
    todo: list[Path] = []
    skipped: list[Path] = []
    total_src_bytes = 0
    for s in sources:
        total_src_bytes += s.stat().st_size
        if already_transcoded(s) is not None:
            skipped.append(s)
        else:
            todo.append(s)

    logger.info(
        "Found %d .rmvb/.rm file(s) (%.2f GB total source).",
        len(sources), total_src_bytes / 1e9,
    )
    logger.info("  to transcode: %d", len(todo))
    logger.info("  already have .mp4 sibling: %d", len(skipped))
    if args.dry_run:
        logger.info("DRY RUN — would create %d mp4 file(s):", len(todo))
        for s in todo:
            logger.info(
                "  %s  ->  %s  (%.1f MB src)",
                s.relative_to(LOCAL_ROOT),
                s.with_suffix(TARGET_EXT).name,
                s.stat().st_size / 1e6,
            )
        if skipped:
            logger.info("Already transcoded (%d):", len(skipped))
            for s in skipped:
                logger.info("  %s", s.relative_to(LOCAL_ROOT))
        return 0

    if not todo:
        logger.info("Nothing to do — every source already has a .mp4 sibling.")
        return 0

    # --- transcode loop
    ok_count = 0
    fail_count = 0
    bytes_in = 0
    bytes_out = 0
    t0 = time.time()
    bar = tqdm(total=len(todo), unit="file", desc="transcode", leave=True)
    for src in todo:
        dst = src.with_suffix(TARGET_EXT)
        in_size = src.stat().st_size
        bar.set_postfix_str(src.name[:40])
        logger.info(
            "=== Transcoding '%s' (%.1f MB input) ===",
            src.name, in_size / 1e6,
        )
        st = time.time()
        ok, msg, rc = transcode_one(src, dst)
        dt = time.time() - st
        if ok:
            out_size = dst.stat().st_size
            bytes_in += in_size
            bytes_out += out_size
            ok_count += 1
            logger.info(
                "  OK %s: %.1f MB -> %.1f MB in %.1fs (rc=%d)",
                src.name, in_size / 1e6, out_size / 1e6, dt, rc,
            )
            if args.delete_rmvb:
                # Verify the output looks sane before deleting source.
                try:
                    if out_size > 0:
                        src.unlink()
                        logger.info("      removed source %s", src.name)
                except OSError as e:
                    logger.warning("      couldn't delete source: %s", e)
        else:
            fail_count += 1
            logger.error(
                "  FAIL %s after %.1fs (rc=%d): %s",
                src.name, dt, rc, msg,
            )
        bar.update(1)
    bar.close()

    elapsed = time.time() - t0
    logger.info("=== Done in %.1f min ===", elapsed / 60)
    logger.info(
        "Transcoded: %d OK, %d failed.", ok_count, fail_count,
    )
    if bytes_in:
        logger.info(
            "Source bytes: %.2f GB  ->  Output bytes: %.2f GB (%.0f%% of source).",
            bytes_in / 1e9, bytes_out / 1e9,
            100 * bytes_out / max(bytes_in, 1),
        )
    return 0 if fail_count == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
