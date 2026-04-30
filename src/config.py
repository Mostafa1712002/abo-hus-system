"""تحميل ملف الإعدادات config.json"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict


class Config:
    def __init__(self, config_path="config.json"):
        self.path = Path(config_path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"ملف الإعدادات مش موجود: {self.path}\n"
                "انسخ config.example.json باسم config.json وعدّل القيم."
            )
        with self.path.open("r", encoding="utf-8") as f:
            self._data = json.load(f)
        self.project_root = self.path.parent.resolve()
        for key, rel in self._data.get("paths", {}).items():
            if isinstance(rel, str) and not os.path.isabs(rel):
                (self.project_root / rel).mkdir(parents=True, exist_ok=True)

    def get(self, *keys, default=None):
        node = self._data
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return default
            node = node[k]
        return node

    @property
    def gemini_api_key(self) -> str:
        key = self._data.get("gemini_api_key", "")
        if not key or "ضع_مفتاح" in key:
            raise ValueError("مفتاح Gemini مش متظبط")
        return key

    @property
    def youtube(self) -> dict:
        return self._data["youtube"]

    @property
    def paths(self) -> dict:
        result = {}
        for k, v in self._data["paths"].items():
            if isinstance(v, bool):
                result[k] = v
            elif isinstance(v, str):
                if os.path.isabs(v):
                    result[k] = v
                else:
                    result[k] = str(self.project_root / v)
            else:
                result[k] = v
        return result

    @property
    def whisper(self) -> dict:
        return self._data.get("whisper", {})

    @property
    def thumbnail(self) -> dict:
        cfg = dict(self._data["thumbnail"])
        for key in ("font_path", "logo_path"):
            v = cfg.get(key)
            if v and not os.path.isabs(v):
                cfg[key] = str(self.project_root / v)
        return cfg

    @property
    def shorts(self) -> dict:
        return self._data["shorts"]

    @property
    def ai_prompts(self) -> dict:
        return self._data["ai_prompts"]

    @property
    def scheduler(self) -> dict:
        return self._data.get("scheduler", {})

    @property
    def channel_branding(self) -> dict:
        return self._data.get("channel_branding", {})
