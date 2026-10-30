#!/usr/bin/env python3
"""Pull competitor tags+hashtags via YouTube Data API (existing key).
Aggregate ranks by CHANNEL-FREQUENCY (shared across channels) => the copy-worthy niche core.
Usage: python3 competitor_research.py <handle|url|id|file.txt> ... [--per N] [--dump pool.json]
"""
import json, os, re, sys, urllib.parse, urllib.request
from collections import Counter

CFG = json.load(open("config.json")); KEY = CFG["youtube_data_api_key"]
API = "https://www.googleapis.com/youtube/v3/"
FLAG_VALS = {"--per", "--dump"}  # flags that consume the next token

def api(ep, **p):
    p["key"] = KEY
    with urllib.request.urlopen(API + ep + "?" + urllib.parse.urlencode(p), timeout=30) as r:
        return json.load(r)

def video_id(tok):
    m = (re.search(r"[?&]v=([\w-]{11})", tok) or re.search(r"youtu\.be/([\w-]{11})", tok)
         or re.search(r"/shorts/([\w-]{11})", tok) or re.match(r"([\w-]{11})$", tok.strip()))
    return m.group(1) if m else None

def resolve_channel(tok):
    tok = tok.strip().rstrip("/")
    m = re.search(r"/channel/(UC[\w-]{22})", tok) or re.match(r"(UC[\w-]{22})$", tok)
    if m: return m.group(1)
    m = re.search(r"@([\w.\-]+)", tok)
    if m:
        d = api("channels", part="id", forHandle="@" + m.group(1))
        if d.get("items"): return d["items"][0]["id"]
    vid = video_id(tok)
    if vid:
        d = api("videos", part="snippet", id=vid)
        if d.get("items"): return d["items"][0]["snippet"]["channelId"]
    return None

def recent_ids(chan_id, n):
    up = api("channels", part="contentDetails", id=chan_id)
    if not up.get("items"): return []
    pl = up["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, token = [], None
    while len(ids) < n:
        d = api("playlistItems", part="contentDetails", playlistId=pl,
                maxResults=min(50, n - len(ids)), **({"pageToken": token} if token else {}))
        ids += [it["contentDetails"]["videoId"] for it in d.get("items", [])]
        token = d.get("nextPageToken")
        if not token: break
    return ids[:n]

def fetch(ids):
    out = []
    for i in range(0, len(ids), 50):
        out += api("videos", part="snippet,statistics", id=",".join(ids[i:i+50])).get("items", [])
    return out

HASH = re.compile(r"#([^\s#\d][^\s#]*)")

def analyze(name, vids):
    """Return (channel-level tag set, hashtag set) + print channel's own top tags."""
    tagc, hashc, tset, hset = Counter(), Counter(), set(), set()
    for v in vids:
        sn = v["snippet"]
        for t in sn.get("tags", []):
            t = t.strip()
            if t: tagc[t] += 1; tset.add(t)
        for h in HASH.findall(sn.get("description", "")):
            hashc[h] += 1; hset.add(h)
    print(f"\n== {name}  ({len(vids)} vids) ==")
    print("   tags: " + " · ".join(t for t, _ in tagc.most_common(12)))
    if hashc: print("   #:    " + " ".join("#"+h for h, _ in hashc.most_common(8)))
    return tset, hset

def main():
    argv = sys.argv[1:]
    per = 15
    if "--per" in argv: per = int(argv[argv.index("--per")+1])
    dump = argv[argv.index("--dump")+1] if "--dump" in argv else None
    # drop flags AND their consumed values
    toks, skip = [], False
    for i, a in enumerate(argv):
        if skip: skip = False; continue
        if a in FLAG_VALS: skip = True; continue
        if a.startswith("--"): continue
        toks.append(a)
    # expand .txt files
    tokens = []
    for a in toks:
        if a.endswith(".txt") and os.path.exists(a):
            tokens += [l.strip() for l in open(a) if l.strip()]
        else:
            tokens.append(a)

    n_ch = 0
    gtag, ghash = Counter(), Counter()   # CHANNEL frequency
    for a in tokens:
        vid = video_id(a) if ("watch" in a or "youtu.be" in a or "shorts" in a) else None
        if vid:
            vids = fetch([vid])
        else:
            ch = resolve_channel(a)
            if not ch: print(f"!! could not resolve: {a}"); continue
            vids = fetch(recent_ids(ch, per))
        if not vids: continue
        tset, hset = analyze(a, vids); n_ch += 1
        for t in tset: gtag[t] += 1
        for h in hset: ghash[h] += 1

    print(f"\n######## SHARED ACROSS {n_ch} COMPETITORS (channel-frequency) ########")
    print("  TOP TAGS (used by N channels):")
    for t, c in gtag.most_common(40):
        if c >= 2: print(f"    {c:>3}/{n_ch}  {t}")
    print("  TOP #HASHTAGS (used by N channels):")
    for h, c in ghash.most_common(25):
        if c >= 2: print(f"    {c:>3}/{n_ch}  #{h}")

    if dump:
        json.dump({"n_channels": n_ch,
                   "tags": [t for t, c in gtag.most_common(60) if c >= 2],
                   "hashtags": [h for h, c in ghash.most_common(40) if c >= 2]},
                  open(dump, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"\n>> dumped pool -> {dump}")

if __name__ == "__main__":
    main()
