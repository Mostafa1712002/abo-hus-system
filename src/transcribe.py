"""
موديول الترجمة - يستخرج SRT من الفيديو باستخدام faster-whisper

Whisper بيشتغل محلي على الجهاز (مجاني تماماً).
أول مرة هيحمل الموديل (مرة واحدة بس) وبعد كده هيشتغل offline.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)


@dataclass
class Segment:
    """قطعة من الترجمة بوقت بدايتها ونهايتها"""
    start: float
    end: float
    text: str


def _format_timestamp(seconds: float) -> str:
    """SRT timestamp: HH:MM:SS,mmm"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def segments_to_srt(segments: List[Segment]) -> str:
    """تحويل قائمة segments لنص SRT"""
    out = []
    for i, seg in enumerate(segments, 1):
        out.append(str(i))
        out.append(f"{_format_timestamp(seg.start)} --> {_format_timestamp(seg.end)}")
        out.append(seg.text.strip())
        out.append("")
    return "\n".join(out)


def transcribe_video(
    video_path: str | Path,
    output_srt_path: str | Path,
    model_size: str = "medium",
    language: str = "ar",
    device: str = "auto",
    compute_type: str = "auto",
) -> List[Segment]:
    """
    يستخرج الترجمة من الفيديو ويحفظها كـ SRT.

    Args:
        video_path: مسار الفيديو
        output_srt_path: مسار حفظ ملف SRT
        model_size: حجم موديل Whisper (tiny/base/small/medium/large-v3)
        language: لغة الفيديو (ar للعربي)
        device: cuda أو cpu أو auto
        compute_type: int8 أو float16 أو auto

    Returns:
        قائمة Segment objects
    """
    from faster_whisper import WhisperModel

    video_path = Path(video_path)
    output_srt_path = Path(output_srt_path)
    output_srt_path.parent.mkdir(parents=True, exist_ok=True)

    if not video_path.exists():
        raise FileNotFoundError(f"الفيديو مش موجود: {video_path}")

    logger.info(f"تحميل موديل Whisper ({model_size})... قد يستغرق دقايق أول مرة")
    # auto device: استخدم cuda لو متاح
    if device == "auto":
        try:
            import torch  # type: ignore
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"

    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"

    model = WhisperModel(model_size, device=device, compute_type=compute_type)

    logger.info(f"بدء استخراج الترجمة من: {video_path.name}")
    segments_iter, info = model.transcribe(
        str(video_path),
        language=language,
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
    )

    logger.info(
        f"اللغة المكتشفة: {info.language} "
        f"(احتمالية {info.language_probability:.0%}), "
        f"المدة: {info.duration:.1f}ث"
    )

    segments: List[Segment] = []
    for seg in segments_iter:
        segments.append(Segment(start=seg.start, end=seg.end, text=seg.text))

    # احفظ الـ SRT
    srt_text = segments_to_srt(segments)
    output_srt_path.write_text(srt_text, encoding="utf-8")
    logger.info(f"تم حفظ SRT: {output_srt_path} ({len(segments)} segment)")

    return segments


def srt_to_plain_text(segments: List[Segment]) -> str:
    """تحويل segments لنص متواصل (بدون أرقام أو timestamps) للـ AI"""
    return " ".join(s.text.strip() for s in segments)


def srt_with_timestamps(segments: List[Segment]) -> str:
    """نص فيه timestamps - مفيد لـ AI عشان يحدد chapters"""
    lines = []
    for s in segments:
        mm = int(s.start // 60)
        ss = int(s.start % 60)
        lines.append(f"[{mm:02d}:{ss:02d}] {s.text.strip()}")
    return "\n".join(lines)
