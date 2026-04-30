"""
Pipeline جديد - Two Phase
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

import google.generativeai as genai

from .ai_generator import GeminiGenerator, metadata_to_dict, TelegramQuote, VideoMetadata
from .config import Config
from .facebook_uploader import (
    load_meta_credentials,
    upload_reel_to_page,
    upload_video_to_page,
)
from .instagram_uploader import upload_reel_to_instagram
from .pending_tracker import PendingTracker, PendingVideo, now_iso
from .shorts_cutter import cut_all_shorts
from .telegram_uploader import (
    TG_VIDEO_MAX_BYTES,
    load_telegram_credentials,
    send_message as tg_send_message,
    send_video as tg_send_video,
)
from .thumbnail import make_short_thumbnail, make_thumbnail
from .youtube_uploader import (
    add_video_to_playlist,
    download_auto_captions_via_timedtext,
    download_auto_captions_via_ytdlp,
    download_caption,
    get_auto_caption,
    get_or_create_playlist,
    get_youtube_service,
    set_thumbnail,
    update_video_metadata,
    upload_short,
    upload_video,
)

logger = logging.getLogger(__name__)

# cache: series title → playlist_id (يتعمر خلال batch run)
_PLAYLIST_CACHE: dict[str, str] = {}

# cache: meta credentials dict (يتحمل مرة واحدة في الـ run)
_META_CREDS_CACHE: dict | None = None

# cache: telegram credentials dict (يتحمل مرة واحدة في الـ run)
_TG_CREDS_CACHE: dict | None = None
# sentinel للـ failed load (عشان ما نحاولش تاني)
_TG_CREDS_FAILED: bool = False

# Hashtag pool لـ FB/IG Reels — هاشتاجز عربية + Shorts/Reels
_REEL_HASHTAGS = [
    "#Reels",
    "#Shorts",
    "#إسلاميات",
    "#علم_شرعي",
    "#الشيخ_سامي_العربي",
    "#أبو_حفص_الأثري",
]

# Hashtag pool لـ Telegram — الـ underscores conventional في تليجرام
_TELEGRAM_HASHTAGS = [
    "#الشيخ_سامي_العربي",
    "#أبو_حفص_الأثري",
    "#علم_شرعي",
    "#دروس_شرعية",
    "#إسلاميات",
]


def _get_meta_creds(cfg: Config) -> dict | None:
    """يحمّل meta_credentials.json مرة واحدة. بيرجع None لو فشل."""
    global _META_CREDS_CACHE
    if _META_CREDS_CACHE is not None:
        return _META_CREDS_CACHE
    rel = cfg.get("facebook", "credentials_file",
                  default="credentials/meta_credentials.json")
    try:
        path = cfg.project_root / rel if not Path(rel).is_absolute() else Path(rel)
        creds = load_meta_credentials(path)
        if not creds.get("pages"):
            logger.warning("meta_credentials.json مفيهوش pages — تعطيل FB/IG")
            return None
        _META_CREDS_CACHE = creds
        return creds
    except FileNotFoundError as e:
        logger.warning(f"meta credentials مش موجود — تعطيل FB/IG: {e}")
        return None
    except Exception as e:
        logger.warning(f"فشل تحميل meta credentials — تعطيل FB/IG: {e}")
        return None


def _build_reel_caption(base_text: str, parent_url: str | None = None) -> str:
    """يبني caption لـ FB/IG Reel: نص + parent URL + hashtags."""
    parts = [base_text.strip()] if base_text else []
    if parent_url:
        parts.append("")
        parts.append(f"المحاضرة كاملةً: {parent_url}")
    parts.append("")
    parts.append(" ".join(_REEL_HASHTAGS))
    caption = "\n".join(parts).strip()
    return caption


def _build_telegram_caption(base_text: str, parent_url: str | None = None,
                            is_short: bool = False) -> str:
    """يبني caption لـ Telegram (≤1024 حرف).

    الفورمات:
        <b>أول سطر = العنوان</b>
        ... باقي النص ...

        المحاضرة كاملةً: <parent_url>

        #الشيخ_سامي_العربي #علم_شرعي ...

    Args:
        base_text: النص (السطر الأول هيتحط bold).
        parent_url: لو محدد، بنضيف رابط المحاضرة الأصلية.
        is_short: لو True، بنستخدم "المقطع كاملاً مع المحاضرة" بدل "المحاضرة كاملةً".
    """
    text = (base_text or "").strip()
    body_parts: list[str] = []
    if text:
        lines = text.splitlines()
        first = lines[0].strip()
        rest = "\n".join(lines[1:]).strip()
        if first:
            # Telegram HTML: <b>...</b> + escape angle brackets في الباقي
            body_parts.append(f"<b>{_tg_escape(first)}</b>")
        if rest:
            body_parts.append(_tg_escape(rest))

    parts: list[str] = []
    if body_parts:
        parts.append("\n\n".join(body_parts))
    if parent_url:
        label = "المحاضرة كاملةً" if not is_short else "المقطع ضمن المحاضرة الكاملة"
        parts.append(f"{label}: {parent_url}")
    parts.append(" ".join(_TELEGRAM_HASHTAGS))

    caption = "\n\n".join(p for p in parts if p).strip()

    # حد Telegram للـ caption = 1024 حرف
    from .telegram_uploader import TG_CAPTION_MAX  # local import لتجنب circular
    if len(caption) > TG_CAPTION_MAX:
        caption = caption[: TG_CAPTION_MAX - 4].rstrip() + " ..."
    return caption


def _tg_escape(text: str) -> str:
    """يهرب الأحرف الخاصة في Telegram HTML (& < >)."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _get_telegram_creds(cfg: Config) -> dict | None:
    """يحمّل telegram_credentials.json مرة واحدة. بيرجع None لو فشل."""
    global _TG_CREDS_CACHE, _TG_CREDS_FAILED
    if _TG_CREDS_CACHE is not None:
        return _TG_CREDS_CACHE
    if _TG_CREDS_FAILED:
        return None
    rel = cfg.get("telegram", "credentials_file",
                  default="credentials/telegram_credentials.json")
    try:
        path = cfg.project_root / rel if not Path(rel).is_absolute() else Path(rel)
        creds = load_telegram_credentials(path)
        if not creds.get("bot_token"):
            logger.warning("telegram_credentials.json بدون bot_token — تعطيل Telegram")
            _TG_CREDS_FAILED = True
            return None
        chat_id = creds.get("channel_numeric_id") or creds.get("channel_username")
        if not chat_id:
            logger.warning("telegram_credentials.json بدون channel_numeric_id/channel_username — تعطيل Telegram")
            _TG_CREDS_FAILED = True
            return None
        _TG_CREDS_CACHE = creds
        return creds
    except FileNotFoundError as e:
        logger.warning(f"telegram credentials مش موجود — تعطيل Telegram: {e}")
        _TG_CREDS_FAILED = True
        return None
    except Exception as e:
        logger.warning(f"فشل تحميل telegram credentials — تعطيل Telegram: {e}")
        _TG_CREDS_FAILED = True
        return None


