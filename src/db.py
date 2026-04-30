"""SQLAlchemy models + session factory for the abuhafs dashboard.

Tables:
- users     : dashboard login (email/bcrypt password)
- videos    : main records (mirrors the legacy pending.json items)
- shorts    : per-video short clips (YT/FB/IG/TG ids)
- quotes    : per-video text quotes (Telegram message ids)
- logs      : ad-hoc dashboard log lines (optional; useful for audits later)
- srt_files : if we ever want to keep multiple SRT versions per video — for
              now the canonical SRT lives on Video.srt_text, but the table is
              there as a forward-compat seam.

The default SQLite path is ``data/abuhafs.db``. ``data/`` is git-ignored.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)  # bcrypt
    name: Mapped[str] = mapped_column(String(120), default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Videos
# ---------------------------------------------------------------------------
class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(primary_key=True)
    # YouTube video_id is the natural key from upstream.
    video_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    original_path: Mapped[str] = mapped_column(Text, default="")
    original_name: Mapped[str] = mapped_column(String(500), default="")
    series: Mapped[str] = mapped_column(String(255), default="", index=True)
    title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    tags_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    chapters_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(32), default="uploaded", index=True)
    captions_checked_count: Mapped[int] = mapped_column(Integer, default=0)
    captions_caption_id: Mapped[str] = mapped_column(String(255), default="")
    error: Mapped[str] = mapped_column(Text, default="")

    publish_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    playlist_id: Mapped[str] = mapped_column(String(64), default="")

    # Cross-platform IDs
    fb_main_video_id: Mapped[str] = mapped_column(String(64), default="")
    tg_main_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # SRT (the raw text from YouTube auto-captions, kept for reuse)
    srt_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    srt_fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Free-form blob with anything else from the legacy metadata dict.
    metadata_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    shorts: Mapped[list["Short"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )
    quotes: Mapped[list["Quote"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )
    srt_files: Mapped[list["SrtFile"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )


# ---------------------------------------------------------------------------
# Shorts
# ---------------------------------------------------------------------------
class Short(Base):
    __tablename__ = "shorts"

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))

    yt_short_id: Mapped[str] = mapped_column(String(64), default="")
    fb_reel_id: Mapped[str] = mapped_column(String(64), default="")
    ig_reel_id: Mapped[str] = mapped_column(String(64), default="")
    tg_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    title: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    hook: Mapped[str] = mapped_column(String(255), default="")
    start_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    end_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    tags_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    video: Mapped[Video] = relationship(back_populates="shorts")


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------
class Quote(Base):
    __tablename__ = "quotes"

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))

    text: Mapped[str] = mapped_column(Text, nullable=False)
    context_hint: Mapped[str] = mapped_column(Text, default="")
    source_timestamp: Mapped[str] = mapped_column(String(16), default="")
    tg_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    video: Mapped[Video] = relationship(back_populates="quotes")


# ---------------------------------------------------------------------------
# SRT files (forward-compat — store multiple revisions per video if needed)
# ---------------------------------------------------------------------------
class SrtFile(Base):
    __tablename__ = "srt_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))

    kind: Mapped[str] = mapped_column(String(32), default="auto")  # auto/manual/edited
    language: Mapped[str] = mapped_column(String(8), default="ar")
    text: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    video: Mapped[Video] = relationship(back_populates="srt_files")


# ---------------------------------------------------------------------------
# Logs (free-form — useful for audit trail, optional)
# ---------------------------------------------------------------------------
class LogEntry(Base):
    __tablename__ = "logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, index=True
    )
    level: Mapped[str] = mapped_column(String(16), default="info")
    source: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    video_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("videos.id", ondelete="SET NULL"), nullable=True
    )


# ---------------------------------------------------------------------------
# Engine + session helpers
# ---------------------------------------------------------------------------
_DEFAULT_DB_PATH = "data/abuhafs.db"


def _resolve_db_path(db_path: str | Path) -> Path:
    """If relative, anchor to the project root (parent of src/)."""
    p = Path(db_path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent.parent / p
    return p


def get_engine(db_path: str | Path = _DEFAULT_DB_PATH):
    p = _resolve_db_path(db_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False so the engine can be shared across FastAPI workers/threads.
    return create_engine(
        f"sqlite:///{p.as_posix()}",
        future=True,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False},
    )


def init_db(db_path: str | Path = _DEFAULT_DB_PATH):
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    return engine


def get_session_factory(engine=None) -> sessionmaker[Session]:
    if engine is None:
        engine = init_db()
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)
