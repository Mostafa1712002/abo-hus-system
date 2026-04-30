"""لوحة تحكم الرفع التلقائي - FastAPI dashboard.

Read-only monitoring UI for the YouTube/FB/IG/Telegram pipeline with a small
retry mutation. Designed to run locally on the operator's machine; deploy
notes for production are in `docs/dashboard.md`.
"""
from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from src.config import Config
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


# --- App --------------------------------------------------------------------
app = FastAPI(
    title="لوحة تحكم - قناة الشيخ سامي العربي",
    version="1.0.0",
    docs_url="/api/docs",
    redoc_url=None,
)

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


# --- Routes: HTML -----------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "channel_name": "قناة الشيخ سامي العربي",
        },
    )


# --- Routes: API ------------------------------------------------------------
@app.get("/api/stats")
async def api_stats():
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
async def api_video_one(video_id: str):
    tracker = _load_tracker()
    item = tracker.get(video_id)
    if not item:
        raise HTTPException(status_code=404, detail=f"video_id {video_id} not found")
    d = _format_dict(item)
    d["links"] = _video_links(d)
    return d


@app.get("/api/logs")
async def api_logs(n: int = Query(200, ge=1, le=5000)):
    return {
        "process_check": _tail(PROCESS_LOG, n),
        "uploader": _tail(UPLOADER_LOG, n),
        "n": n,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/library")
async def api_library():
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
async def api_recent(limit: int = Query(20, ge=1, le=200)):
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
async def api_retry(video_id: str):
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
async def api_pending_raw():
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
async def partial_stats():
    s = await api_stats()
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
async def partial_library():
    data = await api_library()
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
async def partial_active():
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
async def partial_recent():
    data = await api_recent(limit=20)
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
async def partial_failed():
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
async def partial_logs(n: int = Query(100, ge=1, le=2000)):
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
