"""One-time migration from output/pending.json to SQLite + admin user setup.

Usage:
    # Migrate pending.json into the DB (idempotent — safe to run multiple times)
    python -m src.db_migrate

    # Create or update an admin user
    python -m src.db_migrate create-admin <email> <password> [name]
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.db import (
    Short,
    User,
    Video,
    get_session_factory,
    init_db,
)

logger = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PENDING = str(PROJECT_ROOT / "output" / "pending.json")
DEFAULT_DB = str(PROJECT_ROOT / "data" / "abuhafs.db")
DEFAULT_SRT_DIR = str(PROJECT_ROOT / "output" / "srt")


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # Python's fromisoformat handles offsets natively in 3.11+, and the
        # extra Z->+00:00 swap keeps it tolerant of explicit-Z inputs.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _read_srt(srt_dir: Path, original_name: str, video_id: str) -> Optional[str]:
    """Best-effort SRT lookup based on the original filename stem."""
    base = Path(original_name).stem if original_name else video_id
    candidate = srt_dir / f"{base}.srt"
    if candidate.exists():
        try:
            return candidate.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            logger.warning("read SRT %s failed: %s", candidate, e)
    return None


def migrate(
    pending_json_path: str = DEFAULT_PENDING,
    db_path: str = DEFAULT_DB,
    srt_dir: str = DEFAULT_SRT_DIR,
) -> int:
    """Import pending.json into the DB. Returns number of new videos inserted."""
    engine = init_db(db_path)
    Session = get_session_factory(engine)

    pending_path = Path(pending_json_path)
    if not pending_path.exists():
        print(f"no pending.json at {pending_path} — clean DB created.")
        return 0

    try:
        items = json.loads(pending_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"failed to read {pending_path}: {e}")
        return 0

    if not isinstance(items, list):
        print(f"unexpected pending.json shape — expected list, got {type(items)}")
        return 0

    srt_dir_p = Path(srt_dir)
    inserted = 0
    skipped = 0
    with Session() as s:
        for it in items:
            vid = it.get("video_id")
            if not vid:
                continue
            existing = s.query(Video).filter_by(video_id=vid).first()
            if existing:
                skipped += 1
                continue

            metadata = it.get("metadata", {}) or {}
            srt_text = _read_srt(srt_dir_p, it.get("original_name", ""), vid)

            v = Video(
                video_id=vid,
                original_path=it.get("original_path", "") or "",
                original_name=it.get("original_name", "") or "",
                series=it.get("series", "") or "",
                title=it.get("title_updated", "") or "",
                status=it.get("status", "uploaded") or "uploaded",
                captions_checked_count=int(it.get("captions_checked_count", 0) or 0),
                captions_caption_id=it.get("captions_caption_id", "") or "",
                error=it.get("error", "") or "",
                publish_at=_parse_iso(it.get("publish_at", "")),
                uploaded_at=_parse_iso(it.get("uploaded_at", "")) or datetime.utcnow(),
                completed_at=_parse_iso(it.get("completed_at", "")),
                playlist_id=it.get("playlist_id", "") or "",
                fb_main_video_id=metadata.get("fb_main_video_id", "") or "",
                tg_main_message_id=metadata.get("tg_main_message_id"),
                srt_text=srt_text,
                srt_fetched_at=datetime.utcnow() if srt_text else None,
                metadata_json=(
                    json.dumps(metadata, ensure_ascii=False) if metadata else None
                ),
            )
            s.add(v)

            # Best-effort cross-platform short ids (stitched by index).
            short_ids = list(metadata.get("short_video_ids") or [])
            fb_short_ids = list(metadata.get("fb_short_video_ids") or [])
            ig_short_ids = list(metadata.get("ig_short_media_ids") or [])
            tg_short_ids = list(metadata.get("tg_short_message_ids") or [])
            for i, sid in enumerate(short_ids):
                short = Short(
                    yt_short_id=sid or "",
                    fb_reel_id=fb_short_ids[i] if i < len(fb_short_ids) else "",
                    ig_reel_id=ig_short_ids[i] if i < len(ig_short_ids) else "",
                    tg_message_id=(
                        tg_short_ids[i] if i < len(tg_short_ids) else None
                    ),
                )
                v.shorts.append(short)
            inserted += 1
        s.commit()

    print(
        f"migrated {inserted} videos "
        f"(skipped {skipped} already in DB, total in pending.json: {len(items)})"
    )
    return inserted


def ensure_admin_user(
    email: str,
    password: str,
    name: str = "Admin",
    db_path: str = DEFAULT_DB,
) -> None:
    """Create or update an admin user (interactive setup)."""
    import bcrypt

    engine = init_db(db_path)
    Session = get_session_factory(engine)
    with Session() as s:
        user = s.query(User).filter_by(email=email).first()
        ph = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        if user:
            user.password_hash = ph
            user.is_admin = True
            if name:
                user.name = name
            print(f"updated existing admin: {email}")
        else:
            user = User(
                email=email,
                password_hash=ph,
                name=name or "Admin",
                is_admin=True,
            )
            s.add(user)
            print(f"created admin: {email}")
        s.commit()


def _print_help() -> None:
    print(__doc__ or "")


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] in ("-h", "--help", "help"):
        _print_help()
        return 0

    if len(argv) >= 4 and argv[1] == "create-admin":
        email = argv[2]
        password = argv[3]
        name = argv[4] if len(argv) > 4 else "Admin"
        ensure_admin_user(email, password, name=name)
        return 0

    if len(argv) >= 2 and argv[1] == "create-admin":
        print("usage: python -m src.db_migrate create-admin <email> <password> [name]")
        return 2

    migrate()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
