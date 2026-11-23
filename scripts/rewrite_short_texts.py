#!/usr/bin/env python3
"""Rewrite the title/description/hook/tags of queued Shorts with Claude (subscription).

Queued shorts were written by Gemini from a short excerpt. Claude gets the whole
parent lecture transcript plus the old texts, finds the excerpt and writes new
texts. The old texts stay in title_orig/description_orig/hook_orig/tags_json_orig.
Stops at the Claude usage limit; done rows are marked text_model and skipped.
Usage: rewrite_short_texts.py [--limit N] [--shard K/N]
"""
import argparse
import json
import sqlite3
import sys

sys.path.insert(0, "/opt/abuhafs/youtube-auto-uploader/scripts")
import catalog_full_worker as W  # noqa: E402  (sets sys.path for src.*)
from src import claude_model  # noqa: E402
from src.claude_model import ClaudeModel, claude_enabled  # noqa: E402

log = W.log
NEW_COLS = ("text_model", "title_orig", "description_orig", "hook_orig", "tags_json_orig")


def build_prompt(lecture: str, row, honorifics) -> str:
    honor = "\n".join(f"   • {h}" for h in honorifics)
    return (
        "أنت متخصص في كتابة عناوين وأوصاف YouTube Shorts عربية بأسلوب فصيح موقّر يجذب المشاهد بدون ابتذال.\n\n"
        f"عندك تفريغ المحاضرة كاملة (تفريغ آلي فيه أخطاء سمع، صحّحها في ذهنك)، وعنوان المحاضرة: {row['series'] or ''}\n"
        "ومن المحاضرة اتقص مقطع قصير (أقل من دقيقة)، والنصوص القديمة المكتوبة له:\n"
        f"العنوان القديم: {row['title']}\nالوصف القديم: {row['description']}\nالهوك القديم: {row['hook']}\n\n"
        "المطلوب: حدّد المقطع المقصود من التفريغ، ثم اكتب له نصوصاً أفضل مبنية على كلام الشيخ الفعلي فيه.\n"
        f"ألقاب الشيخ المعتمدة (اختر واحداً يناسب الموضوع، وممنوع \"الشيخ\" مجردة):\n{honor}\n\n"
        f"التفريغ:\n---\n{lecture}\n---\n\n"
        "أخرج JSON فقط بالشكل:\n"
        '{"title": "عنوان فصيح ≤80 حرف يثير الفضول ومطابق لكلام المقطع", '
        '"description": "3-5 جمل فصيحة تشرح الفائدة الفعلية، تذكر الشيخ بلقب من القائمة، وتنتهي بدعوة لمتابعة المحاضرة كاملة. بلا روابط", '
        '"hook": "كلمتان أو ثلاث قوية", '
        '"tags": ["8-12 وسماً عربياً طبيعياً بفراغات، بلا _ أو -"]}\n'
        "قواعد: لا تبتكر موضوعاً غير موجود في المقطع، لا تهويل ولا أسئلة سطحية، "
        "والآيات والأحاديث بنصها الصحيح فقط إن ذكرتها."
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--shard", default="0/1")
    args = ap.parse_args()
    k, n = (int(x) for x in args.shard.split("/"))

    cfg = W.Config(str(W.ROOT / "config.json"))
    honorifics = (cfg.channel_branding or {}).get("speaker_honorifics", []) or []
    con = sqlite3.connect(str(W.DB), timeout=60)
    con.row_factory = sqlite3.Row
    have = {r[1] for r in con.execute("PRAGMA table_info(catalog_shorts)")}
    for c in NEW_COLS:
        if c not in have:
            try:
                con.execute(f"ALTER TABLE catalog_shorts ADD COLUMN {c} TEXT")
            except sqlite3.OperationalError:
                pass  # another shard added it first
    con.commit()

    rows = con.execute(
        "SELECT s.*, v.youtube_id AS parent_src FROM catalog_shorts s "
        "JOIN catalog_videos v ON v.id = s.parent_catalog_id "
        "WHERE s.status='pending' AND s.text_model IS NULL ORDER BY s.id").fetchall()
    model = ClaudeModel() if claude_enabled() else None
    if model is None:
        print("Claude not enabled - nothing to do")
        return

    done = 0
    for r in rows[k::n]:
        srt = W.fetch_srt(r["parent_src"])
        if not srt:
            continue
        lecture = W.P._srt_plain_text(srt)[:60000]
        try:
            data = json.loads(W.re.search(r"\{.*\}", model.generate_content(
                build_prompt(lecture, r, honorifics)).text, W.re.S).group(0))
        except Exception as e:
            if claude_model._capped:
                log.warning("Claude limit reached - stopping")
                break
            log.warning("short %s: failed (%s)", r["id"], str(e)[:200])
            continue
        title = str(data.get("title", "")).strip()[:100]
        desc = str(data.get("description", "")).strip()[:1500]
        hook = str(data.get("hook", "")).strip()
        tags = [str(t).strip().lstrip("#").replace("_", " ") for t in data.get("tags") or [] if str(t).strip()][:12]
        if not title or not desc:
            log.warning("short %s: empty reply, kept old texts", r["id"])
            continue
        con.execute(
            "UPDATE catalog_shorts SET title_orig=title, description_orig=description, hook_orig=hook, "
            "tags_json_orig=tags_json, title=?, description=?, hook=?, tags_json=?, text_model=? "
            "WHERE id=? AND status='pending'",
            (title, desc, hook or r["hook"], json.dumps(tags, ensure_ascii=False), model.model_name, r["id"]))
        con.commit()
        done += 1
        log.info("[%s] short %s: %r -> %r", args.shard, r["id"], (r["title"] or "")[:50], title[:60])
        if args.limit and done >= args.limit:
            break
    print(f"[{args.shard}] rewrote {done}")


if __name__ == "__main__":
    main()
