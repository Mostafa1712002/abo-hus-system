"""
موديول الذكاء الاصطناعي (Gemini)

بيستخدم Google Gemini (مجاني) لتوليد:
- عنوان جذاب من الترجمة
- وصف كامل
- chapters للوصف
- تحديد المقاطع المهمة عشان تتعمل Shorts
- hashtags
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from dataclasses import dataclass, asdict, field
from typing import List, Optional

import google.generativeai as genai

logger = logging.getLogger(__name__)


@dataclass
class Chapter:
    timestamp: str  # "MM:SS" أو "HH:MM:SS"
    title: str

    @property
    def seconds(self) -> int:
        parts = [int(p) for p in self.timestamp.split(":")]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return 0


@dataclass
class ImportantClip:
    start_seconds: float
    end_seconds: float
    reason: str
    suggested_short_title: str
    hook: str = ""  # عبارة قصيرة 2-3 كلمات تصلح للـ thumbnail/caption (اختيارية)
    description: str = ""  # 3-5 جمل عربية - وصف الـ Short كفيديو مستقل، ينتهي بدعوة لمتابعة المحاضرة الكاملة
    tags: list[str] = field(default_factory=list)  # 8-12 وسم محسّن للانتشار


@dataclass
class TelegramQuote:
    """مقولة نصية مقتبسة من المحاضرة، صالحة للنشر كمنشور مستقل على تليجرام."""
    text: str  # 1-3 جمل، نص الاقتباس نفسه
    context_hint: str = ""  # سطر قصير اختياري للسياق (مثال: "في معرض الكلام عن منهج الإمام")
    source_timestamp: str = ""  # "MM:SS" — لربط لحظة النطق على يوتيوب


@dataclass
class VideoMetadata:
    title: str
    description: str
    chapters: List[Chapter]
    hashtags: List[str]
    important_clips: List[ImportantClip]
    text_quotes: List[TelegramQuote] = field(default_factory=list)

    def description_with_chapters(self, base_description: str = "") -> str:
        body = base_description or self.description
        if not self.chapters:
            return body + ("\n\n" + " ".join(f"#{h}" for h in self.hashtags) if self.hashtags else "")
        chapters_text = "\n".join(f"{c.timestamp} {c.title}" for c in self.chapters)
        tags_text = " ".join(f"#{h}" for h in self.hashtags) if self.hashtags else ""
        return f"{body}\n\nالفصول:\n{chapters_text}\n\n{tags_text}".strip()


class GeminiGenerator:
    """يولد metadata الفيديو من نص الترجمة - مع دعم rotation للمفاتيح"""

    def __init__(self, api_key: str = "", model_name: str = "gemini-2.5-flash",
                 api_keys: Optional[List[str]] = None):
        # api_keys للـ rotation (كل مفتاح ليه 1500 req/day)
        self.api_keys = [k for k in (api_keys or [api_key]) if k]
        if not self.api_keys:
            raise ValueError("لازم تدي api_key أو api_keys")
        self._key_idx = 0
        self.model_name = model_name
        self._configure_current()

    def _configure_current(self):
        genai.configure(api_key=self.api_keys[self._key_idx])
        self.model = genai.GenerativeModel(self.model_name)

    def _rotate_key(self):
        """انتقل للمفتاح التالي عند rate limit"""
        self._key_idx = (self._key_idx + 1) % len(self.api_keys)
        logger.info(f"تدوير المفتاح → key #{self._key_idx + 1}/{len(self.api_keys)}")
        self._configure_current()

    def generate(
        self,
        srt_text_with_timestamps: str,
        plain_text: str,
        video_duration_seconds: float,
        max_clips: int = 3,
        min_clip_seconds: int = 25,
        max_clip_seconds: int = 58,
        max_title_length: int = 70,
        max_hashtags: int = 8,
        include_chapters: bool = True,
        content_context: str = "",
        description_template_hint: str = "",
        shorts_selection_hint: str = "",
    ) -> VideoMetadata:
        """يولد كل الـ metadata في call واحدة (لتقليل عدد الطلبات)"""
        prompt = self._build_prompt(
            srt_text_with_timestamps,
            plain_text,
            video_duration_seconds,
            max_clips,
            min_clip_seconds,
            max_clip_seconds,
            max_title_length,
            max_hashtags,
            include_chapters,
            content_context,
            description_template_hint,
            shorts_selection_hint,
        )

        logger.info(f"إرسال الطلب لـ Gemini ({self.model_name}, key #{self._key_idx + 1}/{len(self.api_keys)})...")

        last_error = None
        response = None
        for attempt in range(len(self.api_keys)):
            try:
                response = self.model.generate_content(
                    prompt,
                    generation_config=genai.GenerationConfig(
                        response_mime_type="application/json",
                        temperature=0.7,
                        max_output_tokens=8192,
                    ),
                )
                break
            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                rotate_triggers = (
                    "quota", "rate", "429",          # rate-limit
                    "api_key_invalid", "api key not valid", "invalid_argument",  # expired/revoked
                    "401", "403", "permission_denied", "unauthenticated",        # auth issues
                    "expired",
                )
                if any(t in err_str for t in rotate_triggers):
                    if len(self.api_keys) > 1:
                        logger.warning(f"المفتاح #{self._key_idx + 1} فشل ({type(e).__name__})، تدوير...")
                        self._rotate_key()
                        continue
                raise
        if response is None:
            raise RuntimeError(f"كل المفاتيح وصلت rate limit: {last_error}")

        text = response.text or ""
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # fallback 1: استخرج أول JSON object من الرد
            match = re.search(r"\{.*\}", text, re.DOTALL)
            extracted = match.group(0) if match else text
            try:
                data = json.loads(extracted)
            except json.JSONDecodeError:
                # fallback 2: json-repair (يصلح JSON المعطوب من LLMs)
                try:
                    from json_repair import repair_json
                    repaired = repair_json(extracted, return_objects=True)
                    if not isinstance(repaired, dict):
                        raise ValueError("repair_json returned non-dict")
                    data = repaired
                    logger.warning("Gemini رجّع JSON معطوب — تم إصلاحه بـ json-repair")
                except Exception as repair_err:
                    # log الـ response الخام للتشخيص
                    debug_path = Path("logs/gemini_bad_response.txt")
                    debug_path.parent.mkdir(parents=True, exist_ok=True)
                    debug_path.write_text(text, encoding="utf-8")
                    raise ValueError(
                        f"رد Gemini مش JSON صالح ولا قابل للإصلاح. "
                        f"الـ response الخام محفوظ في {debug_path}. "
                        f"خطأ الإصلاح: {repair_err}"
                    )

        return self._parse_response(data, video_duration_seconds, max_clip_seconds, min_clip_seconds, max_clips)

    # المعايير الافتراضية لاختيار مقاطع Shorts (تُستخدم لو ما اتبعتش من config)
    DEFAULT_SHORTS_SELECTION_HINT = (
        "- يحتوي على فائدة مكثفة قابلة للاقتباس (نقطة واحدة واضحة لا تشتيت).\n"
        "- يفتح بهوك قوي خلال أول 3 ثواني: سؤال صادم، حكم قاطع، قصة مؤثرة، نتيجة مفاجئة، أو كلمة قوية.\n"
        "- يقف بذاته بدون الحاجة لخلفية أو سياق سابق (السامع يفهم من غير ما يكون شاف الفيديو الأصلي).\n"
        "- يغطي فكرة واحدة فقط بشكل كامل ومرتب (لا تعدد ولا أفكار متفرعة).\n"
        "- يُفضّل: حكم شرعي واضح، قصة من السلف، رد على شبهة، تحذير قاطع، فائدة لغوية أو حديثية مذهلة، إجابة مختصرة عن سؤال شائع.\n"
        "- يُتجنّب: التمهيدات والمقدمات، السرد الطويل بدون لب، ذكر أسانيد مطولة بدون فائدة عملية للمستمع.\n"
        "- المدة المثالية 30-50 ثانية (الحد الأدنى المطلق والأقصى محددان أدناه)."
    )

    def _build_prompt(
        self,
        srt_with_ts: str,
        plain_text: str,
        duration: float,
        max_clips: int,
        min_clip: int,
        max_clip: int,
        max_title: int,
        max_hashtags: int,
        include_chapters: bool,
        content_context: str = "",
        description_template_hint: str = "",
        shorts_selection_hint: str = "",
    ) -> str:
        # اقطع النص لو طويل جداً
        if len(plain_text) > 30000:
            plain_text = plain_text[:30000] + "..."
        if len(srt_with_ts) > 30000:
            srt_with_ts = srt_with_ts[:30000] + "..."

        chapters_instr = (
            f"6. chapters: قائمة بـ 3-7 فصول. كل فصل {{timestamp: \"MM:SS\", title: \"عنوان الفصل\"}}. "
            f"أول فصل لازم يبدأ من 00:00. لازم يكون مبني على timestamps من النص."
            if include_chapters
            else "6. chapters: []"
        )

        shorts_criteria = (shorts_selection_hint or self.DEFAULT_SHORTS_SELECTION_HINT).strip()

        context_block = ""
        if content_context:
            context_block = f"\nسياق القناة والمحتوى:\n{content_context.strip()}\n"

        desc_hint_block = ""
        if description_template_hint:
            desc_hint_block = f"\nإرشاد لبنية الوصف: {description_template_hint.strip()}\n"

        return f"""أنت مساعد متخصص في تحسين فيديوهات يوتيوب العربية وصناعة Shorts قوية تصل للمشاهد بسرعة.
