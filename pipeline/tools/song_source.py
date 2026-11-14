#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
歌曲获取层：国内直连，不需要代理
================================
优先级：本地库 -> 网易云VIP(无损FLAC) -> B站

用法：
    from song_source import fetch_song
    path, info = fetch_song("Love Story Taylor Swift", out_dir)
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

COOKIE_FILE = os.environ.get("SING_NETEASE_COOKIES") or str(
    Path(__file__).resolve().parents[1] / "secrets" / "netease_cookies.json")

# 标题黑名单：这些不是我们要的"原唱完整歌曲"
BAD_WORDS = [
    "鼓谱", "架子鼓", "伴奏", "纯音乐", "instrumental", "karaoke", "消音",
    "翻唱", "cover by", "remix", "dj版", "DJ版", "慢速", "教程", "教学",
    "钢琴版", "吉他", "尤克里里", "口琴", "八音盒", "midi", "扒谱",
    "片段", "试听", "预告", "reaction", "解说", "分析", "和声分离",
    "鼓点", "节奏", "bpm", "loop", "采样",
]


def _ne_session():
    s = requests.Session()
    h = {"User-Agent": UA, "Referer": "https://music.163.com/"}
    if os.path.exists(COOKIE_FILE):
        try:
            ck = json.load(open(COOKIE_FILE, encoding="utf-8"))
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in ck.items())
        except Exception:
            pass
    s.headers.update(h)
    return s


# ------------------------------------------------------------------ 网易云
def ne_search(s, query, limit=10):
    r = s.get("https://music.163.com/api/search/get",
              params={"s": query, "type": 1, "limit": limit}, timeout=20)
    songs = (r.json().get("result") or {}).get("songs") or []
    out = []
    for x in songs:
        artists = ",".join(a.get("name", "") for a in x.get("artists", []))
        out.append({
            "id": x["id"],
            "title": x.get("name", ""),
            "artist": artists,
            "duration": round(x.get("duration", 0) / 1000),
            "album": (x.get("album") or {}).get("name", ""),
        })
    return out


def ne_get_url(s, song_id, level="lossless"):
    enc = "flac" if level in ("lossless", "hires") else "mp3"
    r = s.get("https://music.163.com/api/song/enhance/player/url/v1",
              params={"ids": f"[{song_id}]", "level": level, "encodeType": enc},
              timeout=25)
    d = (r.json().get("data") or [{}])[0]
    return d.get("url"), d.get("br", 0), d.get("type"), d.get("size", 0)


def ne_download(s, cand, dst_dir, level="lossless"):
    url, br, typ, size = ne_get_url(s, cand["id"], level)
    if not url:
        return None
    ext = typ or "mp3"
    dst = os.path.join(dst_dir, f"ne_{cand['id']}.{ext}")
    r = s.get(url, timeout=180, stream=True)
    with open(dst, "wb") as f:
        for chunk in r.iter_content(1 << 16):
            f.write(chunk)
    return dst, br


# ------------------------------------------------------------------ B站
def bi_search(query, limit=8):
    """B站搜索页是服务端渲染的，可以直接正则抓 bvid"""
    r = requests.get("https://search.bilibili.com/all",
                     params={"keyword": query},
                     headers={"User-Agent": UA}, timeout=25)
    bvs = list(dict.fromkeys(re.findall(r"BV[0-9A-Za-z]{10}", r.text)))
    return bvs[:limit]


def bi_download(bvid, dst_dir):
    env = dict(os.environ)
    # B站国内直连，明确清掉代理，避免代理挂了反而失败
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"):
        env.pop(k, None)
    out_tpl = os.path.join(dst_dir, f"bi_{bvid}.%(ext)s")
    cmd = [sys.executable, "-m", "yt_dlp", "-f", "bestaudio", "-o", out_tpl,
           "--no-warnings", "--no-playlist",
           f"https://www.bilibili.com/video/{bvid}"]
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=env, timeout=300)
    for f in os.listdir(dst_dir):
        if f.startswith(f"bi_{bvid}."):
            return os.path.join(dst_dir, f)
    return None


# ------------------------------------------------------------------ 选优
def score_candidate(cand, want_title="", want_artist="", want_dur=None):
    """越大越好；返回 None 表示不合格"""
    t = (cand.get("title") or "").lower()
    a = (cand.get("artist") or "").lower()
    blob = t + " " + a
    for w in BAD_WORDS:
        if w.lower() in blob:
            return None
    score = 0.0
    wt = (want_title or "").lower()
    wa = (want_artist or "").lower()
    if wt and wt in t:
        score += 50
    elif wt and any(tok in t for tok in wt.split() if len(tok) > 1):
        score += 20
    if wa and wa in a:
        score += 40
    if want_dur and cand.get("duration"):
        d = abs(cand["duration"] - want_dur)
        if d <= 5:
            score += 30
        elif d <= 15:
            score += 15
        elif d > 60:
            score -= 30
    # 时长合理性
    dur = cand.get("duration") or 0
    if dur:
        if dur < 60 or dur > 600:
            score -= 40
    return score


def fetch_song(query, dst_dir, level="lossless", log=print):
    """
    按优先级尝试获取歌曲。
    返回 (wav_path, info) ; 失败返回 (None, info)
    """
    os.makedirs(dst_dir, exist_ok=True)
    info = {"query": query, "tried": []}

    # 1) 网易云
    try:
        s = _ne_session()
        cands = ne_search(s, query, limit=10)
        log(f"  网易云搜索到 {len(cands)} 个结果")
        scored = []
        for c in cands:
            sc = score_candidate(c, want_title=query.split()[0] if query else "")
            if sc is None:
                continue
            scored.append((sc, c))
        scored.sort(key=lambda x: -x[0])
        for sc, c in scored[:3]:
            log(f"    候选: {c['title']} - {c['artist']} ({c['duration']}s) score={sc:.0f}")
            got = ne_download(s, c, dst_dir, level)
            if got:
                path, br = got
                info.update(source="netease", title=c["title"], artist=c["artist"],
                            song_id=c["id"], bitrate=br, duration=c["duration"])
                log(f"    网易云获取成功: {br/1000:.0f} kbps")
                return path, info
            else:
                log(f"    该曲无可用直链（可能非VIP或无版权）")
        info["tried"].append("netease: no playable candidate")
    except Exception as e:
        info["tried"].append(f"netease: {type(e).__name__} {e}")
        log(f"  网易云失败: {e}")

    # 2) B站
    try:
        bvs = bi_search(query)
        log(f"  B站搜索到 {len(bvs)} 个视频")
        for bv in bvs[:4]:
            path = bi_download(bv, dst_dir)
            if path:
                info.update(source="bilibili", bvid=bv)
                log(f"    B站获取成功: {bv}")
                return path, info
        info["tried"].append("bilibili: no usable video")
    except Exception as e:
        info["tried"].append(f"bilibili: {type(e).__name__} {e}")
        log(f"  B站失败: {e}")

    return None, info


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "Love Story Taylor Swift"
    p, i = fetch_song(q, os.environ.get("SING_FETCH_DIR") or "_fetch_test")
    print(json.dumps(i, ensure_ascii=False, indent=2))
    print("PATH:", p)