def _publish_full_video_to_telegram(cfg: Config, video_path: Path,
                                    title: str, description: str,
                                    yt_url: str | None = None,
                                    series: str = "") -> int | None:
    """ينشر منشور نصي على تليجرام يعلن المحاضرة الجديدة + رابط يوتيوب.

    ⚠️ ملاحظة مهمة: المحاضرات الكاملة عادةً > 50MB، وهو حد الـ Bot API الرسمي،
    فبدل ما نحاول نرفع الملف ونفشل، بنبعت **منشور نصي** فيه:
      - عنوان السلسلة (لو موجود) + عنوان الفيديو bold.
      - مقتطف من الوصف.
      - رابط يوتيوب الكامل (مع web_page_preview).
      - هاشتاجز القناة.

    `video_path` مش بيتقرا فعلياً، بس بنحتفظ بالـ signature كان عليه.

    بيرجع message_id لو نجح، None لو معطّل أو فشل.
    """
    tg_cfg = cfg.get("telegram", default={}) or {}
    if not tg_cfg.get("enabled", False):
        return None
    if not tg_cfg.get("publish_full_video", True):
        return None
    creds = _get_telegram_creds(cfg)
    if not creds:
        return None

    bot_token = creds["bot_token"]
    chat_id = creds.get("channel_numeric_id") or creds.get("channel_username")

    try:
        # نص الإعلان: header + عنوان الفيديو + ملخص الوصف + رابط + هاشتاجز
        desc_snippet = (description or "").strip()
        # نقص الوصف لأول 600 حرف (حد منطقي خفيف للقراءة)
        if len(desc_snippet) > 600:
            desc_snippet = desc_snippet[:600].rstrip() + "..."

        text = _build_full_video_announcement(
            title=title, description=desc_snippet, yt_url=yt_url, series=series,
        )
        msg_id = tg_send_message(
            bot_token=bot_token, chat_id=chat_id, text=text,
            parse_mode="HTML", disable_web_page_preview=False,
        )
        logger.info(f"✓ Telegram full-video announcement: message_id={msg_id}")
        return msg_id
    except Exception as e:
        logger.warning(f"فشل نشر إعلان المحاضرة على Telegram: {e}")
        return None


def _build_full_video_announcement(title: str, description: str,
                                   yt_url: str | None,
                                   series: str = "") -> str:
    """يبني نص إعلان المحاضرة الكاملة على تليجرام (بديل عن رفع الفيديو)."""
    parts: list[str] = []
    series = (series or "").strip()
    title = (title or "").strip()
    header = (
        f"📖 <b>محاضرة جديدة من سلسلة {_tg_escape(series)}</b>"
        if series
        else "📖 <b>محاضرة جديدة</b>"
    )
    parts.append(header)
    if title:
        parts.append(f"<b>{_tg_escape(title)}</b>")
    if description:
        parts.append(_tg_escape(description))
    if yt_url:
        parts.append(f"شاهد المحاضرة كاملةً على يوتيوب:\n{yt_url}")
    parts.append(" ".join(_TELEGRAM_HASHTAGS))
    text = "\n\n".join(p for p in parts if p).strip()
    # حد Telegram للنص = 4096 حرف
    if len(text) > 4096:
        text = text[: 4096 - len("... <continued>")].rstrip() + "... <continued>"
    return text


def _build_telegram_text_post(title: str, description: str,
                              yt_url: str | None,
                              file_size: int | None = None) -> str:
    """يبني رسالة نصية طويلة للـ Telegram (حد 4096 حرف) للفيديوهات الكبيرة."""
    parts = []
    if title:
        parts.append(f"<b>{_tg_escape(title)}</b>")
    if description:
        parts.append(_tg_escape(description))
    if file_size:
        parts.append(
            f"<i>الفيديو متوفر بجودة كاملة على يوتيوب "
            f"(الحجم {file_size / (1024 * 1024):.0f} MB)</i>"
        )
    if yt_url:
        parts.append(f"شاهد المحاضرة كاملةً: {yt_url}")
    parts.append(" ".join(_TELEGRAM_HASHTAGS))
    text = "\n\n".join(p for p in parts if p).strip()
    # حد Telegram للنص = 4096
    if len(text) > 4096:
        text = text[: 4096 - len("... <continued>")].rstrip() + "... <continued>"
    return text