هذه ترجمة فيديو مدته {duration:.0f} ثانية تقريباً. اقرأها بعناية وأنتج البيانات المطلوبة.
{context_block}{desc_hint_block}
---
الترجمة مع التوقيتات:
{srt_with_ts}
---

أنتج JSON بالشكل التالي بالظبط (لا تضف أي شرح خارج JSON):

{{
  "title": "عنوان جذاب وقصير بالعربي - أقل من {max_title} حرف، يثير الفضول، بدون كليك بيت مبتذل",
  "description": "وصف بالعربي 3-5 فقرات يلخص الفيديو ويحفز المشاهد للمشاهدة. ابدأ بهوك قوي. اذكر أهم النقاط بدون حرق المحتوى كله.",
  "hashtags": ["الإمام الشافعي", "أدب الخلاف", "طلب العلم"],
  "chapters": [
    {{"timestamp": "00:00", "title": "المقدمة"}},
    {{"timestamp": "MM:SS", "title": "..."}}
  ],
  "important_clips": [
    {{
      "start_seconds": رقم,
      "end_seconds": رقم,
      "reason": "ليه المقطع ده مهم وإيه نوع الهوك فيه",
      "suggested_short_title": "عنوان جذاب لـ Short",
      "hook": "كلمتين أو ثلاثة قوية تصلح كـ caption على الـ thumbnail",
      "description": "3-5 جمل عربية فصيحة تشرح الفائدة في الـ Short باختصار، تذكر الشيخ والقناة، وتنتهي بدعوة لمتابعة المحاضرة الكاملة. لا روابط ولا تطويل ولا تكرار.",
      "tags": ["تربية الأبناء", "الإمام الشافعي", "الشيخ سامي العربي"]
    }}
  ],
  "text_quotes": [
    {{
      "text": "نص الاقتباس بالعربية الفصحى، جملة إلى ثلاث، مكتفٍ بذاته بدون اقتباسات مزدوجة محيطة.",
      "context_hint": "سطر قصير للسياق إن لزم (اختياري، يمكن تركه فاضي)",
      "source_timestamp": "MM:SS"
    }}
  ]
}}

