"""
موديول الرفع ليوتيوب

بيستخدم YouTube Data API v3 - مجاني (10,000 quota في اليوم).
رفع فيديو واحد بياخد ~1600 quota، يعني تقدر ترفع 6 فيديوهات في اليوم بسهولة.

أول مرة هتفتحلك المتصفح عشان تسجل دخول لقناتك وتسمح بالصلاحيات.
بعد كده هيتحفظ token وميسألكش تاني.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path
from typing import List, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

# الصلاحيات المطلوبة
SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


def get_youtube_service(client_secret_file: str | Path, token_file: str | Path):
    """يصرّح للوصول لقناة المستخدم. أول مرة بيفتح المتصفح."""
    client_secret_file = Path(client_secret_file)
    token_file = Path(token_file)
    token_file.parent.mkdir(parents=True, exist_ok=True)

    creds: Optional[Credentials] = None
    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                logger.warning(f"فشل تجديد الـ token، إعادة تسجيل الدخول: {e}")
                creds = None
        if not creds:
            if not client_secret_file.exists():
                raise FileNotFoundError(
                    f"ملف client_secret.json مش موجود في: {client_secret_file}\n"
                    "اعمل OAuth credentials من Google Cloud Console "
                    "(راجع README خطوة بخطوة)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secret_file), SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=creds)


def upload_video(
    youtube,
    video_path: str | Path,
    title: str,
    description: str,
    tags: List[str],
    category_id: str = "22",
    privacy_status: str = "private",
    publish_at: Optional[dt.datetime] = None,
    made_for_kids: bool = False,
    default_language: str = "ar",
) -> str:
    """
    يرفع فيديو ويرجع الـ video_id.

    Args:
        publish_at: لو محدد، الفيديو هيتجدول. لازم privacy_status='private'.
        category_id: 22 = People & Blogs, 27 = Education, 24 = Entertainment...
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"الفيديو مش موجود: {video_path}")

    # YouTube بيقتص العنوان عند 100 والوصف عند 5000
    title = title[:100]
    description = description[:5000]

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags[:30],
            "categoryId": category_id,
            "defaultLanguage": default_language,
            "defaultAudioLanguage": default_language,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": made_for_kids,
            "embeddable": True,
        },
    }

    # جدولة النشر؟
    if publish_at is not None:
        if privacy_status != "private":
            logger.warning("الجدولة محتاجة privacy=private. تعديل تلقائي.")
            body["status"]["privacyStatus"] = "private"
        # YouTube بيتطلب RFC3339 UTC
        if publish_at.tzinfo is None:
            publish_at = publish_at.astimezone()
        body["status"]["publishAt"] = publish_at.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")

    media = MediaFileUpload(
        str(video_path),
        chunksize=8 * 1024 * 1024,  # 8MB chunks
        resumable=True,
        mimetype="video/*",
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    logger.info(f"بدء رفع: {video_path.name}")
    response = None
    last_progress = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                if progress >= last_progress + 10:
                    logger.info(f"الرفع: {progress}%")
                    last_progress = progress
        except HttpError as e:
            logger.error(f"خطأ في الرفع: {e}")
            raise

    video_id = response["id"]
    logger.info(f"تم الرفع! https://youtu.be/{video_id}")
    return video_id


def upload_short(
    youtube,
    file_path: "str | Path",
    title: str,
    description: str,
    tags: List[str],
    privacy: str = "public",  # shorts go public by default for viral reach
    publish_at: Optional[str] = None,  # ISO 8601 - if set, schedule
    category_id: str = "27",  # Education
    made_for_kids: bool = False,
) -> str:
    """
    يرفع Short رأسي (9:16) ويرجع الـ video_id.

    - بيضمن إن العنوان فيه #Shorts (يوتيوب بيستخدمه للتعرف على الـ Shorts).
    - بيضمن إن الوصف فيه #Shorts + أهم 3-5 وسوم كهاشتاجات.
    - chunksize أصغر (4MB) لأن ملفات الـ Shorts صغيرة عادةً.
    - مش بيضبط thumbnail (الـ Shorts بياخدوا thumbnail تلقائي).
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"ملف الـ Short مش موجود: {file_path}")

    # 1) ضمان وجود #Shorts في العنوان
    title = (title or "").strip()
    if "#shorts" not in title.lower():
        # خلي مساحة لـ #Shorts (YouTube limit = 100)
        max_title_body = 100 - len(" #Shorts")
        if len(title) > max_title_body:
            title = title[:max_title_body].rstrip()
        title = f"{title} #Shorts".strip()
    title = title[:100]

    # 2) ضمان #Shorts + أهم الوسوم في آخر سطر من الوصف
    desc = (description or "").strip()
    # اختار أهم 3-5 وسوم بدون تكرار
    hashtag_pool: List[str] = []
    seen_h = set()
    for t in tags or []:
        t_clean = (t or "").strip().lstrip("#").replace(" ", "")
        if not t_clean:
            continue
        key = t_clean.lower()
        if key in seen_h:
            continue
        seen_h.add(key)
        hashtag_pool.append(t_clean)
        if len(hashtag_pool) >= 5:
            break
    hashtag_line_parts = ["#Shorts"] + [f"#{h}" for h in hashtag_pool if h.lower() != "shorts"]
    hashtag_line = " ".join(hashtag_line_parts)
    if "#shorts" not in desc.lower():
        desc = f"{desc}\n\n{hashtag_line}".strip()
    desc = desc[:5000]

    body = {
        "snippet": {
            "title": title,
            "description": desc,
            "tags": (tags or [])[:30],
            "categoryId": category_id,
            "defaultLanguage": "ar",
            "defaultAudioLanguage": "ar",
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": made_for_kids,
            "embeddable": True,
        },
    }

    # جدولة النشر (لو محدد). YouTube بيتطلب privacy=private.
    if publish_at:
        if privacy != "private":
            logger.warning("الجدولة محتاجة privacy=private. تعديل تلقائي.")
            body["status"]["privacyStatus"] = "private"
        # publish_at هنا string ISO 8601
        pa = publish_at
        if isinstance(pa, dt.datetime):
            if pa.tzinfo is None:
                pa = pa.astimezone()
            pa = pa.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        body["status"]["publishAt"] = pa

    media = MediaFileUpload(
        str(file_path),
        chunksize=1024 * 1024 * 4,  # 4MB chunks - shorts files small
        resumable=True,
        mimetype="video/*",
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    logger.info(f"بدء رفع Short: {file_path.name}")
    response = None
    last_progress = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                if progress >= last_progress + 20:
                    logger.info(f"رفع Short: {progress}%")
                    last_progress = progress
        except HttpError as e:
            logger.error(f"خطأ في رفع الـ Short: {e}")
            raise

    video_id = response["id"]
    logger.info(f"تم رفع Short! https://youtube.com/shorts/{video_id}")
    return video_id


def set_thumbnail(youtube, video_id: str, thumbnail_path: str | Path) -> None:
    """يضبط صورة الغلاف للفيديو"""
    thumbnail_path = Path(thumbnail_path)
    if not thumbnail_path.exists():
        logger.warning(f"الـ thumbnail مش موجود: {thumbnail_path}")
        return
    try:
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/jpeg"),
        ).execute()
        logger.info("تم رفع الـ thumbnail")
    except HttpError as e:
        # ملحوظة: thumbnails عايزة قناة موثّقة (verified)
        logger.error(
            f"فشل رفع الـ thumbnail: {e}\n"
            "ملاحظة: لازم القناة تكون موثّقة بفون/ID عشان تقدر ترفع thumbnails مخصصة."
        )


def upload_caption(
    youtube,
    video_id: str,
    srt_path: str | Path,
    language: str = "ar",
    name: str = "العربية",
) -> None:
    """يرفع ملف الترجمة (SRT) للفيديو"""
    srt_path = Path(srt_path)
    if not srt_path.exists():
        return
    try:
        youtube.captions().insert(
            part="snippet",
            body={
                "snippet": {
                    "videoId": video_id,
                    "language": language,
                    "name": name,
                    "isDraft": False,
                }
            },
            media_body=MediaFileUpload(str(srt_path), mimetype="application/octet-stream"),
        ).execute()
        logger.info("تم رفع الترجمة")
    except HttpError as e:
        logger.error(f"فشل رفع الترجمة: {e}")


# ============================================================
# Caption download & video update (للـ flow الجديد: ارفع → استنى → عالج)
# ============================================================

def list_video_captions(youtube, video_id: str) -> list[dict]:
    """يرجع قائمة الـ captions tracks المتاحة للفيديو (auto-generated أو manual)"""
    try:
        response = youtube.captions().list(
            part="snippet",
            videoId=video_id,
        ).execute()
        return response.get("items", [])
    except HttpError as e:
        logger.error(f"خطأ في قراءة captions: {e}")
        return []


def get_auto_caption(youtube, video_id: str, language: str = "ar") -> Optional[dict]:
    """يدور على auto-generated caption track باللغة المطلوبة. يرجع dict أو None"""
    captions = list_video_captions(youtube, video_id)
    for c in captions:
        snippet = c.get("snippet", {})
        # auto-generated دائماً
        is_auto = snippet.get("trackKind", "").lower() == "asr"
        if is_auto and snippet.get("language", "").startswith(language):
            return c
    # لو مفيش auto بالـ language، رجع أول auto
    for c in captions:
        if c.get("snippet", {}).get("trackKind", "").lower() == "asr":
            return c
    return None


def download_caption(youtube, caption_id: str, fmt: str = "srt") -> Optional[str]:
    """ينزل ملف caption (sub) من YouTube. fmt: srt, vtt, ttml..."""
    try:
        response = youtube.captions().download(
            id=caption_id,
            tfmt=fmt,
        ).execute()
        # response عبارة عن bytes
        if isinstance(response, bytes):
            return response.decode("utf-8", errors="replace")
        return str(response)
    except HttpError as e:
        # ملاحظة: captions().download() محتاج OAuth + الـ scope youtube.force-ssl
        # و captions الـ ASR (auto) ممكن ما تنزّلش - لازم تستخدم timedtext API
        logger.warning(f"فشل تنزيل الترجمة عبر captions API: {e}")
        return None


def download_auto_captions_via_timedtext(video_id: str, language: str = "ar") -> Optional[str]:
    """
    fallback: ينزل auto-captions عن طريق timedtext endpoint (مش API).
    ده مش رسمي بس بيشتغل.
    """
    import urllib.parse, urllib.request
    url = (
        f"https://www.youtube.com/api/timedtext?"
        f"lang={language}&v={video_id}&fmt=srv3&kind=asr"
    )
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = resp.read().decode("utf-8")
            if not data.strip():
                return None
            # حول srv3 لـ srt
            return _srv3_to_srt(data)
    except Exception as e:
        logger.warning(f"فشل timedtext: {e}")
        return None


def download_auto_captions_via_ytdlp(video_id: str, language: str = "ar") -> Optional[str]:
    """
    fallback تالت: ينزل auto-captions عن طريق yt-dlp.
    yt-dlp بيقدر يجيب auto-captions للفيديوهات اللي لسه جديدة لما الـ APIs الرسمية تفشل.
    بيرجع SRT string أو None.
    """
    logger.info(f"yt-dlp captions: trying {video_id} (lang={language})")
    try:
        import tempfile
        from yt_dlp import YoutubeDL
    except Exception as e:
        logger.warning(f"yt-dlp مش متاح: {e}")
        return None

    try:
        with tempfile.TemporaryDirectory() as td:
            temp_dir = Path(td)
            ydl_opts = {
                "skip_download": True,
                "writesubtitles": False,
                "writeautomaticsub": True,
                "subtitleslangs": [language, f"{language}.*", "ar", "a.ar"],
                "subtitlesformat": "srt/vtt/best",
                "quiet": True,
                "no_warnings": True,
                "outtmpl": str(temp_dir / "%(id)s.%(ext)s"),
            }
            url = f"https://www.youtube.com/watch?v={video_id}"
            try:
                with YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
            except Exception as e:
                logger.warning(f"yt-dlp فشل تنزيل captions: {e}")
                return None

            # دور على ملف .srt أو .vtt يبدأ بالـ video_id
            srt_files = sorted(temp_dir.glob(f"{video_id}*.srt"))
            vtt_files = sorted(temp_dir.glob(f"{video_id}*.vtt"))

            if srt_files:
                try:
                    return srt_files[0].read_text(encoding="utf-8", errors="replace")
                except Exception as e:
                    logger.warning(f"فشل قراءة srt من yt-dlp: {e}")
                    return None
            if vtt_files:
                try:
                    vtt = vtt_files[0].read_text(encoding="utf-8", errors="replace")
                    return _vtt_to_srt(vtt)
                except Exception as e:
                    logger.warning(f"فشل قراءة/تحويل vtt من yt-dlp: {e}")
                    return None

            logger.info("yt-dlp: مفيش ملف captions اتنزل")
            return None
    except Exception as e:
        logger.warning(f"yt-dlp captions غير متوقع: {e}")
        return None


def _vtt_to_srt(vtt: str) -> str:
    """تحويل بسيط من WEBVTT لـ SRT"""
    import re
    lines = vtt.replace("\r\n", "\n").split("\n")
    out = []
    idx = 0
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            # convert "00:00:00.000" to "00:00:00,000"
            ts = line.replace(".", ",")
            # strip cue settings after the timestamps
            ts = re.sub(r"\s+(align|line|position|size|vertical):.*$", "", ts)
            text_lines = []
            i += 1
            while i < len(lines) and lines[i].strip():
                text_lines.append(lines[i])
                i += 1
            if text_lines:
                idx += 1
                out.append(f"{idx}\n{ts}\n" + "\n".join(text_lines))
        i += 1
    return "\n\n".join(out) + "\n"


def _srv3_to_srt(srv3_xml: str) -> str:
    """حوّل YouTube srv3 XML format لـ SRT"""
    import re
    out = []
    # كل segment بيكون <p t="start_ms" d="duration_ms">text</p>
    matches = re.finditer(r'<p t="(\d+)" d="(\d+)"[^>]*>(.*?)</p>', srv3_xml, re.DOTALL)
    for i, m in enumerate(matches, 1):
        start_ms = int(m.group(1))
        dur_ms = int(m.group(2))
        end_ms = start_ms + dur_ms
        text = m.group(3)
        # شيل أي tags داخلية
        text = re.sub(r'<[^>]+>', '', text).strip()
        if not text:
            continue
        out.append(str(i))
        out.append(f"{_ms_to_ts(start_ms)} --> {_ms_to_ts(end_ms)}")
        out.append(text)
        out.append("")
    return "\n".join(out)


def _ms_to_ts(ms: int) -> str:
    h = ms // 3600000
    m = (ms % 3600000) // 60000
    s = (ms % 60000) // 1000
    msp = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{msp:03d}"


def get_or_create_playlist(youtube, title: str, description: str = "") -> Optional[str]:
    """
    يدور على playlist بنفس العنوان في القناة، لو موجودة يرجع الـ id.
    لو مش موجودة، يعملها (privacy=unlisted) ويرجع الـ id.
    بيرجع None لو حصل خطأ.
    """
    title = (title or "").strip()
    if not title:
        return None
    # دور على playlist موجودة بنفس العنوان
    try:
        page_token = None
        while True:
            req = youtube.playlists().list(
                part="snippet",
                mine=True,
                maxResults=50,
                pageToken=page_token,
            )
            response = req.execute()
            for item in response.get("items", []):
                existing_title = item.get("snippet", {}).get("title", "").strip()
                if existing_title == title:
                    pid = item["id"]
                    logger.info(f"playlist موجودة: {title} ({pid})")
                    return pid
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except HttpError as e:
        logger.warning(f"فشل قراءة playlists: {e}")
        # نكمل ونحاول نعمل واحدة جديدة

    # مفيش playlist بنفس الاسم → اعمل واحدة جديدة
    try:
        body = {
            "snippet": {
                "title": title[:150],
                "description": description[:5000],
                "defaultLanguage": "ar",
            },
            "status": {
                "privacyStatus": "unlisted",
            },
        }
        response = youtube.playlists().insert(
            part="snippet,status",
            body=body,
        ).execute()
        pid = response["id"]
        logger.info(f"تم إنشاء playlist: {title} ({pid})")
        return pid
    except HttpError as e:
        logger.error(f"فشل إنشاء playlist '{title}': {e}")
        return None


def update_playlist_metadata(youtube, playlist_id: str, title: Optional[str] = None,
                              description: Optional[str] = None) -> bool:
    """يحدّث عنوان/وصف playlist موجود."""
    if not playlist_id:
        return False
    try:
        # YouTube API بيحتاج العنوان دايماً في الـ update، فنجيبه أولاً لو ما اتبعتش
        if title is None:
            res = youtube.playlists().list(part="snippet", id=playlist_id).execute()
            if not res.get("items"):
                return False
            title = res["items"][0]["snippet"]["title"]
        body = {"id": playlist_id, "snippet": {"title": title}}
        if description is not None:
            body["snippet"]["description"] = description[:5000]
        youtube.playlists().update(part="snippet", body=body).execute()
        return True
    except HttpError as e:
        logger.error(f"فشل تحديث playlist {playlist_id}: {e}")
        return False


def add_video_to_playlist(youtube, video_id: str, playlist_id: str) -> bool:
    """
    يضيف فيديو لـ playlist. بيرجع True لو نجح أو الفيديو موجود فعلاً.
    بيرجع False لو فشل.
    """
    if not video_id or not playlist_id:
        return False
    try:
        youtube.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": video_id,
                    },
                }
            },
        ).execute()
        logger.info(f"تم إضافة {video_id} لـ playlist {playlist_id}")
        return True
    except HttpError as e:
        # لو الفيديو موجود فعلاً في الـ playlist، YouTube بيرجع 409 أو رسالة فيها duplicate
        msg = str(e).lower()
        if "duplicate" in msg or e.resp.status in (409,):
            logger.info(f"الفيديو {video_id} موجود فعلاً في playlist {playlist_id}")
            return True
        logger.warning(f"فشل إضافة {video_id} لـ playlist {playlist_id}: {e}")
        return False


def update_video_metadata(
    youtube,
    video_id: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[List[str]] = None,
    category_id: Optional[str] = None,
) -> None:
    """يحدّث snippet للفيديو على YouTube بعد ما Gemini يخلص"""
    # YouTube بيتطلب نقرا snippet الحالي ونعدل عليه
    current = youtube.videos().list(part="snippet", id=video_id).execute()
    items = current.get("items", [])
    if not items:
        raise ValueError(f"الفيديو غير موجود: {video_id}")
    snippet = items[0]["snippet"]

    if title is not None:
        snippet["title"] = title[:100]
    if description is not None:
        snippet["description"] = description[:5000]
    if tags is not None:
        snippet["tags"] = tags[:30]
    if category_id is not None:
        snippet["categoryId"] = category_id

    youtube.videos().update(
        part="snippet",
        body={"id": video_id, "snippet": snippet},
    ).execute()
    logger.info(f"تم تحديث الفيديو {video_id}")
