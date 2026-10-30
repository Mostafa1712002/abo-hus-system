"""Re-encode a lecture video to small H.264/AAC before upload (saves the home uplink).

The archive is 320x240 MPEG-4 AVI (~456 kbps) and RealVideo RMVB (~250 kbps);
x264 CRF 27 gives ~90-125 kbps with no visible loss on these talking-head frames,
and runs at 100-200x realtime. YouTube re-encodes anyway.
"""
import subprocess
from pathlib import Path


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def stream_duration(path, kind):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", f"{kind}:0",
                        "-show_entries", "stream=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def has_video(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return "video" in r.stdout


def compress(src, dst):
    """Write dst (.mp4). Returns False (dst untouched) when src has no video or the
    encode fails/truncates — the caller then ships the original file."""
    dst = Path(dst)
    if dst.exists() and dst.stat().st_size > 1_000_000:
        return True
    if not has_video(src):
        return False
    tmp = dst.with_name(dst.stem + ".tmp.mp4")
    dst.parent.mkdir(parents=True, exist_ok=True)
    # -v fatal: old AVIs carry damaged frames; ffmpeg exits non-zero on those but
    # still writes the whole lecture, so success is judged by the output duration.
    r = subprocess.run(["ffmpeg", "-v", "fatal", "-nostats", "-y", "-i", str(src),
                        "-map", "0:v:0", "-map", "0:a:0?",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
                        "-c:a", "aac", "-b:a", "64k", "-ac", "1",
                        "-movflags", "+faststart", str(tmp)])
    d_in, d_out = duration(src), duration(tmp) if tmp.exists() else 0.0
    # 35 archive AVIs have a damaged MP3 track that decodes to a fraction of the lecture
    a_out = stream_duration(tmp, "a") if tmp.exists() else 0.0
    if d_out < 60 or (d_in > 0 and d_out < d_in * 0.97) or abs(a_out - d_out) > d_out * 0.03:
        print(f"  !! compress failed ({r.returncode}, {d_in:.0f}s -> {d_out:.0f}s, audio {a_out:.0f}s): {Path(src).name}")
        tmp.unlink(missing_ok=True)
        return False
    tmp.rename(dst)
    return True