قواعد مهمة:
1. title: بالعربي الفصيح الموقر، أقل من {max_title} حرف، يجذب الانتباه بدون ابتذال.
2. description: مفصلة، فيها قيمة، تساعد SEO. أقل من 4500 حرف.
3. hashtags: حد أقصى {max_hashtags} هاشتاج بالعربي بدون علامة #. هذه القائمة تُستخدم كـ YouTube tags وأيضاً كـ #hashtags في الوصف.
   - كل عنصر لازم يكون عبارة عربية طبيعية بفراغات عادية بين الكلمات.
   - ممنوع منعاً باتاً استخدام underscore (_) أو شَرطة (-) لربط الكلمات.
   - أمثلة صحيحة: "الإمام الشافعي"، "أدب الخلاف"، "طلب العلم"، "الشيخ سامي العربي".
   - أمثلة خاطئة (لا تفعلها): "الإمام_الشافعي"، "أدب-الخلاف"، "طلب_العلم".
{chapters_instr}
7. important_clips: حد أقصى {max_clips} مقاطع موجّهة لـ YouTube Shorts (رأسي ≤60 ثانية).
   - مدة كل مقطع لازم تكون بين {min_clip} و {max_clip} ثانية (الأمثل 30-50 ثانية).
   - يبدأ في نقطة طبيعية في الكلام (مش في نص جملة)، ويفضّل بعد سكتة قصيرة.
   - معايير اختيار المقطع المُثَمَّر (التزم بها بحرفية):
{shorts_criteria}
   - حقل "hook" اختياري لكنه مفضّل: 2-3 كلمات قوية تختصر فكرة المقطع وتصلح كعنوان فرعي على الـ thumbnail (مثال: "احذر هذا الذنب"، "سرٌّ من السلف"، "حكم قاطع").
   - حقل "suggested_short_title" يجب أن يكون استفزازياً للفضول وموقّراً في نفس الوقت (لا تهويل ولا ابتذال).
   - حقل "description" (إلزامي لكل clip): 3-5 جمل عربية فصيحة، تشرح الفائدة في الـ Short باختصار لأن الـ Short ينشر مستقلاً، تذكر الشيخ والقناة، وتنتهي بدعوة لمتابعة المحاضرة الكاملة. لا تضع روابط (تُضاف برمجياً). لا تطويل ولا تكرار.
   - حقل "tags" (إلزامي لكل clip): قائمة من 8-12 وسماً مختاراً بدقة للانتشار، توزيعها كالتالي:
     * 2-3 وسوم خاصة بمحتوى الـ clip (الكلمات المحورية في الفائدة).
     * 2-3 وسوم تُحدد نوع المحتوى (مثل: حديث، فقه، عقيدة، تفسير، سيرة، آداب… بحسب موضوع المقطع).
     * وسوم العلامة التجارية إلزامية: "الشيخ سامي العربي"، "أبو حفص الأثري"، "فضيلة الشيخ أبو حفص".
     * وسوم Shorts عامة: "Shorts"، "إسلاميات"، "دروس شرعية".
     * تجنّب التكرار. تجنّب الكلمات العامة جداً (مثل: "فيديو" أو "مقطع"). الوسوم بدون علامة #.
     * كل وسم يجب أن يكون عبارة عربية طبيعية بفراغات عادية بين الكلمات. ممنوع underscore (_) أو شَرطة (-).
     * أمثلة صحيحة: "تربية الأبناء"، "أدب الخلاف"، "الإمام الشافعي". أمثلة خاطئة: "تربية_الأبناء"، "أدب-الخلاف".
   - الأفضلية المطلقة للمقاطع التي توصل الفائدة بأسرع وقت ممكن دون حشو.
