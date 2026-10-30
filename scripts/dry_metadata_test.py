#!/usr/bin/env python3
"""Dry-run: generate FINAL YouTube tags + description for a real SRT, WITHOUT uploading.
Mirrors pipeline.py main-video metadata build. Usage: dry_metadata_test.py <srt_path>"""
import sys
sys.path.insert(0, "/opt/abuhafs/youtube-auto-uploader")
from src.config import Config
from src.ai_generator import GeminiGenerator
from src.pipeline import (normalize_tags, _srt_plain_text,
                          _srt_with_simple_timestamps, _get_duration_from_srt)

srt_path = sys.argv[1]
srt_text = open(srt_path, encoding="utf-8").read()
cfg = Config("config.json") if False else __import__("src.config", fromlist=["Config"]).Config()
# Config() default loads config.json in most setups; fall back if needed
try: _ = cfg.gemini_api_key
except Exception: cfg = Config("config.json")

plain = _srt_plain_text(srt_text)
with_ts = _srt_with_simple_timestamps(srt_text)
duration = _get_duration_from_srt(srt_text)
rot = cfg.get("gemini_api_keys_rotation") or []
gem = GeminiGenerator(api_key=cfg.gemini_api_key, api_keys=rot or None)

md = gem.generate(
    srt_text_with_timestamps=with_ts, plain_text=plain, video_duration_seconds=duration,
    max_clips=cfg.shorts.get("max_clips_per_video", 3),
    min_clip_seconds=cfg.shorts.get("min_clip_seconds", 25),
    max_clip_seconds=cfg.shorts.get("max_clip_seconds", 58),
    max_title_length=cfg.ai_prompts.get("title_max_length", 75),
    max_hashtags=cfg.ai_prompts.get("max_hashtags", 10),
    include_chapters=cfg.ai_prompts.get("include_chapters", True),
    content_context=cfg.ai_prompts.get("content_context", ""),
    description_template_hint=cfg.ai_prompts.get("description_template_hint", ""),
    shorts_selection_hint=cfg.ai_prompts.get("shorts_selection_hint", ""),
    openai_api_key="",
    speaker_honorifics=(cfg.get("channel_branding", default={}) or {}).get("speaker_honorifics", []) or [],
)
tags = normalize_tags(md.hashtags + list(cfg.youtube.get("default_tags", []) or []), max_count=30)
desc = md.description_with_chapters()

print("="*60)
print("TITLE:", md.title)
print("-"*60)
print("YouTube TAGS field (", len(tags), "):")
print("  ", " | ".join(tags))
print("-"*60)
print("RAW Gemini hashtags:", md.hashtags)
print("-"*60)
print("DESCRIPTION hashtag line(s):")
for ln in desc.splitlines():
    if "#" in ln: print("  ", ln)
print("="*60)
