"""Word-accurate Arabic captions for Shorts, from forced alignment on the known script.

The catalog already stores the exact words of every lecture (subtitle_srt from the tarweig
MySQL), so caption timing does not have to be guessed: ctc-forced-aligner lines that known
text up against the clip's own audio and returns a true onset per word. Those onsets are
re-chunked into short lines that change exactly when the speaker moves on, instead of the
3-5 second SRT blocks the cutter used before.

Two deliberate choices:

* The aligner runs in a SEPARATE venv (venv-align) as a subprocess. torch and numpy never
  enter the uploader's own venv, so an alignment dependency can't break uploading.
* Lines, not per-word karaoke colouring. An inline ASS override tag (\\k, \\c) starts a new
  text run and Arabic letters do not join across runs, so word-level highlighting visibly
  tears words apart. Short precisely-timed lines give the retention benefit with no
  shaping risk.

Falls back to returning None on any failure - the caller then uses the plain SRT filter.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent
ALIGN_PY = APP_DIR / "venv-align" / "bin" / "python"
ALIGN_SCRIPT = APP_DIR / "scripts" / "align_words.py"

MAX_WORDS_PER_LINE = 4
MAX_LINE_SECONDS = 1.9
MIN_LINE_SECONDS = 0.55
ALIGN_TIMEOUT = 420  # MMS-300m on CPU: ~20-40 s for a 60 s clip, plus model load


def srt_to_script(srt_text: str) -> str:
    """Plain words from an SRT, dropping indices and timestamp lines."""
    out: List[str] = []
    for block in re.split(r"\n\s*\n", srt_text.strip()):
        lines = [l.strip() for l in block.split("\n") if l.strip()]
        body = [l for l in lines if not l.isdigit() and "-->" not in l]
        if body:
            out.append(" ".join(body))
    return " ".join(out)


def _extract_wav(media: Path, dst: Path, start: float, duration: float) -> None:
    """Pull just the clip's audio out of the parent - no video decode, no second encode."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", str(media),
         "-t", str(duration), "-vn", "-ac", "1", "-ar", "16000", str(dst)],
        check=True, timeout=300,
    )


def _align(wav: Path, script: str, workdir: Path, lang: str = "ara") -> List[dict]:
    script_path = workdir / "script.txt"
    script_path.write_text(script, encoding="utf-8")
    out = workdir / "words.json"
    subprocess.run(
        [str(ALIGN_PY), str(ALIGN_SCRIPT), str(wav), str(script_path), str(out),
         "--lang", lang],
        check=True, timeout=ALIGN_TIMEOUT, capture_output=True, text=True,
    )
    return json.loads(out.read_text(encoding="utf-8")).get("words", [])


def group_lines(words: List[dict], clip_duration: float) -> List[dict]:
    """Chunk aligned words into short caption lines timed off the word onsets."""
    lines: List[dict] = []
    cur: List[dict] = []

    def flush(end: float) -> None:
        if not cur:
            return
        start = cur[0]["start"]
        if end - start < MIN_LINE_SECONDS:
            end = start + MIN_LINE_SECONDS
        lines.append({"start": start, "end": min(end, clip_duration),
                      "text": " ".join(w["word"] for w in cur)})

    for w in words:
        cur.append(w)
        nxt = w.get("next_start")
        span = (nxt if nxt is not None else clip_duration) - cur[0]["start"]
        if len(cur) >= MAX_WORDS_PER_LINE or span >= MAX_LINE_SECONDS or nxt is None:
            flush(nxt if nxt is not None else clip_duration)
            cur = []
    flush(clip_duration)

    for i in range(len(lines) - 1):          # never let a line outlive the next one
        lines[i]["end"] = min(lines[i]["end"], lines[i + 1]["start"])
    return [l for l in lines if l["end"] > l["start"]]


def _ts(sec: float) -> str:
    sec = max(sec, 0.0)
    return f"{int(sec // 3600):d}:{int((sec % 3600) // 60):02d}:{sec % 60:05.2f}"


def build_ass(lines: List[dict], font_name: str, font_size: int,
              play_w: int = 1080, play_h: int = 1920, margin_v: int = 480) -> str:
    """margin_v keeps the text clear of the Shorts UI that covers the bottom ~15%."""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font_name},{font_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = [f"Dialogue: 0,{_ts(l['start'])},{_ts(l['end'])},Cap,,0,0,0,,{l['text']}"
            for l in lines]
    return header + "\n".join(rows) + "\n"


def build_word_caption_filter(
    media: Path,
    srt_path: Path,
    clip_start: float,
    clip_end: float,
    out_dir: Path,
    font_name: str = "Noto Sans Arabic",
    font_size: int = 76,
    margin_v: int = 480,
    lang: str = "ara",
) -> Optional[str]:
    """Build an .ass for this clip and return the ffmpeg -vf fragment.

    `media` is the PARENT video; the clip's audio is pulled straight out of it with
    -ss/-t, so captions are ready before the single cut-and-burn encode runs.
    Returns None if anything fails, so the caller can fall back to the SRT filter.
    """
    if not ALIGN_PY.exists() or not ALIGN_SCRIPT.exists():
        logger.warning("word captions: venv-align or align_words.py missing, skipping")
        return None
    try:
        from .shorts_cutter import _crop_srt  # reuse the existing cropper
    except Exception:
        return None

    try:
        duration = clip_end - clip_start
        out_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="wordcaps_") as td:
            td_path = Path(td)
            cropped = td_path / "clip.srt"
            _crop_srt(Path(srt_path), cropped, clip_start, clip_end)
            script = srt_to_script(cropped.read_text(encoding="utf-8"))
            if len(script.split()) < 3:
                logger.info("word captions: clip has too little text, skipping")
                return None

            wav = td_path / "clip.wav"
            _extract_wav(Path(media), wav, clip_start, duration)
            words = _align(wav, script, td_path, lang=lang)

        lines = group_lines(words, duration)
        if not lines:
            return None

        stem = f"{Path(media).stem}_{int(clip_start)}_{int(clip_end)}"
        ass_path = out_dir / f"{stem}.ass"
        ass_path.write_text(build_ass(lines, font_name, font_size, margin_v=margin_v),
                            encoding="utf-8")
        # Keep the aligned words beside the .ass: their per-word confidence is how the
        # reviewer detects an SRT whose text does not actually match the audio.
        (out_dir / f"{stem}.words.json").write_text(
            json.dumps({"clip_start": clip_start, "clip_end": clip_end,
                        "lines": lines, "words": words}, ensure_ascii=False),
            encoding="utf-8")
        logger.info("word captions: %d words → %d lines (%s)",
                    len(words), len(lines), ass_path.name)
        escaped = str(ass_path).replace("\\", "/").replace(":", "\\:")
        return f"ass='{escaped}'"
    except subprocess.TimeoutExpired:
        logger.warning("word captions: alignment timed out, falling back to SRT")
    except Exception as e:
        logger.warning("word captions failed (%s), falling back to SRT", e)
    return None