8. text_quotes: اختر من 3 إلى 5 مقولات مكتوبة قصيرة (جملة إلى ثلاث) من المحاضرة، صالحة للنشر على تليجرام كمنشور نصي مستقل. كل مقولة لازم تكون:
   - مكتفية بذاتها (لا تحتاج سياق سابق ليفهمها القارئ).
   - قابلة للاقتباس وبليغة، تنقل فائدة قاطعة أو حكمة مؤثرة أو حكماً شرعياً واضحاً.
   - **اختر مقولات مختلفة عن الـ important_clips ولا تكررها — الـ Shorts تغطي تلك المواضع، فاجعل المقولات هنا من مواضع أخرى في المحاضرة.**
   - تجنّب المقولات المكسورة أو الناقصة أو التي تعتمد على إشارة لشيء سبق.
   - **مهم جداً — تصحيح أخطاء الـ auto-captions:** نص الـ SRT أعلاه ناتج من YouTube auto-captions وقد يحتوي أخطاءً إملائية أو نحوية أو كلمات مسموعة بشكل خاطئ. مهمتك:
     • صحّح الإملاء والنحو في كل اقتباس قبل إخراجه.
     • أضف علامات الترقيم المناسبة (نقطة، فاصلة، علامتي تنصيص).
     • إذا كانت كلمة لا تتفق مع السياق (sound-alike خطأ)، استبدلها بالأقرب معنى.
     • لا تنشر اقتباساً يحتوي أخطاء واضحة كما هو — صحّحه أو تجاهله.
   - الصيغة: نص عربي فصيح صحيح إملائياً ونحوياً، بدون رموز ترقيم زائدة، بدون اقتباسات مزدوجة محيطة بالنص، بدون تنسيق markdown، بدون أقواس ASCII `()` أو `[]`.
   - حقل "context_hint" اختياري — استخدمه فقط إن كان السياق ضرورياً (مثال: "في معرض الكلام عن منهج الإمام")، وإلا اتركه نصاً فارغاً "".
   - حقل "source_timestamp" بصيغة MM:SS لتحديد لحظة النطق في المحاضرة (لاستخدامه لاحقاً مع رابط يوتيوب).