def _publish_short_to_telegram(cfg: Config, short_path: Path,
                               caption: str) -> int | None:
    """يرفع short كفيديو للـ Telegram channel. بيرجع message_id أو None.

    الـ shorts (≤60s) عادة 10-30 MB، بيمر تحت حد 50 MB من غير مشاكل.
    """
    tg_cfg = cfg.get("telegram", default={}) or {}
    if not tg_cfg.get("enabled", False):
        return None
    if not tg_cfg.get("publish_shorts", True):
        return None
    creds = _get_telegram_creds(cfg)
    if not creds:
        return None
    if not Path(short_path).exists():
        logger.warning(f"Telegram: short مش موجود — تخطي: {short_path}")
        return None

    try:
        msg_id = tg_send_video(
            bot_token=creds["bot_token"],
            chat_id=creds.get("channel_numeric_id") or creds.get("channel_username"),
            video_path=short_path,
            caption=caption,
            parse_mode="HTML",
            supports_streaming=True,
        )
        logger.info(f"✓ Telegram Short: message_id={msg_id}")
        return msg_id
    except Exception as e:
        logger.warning(f"فشل رفع short على Telegram ({short_path.name}): {e}")
        return None


def _publish_quote_to_telegram(cfg: Config, quote: TelegramQuote,
                               series: str, parent_yt_url: str,
                               speaker: str) -> int | None:
    """ينسّق اقتباساً نصياً وينشره على قناة تليجرام كمنشور مستقل.

    الفورمات (HTML, RTL):
        ✦ <b>{series} — اقتباس</b>

        ▎{quote.text}
        <i>{quote.context_hint}</i>   ← اختياري لو موجود

        — {speaker}
        🔗 المحاضرة كاملةً: {parent_yt_url[?t=Xs]}

        #اقتباسات #علم_شرعي #الشيخ_سامي_العربي

    لو quote.source_timestamp موجود، بنضيف "?t=Xs" للرابط (أو "&t=Xs" لو فيه ? بالفعل).

    الحد الأعلى للنص = 4096 (sendMessage limit)، لكن بنحاول نخليه ≤1024.

    بيرجع message_id لو نجح، None لو فشل (بنسجل warning).
    """
    tg_cfg = cfg.get("telegram", default={}) or {}
    if not tg_cfg.get("enabled", False):
        return None
    if not tg_cfg.get("publish_text_quotes", True):
        return None
    creds = _get_telegram_creds(cfg)
    if not creds:
        return None
    if not quote or not (quote.text or "").strip():
        return None

    bot_token = creds["bot_token"]
    chat_id = creds.get("channel_numeric_id") or creds.get("channel_username")

    # ===== رابط يوتيوب مع timestamp لو متاح =====
    yt_url = parent_yt_url or ""
    if yt_url and quote.source_timestamp:
        secs = _timestamp_to_seconds(quote.source_timestamp)
        if secs > 0:
            sep = "&" if "?" in yt_url else "?"
            yt_url = f"{yt_url}{sep}t={secs}s"

    # ===== بناء النص =====
    series_clean = (series or "").strip()
    speaker_clean = (speaker or "").strip()

    parts: list[str] = []
    if series_clean:
        parts.append(f"✦ <b>{_tg_escape(series_clean)} — اقتباس</b>")
    else:
        parts.append("✦ <b>اقتباس من المحاضرة</b>")

    # الاقتباس نفسه — نستخدم ▎ كـ visual blockquote bar
    quote_lines = [f"▎{_tg_escape(quote.text.strip())}"]
    ctx = (quote.context_hint or "").strip()
    if ctx:
        quote_lines.append(f"<i>{_tg_escape(ctx)}</i>")
    parts.append("\n".join(quote_lines))

    # attribution + link
    attribution_lines: list[str] = []
    if speaker_clean:
        attribution_lines.append(f"— {_tg_escape(speaker_clean)}")
    if yt_url:
        attribution_lines.append(f"🔗 المحاضرة كاملةً: {yt_url}")
    if attribution_lines:
        parts.append("\n".join(attribution_lines))

    # هاشتاجز خاصة بالاقتباسات
    quote_hashtags = ["#اقتباسات", "#علم_شرعي", "#الشيخ_سامي_العربي"]
    parts.append(" ".join(quote_hashtags))

    text = "\n\n".join(p for p in parts if p).strip()
    if len(text) > 4096:
        text = text[: 4096 - len("... <continued>")].rstrip() + "... <continued>"

    try:
        msg_id = tg_send_message(
            bot_token=bot_token, chat_id=chat_id, text=text,
            parse_mode="HTML", disable_web_page_preview=False,
        )
        logger.info(f"✓ Telegram quote post: message_id={msg_id}")
        return msg_id
    except Exception as e:
        logger.warning(f"فشل نشر اقتباس على Telegram: {e}")
        return None


def _timestamp_to_seconds(ts: str) -> int:
    """يحوّل 'MM:SS' أو 'HH:MM:SS' لعدد ثوانٍ. بيرجع 0 لو الفورمات غلط."""
    ts = (ts or "").strip()
    if not ts:
        return 0
    parts = ts.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return 0


