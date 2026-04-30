"""لوحة تحكم الرفع التلقائي - FastAPI dashboard.

Read-only monitoring UI for the YouTube/FB/IG/Telegram pipeline with a small
retry mutation. Designed to run locally on the operator's machine; deploy
notes for production are in `docs/dashboard.md`.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from src.config import Config
from src.db import User, get_session_factory, init_db
from src.pending_tracker import PendingTracker
from src.wave_planner import (
    find_videos_in_series,
    get_series_in_wave,
)

logger = logging.getLogger(__name__)

# --- Project paths ----------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PENDING_FILE = PROJECT_ROOT / "output" / "pending.json"
LOGS_DIR = PROJECT_ROOT / "logs"
PROCESS_LOG = LOGS_DIR / "process_check.log"
UPLOADER_LOG = LOGS_DIR / "uploader.log"
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"
CONFIG_PATH = PROJECT_ROOT / "config.json"

# Server-side logs (cron + systemd) live under /var/log on the production
# server. On Windows / dev machines this directory simply won't exist and
# we silently skip it.
SERVER_LOGS_DIR = Path("/var/log")
SERVER_LOG_GLOB = "abuhafs-*.log"

# Local logs the dashboard knows about. Order matters — the UI renders
# them top-to-bottom in this exact sequence so the most-watched files are
# above the fold.
LOCAL_LOG_NAMES = (
    "uploader.log",
    "cold_upload.log",
    "transcode.log",
    "sync_ops.log",
    "setup.log",
    "process_check.log",
)


# --- App --------------------------------------------------------------------
app = FastAPI(
    title="لوحة تحكم - قناة الشيخ سامي العربي",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url=None,
)

# Session middleware. Use ABUHAFS_SESSION_SECRET in prod; for dev we generate a
# fresh ephemeral secret per process and warn the operator that sessions will
# not survive a restart.
_SESSION_SECRET = os.environ.get("ABUHAFS_SESSION_SECRET")
if not _SESSION_SECRET:
    _SESSION_SECRET = secrets.token_urlsafe(32)
    logger.warning(
        "ABUHAFS_SESSION_SECRET not set — using an ephemeral random secret. "
        "Logins will be invalidated when the dashboard process restarts. "
        "Set this env var to a stable random string in systemd to fix."
    )
app.add_middleware(
    SessionMiddleware,
    secret_key=_SESSION_SECRET,
    session_cookie="abuhafs_session",
    max_age=14 * 24 * 60 * 60,  # 14 days
    same_site="lax",
    https_only=False,  # nginx in front handles TLS termination
)

# Initialize the DB lazily but eagerly enough that `current_user` doesn't have
# to do it on every request. Failures here are tolerated — the auth dependency
# will still kick users to /login.
try:
    _engine = init_db()
    _Session = get_session_factory(_engine)
except Exception as _e:  # pragma: no cover — defensive
    logger.error("DB init failed: %s — auth will fail open to /login", _e)
    _engine = None
    _Session = None

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# --- Helpers ----------------------------------------------------------------
def _load_tracker() -> PendingTracker:
    """Always re-load from disk so the dashboard reflects pipeline writes."""
    return PendingTracker(PENDING_FILE)


def _load_config() -> Optional[Config]:
    try:
        return Config(str(CONFIG_PATH))
    except Exception as e:  # config might be missing in dev
        logger.warning("Config load failed: %s", e)
        return None


def _video_links(item_dict: dict) -> dict:
    """Build watch URLs for the video on each platform."""
    vid = item_dict.get("video_id") or ""
    meta = item_dict.get("metadata") or {}

    yt_url = f"https://www.youtube.com/watch?v={vid}" if vid else ""
    yt_short_urls = [
        f"https://www.youtube.com/shorts/{sid}"
        for sid in meta.get("short_video_ids", [])
        if sid
    ]

    fb_main = meta.get("fb_main_video_id")
    fb_url = f"https://www.facebook.com/watch/?v={fb_main}" if fb_main else ""
    fb_short_urls = [
        f"https://www.facebook.com/watch/?v={sid}"
        for sid in meta.get("fb_short_video_ids", [])
        if sid
    ]

    ig_short_urls = [
        f"https://www.instagram.com/reel/{mid}/"
        for mid in meta.get("ig_short_media_ids", [])
        if mid
    ]

    tg_main = meta.get("tg_main_message_id")
    tg_short = meta.get("tg_short_message_ids", []) or []
    tg_quote = meta.get("tg_quote_message_ids", []) or []

    return {
        "youtube": yt_url,
        "youtube_shorts": yt_short_urls,
        "facebook": fb_url,
        "facebook_shorts": fb_short_urls,
        "instagram_reels": ig_short_urls,
        "telegram_main_message_id": tg_main,
        "telegram_short_message_ids": tg_short,
        "telegram_quote_message_ids": tg_quote,
    }


def _is_today_utc(iso_ts: str) -> bool:
    if not iso_ts:
        return False
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        today = datetime.now(timezone.utc).date()
        return dt.astimezone(timezone.utc).date() == today
    except Exception:
        return False


def _tail(path: Path, n: int) -> list[str]:
    """Return the last n lines of a text file. Safe on missing/locked files."""
    if not path.exists():
        return []
    try:
        # deque gives O(n) memory regardless of file size and is safe with
        # the file potentially being appended-to by another process.
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return list(deque(f, maxlen=n))
    except Exception as e:
        logger.warning("tail %s failed: %s", path, e)
        return []


def _format_dict(item) -> dict:
    """asdict but tolerant of missing fields."""
    try:
        return asdict(item)
    except Exception:
        # Fall back to __dict__
        return dict(item.__dict__)


# --- Auth -------------------------------------------------------------------
class AuthRedirect(HTTPException):
    """Marker exception so the global handler can redirect HTML clients."""

    def __init__(self):
        super().__init__(status_code=401, detail="login required")


def _get_user_by_id(user_id: int) -> Optional[User]:
    if _Session is None:
        return None
    try:
        with _Session() as s:
            return s.get(User, user_id)
    except Exception as e:
        logger.warning("get_user_by_id failed: %s", e)
        return None


def current_user(request: Request) -> User:
    """Dependency: returns the logged-in User or raises AuthRedirect (401)."""
    uid = request.session.get("user_id")
    if not uid:
        raise AuthRedirect()
    user = _get_user_by_id(int(uid))
    if user is None:
        # Stale session.
        request.session.clear()
        raise AuthRedirect()
    return user


def _is_html_request(request: Request) -> bool:
    """Heuristic: is this a browser navigation (vs an HTMX/JSON XHR)?"""
    if request.headers.get("hx-request") == "true":
        return False
    accept = request.headers.get("accept", "")
    return "text/html" in accept


@app.exception_handler(AuthRedirect)
async def _auth_redirect_handler(request: Request, exc: AuthRedirect):
    if _is_html_request(request):
        next_url = request.url.path
        if request.url.query:
            next_url += f"?{request.url.query}"
        return RedirectResponse(
            url=f"/login?next={next_url}", status_code=303
        )
    # API/HTMX clients get a JSON 401 with an HX-Redirect hint so HTMX can act.
    return JSONResponse(
        {"detail": "login required"},
        status_code=401,
        headers={"HX-Redirect": "/login"},
    )


# --- Routes: Auth -----------------------------------------------------------
@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_form(request: Request, next: str = "/", error: Optional[str] = None):
    # If already logged in, bounce to dashboard.
    uid = request.session.get("user_id")
    if uid and _get_user_by_id(int(uid)) is not None:
        return RedirectResponse(url=next or "/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next": next or "/",
            "error": error,
            "channel_name": "قناة الشيخ سامي العربي",
        },
    )


@app.post("/login", include_in_schema=False)
async def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    import bcrypt

    if _Session is None:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "next": next or "/",
                "error": "تعذّر الاتصال بقاعدة البيانات.",
                "channel_name": "قناة الشيخ سامي العربي",
            },
            status_code=500,
        )

    email = (email or "").strip().lower()
    if not email or not password:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "next": next or "/",
                "error": "أدخل البريد وكلمة المرور.",
                "channel_name": "قناة الشيخ سامي العربي",
            },
            status_code=400,
        )

    with _Session() as s:
        user = s.query(User).filter_by(email=email).first()
        if user is None:
            # Constant-ish work to avoid trivial timing oracle.
            bcrypt.checkpw(b"x", bcrypt.hashpw(b"x", bcrypt.gensalt()))
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "next": next or "/",
                    "error": "البريد أو كلمة المرور غير صحيحة.",
                    "channel_name": "قناة الشيخ سامي العربي",
                },
                status_code=401,
            )
        try:
            ok = bcrypt.checkpw(
                password.encode("utf-8"), user.password_hash.encode("utf-8")
            )
        except Exception as e:
            logger.warning("bcrypt check failed for %s: %s", email, e)
            ok = False
        if not ok:
            return templates.TemplateResponse(
                request,
                "login.html",
                {
                    "next": next or "/",
                    "error": "البريد أو كلمة المرور غير صحيحة.",
                    "channel_name": "قناة الشيخ سامي العربي",
                },
                status_code=401,
            )
        user.last_login = datetime.utcnow()
        s.commit()
        request.session["user_id"] = user.id
        request.session["user_email"] = user.email
        request.session["user_name"] = user.name or user.email

    # Allow only relative redirects to avoid open-redirects.
    safe_next = next if next and next.startswith("/") else "/"
    return RedirectResponse(url=safe_next, status_code=303)


@app.get("/logout", include_in_schema=False)
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


# --- Routes: HTML -----------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request, user: User = Depends(current_user)):
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "channel_name": "قناة الشيخ سامي العربي",
            "user_name": user.name or user.email,
            "user_email": user.email,
        },
    )


# --- Routes: API ------------------------------------------------------------
@app.get("/api/stats")
async def api_stats(user: User = Depends(current_user)):
    tracker = _load_tracker()
    items = tracker.all()

    counts = {
        "uploaded": 0,
        "captions_ready": 0,
        "processing": 0,
        "completed": 0,
        "failed": 0,
        "total": len(items),
    }
    today_completed = 0
    for it in items:
        counts[it.status] = counts.get(it.status, 0) + 1
        if it.status == "completed" and _is_today_utc(it.completed_at):
            today_completed += 1

    pending_total = (
        counts.get("uploaded", 0)
        + counts.get("captions_ready", 0)
        + counts.get("processing", 0)
    )

    return {
        "pending": counts.get("uploaded", 0) + counts.get("captions_ready", 0),
        "processing": counts.get("processing", 0),
        "completed": counts.get("completed", 0),
        "failed": counts.get("failed", 0),
        "today_completed": today_completed,
        "pending_total": pending_total,
        "total": counts["total"],
        "by_status": counts,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/videos")
async def api_videos(
    status: Optional[str] = Query(None),
    series: Optional[str] = Query(None),
    limit: int = Query(500, ge=1, le=5000),
    user: User = Depends(current_user),
):
    tracker = _load_tracker()
    items = [_format_dict(i) for i in tracker.all()]
    if status:
        items = [i for i in items if i.get("status") == status]
    if series:
        items = [i for i in items if (i.get("series") or "") == series]
    items.sort(key=lambda i: i.get("uploaded_at") or "", reverse=True)
    items = items[:limit]
    for i in items:
        i["links"] = _video_links(i)
    return {"count": len(items), "videos": items}


@app.get("/api/video/{video_id}")
async def api_video_one(video_id: str, user: User = Depends(current_user)):
    tracker = _load_tracker()
    item = tracker.get(video_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"video_id {video_id} not found")
    d = _format_dict(item)
    d["links"] = _video_links(d)
    return d


@app.get("/api/logs")
async def api_logs(
    n: int = Query(200, ge=1, le=5000),
    user: User = Depends(current_user),
):
    return {
        "process_check": _tail(PROCESS_LOG, n),
        "uploader": _tail(UPLOADER_LOG, n),
        "n": n,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _resolve_log_path(name: str) -> Optional[Path]:
    """Resolve a log filename to a real path under either logs/ or /var/log.

    Hardened against directory-traversal: only the bare filename is taken
    (no slashes accepted), and the resolved path must live inside one of
    the two known log directories. ``None`` is returned for anything we
    don't recognize so the caller can return 404.
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    # Local logs first.
    p = (LOGS_DIR / name).resolve()
    try:
        p.relative_to(LOGS_DIR.resolve())
        if p.exists():
            return p
    except ValueError:
        return None
    # Server logs — only allow the abuhafs-* pattern.
    if SERVER_LOGS_DIR.exists() and name.startswith("abuhafs-") and name.endswith(".log"):
        sp = (SERVER_LOGS_DIR / name).resolve()
        try:
            sp.relative_to(SERVER_LOGS_DIR.resolve())
            if sp.exists():
                return sp
        except ValueError:
            return None
    return None


