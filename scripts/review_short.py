#!/usr/bin/env python3
"""Score a cut Short before it is allowed to post.

The dispatcher used to post whatever Gemini picked, oldest first, with no check at all -
so a clip whose captions say «المنطبق بتحلم مشتبه لبن», or one that opens with four
seconds of silence, went out exactly like a good one. This scores each clip on signals
that are actually measurable from the artifacts the cutter already produces, and hands
back a verdict the dispatcher can act on.

Dimensions (0-10 each). Every one of them exists because of a defect seen in the real
output, not because it sounded good:

  alignment    mean forced-alignment confidence. The single most useful signal: a low
               score means the catalog SRT text does NOT match what is being said, so
               the burned captions would be wrong.
  coverage     share of the clip actually carrying speech - catches dead air and music.
  opening      the clip must not start mid-word (cut into speech) nor open with a long
               silence; the first ~2 seconds decide the swipe.
  ending       a long silent tail reads as an unfinished thought.
  captions     line lengths and durations inside the readable band.
  audio        integrated loudness; too quiet is unlistenable on a phone speaker.

Verdict: ready (post it) / near (post only if nothing better is queued) / under (hold).

Usage:
    review_short.py clip.mp4 clip.words.json [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# Thresholds. The alignment numbers are MEASURED, not guessed: the same 45 s of audio
# aligned against its own catalog text scored mean -0.472 (median -0.141), and against a
# different passage of the same lecture scored mean -1.910 (median -1.594). The aligner
# returns a log-probability, so these are negative and -1.2 sits cleanly between the two.
ALIGN_GOOD, ALIGN_BAD = -0.25, -1.60
MIN_ALIGN_SCORE = -1.20     # below this the text almost certainly is not the audio
MIN_COVERAGE = 0.55         # share of clip duration spanned by speech
MIN_OPEN_GAP = 0.20         # first word later than this = not cut into a word
MAX_OPEN_GAP = 2.50         # dead air before the first word
MAX_END_GAP = 2.50          # silent tail
TARGET_LUFS = -14.0         # what YouTube normalises to
READY_AVG, READY_MIN = 7.5, 6.0
NEAR_AVG = 6.0


def _ffprobe_duration(media: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(media)],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def _loudness(media: Path) -> float | None:
    """Integrated LUFS via ebur128. None when ffmpeg gives nothing parseable."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", str(media),
             "-af", "ebur128=framelog=quiet", "-f", "null", "-"],
            capture_output=True, text=True, timeout=180)
        m = re.findall(r"I:\s*(-?\d+\.?\d*)\s*LUFS", r.stderr)
        return float(m[-1]) if m else None
    except Exception:
        return None


def _band(value: float, good: float, bad: float) -> float:
    """Map a value onto 0-10, where `good` scores 10 and `bad` scores 0."""
    if good == bad:
        return 10.0
    x = (value - bad) / (good - bad)
    return round(max(0.0, min(1.0, x)) * 10, 1)


def review(media: Path, words_path: Path) -> dict:
    data = json.loads(words_path.read_text(encoding="utf-8"))
    words = data.get("words", [])
    lines = data.get("lines", [])
    duration = _ffprobe_duration(media)

    dims: dict[str, float] = {}
    notes: list[str] = []

    # --- alignment confidence -------------------------------------------------
    scores = [w.get("score", 0.0) for w in words if "score" in w]
    mean_score = sum(scores) / len(scores) if scores else 0.0
    dims["alignment"] = _band(mean_score, good=ALIGN_GOOD, bad=ALIGN_BAD)
    if mean_score < MIN_ALIGN_SCORE:
        notes.append(f"alignment {mean_score:.2f} - caption text likely does not match the audio")

    # --- speech coverage ------------------------------------------------------
    if words:
        speech_span = (words[-1]["start"] - words[0]["start"]) or 0.0
        coverage = speech_span / duration if duration else 0.0
    else:
        coverage = 0.0
    dims["coverage"] = _band(coverage, good=0.92, bad=0.40)
    if coverage < MIN_COVERAGE:
        notes.append(f"speech covers only {coverage:.0%} of the clip")

    # --- opening --------------------------------------------------------------
    first = words[0]["start"] if words else duration
    if first < MIN_OPEN_GAP:
        dims["opening"] = 3.0
        notes.append(f"starts {first:.2f}s in - cut into a word")
    else:
        dims["opening"] = _band(first, good=MIN_OPEN_GAP + 0.15, bad=MAX_OPEN_GAP)
        if first > MAX_OPEN_GAP:
            notes.append(f"{first:.1f}s of dead air before the first word")

    # --- ending ---------------------------------------------------------------
    last = words[-1]["start"] if words else 0.0
    end_gap = max(0.0, duration - last)
    dims["ending"] = _band(end_gap, good=0.6, bad=MAX_END_GAP + 1.5)
    if end_gap > MAX_END_GAP:
        notes.append(f"{end_gap:.1f}s of silence at the end")

    # --- captions -------------------------------------------------------------
    if lines:
        too_long = sum(1 for l in lines if len(l["text"].split()) > 5)
        too_fast = sum(1 for l in lines if (l["end"] - l["start"]) < 0.45)
        bad_ratio = (too_long + too_fast) / len(lines)
        dims["captions"] = _band(1 - bad_ratio, good=0.97, bad=0.6)
        if bad_ratio > 0.15:
            notes.append(f"{too_long} overlong / {too_fast} too-fast caption lines")
    else:
        dims["captions"] = 0.0
        notes.append("no caption lines")

    # --- audio ----------------------------------------------------------------
    lufs = _loudness(media)
    if lufs is None:
        dims["audio"] = 6.0
        notes.append("loudness unreadable")
    else:
        dims["audio"] = _band(-abs(lufs - TARGET_LUFS), good=-2.0, bad=-8.0)
        if abs(lufs - TARGET_LUFS) > 6:
            notes.append(f"loudness {lufs:.1f} LUFS (target {TARGET_LUFS})")

    avg = round(sum(dims.values()) / len(dims), 2)
    low = min(dims.values())
    hard_fail = mean_score < MIN_ALIGN_SCORE or coverage < MIN_COVERAGE or not lines

    if hard_fail or avg < NEAR_AVG:
        verdict = "under"
    elif avg >= READY_AVG and low >= READY_MIN:
        verdict = "ready"
    else:
        verdict = "near"

    return {
        "verdict": verdict, "score": avg, "lowest": low, "dimensions": dims,
        "measured": {"duration": round(duration, 2), "words": len(words),
                     "lines": len(lines), "mean_align_score": round(mean_score, 3),
                     "coverage": round(coverage, 3), "first_word": round(first, 2),
                     "end_gap": round(end_gap, 2), "lufs": lufs},
        "notes": notes,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("media")
    ap.add_argument("words")
    ap.add_argument("--json", default="")
    a = ap.parse_args()

    media, words = Path(a.media), Path(a.words)
    if not media.exists() or not words.exists():
        print(json.dumps({"verdict": "under", "score": 0,
                          "notes": ["clip or words.json missing"]}, ensure_ascii=False))
        sys.exit(0)

    result = review(media, words)
    out = json.dumps(result, ensure_ascii=False, indent=1)
    if a.json:
        Path(a.json).write_text(out, encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
