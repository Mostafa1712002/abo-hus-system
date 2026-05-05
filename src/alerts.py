"""Telegram alerts for pipeline failures.

Sends an HTML-formatted message to ``telegram.admin_chat_id`` (set in config)
whenever a video fails to process. The admin chat is separate from the public
channel — the user must start a private chat with the bot and add their own
chat_id to config.

If ``admin_chat_id`` is not set, this module is a no-op (just logs).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .telegram_uploader import send_message

logger = logging.getLogger(__name__)


def _load_bot_token(cfg) -> Optional[str]:
    tg_cfg = cfg.get("telegram", default={}) or {}
    creds_path = tg_cfg.get("credentials_file", "credentials/telegram_credentials.json")
    p = Path(creds_path)
    if not p.is_absolute():
        p = cfg.project_root / p
    if not p.exists():
        return None
    try:
        creds = json.loads(p.read_text(encoding="utf-8"))
        return creds.get("bot_token")
    except Exception as e:
        logger.warning(f"تعذّر قراءة telegram credentials: {e}")
        return None


def _admin_chat_id(cfg) -> Optional[str]:
    tg_cfg = cfg.get("telegram", default={}) or {}
    chat = tg_cfg.get("admin_chat_id")
    if not chat:
        return None
    return str(chat)


def _escape_html(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def send_alert(
    cfg,
    *,
    title: str,
    video_id: str = "",
    series: str = "",
    video_name: str = "",
    error: str = "",
    extra: str = "",
    severity: str = "error",
) -> bool:
    """Send an alert to the admin Telegram chat. Returns True if sent.

    No-op (returns False) if telegram.admin_chat_id isn't configured.
    Failures sending the alert are logged but never raised — alerts must
    not break the pipeline.
    """
    chat_id = _admin_chat_id(cfg)
    if not chat_id:
        logger.info(f"[alert no-op] {title} — admin_chat_id غير مضبوط")
        return False
    bot_token = _load_bot_token(cfg)
    if not bot_token:
        logger.warning(f"[alert skipped] {title} — bot_token مش موجود")
        return False

    icon = {"error": "🚨", "warning": "⚠️", "info": "ℹ️"}.get(severity, "🔔")
    parts = [f"{icon} <b>{_escape_html(title)}</b>"]
    if series or video_name:
        parts.append(
            f"📺 {_escape_html(series)} — {_escape_html(video_name)}".strip(" —")
        )
    if video_id:
        parts.append(f"🆔 <code>{_escape_html(video_id)}</code>")
        parts.append(f"🔗 https://youtu.be/{_escape_html(video_id)}")
    if error:
        err_short = error[:600]
        parts.append(f"❌ <code>{_escape_html(err_short)}</code>")
    if extra:
        parts.append(_escape_html(extra)[:400])

    text = "\n\n".join(p for p in parts if p)
    try:
        send_message(
            bot_token=bot_token,
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_notification=False,
        )
        logger.info(f"alert sent: {title}")
        return True
    except Exception as e:
        logger.warning(f"[alert send failed] {title} — {e}")
        return False
