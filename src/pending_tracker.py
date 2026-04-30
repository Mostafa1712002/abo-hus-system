"""
نظام tracking للفيديوهات المرفوعة المستنية معالجة (captions + Gemini + update).

النظام في طور انتقال من JSON إلى SQLite:

* JSON (`output/pending.json`) لسه شغّال للـ backwards compatibility.
* SQLite (`data/abuhafs.db`) هو الـ primary store الجديد لما يتفعّل في
  `config.json` تحت `persistence.use_database`.

في طور الانتقال بنعمل **dual-write** — أي تعديل بيتكتب في الاتنين عشان أي
سكريبت/cron قديم لسه بيقرأ من JSON يفضل شغّال. ممكن نشيل الـ JSON بعد ما
كل الكود يبقى بيقرأ من الـ DB.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

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


# ---------------------------------------------------------------------------
# DB persistence config — opt-in. Defaults are conservative so the existing
# CLI / cron flows keep working without a config change.
# ---------------------------------------------------------------------------
def _load_persistence_config() -> dict:
    """Read `persistence.*` from config.json. Falls back to safe defaults."""
    project_root = Path(__file__).resolve().parent.parent
    cfg_path = project_root / "config.json"
    defaults = {
        "use_database": True,
        "db_path": "data/abuhafs.db",
        "fallback_to_json": True,
    }
    if not cfg_path.exists():
        return defaults
    try:
        with cfg_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        merged = dict(defaults)
        merged.update(data.get("persistence") or {})
        return merged
    except Exception as e:
        logger.warning("persistence config load failed (%s) — using defaults", e)
        return defaults


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def _iso_or_empty(d: Optional[datetime]) -> str:
    if not d:
        return ""
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.isoformat()


class PendingTracker:
    """يدير ملف pending.json + (اختياري) قاعدة بيانات SQLite.

    Dual-write semantics (when DB is enabled):
        * ``add()``    → DB + JSON
        * ``update()`` → DB + JSON
        * ``get()``    → JSON first (cheap, in-memory); falls back to DB
        * ``all()``    → JSON; if empty/missing, reads DB
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._items: List[PendingVideo] = []

        # DB wiring is opt-in and isolated — failures here MUST NOT break the
        # legacy JSON tracker (cron jobs, the dashboard, etc., depend on it).
        self._db_enabled = False
        self._db_session_factory = None
        self._fallback_to_json = True
        self._init_db_optional()

        self.load()

    # ------------------------------------------------------------------ DB
    def _init_db_optional(self) -> None:
        cfg = _load_persistence_config()
        self._fallback_to_json = bool(cfg.get("fallback_to_json", True))
        if not cfg.get("use_database"):
            return
        try:
            from src.db import get_session_factory, init_db

            db_path = cfg.get("db_path", "data/abuhafs.db")
            engine = init_db(db_path)
            self._db_session_factory = get_session_factory(engine)
            self._db_enabled = True
            logger.debug("PendingTracker: DB enabled (%s)", db_path)
        except Exception as e:
            logger.warning(
                "PendingTracker: DB disabled (%s) — JSON-only mode", e
            )
            self._db_enabled = False
            self._db_session_factory = None

    # ------------------------------------------------------------------ JSON
    def load(self) -> None:
        if not self.path.exists():
            self._items = []
            # If JSON missing but DB has rows, hydrate from DB so consumers
            # don't see an empty list.
            if self._db_enabled:
                self._items = self._load_from_db()
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._items = [PendingVideo.from_dict(d) for d in data]
        except Exception as e:
            logger.error(f"فشل قراءة {self.path}: {e}")
            self._items = []
        if not self._items and self._db_enabled:
            # JSON parsed empty — pull from DB as last resort.
            self._items = self._load_from_db()

    def save(self) -> None:
        data = [asdict(p) for p in self._items]
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------ DB
    def _load_from_db(self) -> List[PendingVideo]:
        if not self._db_session_factory:
            return []
        try:
            from src.db import Video

            out: List[PendingVideo] = []
            with self._db_session_factory() as s:
                rows = s.query(Video).all()
                for v in rows:
                    out.append(_video_to_pending(v))
            return out
        except Exception as e:
            logger.warning("DB load failed: %s — falling back to JSON only", e)
            return []

    def _db_upsert(self, item: PendingVideo) -> None:
        """Insert or update a video row from a PendingVideo dataclass."""
        if not self._db_session_factory:
            return
        try:
            from src.db import Video

            with self._db_session_factory() as s:
                v = s.query(Video).filter_by(video_id=item.video_id).first()
                if v is None:
                    v = Video(video_id=item.video_id)
                    s.add(v)
                _apply_pending_to_video(item, v)
                s.commit()
        except Exception as e:
            logger.warning("DB upsert for %s failed: %s", item.video_id, e)

    def _db_update_fields(self, video_id: str, fields: dict) -> None:
        """Patch a subset of fields on an existing row."""
        if not self._db_session_factory:
            return
        try:
            from src.db import Video

            with self._db_session_factory() as s:
                v = s.query(Video).filter_by(video_id=video_id).first()
                if v is None:
                    return
                _apply_pending_fields_to_video(fields, v)
                s.commit()
        except Exception as e:
            logger.warning("DB update for %s failed: %s", video_id, e)

    def _db_delete(self, video_id: str) -> None:
        if not self._db_session_factory:
            return
        try:
            from src.db import Video

            with self._db_session_factory() as s:
                v = s.query(Video).filter_by(video_id=video_id).first()
                if v is not None:
                    s.delete(v)
                    s.commit()
        except Exception as e:
            logger.warning("DB delete for %s failed: %s", video_id, e)

    # ------------------------------------------------------------------ Public API
    def add(self, item: PendingVideo) -> None:
        # متضيفش لو فيديو موجود بنفس الـ id
        for existing in self._items:
            if existing.video_id == item.video_id:
                return
        self._items.append(item)
        self.save()
        if self._db_enabled:
            self._db_upsert(item)

    def update(self, video_id: str, **kwargs: Any) -> None:
        for item in self._items:
            if item.video_id == video_id:
                for k, v in kwargs.items():
                    if hasattr(item, k):
                        setattr(item, k, v)
                self.save()
                if self._db_enabled:
                    self._db_update_fields(video_id, kwargs)
                return
        # Not in JSON memory — still try DB (shouldn't happen often).
        if self._db_enabled:
            self._db_update_fields(video_id, kwargs)

    def get(self, video_id: str) -> Optional[PendingVideo]:
        for item in self._items:
            if item.video_id == video_id:
                return item
        # Fallback: ask the DB if JSON didn't have it.
        if self._db_enabled and self._fallback_to_json:
            try:
                from src.db import Video

                with self._db_session_factory() as s:  # type: ignore[misc]
                    v = s.query(Video).filter_by(video_id=video_id).first()
                    if v is not None:
                        return _video_to_pending(v)
            except Exception as e:
                logger.warning("DB get fallback for %s failed: %s", video_id, e)
        return None

    def all_pending(self) -> List[PendingVideo]:
        """فيديوهات لسه مش completed ولا failed"""
        return [
            i for i in self._items
            if i.status not in ("completed", "failed")
        ]

    def all(self) -> List[PendingVideo]:
        return list(self._items)

    def remove(self, video_id: str) -> None:
        self._items = [i for i in self._items if i.video_id != video_id]
        self.save()
        if self._db_enabled:
            self._db_delete(video_id)

    def stats(self) -> dict:
        counts: dict = {}
        for item in self._items:
            counts[item.status] = counts.get(item.status, 0) + 1
        counts["total"] = len(self._items)
        return counts


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# DB <-> dataclass adapters (kept private to this module).
# ---------------------------------------------------------------------------
def _video_to_pending(v) -> PendingVideo:
    """Map a SQLAlchemy Video row back into the legacy PendingVideo shape."""
    metadata: dict = {}
    if v.metadata_json:
        try:
            metadata = json.loads(v.metadata_json)
        except Exception:
            metadata = {}
    # Re-stitch a couple of fields that historically lived in `metadata`.
    if v.fb_main_video_id and not metadata.get("fb_main_video_id"):
        metadata["fb_main_video_id"] = v.fb_main_video_id
    if v.tg_main_message_id is not None and not metadata.get("tg_main_message_id"):
        metadata["tg_main_message_id"] = v.tg_main_message_id

    return PendingVideo(
        video_id=v.video_id,
        original_path=v.original_path or "",
        original_name=v.original_name or "",
        series=v.series or "",
        uploaded_at=_iso_or_empty(v.uploaded_at),
        status=v.status or "uploaded",
        captions_checked_count=v.captions_checked_count or 0,
        captions_caption_id=v.captions_caption_id or "",
        error=v.error or "",
        completed_at=_iso_or_empty(v.completed_at),
        title_updated=v.title or "",
        publish_at=_iso_or_empty(v.publish_at),
        playlist_id=v.playlist_id or "",
        metadata=metadata,
    )