def _publish_full_video_to_fb(cfg: Config, video_path: Path,
                              title: str, description: str) -> str | None:
    """يرفع الفيديو الكامل (16:9) لـ FB Page. بيرجع fb_video_id أو None."""
    fb_cfg = cfg.get("facebook", default={}) or {}
    if not fb_cfg.get("enabled", False):
        return None
    if not fb_cfg.get("publish_full_video", True):
        return None
    creds = _get_meta_creds(cfg)
    if not creds:
        return None
    try:
        page = creds["pages"][0]
        fb_id = upload_video_to_page(
            page_id=page["page_id"],
            page_access_token=page["page_access_token"],
            video_path=video_path,
            title=title,
            description=description,
            published=True,
        )
        logger.info(f"✓ Facebook full video: {fb_id}")
        return fb_id
    except Exception as e:
        logger.warning(f"فشل رفع الفيديو الكامل على FB Page: {e}")
        return None


def _publish_short_to_fb_reel(cfg: Config, short_path: Path,
                              caption: str) -> str | None:
    """يرفع short كـ Facebook Reel. بيرجع fb_video_id أو None."""
    fb_cfg = cfg.get("facebook", default={}) or {}
    if not fb_cfg.get("enabled", False):
        return None
    if not fb_cfg.get("publish_shorts_as_reels", True):
        return None
    creds = _get_meta_creds(cfg)
    if not creds:
        return None
    try:
        page = creds["pages"][0]
        fb_id = upload_reel_to_page(
            page_id=page["page_id"],
            page_access_token=page["page_access_token"],
            video_path=short_path,
            description=caption,
        )
        logger.info(f"✓ Facebook Reel: {fb_id}")
        return fb_id
    except Exception as e:
        logger.warning(f"فشل رفع FB Reel ({short_path.name}): {e}")
        return None


def _publish_short_to_ig_reel(cfg: Config, short_path: Path,
                              caption: str) -> str | None:
    """يرفع short كـ Instagram Reel. بيرجع ig_media_id أو None."""
    ig_cfg = cfg.get("instagram", default={}) or {}
    if not ig_cfg.get("enabled", False):
        return None
    if not ig_cfg.get("publish_shorts_as_reels", True):
        return None
    creds = _get_meta_creds(cfg)
    if not creds:
        return None
    try:
        page = creds["pages"][0]
        ig_id = page.get("instagram_business_account_id")
        if not ig_id:
            logger.warning("ما فيش instagram_business_account_id في الـ credentials")
            return None
        media_id = upload_reel_to_instagram(
            ig_business_account_id=ig_id,
            page_access_token=page["page_access_token"],
            video_path=short_path,
            caption=caption,
            share_to_feed=bool(ig_cfg.get("share_to_feed", True)),
        )
        logger.info(f"✓ Instagram Reel: {media_id}")
        return media_id
    except Exception as e:
        logger.warning(f"فشل رفع IG Reel ({short_path.name}): {e}")
        return None


def normalize_tags(tags: list[str], max_count: int = 30) -> list[str]:
    """Convert underscores/hyphens to spaces, dedupe, cap length."""
    seen: set[str] = set()
    out: list[str] = []
    for t in tags or []:
        if not t:
            continue
        cleaned = " ".join(str(t).replace("_", " ").replace("-", " ").split()).strip()
        cleaned = cleaned.lstrip("#").strip()
        if not cleaned:
            continue
        # YouTube tag length limit per tag is 100 chars; total 500 chars; max 30 tags total
        if len(cleaned) > 100:
            cleaned = cleaned[:100].strip()
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
        if len(out) >= max_count:
            break
    return out


# cache: series → playlist description (يتعمر خلال الـ run)
_PLAYLIST_DESC_CACHE: dict[str, str] = {}


def _generate_playlist_description(cfg: Config, series: str, youtube=None) -> str:
    """يولّد وصف غني للـ playlist بـ Gemini (5-7 جمل عربية)."""
    series = (series or "").strip()
    if not series:
        return ""
    if series in _PLAYLIST_DESC_CACHE:
        return _PLAYLIST_DESC_CACHE[series]

    branding = cfg.channel_branding
    channel_name = branding.get("channel_name", "")
    speaker = branding.get("speaker_title", "")
    handle = branding.get("channel_handle", "")

    fallback = (
        f"سلسلة \"{series}\" — من دروس {speaker}.\n\n"
        f"محاضرات علمية مرتبة بمنهج السلف، تجدونها على قناة {channel_name} {handle}.\n"
        f"اشترك وفعّل الجرس ليصلك كل جديد."
    )

    keys = cfg.get("gemini_api_keys_rotation", default=[]) or [cfg.gemini_api_key]
    if not keys:
        _PLAYLIST_DESC_CACHE[series] = fallback
        return fallback

    try:
        gen = GeminiGenerator(api_keys=keys)
        prompt = (
            f"اكتب وصفاً عربياً فصيحاً لقائمة تشغيل (playlist) على يوتيوب.\n"
            f"اسم السلسلة: \"{series}\"\n"
            f"الشيخ: {speaker}\n"
            f"اسم القناة: {channel_name} ({handle})\n\n"
            "المتطلبات:\n"
            "- 5 إلى 7 جمل بالعربية الفصحى الموقّرة (لا عامية).\n"
            "- ابدأ بترحيب موجز وتعريف بالسلسلة (الكتاب/الموضوع وأهميته العلمية).\n"
            "- اذكر الشيخ ومنهجه السلفي باختصار.\n"
            "- اذكر الفائدة المرجوّة لطالب العلم من متابعة هذه السلسلة.\n"
            "- اختم بدعوة للاشتراك في القناة وتفعيل الجرس ومشاركة الخير.\n"
            "- لا تستخدم الـ markdown ولا hashtags ولا emojis.\n"
            "- لا تكتب عناوين فرعية ولا قوائم نقطية، نص متصل فقط.\n"
            "- لا تذكر مصدر النص (لا تقل \"هذا الوصف\" أو ما شابه).\n"
            "- الطول الإجمالي بين 400 و 800 حرف عربي."
        )
        resp = gen.model.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(
                temperature=0.6, max_output_tokens=2048,
            ),
        )
        text = (resp.text or "").strip()
        # تنظيف: لو فيه markdown أو quotes، شيلهم
        text = text.replace("```", "").strip()
        if text.startswith('"') and text.endswith('"'):
            text = text[1:-1].strip()
        if text and len(text) > 100:
            _PLAYLIST_DESC_CACHE[series] = text
            return text
    except Exception as e:
        logger.warning(f"فشل توليد وصف الـ playlist '{series}': {e}")

    _PLAYLIST_DESC_CACHE[series] = fallback
    return fallback


