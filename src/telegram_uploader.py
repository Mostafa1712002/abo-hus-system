"""
موديول الرفع لـ Telegram Channel

بيستخدم Telegram Bot API مباشرة عبر `requests` (بدون مكتبات إضافية).

البوت لازم يكون admin في الـ channel وعنده صلاحية النشر.

الـ endpoints المستخدمة:
  - POST /bot{token}/sendMessage  → نص (HTML/MarkdownV2)
  - POST /bot{token}/sendVideo    → فيديو multipart (حتى 50MB عبر الـ Bot API الرسمي)
  - POST /bot{token}/sendVideoNote → فيديو مدور (max 60s، مربع)
  - GET  /bot{token}/getChat      → بيانات القناة (diagnostic)

محدوديات Telegram Bot API:
  - sendVideo (multipart): 50 MB كحد أقصى للملف
  - caption (تحت الفيديو/الصورة): 1024 حرف
  - sendMessage (نص): 4096 حرف
  - الفيديوهات الأكبر تحتاج local Bot API server (انظر send_video_via_local_bot_api)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional, Union

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"

# Limits
TG_MESSAGE_MAX = 4096
TG_CAPTION_MAX = 1024
TG_VIDEO_MAX_BYTES = 50 * 1024 * 1024  # 50 MB
UPLOAD_TIMEOUT = 600  # 10 min
DEFAULT_TIMEOUT = 60


def load_telegram_credentials(
    path: Union[str, Path] = "credentials/telegram_credentials.json",
) -> dict:
    """يحمّل ملف credentials. يرفع FileNotFoundError لو مش موجود.

    التحقق من validation_status: لازم == 'ok'.
    """
    p = Path(path)
    if not p.is_absolute():
        candidate = Path.cwd() / p
        if not candidate.exists():
            # fallback: المجلد الأب لـ src/
            candidate = Path(__file__).resolve().parent.parent / p
        p = candidate
    if not p.exists():
        raise FileNotFoundError(f"telegram credentials مش موجود: {p}")
    with p.open("r", encoding="utf-8") as f:
        creds = json.load(f)

    status = (creds.get("validation_status") or "").lower()
    if status and status != "ok":
        logger.warning(
            f"telegram credentials.validation_status != 'ok' (= {status!r}) — قد يفشل الرفع"
        )
    if not creds.get("bot_token"):
        raise ValueError("telegram credentials بدون bot_token")
    return creds


def _api_url(bot_token: str, method: str) -> str:
    return f"{TELEGRAM_API_BASE}/bot{bot_token}/{method}"


def _parse_response(resp: requests.Response, action: str) -> dict:
    """يفك رد Telegram. بيرفع RuntimeError لو ok=false."""
    try:
        body = resp.json()
    except ValueError:
        logger.error(f"Telegram {action} رد بدون JSON: {resp.status_code} - {resp.text[:500]}")
        resp.raise_for_status()
        raise RuntimeError(f"Telegram {action}: invalid response")
    if not body.get("ok"):
        desc = body.get("description") or body
        logger.error(f"Telegram {action} فشل: {desc}")
        raise RuntimeError(f"Telegram {action} فشل: {desc}")
    return body.get("result", {})


def send_message(
    bot_token: str,
    chat_id: Union[str, int],
    text: str,
    parse_mode: str = "HTML",
    disable_notification: bool = False,
    disable_web_page_preview: bool = True,
) -> int:
    """يرسل رسالة نصية للقناة. بيرجع message_id.

    لو النص أطول من 4096 حرف (حد Telegram)، بيتم قصه مع لاحقة '... <continued>'.
    """
    if text is None:
        text = ""
    if len(text) > TG_MESSAGE_MAX:
        truncated = text[: TG_MESSAGE_MAX - len("... <continued>")].rstrip()
        text = f"{truncated}... <continued>"

    url = _api_url(bot_token, "sendMessage")
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_notification": disable_notification,
        "disable_web_page_preview": disable_web_page_preview,
    }
    try:
        resp = requests.post(url, data=data, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as e:
        logger.error(f"خطأ شبكة في Telegram sendMessage: {e}")
        raise
    result = _parse_response(resp, "sendMessage")
    msg_id = int(result.get("message_id"))
    logger.info(f"تم إرسال رسالة Telegram: message_id={msg_id}")
    return msg_id


def send_video(
    bot_token: str,
    chat_id: Union[str, int],
    video_path: Union[str, Path],
    caption: str = "",
    parse_mode: str = "HTML",
    supports_streaming: bool = True,
    width: Optional[int] = None,
    height: Optional[int] = None,
    duration: Optional[int] = None,
    thumbnail_path: Optional[Union[str, Path]] = None,
    disable_notification: bool = False,
) -> int:
    """يرفع فيديو للـ Telegram channel. بيرجع message_id.

    الحدود:
      - حد الـ Bot API الرسمي: 50 MB لكل ملف عبر sendVideo multipart.
        لو الفيديو أكبر من 50 MB → يرفع RuntimeError واضح (الـ caller يتعامل مع الـ fallback).
      - caption max 1024 حرف، لو أطول بنقصه لـ 1020 + ' ...'

    Args:
        thumbnail_path: مسار صورة (jpeg) كـ thumbnail اختياري.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"الفيديو مش موجود: {video_path}")

    file_size = video_path.stat().st_size
    if file_size > TG_VIDEO_MAX_BYTES:
        raise RuntimeError(
            f"حجم الفيديو ({file_size / (1024 * 1024):.1f} MB) يتعدى حد Telegram Bot API (50 MB). "
            f"استخدم send_video_via_local_bot_api أو ابعت رابط YouTube بدالاً عبر send_message."
        )

    caption = (caption or "").strip()
    if len(caption) > TG_CAPTION_MAX:
        caption = caption[: TG_CAPTION_MAX - 4].rstrip() + " ..."

    url = _api_url(bot_token, "sendVideo")
    data: dict = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": parse_mode,
        "supports_streaming": "true" if supports_streaming else "false",
        "disable_notification": "true" if disable_notification else "false",
    }
    if width is not None:
        data["width"] = int(width)
    if height is not None:
        data["height"] = int(height)
    if duration is not None:
        data["duration"] = int(duration)

    logger.info(
        f"بدء رفع Telegram video: {video_path.name} ({file_size / (1024 * 1024):.1f} MB)"
    )

    files: dict = {}
    open_handles: list = []
    try:
        vfh = video_path.open("rb")
        open_handles.append(vfh)
        files["video"] = (video_path.name, vfh, "video/mp4")
        if thumbnail_path:
            tp = Path(thumbnail_path)
            if tp.exists():
                tfh = tp.open("rb")
                open_handles.append(tfh)
                files["thumbnail"] = (tp.name, tfh, "image/jpeg")
            else:
                logger.warning(f"thumbnail مش موجود — تخطي: {tp}")
        try:
            resp = requests.post(url, data=data, files=files, timeout=UPLOAD_TIMEOUT)
        except requests.RequestException as e:
            logger.error(f"خطأ شبكة في Telegram sendVideo: {e}")
            raise
    finally:
        for fh in open_handles:
            try:
                fh.close()
            except Exception:
                pass

    result = _parse_response(resp, "sendVideo")
    msg_id = int(result.get("message_id"))
    logger.info(f"تم رفع فيديو Telegram: message_id={msg_id}")
    return msg_id


