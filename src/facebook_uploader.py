"""
موديول الرفع لـ Facebook Page

بيستخدم Graph API v21.0:
  - Long-form videos: POST /{page_id}/videos (multipart upload)
  - Reels (9:16 vertical): 3-step Reels API (start → upload → finish)

محتاج page_access_token طويل الأجل (long-lived). اقرأ الـ credentials من
credentials/meta_credentials.json (موجود مسبقاً).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

GRAPH_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
GRAPH_VIDEO_BASE = f"https://graph-video.facebook.com/{GRAPH_VERSION}"
RUPLOAD_BASE = f"https://rupload.facebook.com/video-upload/{GRAPH_VERSION}"

# Limits
FB_DESC_MAX = 5000
UPLOAD_TIMEOUT = 600  # 10 min per chunk


def load_meta_credentials(
    credentials_path: str | Path = "credentials/meta_credentials.json",
) -> dict:
    """يرجع dict فيه app_id, app_secret, pages list. لو الملف مش موجود، يرفع FileNotFoundError."""
    p = Path(credentials_path)
    if not p.is_absolute():
        # حاول من الـ cwd الأول
        candidate = Path.cwd() / p
        if not candidate.exists():
            # fallback: المجلد الأب لـ src/
            candidate = Path(__file__).resolve().parent.parent / p
        p = candidate
    if not p.exists():
        raise FileNotFoundError(f"meta credentials مش موجود: {p}")
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


# Single-request POST starts failing with 413 above ~100MB. Above this size
# we use the resumable upload protocol (start/transfer/finish phases).
FB_DIRECT_UPLOAD_LIMIT = 95 * 1024 * 1024  # 95 MB safety threshold


def _upload_video_resumable(
    page_id: str,
    page_access_token: str,
    video_path: Path,
    title: str,
    description: str,
    published: bool,
    scheduled_publish_time: Optional[int],
) -> str:
    """Resumable upload for large Page videos (>~100MB).

    Three-phase protocol: start → transfer (chunks) → finish.
    Returns FB video_id on success.
    """
    file_size = video_path.stat().st_size
    url = f"{GRAPH_VIDEO_BASE}/{page_id}/videos"
    logger.info(
        f"FB resumable start: {video_path.name} ({file_size / (1024*1024):.1f} MB)"
    )

    # Phase 1: start — FB returns the first chunk's offsets
    r = requests.post(
        url,
        data={
            "access_token": page_access_token,
            "upload_phase": "start",
            "file_size": str(file_size),
        },
        timeout=120,
    )
    if r.status_code != 200:
        logger.error(f"FB resumable start فشل: {r.status_code} - {r.text[:500]}")
        r.raise_for_status()
    body = r.json()
    upload_session_id = body["upload_session_id"]
    fb_video_id = body["video_id"]
    start_offset = int(body["start_offset"])
    end_offset = int(body["end_offset"])

    # Phase 2: transfer chunks until FB returns start==end (or we've sent all bytes)
    with video_path.open("rb") as fh:
        chunk_num = 0
        while start_offset < file_size:
            chunk_size = end_offset - start_offset
            if chunk_size <= 0:
                break
            fh.seek(start_offset)
            chunk = fh.read(chunk_size)
            chunk_num += 1
            pct = 100.0 * end_offset / file_size
            logger.info(
                f"FB chunk #{chunk_num}: {start_offset}-{end_offset} "
                f"({chunk_size/(1024*1024):.1f} MB) [{pct:.0f}%]"
            )
            r = requests.post(
                url,
                data={
                    "access_token": page_access_token,
                    "upload_phase": "transfer",
                    "upload_session_id": upload_session_id,
                    "start_offset": str(start_offset),
                },
                files={
                    "video_file_chunk": (
                        video_path.name, chunk, "application/octet-stream",
                    )
                },
                timeout=UPLOAD_TIMEOUT,
            )
            if r.status_code != 200:
                logger.error(f"FB chunk #{chunk_num} فشل: {r.status_code} - {r.text[:500]}")
                r.raise_for_status()
            nb = r.json()
            new_start = int(nb.get("start_offset", end_offset))
            new_end = int(nb.get("end_offset", end_offset))
            if new_start == new_end:
                break  # FB says we're done
            start_offset, end_offset = new_start, new_end

    # Phase 3: finish — commit the upload with title/description
    finish_data = {
        "access_token": page_access_token,
        "upload_phase": "finish",
        "upload_session_id": upload_session_id,
        "title": title,
        "description": description,
    }
    if scheduled_publish_time is not None:
        finish_data["published"] = "false"
        finish_data["scheduled_publish_time"] = str(int(scheduled_publish_time))
    else:
        finish_data["published"] = "true" if published else "false"

    r = requests.post(url, data=finish_data, timeout=120)
    if r.status_code != 200:
        logger.error(f"FB resumable finish فشل: {r.status_code} - {r.text[:500]}")
        r.raise_for_status()
    logger.info(f"تم رفع فيديو على FB Page (resumable): {fb_video_id}")
    return fb_video_id


def upload_video_to_page(
    page_id: str,
    page_access_token: str,
    video_path: str | Path,
    title: str,
    description: str,
    published: bool = True,
    scheduled_publish_time: Optional[int] = None,
) -> str:
    """
    يرفع فيديو طبيعي (16:9) لـ Facebook Page. بيرجع FB video_id.

    للملفات الأكبر من ~95MB بنستخدم الـ resumable upload protocol تلقائياً
    (start/transfer/finish) عشان نتجنب 413 Request Entity Too Large.

    Args:
        scheduled_publish_time: لو محدد (Unix timestamp بالثواني), هيتجدول وبتتعمل
                                تلقائياً published=False.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"الفيديو مش موجود: {video_path}")

    # قص الـ description لو طويل
    title = (title or "").strip()[:255]
    description = (description or "").strip()[:FB_DESC_MAX]

    file_size = video_path.stat().st_size

    # Use resumable for large files
    if file_size > FB_DIRECT_UPLOAD_LIMIT:
        return _upload_video_resumable(
            page_id=page_id, page_access_token=page_access_token,
            video_path=video_path, title=title, description=description,
            published=published, scheduled_publish_time=scheduled_publish_time,
        )

    url = f"{GRAPH_VIDEO_BASE}/{page_id}/videos"
    data = {
        "access_token": page_access_token,
        "title": title,
        "description": description,
    }
    if scheduled_publish_time is not None:
        data["published"] = "false"
        data["scheduled_publish_time"] = str(int(scheduled_publish_time))
    else:
        data["published"] = "true" if published else "false"

    logger.info(
        f"بدء رفع فيديو على FB Page: {video_path.name} ({file_size / (1024 * 1024):.1f} MB)"
    )

    with video_path.open("rb") as fh:
        files = {"source": (video_path.name, fh, "video/mp4")}
        try:
            resp = requests.post(url, data=data, files=files, timeout=UPLOAD_TIMEOUT)
        except requests.RequestException as e:
            logger.error(f"خطأ شبكة في رفع FB video: {e}")
            raise

    if resp.status_code != 200:
        logger.error(f"FB upload فشل: {resp.status_code} - {resp.text[:500]}")
        resp.raise_for_status()

    body = resp.json()
    video_id = body.get("id")
    if not video_id:
        raise RuntimeError(f"FB رد بدون video_id: {body}")
    logger.info(f"تم رفع فيديو على FB Page: {video_id}")
    return video_id


