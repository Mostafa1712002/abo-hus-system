"""
نظام tracking للفيديوهات المرفوعة المستنية معالجة (captions + Gemini + update)
بيستخدم JSON file بسيط
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class PendingVideo:
    """فيديو مرفوع ومستني المعالجة"""
    video_id: str  # YouTube video ID
    original_path: str  # المسار الأصلي للفيديو على الجهاز
    original_name: str  # اسم الملف
    series: str  # اسم السلسلة (الفولدر الفرعي)
    uploaded_at: str  # ISO timestamp
    status: str = "uploaded"  # uploaded | captions_ready | processing | completed | failed
    captions_checked_count: int = 0
    captions_caption_id: str = ""
    error: str = ""
    completed_at: str = ""
    title_updated: str = ""
    publish_at: str = ""
    playlist_id: str = ""
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "PendingVideo":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class PendingTracker:
    """يدير ملف pending.json"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._items: List[PendingVideo] = []
        self.load()

    def load(self):
        if not self.path.exists():
            self._items = []
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._items = [PendingVideo.from_dict(d) for d in data]
        except Exception as e:
            logger.error(f"فشل قراءة {self.path}: {e}")
            self._items = []

    def save(self):
        data = [asdict(p) for p in self._items]
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, item: PendingVideo):
        # متضيفش لو فيديو موجود بنفس الـ id
        for existing in self._items:
            if existing.video_id == item.video_id:
                return
        self._items.append(item)
        self.save()

    def update(self, video_id: str, **kwargs):
        for item in self._items:
            if item.video_id == video_id:
                for k, v in kwargs.items():
                    if hasattr(item, k):
                        setattr(item, k, v)
                self.save()
                return

    def get(self, video_id: str) -> Optional[PendingVideo]:
        for item in self._items:
            if item.video_id == video_id:
                return item
        return None

    def all_pending(self) -> List[PendingVideo]:
        """فيديوهات لسه مش completed ولا failed"""
        return [
            i for i in self._items
            if i.status not in ("completed", "failed")
        ]

    def all(self) -> List[PendingVideo]:
        return list(self._items)

    def remove(self, video_id: str):
        self._items = [i for i in self._items if i.video_id != video_id]
        self.save()

    def stats(self) -> dict:
        counts = {}
        for item in self._items:
            counts[item.status] = counts.get(item.status, 0) + 1
        counts["total"] = len(self._items)
        return counts


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