def _apply_pending_to_video(item: PendingVideo, v) -> None:
    """Copy ALL fields from PendingVideo into a Video row (full upsert)."""
    v.original_path = item.original_path or ""
    v.original_name = item.original_name or ""
    v.series = item.series or ""
    v.title = item.title_updated or ""
    v.status = item.status or "uploaded"
    v.captions_checked_count = int(item.captions_checked_count or 0)
    v.captions_caption_id = item.captions_caption_id or ""
    v.error = item.error or ""
    v.publish_at = _parse_iso(item.publish_at)
    if item.uploaded_at:
        v.uploaded_at = _parse_iso(item.uploaded_at) or v.uploaded_at
    v.completed_at = _parse_iso(item.completed_at)
    v.playlist_id = item.playlist_id or ""

    metadata = item.metadata or {}
    v.fb_main_video_id = metadata.get("fb_main_video_id", "") or ""
    v.tg_main_message_id = metadata.get("tg_main_message_id")
    v.metadata_json = (
        json.dumps(metadata, ensure_ascii=False) if metadata else None
    )


# Map dataclass field name -> Video column setter logic.
def _apply_pending_fields_to_video(fields: dict, v) -> None:
    """Patch a subset of the Video row from a dict of PendingVideo fields."""
    for k, val in fields.items():
        if k == "title_updated":
            v.title = val or ""
        elif k == "uploaded_at":
            parsed = _parse_iso(val) if val else None
            if parsed:
                v.uploaded_at = parsed
        elif k == "completed_at":
            v.completed_at = _parse_iso(val) if val else None
        elif k == "publish_at":
            v.publish_at = _parse_iso(val) if val else None
        elif k == "metadata":
            md = val or {}
            v.metadata_json = (
                json.dumps(md, ensure_ascii=False) if md else None
            )
            if isinstance(md, dict):
                if "fb_main_video_id" in md:
                    v.fb_main_video_id = md.get("fb_main_video_id", "") or ""
                if "tg_main_message_id" in md:
                    v.tg_main_message_id = md.get("tg_main_message_id")
        elif k in ("captions_checked_count",):
            try:
                v.captions_checked_count = int(val or 0)
            except Exception:
                pass
        elif k in (
            "original_path",
            "original_name",
            "series",
            "status",
            "captions_caption_id",
            "error",
            "playlist_id",
        ):
            setattr(v, k, val or "")
