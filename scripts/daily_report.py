# -*- coding: utf-8 -*-
"""تقرير يومي عن تشغيل كرون النشر — يتبعت على Telegram (admin_chat_id أو REPORT_CHAT_ID).
يُشغَّل من جذر المشروع على VPS عبر cron الساعة 21:00 القاهرة (بعد آخر رفعة 20:00)."""
import os, sys, sqlite3, subprocess, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DB = "data/abuhafs.db"
LOG = "logs/catalog_full.log"
SERIES = {
    "sbl-alslam-almosl-al-blogh-almram": "سُبل السلام",
    "shrh-shyh-alamam-mslm-bmgmaa-alfrkan-bmdyn-alsnblaoyn": "شرح صحيح مسلم",
}
# نافذة آخر ~16 ساعة تغطّي يوم النشر بغضّ النظر عن TZ
WINDOW = "datetime('now','-16 hours')"


def q(con, sql, args=()):
    return con.execute(sql, args).fetchall()


def build_report():
    con = sqlite3.connect(DB)
    lines = ["📊 <b>تقرير النشر اليومي</b>", ""]

    # 1) المحاضرات المرفوعة اليوم لكل سلسلة
    lines.append("<b>المحاضرات المرفوعة:</b>")
    total_main = 0
    up_rows = q(con, f"""SELECT series_slug, id, reupload_yt_id, title
                         FROM catalog_videos
                         WHERE process_status='uploaded' AND updated_at >= {WINDOW}
                         ORDER BY updated_at""")
    by_series = {}
    for slug, cid, ytid, title in up_rows:
        by_series.setdefault(slug, []).append((cid, ytid, title))
        total_main += 1
    for slug, name in SERIES.items():
        items = by_series.get(slug, [])
        lines.append(f"• {name}: <b>{len(items)}</b>")
        for cid, ytid, title in items:
            n_sh = q(con, """SELECT COUNT(*) FROM catalog_shorts
                             WHERE parent_catalog_id=? AND status='posted'""", (cid,))[0][0]
            n_all = q(con, "SELECT COUNT(*) FROM catalog_shorts WHERE parent_catalog_id=?", (cid,))[0][0]
            t = (title or "")[:38]
            link = f"https://youtu.be/{ytid}" if ytid else ""
            lines.append(f"   - {t} — shorts {n_sh}/{n_all} {link}")
    lines.append(f"الإجمالي: <b>{total_main}</b> محاضرة")
    lines.append("")

    # 2) shorts المنشورة اليوم
    sh_today = q(con, f"SELECT COUNT(*) FROM catalog_shorts WHERE status='posted' AND posted_at >= {WINDOW}")[0][0]
    sh_pending = q(con, "SELECT COUNT(*) FROM catalog_shorts WHERE status!='posted'")[0][0]
    lines.append(f"🎬 <b>Shorts</b> اليوم: {sh_today} منشور | في الطابور: {sh_pending}")
    lines.append("")

    # 3) Gemini API vs web fallback + الأخطاء (من آخر 1500 سطر باللوج)
    tail = ""
    if os.path.exists(LOG):
        tail = subprocess.run(["tail", "-n", "1500", LOG], capture_output=True, text=True).stdout
    low = tail.lower()
    fb = low.count("web fallback")
    e429 = low.count("429") + low.count("quota")
    failed = low.count("failed") + low.count("❌")
    lines.append("<b>الحالة الفنية:</b>")
    lines.append(f"• Gemini web fallback: {fb} مرة" + (" (الـ API كفى)" if fb == 0 else ""))
    lines.append(f"• 429/quota: {e429}")
    lines.append(f"• إشارات فشل (loader.to/FB/IG/TG): {failed}")
    lines.append("")

    # 4) المساحة على السيرفر
    df = subprocess.run(["df", "-h", "/"], capture_output=True, text=True).stdout.splitlines()
    if len(df) > 1:
        p = df[1].split()
        lines.append(f"💾 القرص: مستخدم {p[2]} / {p[1]} ({p[4]})، فاضي {p[3]}")

    return "\n".join(lines)


def main():
    text = build_report()
    # اكتب الملف دايماً
    out = Path("logs/daily_report.txt"); out.write_text(text, encoding="utf-8")
    # الإرسال على Telegram
    chat = os.environ.get("REPORT_CHAT_ID")
    try:
        from src.config import Config
        cfg = Config()
        if not chat:
            chat = (cfg.get("telegram", default={}) or {}).get("admin_chat_id")
        tok = json.load(open("credentials/telegram_credentials.json")).get("bot_token")
        if chat and tok:
            from src.telegram_uploader import send_message
            send_message(bot_token=tok, chat_id=str(chat), text=text, parse_mode="HTML")
            print("report sent to", chat)
        else:
            print("no chat_id — report written to logs/daily_report.txt only")
    except Exception as e:
        print("send failed:", e, "— report in logs/daily_report.txt")
    print(text)


if __name__ == "__main__":
    main()
