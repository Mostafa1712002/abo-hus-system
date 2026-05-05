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
        openai_api_key: str = "",
        speaker_honorifics: Optional[List[str]] = None,
    ) -> VideoMetadata:
        """يولد كل الـ metadata في call واحدة (لتقليل عدد الطلبات).

        لو openai_api_key متوفر، بنستخدم OpenAI كـ verifier:
        لكل clip يقترحه Gemini، نستخرج النص الفعلي للـ time range من الـ SRT
        ونطلب من OpenAI يولّد title/description مبنيين على المحتوى الحقيقي
        (مش على توصيف Gemini اللي ممكن يخالف الـ timestamps).
        """
        # Cache the honorifics on the instance so the per-clip writer can reuse
        # them without needing them threaded through every internal call.
        self._speaker_honorifics = list(speaker_honorifics or [])
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
            speaker_honorifics=self._speaker_honorifics,
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

        # Pass extra candidates to give OpenAI verifier room to filter
        gemini_max = max_clips * 2 if openai_api_key else max_clips
        md = self._parse_response(data, video_duration_seconds, max_clip_seconds, min_clip_seconds, gemini_max)

        # ===== Hallucination check =====
        # Gemini sometimes returns timestamps that don't exist in the SRT we
        # actually sent it (e.g. picks "17s" when the filter hid everything
        # before 12:41). Validate each clip's start_seconds against the SRT
        # by ensuring there's at least one transcript line within ±5s of it.
        try:
            md.important_clips = self._reject_hallucinated_clips(
                md.important_clips, srt_text_with_timestamps,
            )
        except Exception as e:
            logger.warning(f"Hallucination check failed (skipping): {e}")

        # ===== OpenAI verifier =====
        # For each clip Gemini picked, extract the actual SRT for that range
        # and have OpenAI generate a faithful title/description + score.
        # Gemini sometimes hallucinates titles that don't match the timestamps.
        if openai_api_key and md.important_clips:
            try:
                md.important_clips = self._verify_clips_with_openai(
                    md.important_clips,
                    srt_text_with_timestamps,
                    openai_api_key,
                    target_count=max_clips,
                )
            except Exception as e:
                logger.warning(f"OpenAI verification failed (kept Gemini output): {e}")

        return md

    def _verify_clips_with_openai(
        self,
        clips: List["ImportantClip"],
        full_srt: str,
        openai_api_key: str,
        target_count: int,
    ) -> List["ImportantClip"]:
        """OpenAI verifier with key rotation.

        ``openai_api_key`` may contain multiple keys separated by newlines or
        commas. On a 429 / insufficient_quota error, the current key is marked
        dead for the rest of the call and we fail over to the next.
        """
        """For each clip, extract the actual SRT text for its time range,
        then ask OpenAI to: (1) score the clip 0-10, (2) generate a faithful
        Arabic title/description/hook/tags from the actual content.

        Replaces the Gemini-supplied title/description/etc. with OpenAI's
        version. Returns the top `target_count` clips by score (approve only).
        """
        try:
            import openai  # type: ignore
        except ImportError:
            logger.warning("openai package not installed; skipping verifier")
            return clips[:target_count]

        # Parse keys: split on newline or comma, strip, drop empties
        all_keys = [k.strip() for line in openai_api_key.splitlines()
                    for k in line.split(",")]
        all_keys = [k for k in all_keys if k.startswith("sk-")]
        if not all_keys:
            logger.warning("No valid OpenAI keys found")
            return clips[:target_count]
        logger.info(f"OpenAI verifier: {len(all_keys)} keys available")
        # Build a list of clients we can try in order; mark dead keys
        dead_keys: set[int] = set()
        clients = [openai.OpenAI(api_key=k) for k in all_keys]
        # Index of the current key we'll try first; once we find a working one
        # we keep using it for the rest of the call.
        cur_idx = 0

        def _call_openai(prompt: str):
            """Call OpenAI rotating through keys on quota errors."""
            nonlocal cur_idx
            tried = 0
            while tried < len(clients):
                if cur_idx in dead_keys:
                    cur_idx = (cur_idx + 1) % len(clients)
                    tried += 1
                    continue
                try:
                    resp = clients[cur_idx].chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{"role": "user", "content": prompt}],
                        response_format={"type": "json_object"},
                        temperature=0.3,
                    )
                    return resp
                except openai.RateLimitError as e:  # type: ignore
                    err = str(e).lower()
                    if "insufficient_quota" in err or "exceeded" in err:
                        logger.warning(
                            f"Key #{cur_idx + 1}/{len(clients)} dead (quota)"
                        )
                        dead_keys.add(cur_idx)
                        cur_idx = (cur_idx + 1) % len(clients)
                        tried += 1
                        continue
                    raise
                except Exception:
                    raise
            raise RuntimeError("All OpenAI keys exhausted/dead")

        client = clients[0]  # placeholder, actual calls go through _call_openai

        # The SRT here is in "simple" format produced by _srt_with_simple_timestamps:
        # each line is "[MM:SS] text" (MM is total minutes, can exceed 59).
        # Also accept standard SRT blocks with "HH:MM:SS,mmm --> ..." for safety.
        simple_re = re.compile(r"^\s*\[(\d+):(\d{2})\]\s*(.+)$")
        srt_block_re = re.compile(
            r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)"
        )

        def _excerpt(start_s: float, end_s: float) -> str:
            texts: list[str] = []
            # Try simple-format line-by-line first
            for line in full_srt.splitlines():
                m = simple_re.match(line)
                if not m:
                    continue
                sec = int(m.group(1)) * 60 + int(m.group(2))
                if sec < start_s or sec > end_s:
                    continue
                t = (m.group(3) or "").strip()
                if t:
                    texts.append(t)
            if texts:
                return " ".join(texts).strip()
            # Fallback: try standard SRT block format
            blocks = re.split(r"\n\s*\n", full_srt)
            for b in blocks:
                m = srt_block_re.search(b)
                if not m:
                    continue
                bs = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
                be = int(m[5]) * 3600 + int(m[6]) * 60 + int(m[7])
                if be < start_s or bs > end_s:
                    continue
                lines = b.strip().split("\n")
                if len(lines) >= 3:
                    texts.append(" ".join(ln.strip() for ln in lines[2:]))
            return " ".join(texts).strip()

        scored: list[tuple[float, "ImportantClip"]] = []
        for clip in clips:
            excerpt = _excerpt(clip.start_seconds, clip.end_seconds)
            if not excerpt:
                logger.info(
                    "OpenAI verifier: no SRT text for %.0f-%.0fs, skipping",
                    clip.start_seconds, clip.end_seconds,
                )
                continue

            prompt = (
                "أنت ناقد محتوى ديني عربي تحلّل مقاطع YouTube Shorts من محاضرات إسلامية.\n"
                "مهمتك: التعرف على موضوع المقطع وتقييم مدى صلاحيته كـ Short مستقل.\n"
                "❌ لا تكتب عناوين أو وصف — مهمتك التحليل والتقييم فقط (Gemini سيكتب النص بعدك).\n\n"
                f"مدة المقطع: {clip.end_seconds - clip.start_seconds:.0f} ثانية\n"
                f"النص الفعلي للمقطع (من auto-captions، قد يحتوي أخطاء إملائية بسيطة فاحرص على فهم المعنى):\n"
                f"---\n{excerpt}\n---\n\n"
                "أنتج JSON يحتوي:\n"
                "- topic_summary: جملة عربية واحدة (15-30 كلمة) تلخص الفكرة الرئيسية الموجودة فعلياً في النص.\n"
                "- key_points: قائمة 2-4 نقاط مفتاحية ذُكرت بالنص (لاستخدامها كمرجع لـ Gemini).\n"
                "- standalone: bool — يقف بذاته بدون سياق سابق؟\n"
                "- score: 0-10:\n"
                "    8-10 = مفيد ومكتفٍ بذاته\n"
                "    5-7 = مفيد لكن يحتاج سياق\n"
                "    0-4 = حشو/مقدمة/تكرار\n"
                "- approve: bool — true لو score >= 7 و standalone=true\n"
                "- reason: سبب التقييم\n\n"
                "ارجع JSON بالظبط:\n"
                "{\n"
                '  "topic_summary": "ملخص جملة واحدة لموضوع المقطع",\n'
                '  "key_points": ["نقطة 1", "نقطة 2"],\n'
                '  "standalone": true|false,\n'
                '  "score": رقم 0-10,\n'
                '  "approve": true|false,\n'
                '  "reason": "سبب التقييم"\n'
                "}"
            )
            try:
                resp = _call_openai(prompt)
                result = json.loads(resp.choices[0].message.content or "{}")
            except Exception as e:
                logger.warning(f"OpenAI call failed for clip {clip.start_seconds:.0f}s: {e}")
                continue

            score = float(result.get("score", 0))
            approve = bool(result.get("approve", False)) and score >= 6
            topic = str(result.get("topic_summary", "")).strip()
            key_points = [str(p).strip() for p in (result.get("key_points") or []) if p]
            logger.info(
                "OpenAI clip %.0f-%.0fs: score=%.1f approve=%s topic=%s",
                clip.start_seconds, clip.end_seconds, score, approve, topic[:60],
            )
            if not approve:
                continue

            # ===== Stage 2: Gemini writes title/description =====
            # OpenAI told us the actual topic; now ask Gemini to write the
            # final marketing copy in its preferred eloquent Arabic style.
            try:
                gemini_text = self._gemini_write_clip_text(
                    excerpt=excerpt,
                    topic=topic,
                    key_points=key_points,
                    duration=clip.end_seconds - clip.start_seconds,
                )
                if gemini_text.get("title"):
                    clip.suggested_short_title = gemini_text["title"][:100]
                if gemini_text.get("description"):
                    clip.description = gemini_text["description"][:1500]
                if gemini_text.get("hook"):
                    clip.hook = gemini_text["hook"][:60]
                if gemini_text.get("tags"):
                    clip.tags = list(gemini_text["tags"])[:12]
            except Exception as e:
                logger.warning(
                    f"Gemini text gen failed for clip {clip.start_seconds:.0f}s "
                    f"(keeping Gemini's original): {e}"
                )

            scored.append((score, clip))

        # Sort by score descending, take top N
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [c for _, c in scored[:target_count]]
        logger.info(
            f"OpenAI verifier: {len(top)} approved out of {len(clips)} candidates "
            f"(target {target_count})"
        )
        return top

    @staticmethod
    def _reject_hallucinated_clips(
        clips: List["ImportantClip"], srt_text: str, window_s: float = 5.0,
    ) -> List["ImportantClip"]:
        """Reject clips whose start_seconds doesn't correspond to any SRT
        line within ``window_s`` seconds. Catches Gemini hallucinations
        (returning timestamps that don't exist in the input SRT).
        """
        # Parse all timestamps from the SRT (simple [MM:SS] or block format)
        timestamps: list[float] = []
        simple_re = re.compile(r"^\s*\[(\d+):(\d{2})\]")
        for line in srt_text.splitlines():
            m = simple_re.match(line)
            if m:
                timestamps.append(int(m.group(1)) * 60 + int(m.group(2)))
        if not timestamps:
            # Fallback to standard SRT block format
            block_re = re.compile(
                r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->"
            )
            for m in block_re.finditer(srt_text):
                timestamps.append(
                    int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                )
        if not timestamps:
            return clips  # nothing to validate against; pass through

        timestamps.sort()
        kept: list["ImportantClip"] = []
        for clip in clips:
            # Binary search for nearest timestamp
            import bisect
            idx = bisect.bisect_left(timestamps, clip.start_seconds)
            candidates = []
            if idx > 0:
                candidates.append(timestamps[idx - 1])
            if idx < len(timestamps):
                candidates.append(timestamps[idx])
            nearest_dist = min(abs(t - clip.start_seconds) for t in candidates) if candidates else 1e9
            if nearest_dist > window_s:
                logger.info(
                    "rejecting hallucinated clip start=%.0fs (nearest SRT line %.0fs away)",
                    clip.start_seconds, nearest_dist,
                )
                continue
            kept.append(clip)
        if len(kept) < len(clips):
            logger.info(
                "hallucination check: kept %d/%d clips",
                len(kept), len(clips),
            )
        return kept

    def _gemini_write_clip_text(
        self,
        excerpt: str,
        topic: str,
        key_points: list,
        duration: float,
    ) -> dict:
        """Use Gemini (better at eloquent Arabic) to write the title/description/hook/tags
        for a Short, given the actual SRT excerpt + OpenAI's topic identification.

        Returns: {"title": str, "description": str, "hook": str, "tags": [str]}
        """
        kp_block = "\n".join(f"  • {p}" for p in (key_points or []))
        honorifics = getattr(self, "_speaker_honorifics", []) or []
        honorifics_rule = ""
        if honorifics:
            honor_list = "\n".join(f'   • "{h}"' for h in honorifics)
            honorifics_rule = (
                "\nألقاب الشيخ المعتمدة (اختر **واحداً فقط** يناسب موضوع هذا المقطع، ونوّع بين المقاطع):\n"
                f"{honor_list}\n"
                "🚫 ممنوع كتابة \"الشيخ\" مجردة. استخدم لقباً من القائمة (مثل المحدث/العلامة/الفقيه/فضيلة).\n"
                "اختر اللقب الأنسب: المحدث للأحاديث، العلامة/الفقيه للأحكام، فضيلة الشيخ ... الأثري للعام.\n"
            )
        prompt = (
            "أنت متخصص في كتابة عناوين وأوصاف YouTube Shorts عربية بأسلوب فصيح موقّر "
            "يجذب المشاهد بدون ابتذال.\n\n"
            f"مدة المقطع: {duration:.0f} ثانية\n"
            f"موضوع المقطع (مُحدَّد بدقة): {topic}\n"
            f"النقاط الرئيسية الفعلية:\n{kp_block}\n"
            f"{honorifics_rule}\n"
            "النص الفعلي للمقطع (auto-captions، صحّح أخطاء واضحة عند الإشارة لها):\n"
            f"---\n{excerpt}\n---\n\n"
            "اكتب JSON بالظبط:\n"
            "{\n"
            '  "title": "عنوان عربي فصيح ≤80 حرف، يثير الفضول، مبني على النص الفعلي والموضوع المحدد",\n'
            '  "description": "وصف 3-5 جمل عربية فصيحة، يشرح الفائدة الفعلية في المقطع، يذكر الشيخ بلقب من القائمة أعلاه والقناة، وينتهي بدعوة لمتابعة المحاضرة كاملة. لا روابط ولا تكرار.",\n'
            '  "hook": "كلمتين أو ثلاثة قوية كهوك على الصورة",\n'
            '  "tags": ["8-12 وسم عربي مبني على المحتوى الفعلي، بدون _ أو -"]\n'
            "}\n\n"
            "قواعد:\n"
            "- العنوان والوصف لازم يكونا مطابقين للنص الفعلي والموضوع المحدد، مش يبتكر مواضيع غير موجودة.\n"
            "- العنوان فصيح موقّر — لا تهويل ولا أسئلة سطحية.\n"
            "- لو ذُكر الشيخ في العنوان أو الوصف، استخدم لقباً من القائمة (ممنوع \"الشيخ\" مجردة).\n"
            "- الوسوم: عبارات عربية طبيعية بفراغات (مثلاً: \"محبة الله\"، \"دعاء المصيبة\")، ممنوع underscore أو شَرطة."
        )

        # Try with rotation across the existing Gemini keys
        last_err: Optional[Exception] = None
        for attempt in range(len(self.api_keys)):
            try:
                resp = self.model.generate_content(
                    prompt,
                    generation_config=genai.GenerationConfig(
                        response_mime_type="application/json",
                        temperature=0.6,
                    ),
                )
                txt = resp.text or "{}"
                data = json.loads(txt)
                title = str(data.get("title", "")).strip()
                desc = str(data.get("description", "")).strip()
                hook = str(data.get("hook", "")).strip()
                raw_tags = data.get("tags") or []
                tags: list[str] = []
                seen = set()
                for t in raw_tags:
                    if not isinstance(t, (str, int, float)):
                        continue
                    tc = str(t).strip().lstrip("#")
                    if not tc or tc.lower() in seen:
                        continue
                    seen.add(tc.lower())
                    tags.append(tc)
                return {"title": title, "description": desc, "hook": hook, "tags": tags}
            except Exception as e:
                last_err = e
                err_str = str(e).lower()
                if "rate" in err_str or "quota" in err_str or "429" in err_str:
                    self._rotate_key()
                    continue
                raise
        if last_err is not None:
            raise last_err
        return {}

    # المعايير الافتراضية لاختيار مقاطع Shorts (تُستخدم لو ما اتبعتش من config)
    DEFAULT_SHORTS_SELECTION_HINT = (
        "🚫 ممنوع منعاً باتاً اختيار مقاطع من المقدمة (أول 15% من الفيديو) أو الخاتمة (آخر 10%).\n"
        "✅ المقاطع لازم تكون من **منتصف المحاضرة** فقط — تحديداً بين 15% و 90% من إجمالي مدة الفيديو.\n"
        "   مثال: لو الفيديو 60 دقيقة (3600 ثانية)، اختر فقط بين الثانية 540 (15%) والثانية 3240 (90%).\n"
        "- يحتوي على فائدة مكثفة قابلة للاقتباس (نقطة واحدة واضحة لا تشتيت).\n"
        "- يفتح بهوك قوي خلال أول 3 ثواني: سؤال صادم، حكم قاطع، قصة مؤثرة، نتيجة مفاجئة، أو كلمة قوية.\n"
        "- يقف بذاته بدون الحاجة لخلفية أو سياق سابق (السامع يفهم من غير ما يكون شاف الفيديو الأصلي).\n"
        "- يغطي فكرة واحدة فقط بشكل كامل ومرتب (لا تعدد ولا أفكار متفرعة).\n"
        "- يُفضّل: حكم شرعي واضح، قصة من السلف، رد على شبهة، تحذير قاطع، فائدة لغوية أو حديثية مذهلة، إجابة مختصرة عن سؤال شائع.\n"
        "- يُتجنّب تماماً: المقدمات وذكر اسم الشيخ والترحيب والتقديم والبسملة والحمدلة الافتتاحية،\n"
        "  السرد الطويل بدون لب، ذكر أسانيد مطولة بدون فائدة عملية للمستمع، الخاتمة والدعاء وآخر المحاضرة.\n"
        "- المقاطع المختارة لازم تكون موزّعة على المنتصف — لا تختار 3 مقاطع متجاورة، بل من مواضع مختلفة.\n"
        "- المدة المثالية 30-50 ثانية (الحد الأدنى المطلق والأقصى محددان أدناه)."
    )

    @staticmethod
    def _filter_srt_to_middle(
        srt_text: str, duration: float,
        intro_pct: float = 0.15, outro_pct: float = 0.90,
    ) -> str:
        """Keep only SRT lines whose timestamp is in [intro_pct, outro_pct] of duration.

        Supports two formats:
        - Simple "[MM:SS] text" (one line per cue, total minutes can exceed 59)
        - Standard SRT blocks "HH:MM:SS,mmm --> HH:MM:SS,mmm\\ntext"

        This prevents the model from "anchoring" on early intro lines.
        """
        if duration <= 0:
            return srt_text
        intro_cutoff = duration * intro_pct
        outro_cutoff = duration * outro_pct

        # Detect simple format (line-based, "[MM:SS] text")
        simple_re = re.compile(r"^\s*\[(\d+):(\d{2})\]")
        srt_block_re = re.compile(
            r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)"
        )

        # Try simple format first
        simple_lines = [ln for ln in srt_text.splitlines() if simple_re.match(ln)]
        if simple_lines:
            kept = []
            for ln in srt_text.splitlines():
                m = simple_re.match(ln)
                if not m:
                    # Non-timestamped line — drop (it's noise in simple format)
                    continue
                sec = int(m.group(1)) * 60 + int(m.group(2))
                if sec < intro_cutoff or sec > outro_cutoff:
                    continue
                kept.append(ln)
            return "\n".join(kept)

        # Fallback: standard SRT block format
        blocks = re.split(r"\n\s*\n", srt_text)
        kept_blocks: list[str] = []
        for b in blocks:
            b = b.strip()
            if not b:
                continue
            m = srt_block_re.search(b)
            if not m:
                kept_blocks.append(b)
                continue
            start_sec = (
                int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            )
            if start_sec < intro_cutoff or start_sec > outro_cutoff:
                continue
            kept_blocks.append(b)
        return "\n\n".join(kept_blocks)

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
        speaker_honorifics: Optional[List[str]] = None,
    ) -> str:
        # CRITICAL: For Shorts selection to actually pick from the middle, hide
        # the intro/outro from the model entirely. Truncating the full SRT to
        # 30000 chars from the start would mean Gemini only sees the intro on
        # long videos. Instead we filter to the middle BEFORE truncating.
        full_srt_for_chapters = srt_with_ts  # keep full for chapter reasoning
        srt_with_ts = self._filter_srt_to_middle(srt_with_ts, duration)

        # اقطع النص لو طويل جداً (sample evenly to span the middle, not just the start)
        def _sample_evenly(text: str, max_chars: int) -> str:
            if len(text) <= max_chars:
                return text
            blocks = re.split(r"\n\s*\n", text)
            if len(blocks) <= 1:
                return text[:max_chars] + "..."
            # Pick blocks at even stride to span the middle window
            target_blocks = max_chars // max(1, (len(text) // max(1, len(blocks))))
            stride = max(1, len(blocks) // max(1, target_blocks))
            sampled = blocks[::stride]
            out = "\n\n".join(sampled)
            if len(out) > max_chars:
                out = out[:max_chars]
            return out

        if len(plain_text) > 30000:
            plain_text = plain_text[:30000] + "..."
        if len(srt_with_ts) > 30000:
            srt_with_ts = _sample_evenly(srt_with_ts, 30000)

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

        honorifics_block = ""
        if speaker_honorifics:
            honor_list = "\n".join(f'   • "{h}"' for h in speaker_honorifics)
            honorifics_block = (
                "\nألقاب الشيخ المعتمدة (اختر **واحداً فقط** يناسب موضوع هذا الفيديو):\n"
                f"{honor_list}\n"
                "🚫 ممنوع منعاً باتاً كتابة كلمة \"الشيخ\" مجردة (بدون لقب أو إضافة بعدها) "
                "في أي عنوان أو وصف أو اقتباس أو نص short.\n"
                "✅ لازم تستخدم لقباً من القائمة أعلاه — ونوّع بين الفيديوهات حسب موضوع كل واحد:\n"
                "   - فيديو فيه أحاديث/أسانيد → فضّل \"المحدث\"\n"
                "   - فيديو فقهي/أحكام → فضّل \"العلامة\" أو \"الفقيه\"\n"
                "   - فيديو عام أو وعظ → فضّل \"فضيلة الشيخ ... الأثري\"\n"
                "   - لا تكرر نفس اللقب في كل العناوين، نوّع.\n"
            )

        return f"""أنت مساعد متخصص في تحسين فيديوهات يوتيوب العربية وصناعة Shorts قوية تصل للمشاهد بسرعة.
هذه ترجمة فيديو مدته {duration:.0f} ثانية تقريباً. اقرأها بعناية وأنتج البيانات المطلوبة.
{context_block}{desc_hint_block}{honorifics_block}
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
      "tags": ["تربية الأبناء", "الإمام الشافعي", "فضيلة الشيخ أبو حفص الأثري"]
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
   - أمثلة صحيحة: "الإمام الشافعي"، "أدب الخلاف"، "طلب العلم"، "فضيلة الشيخ أبو حفص الأثري".
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
        # Reject clips that fall in the intro (first 15%) or outro (last 10%)
        # to avoid the boring "اسمي/مقدمتي/أهلاً وسهلاً" segments that Gemini
        # sometimes still picks. We allow some tolerance — if the model starts
        # at e.g. 12% but the clip mostly extends into the middle, we keep it.
        intro_cutoff = video_duration * 0.15
        outro_cutoff = video_duration * 0.90
        rejected_intro = 0
        for c in data.get("important_clips", []) or []:
            try:
                s = float(c.get("start_seconds", 0))
                e = float(c.get("end_seconds", 0))
            except (TypeError, ValueError):
                continue
            # ضمان الحدود
            if e <= s:
                continue
            # Reject intro/outro picks
            if s < intro_cutoff:
                logger.info(
                    "rejecting intro clip start=%.1fs (cutoff=%.1fs)", s, intro_cutoff,
                )
                rejected_intro += 1
                continue
            if s > outro_cutoff:
                logger.info(
                    "rejecting outro clip start=%.1fs (cutoff=%.1fs)", s, outro_cutoff,
                )
                rejected_intro += 1
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