def _list_log_files() -> list[dict]:
    """Return metadata for every log file the dashboard knows about.

    Each entry: ``{"name", "path", "size", "mtime", "exists"}``. The
    ordering is: local logs (in ``LOCAL_LOG_NAMES`` order) followed by any
    ``/var/log/abuhafs-*.log`` discovered on the server.
    """
    out: list[dict] = []
    seen: set[str] = set()

    for name in LOCAL_LOG_NAMES:
        p = LOGS_DIR / name
        seen.add(name)
        st = None
        if p.exists():
            try:
                st = p.stat()
            except OSError:
                st = None
        out.append({
            "name": name,
            "path": str(p),
            "size": st.st_size if st else 0,
            "mtime": (
                datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
                if st else ""
            ),
            "exists": st is not None,
            "scope": "local",
        })

    # Pick up any extra local logs we don't know by name (so future log
    # files appear without code changes).
    if LOGS_DIR.exists():
        try:
            extras = sorted(p for p in LOGS_DIR.iterdir()
                            if p.is_file() and p.suffix == ".log"
                            and p.name not in seen)
        except OSError:
            extras = []
        for p in extras:
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({
                "name": p.name,
                "path": str(p),
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "exists": True,
                "scope": "local",
            })

    # Server-side logs (only on the production deployment).
    if SERVER_LOGS_DIR.exists():
        try:
            srv = sorted(SERVER_LOGS_DIR.glob(SERVER_LOG_GLOB))
        except OSError:
            srv = []
        for p in srv:
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({
                "name": p.name,
                "path": str(p),
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                "exists": True,
                "scope": "server",
            })

    return out