def get_next_publish_time(cfg: Config, day_offset: int = 0) -> dt.datetime:
    yt = cfg.youtube
    tz = ZoneInfo(yt.get("timezone", "Africa/Cairo"))
    now = dt.datetime.now(tz)
    target = now.replace(
        hour=int(yt.get("publish_hour_local", 18)),
        minute=int(yt.get("publish_minute_local", 0)),
        second=0, microsecond=0,
    )
    if target <= now:
        target += dt.timedelta(days=1)
    target += dt.timedelta(days=day_offset)
    min_target = now + dt.timedelta(minutes=20)
    if target < min_target:
        target = min_target
    return target


def get_series_name(video_path: Path, root: Path) -> str:
    try:
        rel = video_path.relative_to(root)
        if len(rel.parts) >= 2:
            return rel.parts[0]
    except ValueError:
        pass
    return ""


def get_pending_path(cfg: Config) -> Path:
    return cfg.project_root / "output" / "pending.json"


def get_youtube(cfg: Config):
    yt_cfg = cfg.youtube
    return get_youtube_service(
        client_secret_file=cfg.project_root / yt_cfg["client_secret_file"],
        token_file=cfg.project_root / yt_cfg["token_file"],
    )


def upload_phase1(video_path: Path, cfg: Config,
                  publish_at: Optional[dt.datetime] = None,
                  youtube=None, series: str = "") -> dict:
    logger.info(f"=== رفع: {video_path.name} ===")
    if youtube is None:
        youtube = get_youtube(cfg)
    yt_cfg = cfg.youtube
    placeholder_title = video_path.stem
    if series:
        placeholder_title = f"[قيد المعالجة] {series} - {placeholder_title}"
    else:
        placeholder_title = f"[قيد المعالجة] {placeholder_title}"
    branding = cfg.channel_branding
    speaker = branding.get("speaker_title", "فضيلة الشيخ أبو حفص بن العربي الأثري")
    placeholder_desc = (
        f"محاضرة لـ{speaker}\n"
        f"السلسلة: {series}\n"
        f"الفيديو الأصلي: {video_path.name}\n\n"
        f"[سيتم تحديث العنوان والوصف تلقائياً بعد جاهزية الترجمة من YouTube]"
    )
    if publish_at is None and yt_cfg.get("schedule_publish", True):
        publish_at = get_next_publish_time(cfg)
    privacy = "private" if publish_at else yt_cfg.get("default_privacy", "private")

    video_id = upload_video(
        youtube=youtube, video_path=video_path,
        title=placeholder_title, description=placeholder_desc,
        tags=yt_cfg.get("default_tags", []),
        category_id=yt_cfg.get("default_category_id", "27"),
        privacy_status=privacy, publish_at=publish_at,
        made_for_kids=yt_cfg.get("made_for_kids", False),
        default_language="ar",
    )

    # ضيف الفيديو لـ playlist السلسلة (لو فيه series)
    playlist_id = ""
    series_key = (series or "").strip()
    if series_key:
        try:
            if series_key in _PLAYLIST_CACHE:
                playlist_id = _PLAYLIST_CACHE[series_key]
            else:
                pl_desc = _generate_playlist_description(cfg, series_key, youtube)
                pid = get_or_create_playlist(youtube, series_key, pl_desc)
                if pid:
                    _PLAYLIST_CACHE[series_key] = pid
                    playlist_id = pid
            if playlist_id:
                add_video_to_playlist(youtube, video_id, playlist_id)
        except Exception as e:
            logger.warning(f"فشل ربط الفيديو بـ playlist '{series_key}': {e}")

    tracker = PendingTracker(get_pending_path(cfg))
    tracker.add(PendingVideo(
        video_id=video_id, original_path=str(video_path),
        original_name=video_path.name, series=series,
        uploaded_at=now_iso(), status="uploaded",
        publish_at=publish_at.isoformat() if publish_at else "",
        playlist_id=playlist_id,
    ))
    logger.info(f"✓ تم رفع: https://youtu.be/{video_id} (مستني captions)")
    return {
        "video_id": video_id,
        "url": f"https://youtu.be/{video_id}",
        "status": "pending_captions",
        "publish_at": publish_at.isoformat() if publish_at else None,
        "playlist_id": playlist_id,
    }


