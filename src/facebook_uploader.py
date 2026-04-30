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

    file_size = video_path.stat().st_size
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
