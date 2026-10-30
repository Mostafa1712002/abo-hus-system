"""Prepend a standard 3-second intro to a catalog video before upload.

The catalog videos come in mixed resolutions (1280x720, 320x240, ...) and
codecs, so a naive concat (-c copy or the concat demuxer) breaks. Instead we
normalize BOTH the intro and the main video to the MAIN video's
resolution/fps/codec and concat with the concat *filter* (full re-encode).

Design notes / resilience:
- The intro is scaled to the main video's WxH preserving aspect, then padded
  (letterbox) so it never distorts. The main lecture keeps its native quality.
- fps is forced to the main video's fps so the concat filter is happy.
- If the main video has no audio stream we synthesize silent audio for it; the
  intro always has audio. Both audio tracks are resampled to 44100/stereo.
- If anything fails, we return the ORIGINAL src path unchanged and log a
  warning — shipping the video without an intro beats failing the whole upload.

Public API:
    prepend_intro(src_mp4, out_mp4, intro="/opt/abuhafs/assets/intro.mp4") -> str
        Returns out_mp4 on success, or src_mp4 (unchanged) on any failure.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

DEFAULT_INTRO = "/opt/abuhafs/assets/intro.mp4"


def _log(msg: str) -> None:
    print(f"[intro_prepend] {msg}", file=sys.stderr, flush=True)


def _probe(path: str) -> dict:
    """Return {'width','height','fps','has_audio'} for the first video stream."""
    # NOTE: do NOT use text=True — some catalog files carry non-UTF-8 bytes in
    # their metadata that ffprobe echoes back, which would crash strict decode.
    raw = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format",
         "-of", "json", path],
        capture_output=True, check=True,
    ).stdout
    data = json.loads(raw.decode("utf-8", errors="replace"))
    streams = data.get("streams", [])
    width = height = 0
    fps = 30.0
    has_audio = False
    for s in streams:
        ct = s.get("codec_type")
        if ct == "video" and width == 0:
            width = int(s.get("width") or 0)
            height = int(s.get("height") or 0)
            rate = s.get("avg_frame_rate") or s.get("r_frame_rate") or "30/1"
            try:
                num, den = rate.split("/")
                den = float(den)
                fps = (float(num) / den) if den else 30.0
            except Exception:
                fps = 30.0
            if not fps or fps <= 0:
                fps = 30.0
        elif ct == "audio":
            has_audio = True
    if width <= 0 or height <= 0:
        raise RuntimeError(f"no video stream in {path}")
    # ensure even dimensions (libx264 / yuv420p requirement)
    width -= width % 2
    height -= height % 2
    # cap fps to a sane value
    if fps > 120:
        fps = 30.0
    return {"width": width, "height": height, "fps": round(fps, 3), "has_audio": has_audio}


def prepend_intro(src_mp4: str, out_mp4: str, intro: str = DEFAULT_INTRO) -> str:
    """Prepend `intro` to `src_mp4`, writing `out_mp4`. Returns the path to use
    for upload: `out_mp4` on success, or `src_mp4` unchanged on any failure."""
    src_mp4 = str(src_mp4)
    out_mp4 = str(out_mp4)
    intro = str(intro)
    try:
        if not Path(intro).exists():
            _log(f"intro missing ({intro}); using original")
            return src_mp4
        if not Path(src_mp4).exists():
            _log(f"src missing ({src_mp4}); using original")
            return src_mp4

        info = _probe(src_mp4)
        w, h, fps = info["width"], info["height"], info["fps"]
        main_has_audio = info["has_audio"]

        # Scale intro to fit WxH preserving aspect, then pad (letterbox) to WxH.
        # force_original_aspect_ratio=decrease + pad = no distortion, no crop.
        # use explicit :0 selectors — some catalog files carry a 2nd video
        # stream (embedded cover/thumbnail) that must not feed the concat.
        vintro = (
            f"[1:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps={fps},format=yuv420p[iv]"
        )
        vmain = f"[0:v:0]scale={w}:{h},setsar=1,fps={fps},format=yuv420p[mv]"

        cmd = ["ffmpeg", "-y"]
        filters = []

        if main_has_audio:
            # main has its own audio; intro audio comes from the intro file
            cmd += ["-i", src_mp4, "-i", intro]
            filters += [vmain, vintro]
            # normalize both audio streams to 44100 stereo
            filters.append("[1:a:0]aformat=sample_rates=44100:channel_layouts=stereo[ia]")
            filters.append("[0:a:0]aformat=sample_rates=44100:channel_layouts=stereo[ma]")
            concat = "[iv][ia][mv][ma]concat=n=2:v=1:a=1[v][a]"
        else:
            # main has no audio: synthesize silence for the main portion
            cmd += ["-i", src_mp4, "-i", intro,
                    "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
            filters += [vmain, vintro]
            filters.append("[1:a:0]aformat=sample_rates=44100:channel_layouts=stereo[ia]")
            # trim silent audio to the main video's duration via -shortest on concat output
            filters.append("[2:a]aformat=sample_rates=44100:channel_layouts=stereo[ma]")
            concat = "[iv][ia][mv][ma]concat=n=2:v=1:a=1[v][a]"

        filters.append(concat)
        cmd += ["-filter_complex", ";".join(filters),
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
                "-movflags", "+faststart"]
        if not main_has_audio:
            # stop when the shortest input (the real video+intro) ends so the
            # infinite anullsrc doesn't run forever
            cmd += ["-shortest"]
        cmd += [out_mp4]

        # NOTE: ffmpeg echoes the source's (possibly non-UTF-8) metadata into
        # stderr, so capture bytes and decode leniently — text=True would crash.
        proc = subprocess.run(cmd, capture_output=True)
        if proc.returncode != 0:
            err_tail = proc.stderr.decode("utf-8", errors="replace")[-400:]
            _log(f"ffmpeg failed (rc={proc.returncode}); using original. "
                 f"tail: {err_tail}")
            try:
                Path(out_mp4).unlink(missing_ok=True)
            except Exception:
                pass
            return src_mp4

        # sanity-check the output
        try:
            chk = _probe(out_mp4)
        except Exception as e:
            _log(f"output probe failed ({e}); using original")
            try:
                Path(out_mp4).unlink(missing_ok=True)
            except Exception:
                pass
            return src_mp4
        if not chk.get("has_audio"):
            _log("output missing audio; using original")
            try:
                Path(out_mp4).unlink(missing_ok=True)
            except Exception:
                pass
            return src_mp4

        _log(f"ok: {src_mp4} -> {out_mp4} ({w}x{h}@{fps})")
        return out_mp4

    except Exception as e:
        _log(f"unexpected error ({e}); using original")
        return src_mp4


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--intro", default=DEFAULT_INTRO)
    a = ap.parse_args()
    result = prepend_intro(a.src, a.out, a.intro)
    print(result)
