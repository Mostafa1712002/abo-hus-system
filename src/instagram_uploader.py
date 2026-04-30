"""
موديول الرفع لـ Instagram Reels

بيستخدم Instagram Graph API v21.0 — Content Publishing مع upload_type=resumable
عشان نرفع الـ video مباشرة (بدون CDN خارجي).

الخطوات:
  1) Create container: POST /{ig-id}/media?media_type=REELS&upload_type=resumable&caption=&...
     → يرجع {"id": container_id, "uri": upload_uri}
  2) Upload binary to upload_uri مع OAuth header + offset + file_size
  3) Poll status until FINISHED (max ~5 دقائق)
  4) Publish: POST /{ig-id}/media_publish?creation_id=container_id

محدوديات Reels:
  - 9:16 vertical
  - <= 90 ثانية، <= 100 MB
  - caption max 2200 char
  - max ~30 hashtag
  - max 50 published posts/24h
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

GRAPH_VERSION = "v21.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

IG_CAPTION_MAX = 2200
IG_REEL_MAX_BYTES = 100 * 1024 * 1024  # 100 MB
POLL_TIMEOUT_SECONDS = 300  # 5 دقائق
POLL_INTERVAL = 5
UPLOAD_TIMEOUT = 600  # 10 min


def upload_reel_to_instagram(
    ig_business_account_id: str,
    page_access_token: str,
    video_path: str | Path,
    caption: str,
    cover_url: Optional[str] = None,
    share_to_feed: bool = True,
) -> str:
    """
    يرفع Reel على Instagram. بيرجع IG media_id (الـ Reel المنشور).

    الـ flow بـ 4 خطوات (راجع docstring الموديول).
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"ملف الـ Reel مش موجود: {video_path}")

    file_size = video_path.stat().st_size
    if file_size > IG_REEL_MAX_BYTES:
        logger.warning(
            f"حجم الـ Reel ({file_size / (1024 * 1024):.1f} MB) يتعدى حد IG (100 MB) — قد يفشل"
        )

    # قص الـ caption لـ 2200 حرف
    caption = (caption or "").strip()
    if len(caption) > IG_CAPTION_MAX:
        caption = caption[: IG_CAPTION_MAX - 1].rstrip() + "…"

    # ---- Step 1: create container with resumable upload ----
    create_url = f"{GRAPH_BASE}/{ig_business_account_id}/media"
    create_params = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": caption,
        "share_to_feed": "true" if share_to_feed else "false",
        "access_token": page_access_token,
    }
    if cover_url:
        create_params["cover_url"] = cover_url

    logger.info(
        f"IG Reel create container: {video_path.name} "
        f"({file_size / (1024 * 1024):.1f} MB)"
    )
    r1 = requests.post(create_url, params=create_params, timeout=60)
    if r1.status_code != 200:
        logger.error(f"IG container create فشل: {r1.status_code} - {r1.text[:500]}")
        r1.raise_for_status()
    body = r1.json()
    container_id = body.get("id")
    upload_uri = body.get("uri")
    if not container_id or not upload_uri:
        raise RuntimeError(f"IG container create response بدون id/uri: {body}")

    # ---- Step 2: upload binary ----
    headers = {
        "Authorization": f"OAuth {page_access_token}",
        "offset": "0",
        "file_size": str(file_size),
    }
    with video_path.open("rb") as fh:
        try:
            r2 = requests.post(
                upload_uri,
                headers=headers,
                data=fh.read(),
                timeout=UPLOAD_TIMEOUT,
            )
        except requests.RequestException as e:
            logger.error(f"خطأ شبكة في IG Reel upload: {e}")
            raise

    if r2.status_code != 200:
        logger.error(f"IG Reel upload فشل: {r2.status_code} - {r2.text[:500]}")
        r2.raise_for_status()
    upload_body = r2.json() if r2.text else {}
    if not upload_body.get("success", True):
        raise RuntimeError(f"IG Reel upload لم ينجح: {upload_body}")

    # ---- Step 3: poll status ----
    status_url = f"{GRAPH_BASE}/{container_id}"
    status_params = {
        "fields": "status_code,status",
        "access_token": page_access_token,
    }
    deadline = time.time() + POLL_TIMEOUT_SECONDS
    last_status: dict = {}
    while time.time() < deadline:
        try:
            sr = requests.get(status_url, params=status_params, timeout=30)
        except requests.RequestException as e:
            logger.warning(f"IG status poll خطأ شبكة، إعادة المحاولة: {e}")
            time.sleep(POLL_INTERVAL)
            continue
        if sr.status_code != 200:
            logger.warning(
                f"IG status poll فشل {sr.status_code}: {sr.text[:300]}"
            )
            time.sleep(POLL_INTERVAL)
            continue
        last_status = sr.json()
        code = (last_status.get("status_code") or "").upper()
        if code == "FINISHED":
            logger.info("IG container جاهز للنشر")
            break
        if code == "ERROR":
            raise RuntimeError(
                f"IG container في حالة ERROR: {last_status.get('status') or last_status}"
            )
        # IN_PROGRESS / PUBLISHED / EXPIRED
        time.sleep(POLL_INTERVAL)
    else:
        raise TimeoutError(
            f"IG container لم يصل لـ FINISHED خلال {POLL_TIMEOUT_SECONDS}s. "
            f"آخر حالة: {last_status}"
        )

    # ---- Step 4: publish ----
    publish_url = f"{GRAPH_BASE}/{ig_business_account_id}/media_publish"
    publish_params = {
        "creation_id": container_id,
        "access_token": page_access_token,
    }
    pr = requests.post(publish_url, params=publish_params, timeout=60)
    if pr.status_code != 200:
        logger.error(f"IG publish فشل: {pr.status_code} - {pr.text[:500]}")
        pr.raise_for_status()
    pub_body = pr.json()
    media_id = pub_body.get("id")
    if not media_id:
        raise RuntimeError(f"IG publish response بدون media_id: {pub_body}")
    logger.info(f"تم نشر IG Reel: {media_id}")
    return media_id


def check_publishing_limit(
    ig_business_account_id: str, page_access_token: str
) -> dict:
    """
    يقرأ quota IG content publishing.
    GET /{ig-id}/content_publishing_limit?access_token=...
    """
    url = f"{GRAPH_BASE}/{ig_business_account_id}/content_publishing_limit"
    params = {
        "fields": "quota_usage,config",
        "access_token": page_access_token,
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            logger.warning(
                f"IG check_publishing_limit فشل: {resp.status_code} - {resp.text[:300]}"
            )
            return {"error": resp.text, "status_code": resp.status_code}
        return resp.json()
    except requests.RequestException as e:
        logger.warning(f"IG check_publishing_limit خطأ شبكة: {e}")
        return {"error": str(e)}
