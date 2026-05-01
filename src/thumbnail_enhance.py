"""
موديول تحسين الـ Thumbnail
- بيستخدم OpenCV dnn_superres لرفع جودة الـ frame المستخرج من الفيديوهات القديمة
  (الفيديوهات الأصلية 320×240 مرفوعة لـ 720p — في تشوهات واضحة)
- بيدعم EDSR (أحسن جودة) و FSRCNN (أسرع) و ESPCN
- لو الموديول مش متاح أو حصل خطأ، بنرجع للصورة الأصلية بدون كسر الـ pipeline
- بنضيف طبقة "studio frame": خلفية مضببة من الإطار نفسه + كروب ميد للسرعة
- أخف من Real-ESRGAN / GFPGAN اللي بيحتاجوا GPU أو RAM ضخمة (السيرفر 3.7GB RAM فقط بدون GPU)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# اسم الموديل → (URL, supported scales)
MODEL_REGISTRY = {
    "edsr": {
        "url_template": "https://github.com/Saafke/EDSR_Tensorflow/raw/master/models/EDSR_x{scale}.pb",
        "scales": [2, 3, 4],
        "alg_name": "edsr",
    },
    "fsrcnn": {
        "url_template": "https://github.com/Saafke/FSRCNN_Tensorflow/raw/master/models/FSRCNN_x{scale}.pb",
        "scales": [2, 3, 4],
        "alg_name": "fsrcnn",
    },
    "espcn": {
        "url_template": "https://github.com/fannymonori/TF-ESPCN/raw/master/export/ESPCN_x{scale}.pb",
        "scales": [2, 3, 4],
        "alg_name": "espcn",
    },
}


# Cache للموديل عشان منحملوش كل مرة
_model_cache: dict[str, object] = {}


def _load_sr_model(method: str, scale: int, model_path: Path):
    """يحمّل موديل OpenCV super-resolution مرة واحدة فقط (cached)."""
    key = f"{method}_x{scale}"
    if key in _model_cache:
        return _model_cache[key]

    try:
        import cv2  # type: ignore
    except ImportError as e:
        raise RuntimeError("opencv-contrib-python غير مثبت. ثبّت: pip install opencv-contrib-python") from e

    if not hasattr(cv2, "dnn_superres"):
        raise RuntimeError("cv2.dnn_superres غير متاح — تأكد إنك مثبّت opencv-contrib-python (مش opencv-python)")

    if not model_path.exists():
        raise FileNotFoundError(f"موديل {key} مش موجود في: {model_path}")

    if method not in MODEL_REGISTRY:
        raise ValueError(f"method غير مدعوم: {method}. المدعوم: {list(MODEL_REGISTRY.keys())}")

    sr = cv2.dnn_superres.DnnSuperResImpl_create()
    sr.readModel(str(model_path))
    sr.setModel(MODEL_REGISTRY[method]["alg_name"], scale)
    _model_cache[key] = sr
    logger.info(f"تم تحميل موديل super-resolution: {key} من {model_path}")
    return sr


def _unsharp_mask(image, amount: float = 0.6, radius: float = 1.5):
    """sharpening خفيف بعد الـ upscale لإبراز التفاصيل."""
    import cv2  # type: ignore
    blurred = cv2.GaussianBlur(image, (0, 0), radius)
    return cv2.addWeighted(image, 1 + amount, blurred, -amount, 0)


def _color_grade(image, saturation: float = 1.10, contrast: float = 1.05):
    """ تعديل لوني خفيف: زيادة التشبع والتباين الطفيف لإحساس أكثر حيوية."""
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    # تباين خفيف
    image = cv2.convertScaleAbs(image, alpha=contrast, beta=0)

    # saturation عبر HSV
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV).astype("float32")
    hsv[..., 1] = np.clip(hsv[..., 1] * saturation, 0, 255)
    image = cv2.cvtColor(hsv.astype("uint8"), cv2.COLOR_HSV2BGR)
    return image


def enhance_frame(
    image_path: Path,
    output_path: Optional[Path] = None,
    method: str = "edsr",
    scale_factor: int = 4,
    model_path: str = "",
    target_width: int = 1280,
    target_height: int = 720,
    apply_sharpen: bool = True,
    apply_color_grade: bool = True,
    max_input_pixels: int = 2_000_000,  # ~1.7MP — guard against OOM
) -> Path:
    """
    يرفع جودة الـ frame باستخدام موديل CNN (OpenCV dnn_superres).

    Args:
        image_path: الصورة الأصلية (الـ frame المستخرج).
        output_path: لو None، بيكتب فوق الأصلية.
        method: edsr | fsrcnn | espcn.
        scale_factor: 2 | 3 | 4 (بيتطابق مع الموديل المحمّل).
        model_path: مسار ملف .pb للموديل.
        target_width/height: المقاس النهائي المطلوب (بنعمل resize Lanczos في الآخر).
        max_input_pixels: لو الصورة كبيرة جداً، نتجنب SR (مش هيفيد) ونرجعها كما هي.

    Returns:
        Path للصورة المحسّنة. لو حصل أي خطأ، بترجع الـ image_path الأصلي.
    """
    image_path = Path(image_path)
    if output_path is None:
        output_path = image_path
    output_path = Path(output_path)

    if not image_path.exists():
        logger.warning(f"الصورة المراد تحسينها مش موجودة: {image_path}")
        return image_path

    try:
        import cv2  # type: ignore
    except ImportError:
        logger.warning("OpenCV (cv2) غير مثبت — تخطي مرحلة التحسين، استخدام الـ frame الأصلي")
        return image_path

    try:
        img = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if img is None:
            logger.warning(f"فشل قراءة الصورة: {image_path} — استخدام الأصلية")
            return image_path

        h, w = img.shape[:2]

        # لو الصورة كبيرة بالفعل، خلاص ما فيش حاجة للـ super-resolution
        if w * h > max_input_pixels:
            logger.info(
                f"الإطار {w}×{h} كبير بالفعل ({w*h:,} pixel) — تخطي super-resolution، تطبيق sharpen+grade فقط"
            )
            enhanced = img
        else:
            # طبّق super-resolution
            try:
                sr = _load_sr_model(method, scale_factor, Path(model_path))
                logger.info(f"تطبيق {method.upper()}×{scale_factor} على إطار {w}×{h}...")
                enhanced = sr.upsample(img)
                eh, ew = enhanced.shape[:2]
                logger.info(f"تم رفع الجودة → {ew}×{eh}")
            except Exception as e:
                logger.warning(f"فشل super-resolution ({e}) — استخدام Lanczos بديل")
                # fallback لأبسط upscale
                new_w = w * scale_factor
                new_h = h * scale_factor
                enhanced = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)

        # ✦ resize للحجم النهائي مع المحافظة على الأبعاد (16:9 crop)
        eh, ew = enhanced.shape[:2]
        target_ratio = target_width / target_height
        cur_ratio = ew / eh

        if cur_ratio > target_ratio:
            # عرض زائد — اقطع من الجانبين
            new_w = int(eh * target_ratio)
            x0 = (ew - new_w) // 2
            enhanced = enhanced[:, x0:x0 + new_w]
        elif cur_ratio < target_ratio:
            # ارتفاع زائد — اقطع من فوق وتحت
            new_h = int(ew / target_ratio)
            y0 = (eh - new_h) // 2
            enhanced = enhanced[y0:y0 + new_h, :]

        enhanced = cv2.resize(enhanced, (target_width, target_height), interpolation=cv2.INTER_LANCZOS4)

        # ✦ sharpening
        if apply_sharpen:
            enhanced = _unsharp_mask(enhanced, amount=0.5, radius=1.2)

        # ✦ color grading
        if apply_color_grade:
            enhanced = _color_grade(enhanced, saturation=1.08, contrast=1.04)

        # احفظ
        output_path.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(output_path), enhanced, [cv2.IMWRITE_JPEG_QUALITY, 95])
        logger.info(f"تم حفظ الـ frame المحسّن: {output_path}")
        return output_path

    except MemoryError:
        logger.error("MemoryError أثناء التحسين — استخدام الإطار الأصلي")
        return image_path
    except Exception as e:
        logger.warning(f"خطأ غير متوقع أثناء التحسين ({type(e).__name__}: {e}) — استخدام الإطار الأصلي")
        return image_path


def is_available() -> bool:
    """check سريع لو OpenCV+contrib مثبتين."""
    try:
        import cv2  # type: ignore
        return hasattr(cv2, "dnn_superres")
    except ImportError:
        return False
