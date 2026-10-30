"""
موديول قص الـ Shorts من الفيديو الرئيسي

بياخد المقاطع المهمة (من Gemini) ويحولها لفيديوهات shorts (9:16):
- يقص الجزء المطلوب
- يحوله من 16:9 لـ 9:16 (إما crop ذكي أو blur background)
- يضيف الترجمة (subtitles burned in) لو مطلوب
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from .ai_generator import ImportantClip
from .transcribe import Segment

logger = logging.getLogger(__name__)


def _check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError(
            "ffmpeg مش متثبت أو مش في PATH.\n"
            "حمله من: https://www.gyan.dev/ffmpeg/builds/ (لويندوز)"
        )


def cut_clip(
    video_path: str | Path,
    output_path: str | Path,
    start_seconds: float,
    end_seconds: float,
    aspect: str = "9:16",
    burn_subtitles: bool = False,
    srt_path: Optional[str | Path] = None,
    subtitle_font_size: int = 48,
    word_level_captions: bool = False,
    caption_font_name: str = "Noto Sans Arabic",
) -> Path:
    """
    يقص مقطع من الفيديو ويحوله لـ aspect ratio معين.
    """
    _check_ffmpeg()
    video_path = Path(video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    duration = end_seconds - start_seconds

    # video filter
    if aspect == "9:16":
        # Strategy: نعمل blurred background بنفس الفيديو، ونحط الفيديو الأصلي في النص
        # Blurred-pad, NOT centre crop. This channel's source is mostly static title
        # cards and book covers, so cropping to 9:16 slices the text off both sides
        # (verified on frames from 85tL9JbkXYw). Fit the whole 16:9 frame over a
        # blurred copy of itself so nothing is ever cut.
        vf = (
            "split=2[bg][fg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=20:5[bg];"
            "[fg]scale=1080:-2[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
    elif aspect == "1:1":
        vf = "scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080"
    else:
        vf = "scale=1280:720"

    # subtitles burn-in
    if burn_subtitles and srt_path:
        sub_filter = None
        if word_level_captions:
            # Forced alignment on the known catalog text gives a true onset per word, so
            # captions can change on the word instead of sitting in 3-5 s SRT blocks.
            from .word_captions import build_word_caption_filter
            sub_filter = build_word_caption_filter(
                media=Path(video_path), srt_path=Path(srt_path),
                clip_start=start_seconds, clip_end=end_seconds,
                out_dir=output_path.parent,
                font_name=caption_font_name, font_size=subtitle_font_size,
            )
        if sub_filter is None:   # aligner unavailable or failed -> plain SRT blocks
            sub_filter = _build_subtitle_filter(srt_path, subtitle_font_size,
                                                start_seconds, end_seconds)
        if sub_filter:
            vf = f"{vf},{sub_filter}"

    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start_seconds),
        "-i", str(video_path),
        "-t", str(duration),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]
    logger.info(f"قص short: {start_seconds:.1f}s → {end_seconds:.1f}s")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        logger.error(f"ffmpeg error: {result.stderr[-2000:]}")
        raise RuntimeError("فشل قص الـ short - شوف اللوج")

    return output_path


def _build_subtitle_filter(
    srt_path: str | Path,
    font_size: int,
    clip_start: float,
    clip_end: float,
) -> Optional[str]:
    """
    يبني subtitles filter للـ ffmpeg.
    ملاحظة: الترجمة timestamps بتاعتها لازم تتعدل بالنسبة لبداية الـ clip.
    عشان كده بنعمل ملف SRT مؤقت مقصوص.
    """
    srt_path = Path(srt_path)
    if not srt_path.exists():
        return None

    # اقرأ الـ SRT الأصلي وانتج نسخة مقصوصة
    clip_srt_path = srt_path.with_suffix(f".clip_{int(clip_start)}_{int(clip_end)}.srt")
    _crop_srt(srt_path, clip_srt_path, clip_start, clip_end)

    # escape للويندوز
    srt_str = str(clip_srt_path).replace("\\", "/").replace(":", "\\:")
    return (
        f"subtitles='{srt_str}':force_style="
        f"'FontName=Arial,FontSize={font_size},PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H00000000,BorderStyle=1,Outline=3,Shadow=0,"
        f"Alignment=2,MarginV=80'"
    )


def _crop_srt(src: Path, dst: Path, start: float, end: float) -> None:
    """يقص ملف SRT لمدة الـ clip ويعيد ضبط الـ timestamps من 0"""
    import re

    def parse_ts(ts: str) -> float:
        h, m, rest = ts.split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    def fmt_ts(sec: float) -> str:
        if sec < 0:
            sec = 0
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        ms = int((sec - int(sec)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    text = src.read_text(encoding="utf-8")
    blocks = re.split(r"\n\s*\n", text.strip())
    out_blocks = []
    counter = 1
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        # lines[0] index, lines[1] timestamps, lines[2:] text
        try:
            ts_line = lines[1]
            start_ts, end_ts = ts_line.split("-->")
            seg_start = parse_ts(start_ts.strip())
            seg_end = parse_ts(end_ts.strip())
        except Exception:
            continue
        # تقاطع مع نطاق الـ clip؟
        if seg_end <= start or seg_start >= end:
            continue
        new_start = max(0.0, seg_start - start)
        new_end = min(end - start, seg_end - start)
        if new_end <= new_start:
            continue
        new_block = (
            f"{counter}\n"
            f"{fmt_ts(new_start)} --> {fmt_ts(new_end)}\n"
            + "\n".join(lines[2:])
        )
        out_blocks.append(new_block)
        counter += 1

    dst.write_text("\n\n".join(out_blocks), encoding="utf-8")


def cut_all_shorts(
    video_path: str | Path,
    clips: List[ImportantClip],
    output_dir: str | Path,
    base_name: str,
    srt_path: Optional[str | Path] = None,
    aspect: str = "9:16",
    burn_subtitles: bool = True,
    subtitle_font_size: int = 48,
    word_level_captions: bool = False,
    caption_font_name: str = "Noto Sans Arabic",
) -> List[Path]:
    """يقص كل الـ shorts من قائمة الـ clips ويرجع المسارات"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    paths: List[Path] = []
    for i, clip in enumerate(clips, 1):
        out = output_dir / f"{base_name}_short_{i:02d}.mp4"
        try:
            cut_clip(
                video_path=video_path,
                output_path=out,
                start_seconds=clip.start_seconds,
                end_seconds=clip.end_seconds,
                aspect=aspect,
                burn_subtitles=burn_subtitles,
                srt_path=srt_path,
                subtitle_font_size=subtitle_font_size,
                word_level_captions=word_level_captions,
                caption_font_name=caption_font_name,
            )
            paths.append(out)
        except Exception as e:
            logger.error(f"فشل قص short #{i}: {e}")
            continue
    return paths