9. لا تخترع معلومات. اعتمد فقط على ما في الترجمة، ولا تنسب أحاديث أو أقوال غير موجودة فيها.
10. ارجع JSON صحيح فقط بدون أي markdown أو شرح.
"""

    def _parse_response(
        self,
        data: dict,
        video_duration: float,
        max_clip_seconds: int,
        min_clip_seconds: int,
        max_clips: int,
    ) -> VideoMetadata:
        title = (data.get("title") or "").strip()
        description = (data.get("description") or "").strip()
        hashtags = [h.strip().lstrip("#") for h in data.get("hashtags", []) if h.strip()]

        chapters: List[Chapter] = []
        for ch in data.get("chapters", []) or []:
            ts = (ch.get("timestamp") or "").strip()
            t = (ch.get("title") or "").strip()
            if ts and t and re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", ts):
                chapters.append(Chapter(timestamp=ts, title=t))

        clips: List[ImportantClip] = []
        for c in data.get("important_clips", []) or []:
            try:
                s = float(c.get("start_seconds", 0))
                e = float(c.get("end_seconds", 0))
            except (TypeError, ValueError):
                continue
            # ضمان الحدود
            if e <= s:
                continue
            duration_clip = e - s
            # قص لو طويل أوي
            if duration_clip > max_clip_seconds:
                e = s + max_clip_seconds
            if duration_clip < min_clip_seconds:
                continue
            if e > video_duration:
                e = video_duration
            hook_val = str(c.get("hook", "") or "").strip()[:60]
            short_title_val = str(c.get("suggested_short_title", title))[:100]
            description_val = str(c.get("description", "") or "").strip()[:1500]
            raw_tags = c.get("tags", []) or []
            tags_val: List[str] = []
            seen_tags = set()
            if isinstance(raw_tags, list):
                for t in raw_tags:
                    if not isinstance(t, (str, int, float)):
                        continue
                    t_clean = str(t).strip().lstrip("#")
                    if not t_clean:
                        continue
                    key = t_clean.lower()
                    if key in seen_tags:
                        continue
                    seen_tags.add(key)
                    tags_val.append(t_clean)
            clips.append(ImportantClip(
                start_seconds=s,
                end_seconds=e,
                reason=str(c.get("reason", ""))[:300],
                suggested_short_title=short_title_val,
                # fallback: لو الموديل ما رجعش hook، خد أول كلمتين/ثلاثة من suggested_short_title
                hook=hook_val or " ".join(short_title_val.split()[:3]),
                description=description_val,
                tags=tags_val,
            ))

        clips = clips[:max_clips]

        # ===== text_quotes (للنشر على تليجرام كمنشورات نصية مستقلة) =====
        quotes: List[TelegramQuote] = []
        raw_quotes = data.get("text_quotes", []) or []
        if isinstance(raw_quotes, list):
            for q in raw_quotes:
                if not isinstance(q, dict):
                    continue
                text = str(q.get("text", "") or "").strip()
                # نشيل اقتباسات مزدوجة محيطة لو الموديل حطها
                if len(text) >= 2 and text[0] in '"“«' and text[-1] in '"”»':
                    text = text[1:-1].strip()
                if not text:
                    continue
                # حد منطقي للطول (3 جمل ~ 500 حرف)
                if len(text) > 800:
                    text = text[:800].rstrip() + "..."
                ctx = str(q.get("context_hint", "") or "").strip()[:200]
                ts = str(q.get("source_timestamp", "") or "").strip()
                # ضمان فورمات MM:SS أو HH:MM:SS
                if ts and not re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", ts):
                    ts = ""
                quotes.append(TelegramQuote(
                    text=text, context_hint=ctx, source_timestamp=ts,
                ))
        # نحد بـ 5 (الحد الأعلى المطلوب)
        quotes = quotes[:5]

        if not title:
            raise ValueError("Gemini رجع عنوان فاضي - حاول تاني")

        return VideoMetadata(
            title=title,
            description=description,
            chapters=chapters,
            hashtags=hashtags,
            important_clips=clips,
            text_quotes=quotes,
        )


def metadata_to_dict(md: VideoMetadata) -> dict:
    """تحويل metadata لـ dict يتحفظ في JSON"""
    return {
        "title": md.title,
        "description": md.description,
        "hashtags": md.hashtags,
        "chapters": [asdict(c) for c in md.chapters],
        "important_clips": [asdict(c) for c in md.important_clips],
        "text_quotes": [asdict(q) for q in md.text_quotes],
    }
