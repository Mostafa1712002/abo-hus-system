#!/usr/bin/env python3
"""Prepare YouTube metadata ahead of time with Claude on the subscription.

Walks the pending catalog videos that have an SRT (soonest-to-publish first),
writes /opt/abuhafs/catalog_meta/<yt>.meta.json for each, and stops as soon as
Claude stops answering (usage limit). The scheduled catalog_full_worker runs
then read the prepared file instead of calling any model.

Nothing is uploaded here. Safe to re-run: prepared videos are skipped.
Usage: prefill_metadata.py [--limit N]
"""
import argparse
import sqlite3
import sys

sys.path.insert(0, "/opt/abuhafs/youtube-auto-uploader/scripts")
import catalog_full_worker as W  # noqa: E402  (sets sys.path for src.*)
from src import claude_model  # noqa: E402
from src.claude_model import ClaudeModel  # noqa: E402

log = W.log


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="stop after N prepared videos")
    args = ap.parse_args()

    cfg = W.Config(str(W.ROOT / "config.json"))
    gemini = W.GeminiGenerator(api_key=cfg.gemini_api_key,
                               api_keys=cfg.get("gemini_api_keys_rotation") or None)
    con = sqlite3.connect(str(W.DB))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT youtube_id FROM catalog_videos WHERE process_status='pending' "
        "ORDER BY CASE download_status WHEN 'downloaded' THEN 0 WHEN 'local' THEN 1 "
        "WHEN 'source' THEN 2 ELSE 3 END, series_slug, ord, id").fetchall()
    con.close()

    done = 0
    for r in rows:
        yt = r["youtube_id"]
        if not yt or (W.SRT_CACHE / f"{yt}.meta.json").exists():
            continue
        if not isinstance(gemini.model, ClaudeModel):
            log.warning("Claude is not answering (limit reached or disabled) - stopping")
            break
        srt = W.fetch_srt(yt)
        if not srt:
            continue
        try:
            md = W._generate_split(
                gemini, cfg, with_ts=W.P._srt_with_simple_timestamps(srt),
                plain=W.P._srt_plain_text(srt), duration=W.P._get_duration_from_srt(srt),
                allow_web=False)
        except Exception as e:
            log.warning("%s: generation failed (%s)", yt, str(e)[:200])
            if claude_model._capped:
                break
            continue
        if claude_model._capped:
            # the limit hit inside this video: part of it may come from Gemini - redo it later
            log.warning("Claude limit reached during %s - stopping", yt)
            break
        if md and md.title:
            W.save_cached_md(yt, md)
            done += 1
            log.info("prepared %s (%d) title=%r", yt, done, md.title[:60])
        else:
            log.warning("%s: no title, not saved", yt)
        if args.limit and done >= args.limit:
            break
    print(f"prepared {done}")


if __name__ == "__main__":
    main()