def process_pending(cfg: Config, video_id: Optional[str] = None) -> dict:
    tracker = PendingTracker(get_pending_path(cfg))
    youtube = get_youtube(cfg)
    pending = tracker.all_pending()
    if video_id:
        pending = [p for p in pending if p.video_id == video_id]
    if not pending:
        logger.info("مفيش فيديوهات pending")
        return {"processed": 0, "ready": 0, "failed": 0, "not_ready": 0}
    logger.info(f"عدد الـ pending: {len(pending)}")
    processed = failed = not_ready = 0
    for item in pending:
        try:
            logger.info(f"--- {item.video_id} ({item.original_name}) ---")
            srt_text = _try_download_captions(youtube, item.video_id)
            if not srt_text:
                logger.info("captions مش جاهزة لسه")
                tracker.update(item.video_id, captions_checked_count=item.captions_checked_count + 1)
                not_ready += 1
                continue
            tracker.update(item.video_id, status="processing")
            _process_one(cfg, youtube, tracker, item, srt_text)
            processed += 1
        except Exception as e:
            logger.error(f"فشل {item.video_id}: {e}", exc_info=True)
            tracker.update(item.video_id, status="failed", error=str(e)[:500])
            failed += 1
    return {"processed": processed, "ready": processed,
            "not_ready": not_ready, "failed": failed}


def _try_download_captions(youtube, video_id: str) -> Optional[str]:
    cap = get_auto_caption(youtube, video_id, language="ar")
    if cap:
        srt = download_caption(youtube, cap["id"], fmt="srt")
        if srt and srt.strip():
            return srt
    srt = download_auto_captions_via_timedtext(video_id, language="ar")
    if srt and srt.strip():
        return srt
    # 3rd fallback via yt-dlp (auto-captions reliable للفيديوهات الجديدة)
    srt = download_auto_captions_via_ytdlp(video_id, language="ar")
    if srt and srt.strip():
        return srt
    return None


def _process_one(cfg, youtube, tracker, item, srt_text):
    base = Path(item.original_name).stem
    paths = cfg.paths
    srt_path = Path(paths["output_srt"]) / f"{base}.srt"
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.write_text(srt_text, encoding="utf-8")
    logger.info(f"✓ SRT محفوظ: {srt_path}")

    duration = _get_duration_from_srt(srt_text)
    plain = _srt_plain_text(srt_text)
    with_ts = _srt_with_simple_timestamps(srt_text)
    rotation_keys = cfg.get("gemini_api_keys_rotation") or []
    gemini = GeminiGenerator(
        api_key=cfg.gemini_api_key,
        api_keys=rotation_keys if rotation_keys else None,
    )
    md = gemini.generate(
        srt_text_with_timestamps=with_ts,
        plain_text=plain,
        video_duration_seconds=duration,
        max_clips=cfg.shorts.get("max_clips_per_video", 3),
        min_clip_seconds=cfg.shorts.get("min_clip_seconds", 25),
        max_clip_seconds=cfg.shorts.get("max_clip_seconds", 58),
        max_title_length=cfg.ai_prompts.get("title_max_length", 75),
        max_hashtags=cfg.ai_prompts.get("max_hashtags", 10),
        include_chapters=cfg.ai_prompts.get("include_chapters", True),
        content_context=cfg.ai_prompts.get("content_context", ""),
        description_template_hint=cfg.ai_prompts.get("description_template_hint", ""),
        shorts_selection_hint=cfg.ai_prompts.get("shorts_selection_hint", ""),
    )
    md_path = Path(paths["output_metadata"]) / f"{base}.json"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(json.dumps(metadata_to_dict(md), ensure_ascii=False, indent=2),
                       encoding="utf-8")
    logger.info(f"✓ العنوان: {md.title}")

    full_description = md.description_with_chapters()
    yt_cfg = cfg.youtube
    tags = normalize_tags(md.hashtags + list(yt_cfg.get("default_tags", []) or []), max_count=30)
    update_video_metadata(youtube=youtube, video_id=item.video_id,
                          title=md.title, description=full_description, tags=tags)
    logger.info("✓ تم تحديث الفيديو على YouTube")

    # تأكيد الـ playlist linkage (لو ما اتعملش في Phase 1 لأي سبب)
    series_key = (item.series or "").strip()
    if series_key and not (item.playlist_id or "").strip():
        try:
            pl_desc = f"سلسلة {series_key} — قناة نقال الخير"
            if series_key in _PLAYLIST_CACHE:
                pid = _PLAYLIST_CACHE[series_key]
            else:
                pid = get_or_create_playlist(youtube, series_key, pl_desc)
                if pid:
                    _PLAYLIST_CACHE[series_key] = pid
            if pid:
                add_video_to_playlist(youtube, item.video_id, pid)
                tracker.update(item.video_id, playlist_id=pid)
                item.playlist_id = pid  # local copy for shorts loop
                logger.info(f"✓ playlist linkage: {series_key} ({pid})")
        except Exception as e:
            logger.warning(f"playlist linkage فشل (Phase 2): {e}")

    thumb_path = Path(paths["output_thumbnails"]) / f"{base}.jpg"
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    if Path(item.original_path).exists():
        try:
            make_thumbnail(video_path=Path(item.original_path),
                           title=md.title, output_path=thumb_path,
                           config=cfg.thumbnail)
            set_thumbnail(youtube, item.video_id, thumb_path)
            logger.info("✓ Thumbnail تم رفعه")
        except Exception as e:
            logger.warning(f"فشل thumbnail: {e}")

    # ===== رفع الفيديو الكامل على Facebook Page (لو enabled) =====
    fb_main_video_id: str | None = None
    tg_main_message_id: int | None = None
    yt_url = f"https://youtu.be/{item.video_id}"
    series_for_tg = (item.series or "").strip()
    if Path(item.original_path).exists():
        fb_main_video_id = _publish_full_video_to_fb(
            cfg=cfg,
            video_path=Path(item.original_path),
            title=md.title,
            description=full_description,
        )
    # ===== Telegram (منشور إعلان نصي + رابط YouTube — مش رفع للملف) =====
    # لا نشترط وجود الملف الأصلي لأننا بنبعت نص فقط
    tg_main_message_id = _publish_full_video_to_telegram(
        cfg=cfg,
        video_path=Path(item.original_path),
        title=md.title,
        description=full_description,
        yt_url=yt_url,
        series=series_for_tg,
    )

    uploaded_shorts: list[str] = []
    fb_short_video_ids: list[str] = []
    ig_short_media_ids: list[str] = []
    tg_short_message_ids: list[int] = []
    if cfg.shorts.get("enabled", True) and md.important_clips and Path(item.original_path).exists():
        cut_paths: list[Path] = []
        try:
            shorts_dir = Path(paths["output_shorts"]) / base
            cut_paths = cut_all_shorts(
                video_path=Path(item.original_path),
                clips=md.important_clips, output_dir=shorts_dir,
                base_name=base, srt_path=srt_path,
                aspect=cfg.shorts.get("output_aspect", "9:16"),
                burn_subtitles=cfg.shorts.get("burn_subtitles", True),
                subtitle_font_size=cfg.shorts.get("subtitle_font_size", 48),
            )
            logger.info(f"✓ تم قص {len(cut_paths)} short")
        except Exception as e:
            logger.warning(f"فشل shorts: {e}")

        # ===== رفع كل short على YouTube + FB Reel + IG Reel + Telegram =====
        if cut_paths:
            (uploaded_shorts, fb_short_video_ids, ig_short_media_ids,
             tg_short_message_ids) = (
                _upload_shorts_for_video(
                    cfg=cfg, youtube=youtube, parent=item, md=md,
                    clips=md.important_clips, cut_paths=cut_paths,
                )
            )

    # ===== نشر اقتباسات نصية على Telegram (3-5 منشورات) =====
    tg_quote_message_ids: list[int] = []
    try:
        tg_cfg = cfg.get("telegram", default={}) or {}
        if (tg_cfg.get("enabled", False)
                and tg_cfg.get("publish_text_quotes", True)
                and md.text_quotes):
            target_count = int(tg_cfg.get("quotes_count", 4))
            # نقص العدد لو target_count أقل من اللي عندنا
            quotes_to_post = list(md.text_quotes)[:max(0, target_count)]
            delay = float(tg_cfg.get("delay_between_posts_seconds", 3))
            speaker_for_quotes = (cfg.channel_branding.get("speaker_short")
                                  or cfg.channel_branding.get("speaker_title")
                                  or "")
            import time
            for q_idx, q in enumerate(quotes_to_post, 1):
                msg_id = _publish_quote_to_telegram(
                    cfg=cfg, quote=q,
                    series=series_for_tg, parent_yt_url=yt_url,
                    speaker=speaker_for_quotes,
                )
                if msg_id:
                    tg_quote_message_ids.append(msg_id)
                # space them out so the channel doesn't burst-post
                if q_idx < len(quotes_to_post) and delay > 0:
                    time.sleep(delay)
            logger.info(
                f"✓ تم نشر {len(tg_quote_message_ids)} اقتباس نصي على Telegram "
                f"(من {len(quotes_to_post)})"
            )
    except Exception as e:
        logger.warning(f"فشل نشر اقتباسات Telegram (تم تجاهلها): {e}")

    metadata_summary = {
        "chapters": len(md.chapters),
        "shorts": len(md.important_clips),
        "shorts_uploaded": len(uploaded_shorts),
    }
    if uploaded_shorts:
        metadata_summary["short_video_ids"] = uploaded_shorts
    if fb_main_video_id:
        metadata_summary["fb_main_video_id"] = fb_main_video_id
    if fb_short_video_ids:
        metadata_summary["fb_short_video_ids"] = fb_short_video_ids
    if ig_short_media_ids:
        metadata_summary["ig_short_media_ids"] = ig_short_media_ids
    if tg_main_message_id:
        metadata_summary["tg_main_message_id"] = tg_main_message_id
    if tg_short_message_ids:
        metadata_summary["tg_short_message_ids"] = tg_short_message_ids
    if tg_quote_message_ids:
        metadata_summary["tg_quote_message_ids"] = tg_quote_message_ids
    tracker.update(item.video_id, status="completed",
                   completed_at=now_iso(), title_updated=md.title,
                   metadata=metadata_summary)