def send_video_note(
    bot_token: str,
    chat_id: Union[str, int],
    video_path: Union[str, Path],
    duration: Optional[int] = None,
) -> int:
    """يرسل video note (فيديو مدور، max 60s، لازم يكون مربع). بيرجع message_id.

    ملحوظة: مش مستخدم في الـ pipeline الأساسي، بس مفيد لاستخدامات مستقبلية.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"الفيديو مش موجود: {video_path}")

    file_size = video_path.stat().st_size
    if file_size > TG_VIDEO_MAX_BYTES:
        raise RuntimeError(
            f"حجم الـ video note ({file_size / (1024 * 1024):.1f} MB) يتعدى حد Bot API (50 MB)."
        )

    url = _api_url(bot_token, "sendVideoNote")
    data: dict = {"chat_id": chat_id}
    if duration is not None:
        data["duration"] = int(duration)

    with video_path.open("rb") as fh:
        files = {"video_note": (video_path.name, fh, "video/mp4")}
        try:
            resp = requests.post(url, data=data, files=files, timeout=UPLOAD_TIMEOUT)
        except requests.RequestException as e:
            logger.error(f"خطأ شبكة في Telegram sendVideoNote: {e}")
            raise
    result = _parse_response(resp, "sendVideoNote")
    msg_id = int(result.get("message_id"))
    logger.info(f"تم إرسال video note: message_id={msg_id}")
    return msg_id


def get_channel_info(bot_token: str, chat_id: Union[str, int]) -> dict:
    """GET getChat — بيرجع dict فيه title, id, type, member_count إلخ.

    مفيد للـ diagnostics. مش بيرفع exception لو فشل، بيرجع dict فيه مفتاح "error".
    """
    url = _api_url(bot_token, "getChat")
    params = {"chat_id": chat_id}
    try:
        resp = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
    except requests.RequestException as e:
        logger.warning(f"Telegram getChat خطأ شبكة: {e}")
        return {"error": str(e)}
    try:
        body = resp.json()
    except ValueError:
        return {"error": resp.text[:500]}
    if not body.get("ok"):
        return {"error": body.get("description") or body}
    return body.get("result", {})


def send_video_via_local_bot_api(
    bot_token: str,
    chat_id: Union[str, int],
    video_path: Union[str, Path],
    caption: str = "",
    parse_mode: str = "HTML",
    local_api_url: str = "http://localhost:8081",
    supports_streaming: bool = True,
) -> int:
    """يرفع فيديو عبر local Bot API server (تجاوز حد الـ 50 MB).

    تشغيل local server:
      $ telegram-bot-api --api-id=... --api-hash=... --local
    وبعدين logOut من الـ cloud server وlogIn على الـ local.
    تفاصيل: https://github.com/tdlib/telegram-bot-api

    لو ما عندكش server شغال، استخدم send_video العادي مع fallback نص لرابط YouTube.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"الفيديو مش موجود: {video_path}")

    caption = (caption or "").strip()
    if len(caption) > TG_CAPTION_MAX:
        caption = caption[: TG_CAPTION_MAX - 4].rstrip() + " ..."

    url = f"{local_api_url.rstrip('/')}/bot{bot_token}/sendVideo"
    data = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": parse_mode,
        "supports_streaming": "true" if supports_streaming else "false",
    }
    file_size = video_path.stat().st_size
    logger.info(
        f"بدء رفع Telegram video عبر local Bot API: {video_path.name} "
        f"({file_size / (1024 * 1024):.1f} MB) → {local_api_url}"
    )
    with video_path.open("rb") as fh:
        files = {"video": (video_path.name, fh, "video/mp4")}
        try:
            resp = requests.post(url, data=data, files=files, timeout=UPLOAD_TIMEOUT * 3)
        except requests.RequestException as e:
            logger.error(f"خطأ شبكة في local Bot API sendVideo: {e}")
            raise
    result = _parse_response(resp, "sendVideo (local)")
    msg_id = int(result.get("message_id"))
    logger.info(f"تم رفع فيديو Telegram (local): message_id={msg_id}")
    return msg_id