@app.get("/api/logs/{name}")
async def api_log_one(
    name: str,
    n: int = Query(200, ge=1, le=10000),
    user: User = Depends(current_user),
):
    """Return the last `n` lines of the named log file.

    Accepts either a bare local log filename (``cold_upload.log``) or a
    server log filename (``abuhafs-process.log``). Returns 404 for unknown
    or path-traversing names.
    """
    p = _resolve_log_path(name)
    if p is None:
        raise HTTPException(status_code=404, detail=f"unknown log: {name}")
    try:
        st = p.stat()
        size = st.st_size
        mtime = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
    except OSError:
        size, mtime = 0, ""
    return {
        "name": name,
        "lines": _tail(p, n),
        "size": size,
        "mtime": mtime,
        "n": n,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/logs-list")
async def api_logs_list(user: User = Depends(current_user)):
    """List every log file the dashboard can show, with size + mtime."""
    return {
        "logs": _list_log_files(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/library")
async def api_library(user: User = Depends(current_user)):
    cfg = _load_config()
    input_root_str = ""
    if cfg is not None:
        input_root_str = cfg.paths.get("videos_input", "")
    if not input_root_str:
        return {"error": "videos_input path is not configured", "waves": []}

    input_root = Path(input_root_str)
    tracker = _load_tracker()
    all_items = tracker.all()

    # Build lookup: original_path -> status
    path_to_status: dict[str, str] = {}
    for it in all_items:
        if it.original_path:
            path_to_status[it.original_path] = it.status

    waves_out = []
    for wave in (3, 2, 1):
        series_paths = []
        try:
            series_paths = get_series_in_wave(input_root, wave)
        except Exception as e:
            logger.warning("get_series_in_wave failed: %s", e)
            series_paths = []

        videos_total = 0
        videos_uploaded = 0  # any status (uploaded/processing/completed/failed)
        videos_completed = 0
        videos_failed = 0
        series_breakdown = []

        for sp in series_paths:
            try:
                videos = find_videos_in_series(sp)
            except Exception:
                videos = []

            s_total = len(videos)
            s_uploaded = 0
            s_completed = 0
            s_failed = 0
            for v in videos:
                st = path_to_status.get(str(v))
                if st is None:
                    continue
                s_uploaded += 1
                if st == "completed":
                    s_completed += 1
                elif st == "failed":
                    s_failed += 1

            videos_total += s_total
            videos_uploaded += s_uploaded
            videos_completed += s_completed
            videos_failed += s_failed

            series_breakdown.append({
                "name": sp.name,
                "videos_total": s_total,
                "videos_uploaded": s_uploaded,
                "videos_completed": s_completed,
                "videos_remaining": max(0, s_total - s_uploaded),
            })

        videos_remaining = max(0, videos_total - videos_uploaded)
        progress_pct = (
            round(100.0 * videos_uploaded / videos_total, 1)
            if videos_total else 0.0
        )

        waves_out.append({
            "wave": wave,
            "series_count": len(series_paths),
            "videos_total": videos_total,
            "videos_uploaded": videos_uploaded,
            "videos_completed": videos_completed,
            "videos_failed": videos_failed,
            "videos_remaining": videos_remaining,
            "progress_pct": progress_pct,
            "series": series_breakdown,
        })

    return {
        "input_root": str(input_root),
        "waves": waves_out,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/recent")
async def api_recent(
    limit: int = Query(20, ge=1, le=200),
    user: User = Depends(current_user),
):
    tracker = _load_tracker()
    items = [
        _format_dict(i) for i in tracker.all()
        if i.status in ("completed", "failed")
    ]
    items.sort(
        key=lambda i: i.get("completed_at") or i.get("uploaded_at") or "",
        reverse=True,
    )
    items = items[:limit]
    for i in items:
        i["links"] = _video_links(i)
    return {"count": len(items), "videos": items}


@app.post("/api/retry/{video_id}")
async def api_retry(video_id: str, user: User = Depends(current_user)):
    tracker = _load_tracker()
    item = tracker.get(video_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"video_id {video_id} not found")
    if item.status != "failed":
        raise HTTPException(
            status_code=400,
            detail=f"only failed videos can be retried, current status={item.status}",
        )
    tracker.update(
        video_id,
        status="uploaded",
        error="",
        captions_checked_count=0,
    )
    return {"ok": True, "video_id": video_id, "status": "uploaded"}


# Convenience: also expose pending.json raw for debugging
@app.get("/api/pending.json", include_in_schema=False)
async def api_pending_raw(user: User = Depends(current_user)):
    if not PENDING_FILE.exists():
        return JSONResponse([])
    try:
        return JSONResponse(json.loads(PENDING_FILE.read_text(encoding="utf-8")))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- HTMX partials (server-rendered fragments) ------------------------------
STATUS_LABEL_AR = {
    "uploaded":       ("في الانتظار",   "yellow"),
    "captions_ready": ("جاهز للمعالجة", "yellow"),
    "processing":     ("قيد المعالجة",  "blue"),
    "completed":      ("مكتمل",         "green"),
    "failed":         ("فشل",           "red"),
}


def _status_badge(status: str) -> str:
    label, color = STATUS_LABEL_AR.get(status, (status, "gray"))
    return (
        f'<span class="badge"><span class="dot dot-{color}"></span>{label}</span>'
    )


def _short_time(iso_ts: str) -> str:
    if not iso_ts:
        return ""
    try:
        dt = datetime.fromisoformat(iso_ts)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso_ts[:16]


def _esc(s: str) -> str:
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _link_btn(url: str, label: str, title: str) -> str:
    if not url:
        return f'<span class="icon-btn disabled" title="{title}">{label}</span>'
    return (
        f'<a href="{_esc(url)}" target="_blank" rel="noopener" '
        f'class="icon-btn" title="{title}">{label}</a>'
    )


def _telegram_link(message_id) -> str:
    """We don't know the channel handle here, so render as inert badge if set."""
    if not message_id:
        return ""
    return f'<span class="icon-btn" title="Telegram message #{message_id}">TG</span>'


def _platform_links_html(item: dict) -> str:
    links = item.get("links") or _video_links(item)
    yt = links.get("youtube") or ""

    fb = links.get("facebook") or ""
    fb_shorts = links.get("facebook_shorts") or []
    if not fb and fb_shorts:
        fb = fb_shorts[0]

    yt_shorts = links.get("youtube_shorts") or []
    yt_short_first = yt_shorts[0] if yt_shorts else ""

    ig_reels = links.get("instagram_reels") or []
    ig_first = ig_reels[0] if ig_reels else ""

    tg_main = links.get("telegram_main_message_id")
    tg_short = links.get("telegram_short_message_ids") or []
    tg_present = bool(tg_main) or bool(tg_short)

    parts = [
        _link_btn(yt, "YT", "YouTube"),
        _link_btn(yt_short_first, "SH", "YouTube Shorts"),
        _link_btn(fb, "FB", "Facebook"),
        _link_btn(ig_first, "IG", "Instagram"),
    ]
    if tg_present:
        parts.append(
            f'<span class="icon-btn" title="Telegram (تم النشر)">TG</span>'
        )
    else:
        parts.append('<span class="icon-btn disabled" title="Telegram">TG</span>')
    return '<div class="flex items-center gap-2 flex-wrap">' + "".join(parts) + "</div>"


@app.get("/partials/stats", response_class=HTMLResponse, include_in_schema=False)
async def partial_stats(user: User = Depends(current_user)):
    s = await api_stats(user=user)
    cards = [
        ("في الانتظار",     s["pending"],          "yellow", "uploaded"),
        ("قيد المعالجة",    s["processing"],       "blue",   "processing"),
        ("مكتمل اليوم",    s["today_completed"],  "green",  "completed"),
        ("فاشل",            s["failed"],           "red",    "failed"),
    ]
    out = []
    for label, value, color, _filt in cards:
        out.append(f"""
        <div class="card p-5">
          <div class="flex items-center justify-between">
            <span class="text-sm text-slate-400">{label}</span>
            <span class="dot dot-{color}"></span>
          </div>
          <div class="text-3xl font-extrabold mt-2 gold">{value}</div>
          <div class="text-xs text-slate-500 mt-1">من إجمالي {s['total']}</div>
        </div>
        """)
    return HTMLResponse("\n".join(out))


@app.get("/partials/library", response_class=HTMLResponse, include_in_schema=False)
async def partial_library(user: User = Depends(current_user)):
    data = await api_library(user=user)
    waves = data.get("waves", [])
    if not waves:
        return HTMLResponse(
            '<div class="text-slate-400 text-sm">'
            'مسار المكتبة غير مهيّأ في config.json (paths.videos_input).'
            '</div>'
        )
    rows = []
    for w in waves:
        wave_label = {3: "الموجة 3 (الأحدث)", 2: "الموجة 2", 1: "الموجة 1 (الأقدم)"}.get(w["wave"], f"موجة {w['wave']}")
        rows.append(f"""
        <tr>
          <td class="font-semibold gold">{wave_label}</td>
          <td>{w['series_count']}</td>
          <td>{w['videos_total']}</td>
          <td><span class="text-emerald-300">{w['videos_uploaded']}</span></td>
          <td><span class="text-amber-200">{w['videos_remaining']}</span></td>
          <td style="min-width: 220px;">
            <div class="flex items-center gap-3">
              <div class="progress flex-1"><span style="width: {w['progress_pct']}%;"></span></div>
              <span class="text-xs text-slate-400">{w['progress_pct']}%</span>
            </div>
          </td>
        </tr>
        """)
    return HTMLResponse(f"""
    <div class="overflow-x-auto">
      <table class="dash">
        <thead>
          <tr>
            <th>الموجة</th>
            <th>عدد السلاسل</th>
            <th>عدد الفيديوهات</th>
            <th>تم رفعها</th>
            <th>الباقي</th>
            <th>التقدم</th>
          </tr>
        </thead>
        <tbody>
          {''.join(rows)}
        </tbody>
      </table>
      <div class="text-xs text-slate-500 mt-3">
        المصدر: {_esc(data.get('input_root', ''))}
      </div>
    </div>
    """)


@app.get("/partials/active", response_class=HTMLResponse, include_in_schema=False)
async def partial_active(user: User = Depends(current_user)):
    tracker = _load_tracker()
    items = [
        _format_dict(i) for i in tracker.all()
        if i.status in ("uploaded", "captions_ready", "processing")
    ]
    items.sort(key=lambda i: i.get("uploaded_at") or "")
    if not items:
        return HTMLResponse(
            '<div class="text-slate-400 text-sm">لا توجد عناصر قيد الانتظار حاليًا.</div>'
        )
    rows = []
    for i in items:
        rows.append(f"""
        <tr>
          <td class="max-w-[420px] truncate" title="{_esc(i.get('title_updated') or i.get('original_name'))}">
            {_esc(i.get('title_updated') or i.get('original_name') or i.get('video_id'))}
          </td>
          <td class="text-slate-300">{_esc(i.get('series') or '')}</td>
          <td>{_status_badge(i.get('status'))}</td>
          <td class="text-slate-400 text-xs">{_short_time(i.get('uploaded_at'))}</td>
          <td class="text-slate-400 text-xs">محاولات الترجمة: {i.get('captions_checked_count', 0)}</td>
        </tr>
        """)
    return HTMLResponse(f"""
    <div class="overflow-x-auto">
      <table class="dash">
        <thead>
          <tr>
            <th>الفيديو</th>
            <th>السلسلة</th>
            <th>الحالة</th>
            <th>تم الرفع</th>
            <th>ملاحظات</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>
    """)


@app.get("/partials/recent", response_class=HTMLResponse, include_in_schema=False)
async def partial_recent(user: User = Depends(current_user)):
    data = await api_recent(limit=20, user=user)
    items = data.get("videos", [])
    if not items:
        return HTMLResponse(
            '<div class="text-slate-400 text-sm">لم يكتمل أي فيديو بعد.</div>'
        )
    rows = []
    for i in items:
        title = i.get("title_updated") or i.get("original_name") or i.get("video_id")
        rows.append(f"""
        <tr>
          <td class="max-w-[360px] truncate" title="{_esc(title)}">
            <a href="https://www.youtube.com/watch?v={_esc(i.get('video_id'))}" target="_blank" rel="noopener" class="hover:gold">
              {_esc(title)}
            </a>
          </td>
          <td class="text-slate-300 text-sm">{_esc(i.get('series') or '')}</td>
          <td>{_status_badge(i.get('status'))}</td>
          <td class="text-slate-400 text-xs whitespace-nowrap">{_short_time(i.get('completed_at') or i.get('uploaded_at'))}</td>
          <td>{_platform_links_html(i)}</td>
        </tr>
        """)
    return HTMLResponse(f"""
    <div class="overflow-x-auto">
      <table class="dash">
        <thead>
          <tr>
            <th>العنوان</th>
            <th>السلسلة</th>
            <th>الحالة</th>
            <th>الوقت</th>
            <th>المنصات</th>
          </tr>
        </thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>
    """)


@app.get("/partials/failed", response_class=HTMLResponse, include_in_schema=False)
async def partial_failed(user: User = Depends(current_user)):
    tracker = _load_tracker()
    items = [_format_dict(i) for i in tracker.all() if i.status == "failed"]
    items.sort(key=lambda i: i.get("completed_at") or i.get("uploaded_at") or "", reverse=True)
    if not items:
        return HTMLResponse(
            '<div class="text-slate-400 text-sm">لا يوجد عناصر فاشلة. الحمد لله.</div>'
        )
    out = []
    for i in items:
        title = i.get("title_updated") or i.get("original_name") or i.get("video_id")
        err = (i.get("error") or "").strip()
        out.append(f"""
        <div class="border rounded-lg p-3 mb-2" style="border-color: var(--line);">
          <div class="flex items-center justify-between gap-3">
            <div class="min-w-0">
              <div class="font-semibold truncate" title="{_esc(title)}">{_esc(title)}</div>
              <div class="text-xs text-slate-400 mt-1">{_esc(i.get('series') or '')} · {_short_time(i.get('completed_at') or i.get('uploaded_at'))}</div>
            </div>
            <button class="retry-btn"
                    hx-post="/api/retry/{_esc(i.get('video_id'))}"
                    hx-confirm="إعادة المحاولة لهذا الفيديو؟"
                    hx-trigger="click"
                    hx-target="#failed-list"
                    hx-swap="none"
                    onclick="setTimeout(() => htmx.trigger('#failed-list', 'load'), 400);">
              إعادة المحاولة
            </button>
          </div>
          {f'<div class="text-xs text-red-300 mt-2 whitespace-pre-wrap break-words">{_esc(err[:400])}</div>' if err else ''}
        </div>
        """)
    return HTMLResponse("".join(out))


@app.get("/partials/logs", response_class=HTMLResponse, include_in_schema=False)
async def partial_logs(
    n: int = Query(100, ge=1, le=2000),
    user: User = Depends(current_user),
):
    process = _tail(PROCESS_LOG, n)
    uploader = _tail(UPLOADER_LOG, n)

    def render(lines: list[str]) -> str:
        if not lines:
            return '<div class="text-slate-500 text-xs">لا توجد سطور.</div>'
        out = []
        for ln in lines:
            t = ln.rstrip("\n")
            cls = ""
            low = t.lower()
            if "error" in low or "failed" in low or "traceback" in low:
                cls = "err"
            elif "completed" in low or " ok " in low or "success" in low:
                cls = "ok"
            out.append(
                f'<div class="{cls}">{_esc(t)}</div>'
                if cls else f'<div>{_esc(t)}</div>'
            )
        return "".join(out)

    return HTMLResponse(f"""
    <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <div>
        <div class="text-xs text-slate-400 mb-2">process_check.log</div>
        <div class="logbox">{render(process)}</div>
      </div>
      <div>
        <div class="text-xs text-slate-400 mb-2">uploader.log</div>
        <div class="logbox">{render(uploader)}</div>
      </div>
    </div>
    """)


# --- Logs Hub ---------------------------------------------------------------
def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _render_log_lines(lines: list[str]) -> str:
    """Color-code log lines: errors red, success green, ts greyed."""
    if not lines:
        return '<div class="text-slate-500 text-xs">لا توجد سطور.</div>'
    out = []
    for ln in lines:
        t = ln.rstrip("\n")
        cls = ""
        low = t.lower()
        if "error" in low or "failed" in low or "traceback" in low or "[error]" in low:
            cls = "err"
        elif (
            "completed" in low or " ok " in low or "success" in low
            or "uploaded" in low or "=== done" in low
        ):
            cls = "ok"
        out.append(
            f'<div class="{cls}">{_esc(t)}</div>'
            if cls else f'<div>{_esc(t)}</div>'
        )
    return "".join(out)


@app.get("/logs", response_class=HTMLResponse)
async def logs_hub(request: Request, user: User = Depends(current_user)):
    """Logs Hub — collapsible sections, one per log file, auto-refresh.

    The page itself is static; each section pulls its own tail via HTMX
    so we can refresh them on independent cadences without re-rendering
    everything at once.
    """
    return templates.TemplateResponse(
        request,
        "logs_hub.html",
        {
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "channel_name": "قناة الشيخ سامي العربي",
            "user_name": user.name or user.email,
            "user_email": user.email,
            "logs": _list_log_files(),
        },
    )


@app.get("/partials/log/{name}", response_class=HTMLResponse, include_in_schema=False)
async def partial_log_one(
    name: str,
    n: int = Query(120, ge=1, le=5000),
    user: User = Depends(current_user),
):
    """HTMX partial: header (size+mtime) + tail of one log."""
    p = _resolve_log_path(name)
    if p is None:
        return HTMLResponse(
            f'<div class="text-slate-500 text-xs">'
            f'الملف غير موجود: {_esc(name)}'
            f'</div>'
        )
    try:
        st = p.stat()
        size = _human_size(st.st_size)
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        size, mtime = "?", "?"
    lines = _tail(p, n)
    return HTMLResponse(f"""
    <div class="flex items-center justify-between text-xs text-slate-400 mb-2">
      <div>{_esc(str(p))}</div>
      <div>{_esc(size)} · آخر تحديث: {_esc(mtime)}</div>
    </div>
    <div class="logbox">{_render_log_lines(lines)}</div>
    """)


# === Series & Detail Views (added by UI expansion agent) ===

from src.wave_planner import (  # noqa: E402  -- kept local to this section
    find_videos_in_series_with_cold_fallback,
    get_series_in_wave_with_cold_fallback,
)


def _ui_pending_to_dict(item) -> dict:
    """Render a PendingVideo into a template-friendly dict (with links)."""
    d = _format_dict(item)
    d["links"] = _video_links(d)
    d["uploaded_at_short"] = _short_time(d.get("uploaded_at") or "")
    d["completed_at_short"] = _short_time(d.get("completed_at") or "")
    return d


def _ui_load_gemini_md_for(title: str | None) -> Optional[dict]:
    """Best-effort match of the Gemini-generated metadata JSON for a video.

    The pipeline writes per-video metadata under ``output/metadata/*.json``.
    We try to match by exact title; if that fails and only one file exists we
    return it (useful early in the pipeline when there is one in-flight item).
    """
    md_dir = PROJECT_ROOT / "output" / "metadata"
    if not md_dir.exists():
        return None
    try:
        candidates = list(md_dir.glob("*.json"))
    except Exception:
        return None
    if not candidates:
        return None
    if title:
        for cand in candidates:
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and data.get("title") == title:
                return data
    if len(candidates) == 1:
        try:
            return json.loads(candidates[0].read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


@app.get("/series", response_class=HTMLResponse)
async def series_list(request: Request, user: User = Depends(current_user)):
    """List all series across all waves with progress per series."""
    cfg = _load_config()
    tracker = _load_tracker()
    all_videos = tracker.all()

    series_data: list[dict] = []
    seen_names: set[str] = set()

    if cfg is not None:
        try:
            input_root = Path(cfg.paths.get("videos_input", ""))
        except Exception:
            input_root = None

        for wave in (1, 2, 3):
            try:
                series_paths = (
                    get_series_in_wave(input_root, wave) if input_root else []
                )
            except Exception as e:
                logger.warning("get_series_in_wave(%s) failed: %s", wave, e)
                series_paths = []

            for sp in series_paths:
                name = sp.name
                if name in seen_names:
                    continue
                seen_names.add(name)
                try:
                    library_videos = find_videos_in_series(sp)
                except Exception:
                    library_videos = []

                uploaded_records = [v for v in all_videos if v.series == name]
                completed = sum(
                    1 for v in uploaded_records if v.status == "completed"
                )
                failed = sum(1 for v in uploaded_records if v.status == "failed")
                in_progress = sum(
                    1 for v in uploaded_records
                    if v.status in ("uploaded", "captions_ready", "processing")
                )
                total = len(library_videos) if library_videos else len(uploaded_records)
                series_data.append({
                    "name": name,
                    "wave": wave,
                    "total": total,
                    "uploaded": len(uploaded_records),
                    "completed": completed,
                    "failed": failed,
                    "in_progress": in_progress,
                    "remaining": max(0, total - len(uploaded_records)),
                    "progress_pct": (
                        round(100 * completed / total, 1) if total else 0
                    ),
                })

    # Add tracker-only series (rows in pending.json whose folder is gone or
    # lives only on cold storage). Default them to wave 3 so they show up.
    tracker_series_names = {v.series for v in all_videos if v.series}
    for name in sorted(tracker_series_names):
        if name in seen_names:
            continue
        seen_names.add(name)
        uploaded_records = [v for v in all_videos if v.series == name]
        completed = sum(1 for v in uploaded_records if v.status == "completed")
        failed = sum(1 for v in uploaded_records if v.status == "failed")
        in_progress = sum(
            1 for v in uploaded_records
            if v.status in ("uploaded", "captions_ready", "processing")
        )
        total = len(uploaded_records)
        series_data.append({
            "name": name,
            "wave": 3,
            "total": total,
            "uploaded": len(uploaded_records),
            "completed": completed,
            "failed": failed,
            "in_progress": in_progress,
            "remaining": 0,
            "progress_pct": (
                round(100 * completed / total, 1) if total else 0
            ),
        })

    series_data.sort(key=lambda s: (-s["wave"], s["name"]))
    total_videos = sum(s["total"] for s in series_data)
    total_uploaded = sum(s["uploaded"] for s in series_data)

    return templates.TemplateResponse(
        request,
        "series_list.html",
        {
            "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "channel_name": "قناة الشيخ سامي العربي",
            "series": series_data,
            "total_videos": total_videos,
            "total_uploaded": total_uploaded,
        },
    )


@app.get("/series/{series_name}", response_class=HTMLResponse)
async def series_detail(
    request: Request,
    series_name: str,
    user: User = Depends(current_user),
):
    """Drill-down: all videos in this series with status and links."""
    cfg = _load_config()
    tracker = _load_tracker()

    uploaded = [v for v in tracker.all() if v.series == series_name]
    uploaded.sort(key=lambda v: v.uploaded_at or "")

    library_videos: list[Path] = []
    if cfg is not None:
        try:
            input_root = Path(cfg.paths.get("videos_input", ""))
            series_dir = input_root / series_name
            if series_dir.exists():
                library_videos = find_videos_in_series(series_dir)
        except Exception as e:
            logger.warning("series_detail library scan failed: %s", e)

    uploaded_paths = {v.original_path for v in uploaded if v.original_path}
    pending_videos = [
        {
            "original_path": str(p),
            "name": p.name,
            "status": "not_started",
        }
        for p in library_videos
        if str(p) not in uploaded_paths
    ]

    uploaded_dicts = [_ui_pending_to_dict(v) for v in uploaded]

    stats = {
        "total": len(uploaded) + len(pending_videos),
        "uploaded": len(uploaded),
        "completed": sum(1 for v in uploaded if v.status == "completed"),
        "remaining": len(pending_videos),
    }

    return templates.TemplateResponse(
        request,
        "series_detail.html",
        {
            "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "channel_name": "قناة الشيخ سامي العربي",
            "series_name": series_name,
            "uploaded": uploaded_dicts,
            "pending_videos": pending_videos,
            "stats": stats,
        },
    )


@app.get("/video/{video_id}", response_class=HTMLResponse)
async def video_detail(
    request: Request,
    video_id: str,
    user: User = Depends(current_user),
):
    """Full detail page for a single video with all platform links."""
    tracker = _load_tracker()
    item = tracker.get(video_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"video_id {video_id} not found")

    vd = _ui_pending_to_dict(item)
    metadata = item.metadata or {}

    gemini_md = _ui_load_gemini_md_for(item.title_updated)

    yt_url = f"https://youtu.be/{video_id}"
    fb_main_id = metadata.get("fb_main_video_id") or ""
    fb_main_url = f"https://www.facebook.com/watch/?v={fb_main_id}" if fb_main_id else ""

    tg_main_id = metadata.get("tg_main_message_id")
    tg_main_url = (
        f"https://t.me/abohafs_elaraby/{tg_main_id}" if tg_main_id else ""
    )

    short_yt_ids = list(metadata.get("short_video_ids") or [])
    fb_short_ids = list(metadata.get("fb_short_video_ids") or [])
    ig_short_ids = list(metadata.get("ig_short_media_ids") or [])
    tg_short_ids = list(metadata.get("tg_short_message_ids") or [])
    important_clips = []
    if gemini_md and isinstance(gemini_md.get("important_clips"), list):
        important_clips = gemini_md["important_clips"]

    shorts: list[dict] = []
    for i, yt_id in enumerate(short_yt_ids):
        if not yt_id:
            continue
        fb_id = fb_short_ids[i] if i < len(fb_short_ids) else ""
        ig_id = ig_short_ids[i] if i < len(ig_short_ids) else ""
        tg_id = tg_short_ids[i] if i < len(tg_short_ids) else ""
        title = ""
        if i < len(important_clips) and isinstance(important_clips[i], dict):
            title = important_clips[i].get("suggested_short_title", "") or ""
        shorts.append({
            "yt_url": f"https://www.youtube.com/shorts/{yt_id}" if yt_id else "",
            "fb_url": f"https://www.facebook.com/watch/?v={fb_id}" if fb_id else "",
            "ig_id": ig_id,
            "tg_url": (
                f"https://t.me/abohafs_elaraby/{tg_id}" if tg_id else ""
            ),
            "title": title,
        })

    quotes = [
        {"url": f"https://t.me/abohafs_elaraby/{q}", "msg_id": q}
        for q in (metadata.get("tg_quote_message_ids") or [])
        if q
    ]

    try:
        raw_json = json.dumps(vd, ensure_ascii=False, indent=2, default=str)
    except Exception:
        raw_json = str(vd)

    return templates.TemplateResponse(
        request,
        "video_detail.html",
        {
            "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "channel_name": "قناة الشيخ سامي العربي",
            "vd": vd,
            "yt_url": yt_url,
            "fb_main_url": fb_main_url,
            "tg_main_url": tg_main_url,
            "shorts": shorts,
            "quotes": quotes,
            "gemini_md": gemini_md,
            "raw_json": raw_json,
        },
    )