def upload_reel_to_page(
    page_id: str,
    page_access_token: str,
    video_path: str | Path,
    description: str,
) -> str:
    """
    يرفع Reel رأسي (9:16) لـ Facebook Page. بيرجع FB video_id.

    خطوات الـ Reels API الثلاث:
    1) start: POST /{page-id}/video_reels?upload_phase=start → بيرجع video_id + upload_url
    2) upload: POST <upload_url> ببنية binary + offset/file_size headers
    3) finish: POST /{page-id}/video_reels?upload_phase=finish&video_state=PUBLISHED&description=...

    المرجع: https://developers.facebook.com/docs/video-api/guides/reels-publishing
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"ملف الـ Reel مش موجود: {video_path}")

    description = (description or "").strip()[:FB_DESC_MAX]
    file_size = video_path.stat().st_size

    # ---- Step 1: start ----
    start_url = f"{GRAPH_BASE}/{page_id}/video_reels"
    start_params = {
        "upload_phase": "start",
        "access_token": page_access_token,
    }
    logger.info(f"FB Reel start: {video_path.name} ({file_size / (1024 * 1024):.1f} MB)")
    r1 = requests.post(start_url, params=start_params, timeout=60)
    if r1.status_code != 200:
        logger.error(f"FB Reel start فشل: {r1.status_code} - {r1.text[:500]}")
        r1.raise_for_status()
    start_body = r1.json()
    video_id = start_body.get("video_id") or start_body.get("id")
    upload_url = start_body.get("upload_url")
    if not video_id or not upload_url:
        raise RuntimeError(f"FB Reel start: missing video_id/upload_url: {start_body}")

    # ---- Step 2: upload binary ----
    headers = {
        "Authorization": f"OAuth {page_access_token}",
        "offset": "0",
        "file_size": str(file_size),
    }
    with video_path.open("rb") as fh:
        try:
            r2 = requests.post(
                upload_url,
                headers=headers,
                data=fh.read(),
                timeout=UPLOAD_TIMEOUT,
            )
        except requests.RequestException as e:
            logger.error(f"خطأ شبكة في FB Reel upload: {e}")
            raise

    if r2.status_code != 200:
        logger.error(f"FB Reel upload فشل: {r2.status_code} - {r2.text[:500]}")
        r2.raise_for_status()
    upload_body = r2.json() if r2.text else {}
    if not upload_body.get("success", True):
        raise RuntimeError(f"FB Reel upload لم ينجح: {upload_body}")

    # ---- Step 3: finish ----
    finish_url = f"{GRAPH_BASE}/{page_id}/video_reels"
    finish_params = {
        "upload_phase": "finish",
        "video_id": video_id,
        "video_state": "PUBLISHED",
        "description": description,
        "access_token": page_access_token,
    }
    r3 = requests.post(finish_url, params=finish_params, timeout=120)
    if r3.status_code != 200:
        logger.error(f"FB Reel finish فشل: {r3.status_code} - {r3.text[:500]}")
        r3.raise_for_status()
    finish_body = r3.json()
    if not finish_body.get("success", True):
        # ساعات بترجع success=false مع error feedback
        raise RuntimeError(f"FB Reel finish لم ينجح: {finish_body}")

    logger.info(f"تم نشر FB Reel: {video_id}")
    return video_id


def get_post_status(video_id: str, page_access_token: str) -> dict:
    """يقرأ حالة فيديو/Reel على FB. مفيد للـ debug/polling."""
    url = f"{GRAPH_BASE}/{video_id}"
    params = {
        "fields": "id,status,published,permalink_url",
        "access_token": page_access_token,
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            logger.warning(f"FB get_post_status فشل: {resp.status_code} - {resp.text[:300]}")
            return {"error": resp.text}
        return resp.json()
    except requests.RequestException as e:
        logger.warning(f"FB get_post_status خطأ شبكة: {e}")
        return {"error": str(e)}