def _upload_shorts_for_video(cfg, youtube, parent, md: VideoMetadata,
                             clips, cut_paths) -> tuple[list[str], list[str], list[str], list[int]]:
    """يرفع كل short على YouTube + FB Reel + IG Reel + Telegram.

    بيرجع tuple: (yt_short_ids, fb_short_ids, ig_short_ids, tg_message_ids)
    """
    yt_cfg = cfg.youtube
    branding = cfg.channel_branding
    speaker = branding.get("speaker_title", "فضيلة الشيخ أبو حفص بن العربي الأثري")
    channel_name = branding.get("channel_name", "نقال الخير")
    series = (parent.series or "").strip()
    parent_url = f"https://youtu.be/{parent.video_id}"
    default_tags = list(yt_cfg.get("default_tags", []) or [])

    uploaded: list[str] = []
    fb_uploaded: list[str] = []
    ig_uploaded: list[str] = []
    tg_uploaded: list[int] = []
    total = len(cut_paths)
    # نمشي الـ clips بالترتيب اللي اتقصت بيه (نفس ترتيب important_clips)
    for idx, short_path in enumerate(cut_paths, 1):
        # حدد الـ clip المقابل حسب الـ index في cut_all_shorts
        # cut_all_shorts بيمشي 1..N بنفس ترتيب clips
        clip = clips[idx - 1] if idx - 1 < len(clips) else None
        if clip is None:
            logger.warning(f"مفيش metadata لـ short #{idx}، تخطي")
            continue
        try:
            # ===== العنوان =====
            raw_title = (clip.suggested_short_title or md.title or "").strip()
            # خلي مساحة لـ " #Shorts" (8 حرف)
            short_title = raw_title[:90].rstrip()

            # ===== الوصف =====
            clip_desc = (clip.description or "").strip()
            if not clip_desc:
                # fallback: وصف افتراضي self-contained مع CTA
                series_part = f" من سلسلة {series}" if series else ""
                clip_desc = (
                    f"{md.title} — مقطع مختار{series_part}\n"
                    f"لمشاهدة المحاضرة كاملةً يُرجى متابعة الرابط في الأسفل."
                )
            description_parts = [
                clip_desc,
                "",
                f"المحاضرة كاملةً: {parent_url}",
                f"قناة {channel_name} — {speaker}",
            ]
            if series:
                description_parts.append(f"السلسلة: {series}")
            full_description = "\n".join(description_parts)

            # ===== الوسوم =====
            clip_tags = list(clip.tags or [])
            if not clip_tags:
                clip_tags = default_tags[:8]
            merged = normalize_tags(clip_tags + default_tags, max_count=30)

            # ===== الرفع =====
            short_id = upload_short(
                youtube=youtube,
                file_path=short_path,
                title=short_title,
                description=full_description,
                tags=merged,
                privacy="public",
                publish_at=None,
                category_id=yt_cfg.get("default_category_id", "27"),
                made_for_kids=yt_cfg.get("made_for_kids", False),
            )
            uploaded.append(short_id)

            # ===== Thumbnail مخصص للـ Short (اللوجو فقط، بدون نص) =====
            try:
                logo_path = cfg.thumbnail.get("logo_path", "") if hasattr(cfg, "thumbnail") else ""
                if not logo_path:
                    logo_path = str(cfg.project_root / "assets" / "logo.png")
                else:
                    lp = Path(logo_path)
                    if not lp.is_absolute():
                        logo_path = str(cfg.project_root / lp)
                short_thumb_path = Path(short_path).with_suffix(".jpg")
                make_short_thumbnail(
                    short_video_path=short_path,
                    output_path=short_thumb_path,
                    logo_path=logo_path,
                )
                set_thumbnail(youtube, short_id, short_thumb_path)
                logger.info(f"✓ Thumbnail الـ Short تم رفعه: {short_thumb_path.name}")
            except Exception as e:
                logger.warning(f"فشل thumbnail الـ Short #{idx}: {e}")

            # ===== ضيف الـ Short لنفس playlist السلسلة =====
            playlist_id = (parent.playlist_id or "").strip()
            if not playlist_id and series:
                # لو cache فيه واحدة للسلسلة استخدمها
                playlist_id = _PLAYLIST_CACHE.get(series, "")
            if playlist_id:
                try:
                    add_video_to_playlist(youtube, short_id, playlist_id)
                except Exception as e:
                    logger.warning(f"فشل ربط الـ Short بـ playlist: {e}")

            # ===== رفع نفس الـ short كـ Facebook Reel + Instagram Reel =====
            reel_caption = _build_reel_caption(
                base_text=clip_desc, parent_url=parent_url
            )
            fb_id = _publish_short_to_fb_reel(
                cfg=cfg, short_path=short_path, caption=reel_caption
            )
            if fb_id:
                fb_uploaded.append(fb_id)
            ig_id = _publish_short_to_ig_reel(
                cfg=cfg, short_path=short_path, caption=reel_caption
            )
            if ig_id:
                ig_uploaded.append(ig_id)

            # ===== رفع نفس الـ short كرسالة فيديو على Telegram channel =====
            tg_caption = _build_telegram_caption(
                base_text=clip_desc, parent_url=parent_url, is_short=True,
            )
            tg_msg_id = _publish_short_to_telegram(
                cfg=cfg, short_path=short_path, caption=tg_caption,
            )
            if tg_msg_id:
                tg_uploaded.append(tg_msg_id)
        except Exception as e:
            logger.warning(f"فشل رفع short #{idx} ({short_path.name}): {e}")
            continue

    logger.info(
        f"✓ تم رفع {len(uploaded)} من {total} shorts على YouTube، "
        f"FB Reels: {len(fb_uploaded)}، IG Reels: {len(ig_uploaded)}، "
        f"Telegram: {len(tg_uploaded)}"
    )
    return uploaded, fb_uploaded, ig_uploaded, tg_uploaded


def _get_duration_from_srt(srt_text: str) -> float:
    import re
    timestamps = re.findall(r'(\d{2}):(\d{2}):(\d{2}),(\d{3})', srt_text)
    if not timestamps:
        return 0
    last = timestamps[-1]
    h, m, s, ms = [int(x) for x in last]
    return h * 3600 + m * 60 + s + ms / 1000


def _srt_plain_text(srt_text: str) -> str:
    import re
    text = re.sub(r'^\d+\s*$', '', srt_text, flags=re.MULTILINE)
    text = re.sub(r'\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}', '', text)
    return ' '.join(line.strip() for line in text.splitlines() if line.strip())


def _srt_with_simple_timestamps(srt_text: str) -> str:
    import re
    out = []
    blocks = re.split(r'\n\s*\n', srt_text.strip())
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue
        m = re.match(r'(\d{2}):(\d{2}):(\d{2}),\d{3}', lines[1])
        if not m:
            continue
        h, mm, s = [int(x) for x in m.groups()]
        total_min = h * 60 + mm
        text = ' '.join(lines[2:]).strip()
        if text:
            out.append(f"[{total_min:02d}:{s:02d}] {text}")
    return '\n'.join(out)
