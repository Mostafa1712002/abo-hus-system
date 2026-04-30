"""
موديول الـ Thumbnail
- بيختار أحسن frame من الفيديو (frame واضح، مش أسود ولا blurry)
- بيضيف العنوان العربي عليه بخط عريض ونص متوسط
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

logger = logging.getLogger(__name__)


# ---------------- استخراج Frame ----------------

def get_video_duration(video_path: str | Path) -> float:
    """يرجع مدة الفيديو بالثواني باستخدام ffprobe"""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of",
            "default=noprint_wrappers=1:nokey=1", str(video_path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def _frame_quality_score(img: Image.Image) -> float:
    """
    score أعلى = أحسن frame.
    بيقيس: السطوع (مش أسود)، التباين، والـ edges (مش blurry).
    """
    gray = img.convert("L")
    stat = ImageStat.Stat(gray)
    brightness = stat.mean[0]
    # عقاب على frames سوداء أو بيضاء جداً
    if brightness < 30 or brightness > 230:
        return 0.0
    # تباين (stddev بيدي إشارة على وجود تفاصيل)
    contrast = stat.stddev[0]
    # edge density (تقريب)
    edges = gray.filter(ImageFilter.FIND_EDGES)
    edge_score = ImageStat.Stat(edges).mean[0]
    return contrast * 1.5 + edge_score * 2.0


def extract_best_frame(
    video_path: str | Path,
    output_path: str | Path,
    samples: int = 12,
    skip_intro_seconds: float = 3.0,
    skip_outro_seconds: float = 3.0,
) -> Path:
    """
    يأخذ عدة لقطات على مدار الفيديو ويختار أحسنها (بناءً على وضوح الصورة).
    """
    video_path = Path(video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    duration = get_video_duration(video_path)
    start = min(skip_intro_seconds, duration * 0.05)
    end = max(duration - skip_outro_seconds, duration * 0.95)
    if end <= start:
        end = duration

    timestamps = [start + (end - start) * i / (samples - 1) for i in range(samples)]

    best_score = -1.0
    best_path: Optional[Path] = None

    with tempfile.TemporaryDirectory() as tmp:
        for i, ts in enumerate(timestamps):
            frame_path = Path(tmp) / f"frame_{i:02d}.jpg"
            subprocess.run(
                [
                    "ffmpeg", "-y", "-ss", str(ts), "-i", str(video_path),
                    "-frames:v", "1", "-q:v", "2", str(frame_path),
                ],
                capture_output=True, check=False,
            )
            if not frame_path.exists():
                continue
            try:
                with Image.open(frame_path) as im:
                    score = _frame_quality_score(im)
            except Exception:
                continue
            if score > best_score:
                best_score = score
                # احتفظ بنسخة دائمة
                with Image.open(frame_path) as im:
                    im.save(output_path, "JPEG", quality=95)
                best_path = output_path

    if best_path is None:
        # fallback: خد frame من نص الفيديو
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", str(duration / 2), "-i", str(video_path),
                "-frames:v", "1", "-q:v", "2", str(output_path),
            ],
            check=True, capture_output=True,
        )
        best_path = output_path

    logger.info(f"تم اختيار أحسن frame (score={best_score:.1f}): {best_path}")
    return best_path


# ---------------- إضافة النص العربي ----------------

def _shape_arabic(text: str) -> str:
    """يحول العربي للشكل الصحيح للعرض (connect letters + RTL)"""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        reshaped = arabic_reshaper.reshape(text)
        return get_display(reshaped)
    except ImportError:
        logger.warning("arabic_reshaper مش متثبت، النص العربي قد يظهر مكسور")
        return text


def _wrap_arabic(text: str, max_chars_per_line: int = 22) -> list[str]:
    """يقسم النص لأسطر بحيث ميتعدش max_chars في السطر، بدون قطع كلمة"""
    words = text.split()
    lines: list[str] = []
    current = ""
    for w in words:
        candidate = (current + " " + w).strip()
        if len(candidate) <= max_chars_per_line:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def _overlay_logo(base: Image.Image, logo_path: str | Path, position: str = "top-left",
                  size_ratio: float = 0.18, padding_ratio: float = 0.03) -> Image.Image:
    """يضع اللوجو على الـ thumbnail. اللوجو لازم يكون PNG بخلفية شفافة."""
    logo_path = Path(logo_path)
    if not logo_path.exists():
        return base
    logo = Image.open(logo_path).convert("RGBA")
    bw, bh = base.size
    # حجم اللوجو
    logo_h = int(bh * size_ratio)
    logo_w = int(logo.width * (logo_h / logo.height))
    logo = logo.resize((logo_w, logo_h), Image.LANCZOS)
    pad = int(bh * padding_ratio)
    positions = {
        "top-left": (pad, pad),
        "top-right": (bw - logo_w - pad, pad),
        "bottom-left": (pad, bh - logo_h - pad),
        "bottom-right": (bw - logo_w - pad, bh - logo_h - pad),
        "center": ((bw - logo_w) // 2, (bh - logo_h) // 2),
    }
    pos = positions.get(position, positions["top-left"])
    base = base.convert("RGBA")
    base.paste(logo, pos, logo)
    return base


def add_text_to_thumbnail(
    image_path: str | Path,
    output_path: str | Path,
    title: str,
    width: int = 1280,
    height: int = 720,
    font_path: str = "",
    title_color: str = "#FFFFFF",
    stroke_color: str = "#000000",
    stroke_width: int = 6,
    overlay_opacity: float = 0.45,
    logo_path: str = "",
    logo_position: str = "top-left",
    logo_size_ratio: float = 0.18,
) -> Path:
    """
    يأخذ صورة (frame) ويضيف عليها العنوان كـ overlay.
    """
    image_path = Path(image_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # حضّر الصورة الأساسية
    base = Image.open(image_path).convert("RGB")
    # crop & resize لـ 16:9
    base = _smart_crop_to_aspect(base, width, height)

    # طبقة شفافة سوداء عشان النص يبان
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, int(255 * overlay_opacity)))
    # gradient من تحت لفوق (أغمق تحت)
    grad = Image.new("L", (1, height), 0)
    for y in range(height):
        # شدة العتمة من 0 لفوق لـ 180 لتحت
        grad.putpixel((0, y), int(40 + 140 * (y / height)))
    grad = grad.resize((width, height))
    gradient_overlay = Image.new("RGBA", (width, height))
    gradient_overlay.putalpha(grad)
    gradient_overlay = Image.composite(
        Image.new("RGBA", (width, height), (0, 0, 0, 255)),
        Image.new("RGBA", (width, height), (0, 0, 0, 0)),
        gradient_overlay.split()[-1],
    )

    composed = base.convert("RGBA")
    composed = Image.alpha_composite(composed, gradient_overlay)

    draw = ImageDraw.Draw(composed)

    # حمّل الخط
    font = _load_font(font_path, size=int(height * 0.10))
    # لف النص لسطور
    shaped_lines = [_shape_arabic(line) for line in _wrap_arabic(title)]

    # احسب ارتفاع كل سطر
    line_heights = []
    for line in shaped_lines:
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        line_heights.append(bbox[3] - bbox[1])
    total_h = sum(line_heights) + (len(shaped_lines) - 1) * 10

    # ارسم من الأسفل تجاه فوق (بعيد عن الحواف بـ 8%)
    y = height - int(height * 0.08) - total_h
    for line, lh in zip(shaped_lines, line_heights):
        bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
        text_w = bbox[2] - bbox[0]
        x = (width - text_w) // 2
        draw.text(
            (x, y),
            line,
            font=font,
            fill=title_color,
            stroke_width=stroke_width,
            stroke_fill=stroke_color,
        )
        y += lh + 10

    # ضيف اللوجو لو موجود
    if logo_path:
        composed = _overlay_logo(composed, logo_path, position=logo_position,
                                  size_ratio=logo_size_ratio)

    composed.convert("RGB").save(output_path, "JPEG", quality=92)
    logger.info(f"تم حفظ الـ thumbnail: {output_path}")
    return output_path


def _smart_crop_to_aspect(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """crop & resize للأبعاد المطلوبة مع الحفاظ على المركز"""
    target_ratio = target_w / target_h
    src_w, src_h = img.size
    src_ratio = src_w / src_h

    if src_ratio > target_ratio:
        # الصورة عريضة جداً → اقطع من الجانبين
        new_w = int(src_h * target_ratio)
        x0 = (src_w - new_w) // 2
        img = img.crop((x0, 0, x0 + new_w, src_h))
    elif src_ratio < target_ratio:
        # الصورة طويلة جداً → اقطع من فوق وتحت
        new_h = int(src_w / target_ratio)
        y0 = (src_h - new_h) // 2
        img = img.crop((0, y0, src_w, y0 + new_h))

    return img.resize((target_w, target_h), Image.LANCZOS)


def _load_font(font_path: str, size: int) -> ImageFont.FreeTypeFont:
    """يحاول يحمل الخط، ولو مش موجود يستخدم default"""
    if font_path and Path(font_path).exists():
        return ImageFont.truetype(font_path, size)

    # حاول إيجاد خط عربي عام
    candidates = [
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/tahoma.ttf",
        "C:/Windows/Fonts/calibrib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for c in candidates:
        if Path(c).exists():
            logger.warning(f"الخط المحدد مش موجود، استخدام: {c}")
            return ImageFont.truetype(c, size)

    logger.warning("مفيش أي خط متاح، استخدام الخط الافتراضي (مش بيدعم العربي كويس)")
    return ImageFont.load_default()


def _extract_first_frame(video_path: str | Path, output_path: str | Path,
                         offset_seconds: float = 1.0) -> Optional[Path]:
    """يستخرج frame واحد من بداية الفيديو (افتراضياً عند الثانية الأولى)."""
    video_path = Path(video_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", str(offset_seconds), "-i", str(video_path),
                "-frames:v", "1", "-q:v", "2", str(output_path),
            ],
            capture_output=True, check=False,
        )
    except Exception as e:
        logger.warning(f"فشل استخراج frame: {e}")
        return None
    if not output_path.exists() or output_path.stat().st_size == 0:
        return None
    return output_path


def make_short_thumbnail(
    short_video_path: str | Path,
    output_path: str | Path,
    logo_path: str | Path,
    width: int = 1080,
    height: int = 1920,
    background_strategy: str = "blurred_frame",  # or "solid_dark"
) -> Path:
    """يعمل thumbnail عمودي 9:16 لـ Short باللوجو في المنتصف بدون أي نص."""
    short_video_path = Path(short_video_path)
    output_path = Path(output_path)
    logo_path = Path(logo_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ===== 1) الخلفية =====
    base: Optional[Image.Image] = None
    if background_strategy == "blurred_frame":
        with tempfile.TemporaryDirectory() as tmp:
            frame_path = Path(tmp) / "first_frame.jpg"
            extracted = _extract_first_frame(short_video_path, frame_path)
            if extracted is not None:
                try:
                    with Image.open(extracted) as im:
                        # crop & resize إلى 9:16
                        cropped = _smart_crop_to_aspect(im.convert("RGB"), width, height)
                    # blur ثقيل
                    blurred = cropped.filter(ImageFilter.GaussianBlur(radius=30))
                    # طبقة سوداء 50% فوق الخلفية المضببة
                    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 128))
                    composed = blurred.convert("RGBA")
                    composed = Image.alpha_composite(composed, overlay)
                    base = composed
                except Exception as e:
                    logger.warning(f"فشل تحضير الخلفية المضببة: {e}")
                    base = None

    if base is None:
        # solid_dark fallback: خلفية بلون أزرق غامق متناسق مع اللوجو
        base = Image.new("RGBA", (width, height), (0x0E, 0x1B, 0x2C, 255))

    # ===== 2) اللوجو في المنتصف =====
    if logo_path.exists():
        try:
            logo = Image.open(logo_path).convert("RGBA")
            # حجم اللوجو ≈ 50% من عرض الـ thumbnail
            target_w = int(width * 0.5)
            ratio = target_w / logo.width
            target_h = max(1, int(logo.height * ratio))
            logo = logo.resize((target_w, target_h), Image.LANCZOS)
            x = (width - target_w) // 2
            y = (height - target_h) // 2
            base.paste(logo, (x, y), logo)
        except Exception as e:
            logger.warning(f"فشل لصق اللوجو: {e}")
    else:
        logger.warning(f"اللوجو مش موجود: {logo_path}")

    # ===== 3) حفظ JPEG =====
    base.convert("RGB").save(output_path, "JPEG", quality=92)
    logger.info(f"تم حفظ short thumbnail: {output_path}")
    return output_path


def make_thumbnail(
    video_path: str | Path,
    title: str,
    output_path: str | Path,
    config: dict,
) -> Path:
    """الواجهة الرئيسية: يعمل thumbnail كامل من فيديو + عنوان"""
    output_path = Path(output_path)
    frame_path = output_path.with_suffix(".frame.jpg")
    extract_best_frame(video_path, frame_path)

    final = add_text_to_thumbnail(
        image_path=frame_path,
        output_path=output_path,
        title=title,
        width=config.get("width", 1280),
        height=config.get("height", 720),
        font_path=config.get("font_path", ""),
        title_color=config.get("title_color", "#FFFFFF"),
        stroke_color=config.get("stroke_color", "#000000"),
        stroke_width=config.get("stroke_width", 6),
        overlay_opacity=config.get("overlay_opacity", 0.45),
        logo_path=config.get("logo_path", ""),
        logo_position=config.get("logo_position", "top-left"),
        logo_size_ratio=config.get("logo_size_ratio", 0.18),
    )
    try:
        os.remove(frame_path)
    except OSError:
        pass

    return final
