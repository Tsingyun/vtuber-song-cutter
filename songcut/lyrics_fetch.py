# -*- coding: utf-8 -*-
"""歌词与封面自动匹配（歌切渲染用）。
歌词来源优先级：LRCLIB（开源、无鉴权、带同步 LRC）→ 网易云 → 无。
封面来源：网易云搜索结果的专辑图 → 无（播放器自动用占位封面）。
所有函数失败都返回 None / 降级，不抛异常阻断主流程。
"""
import io, json, difflib, os, re, time
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TIMEOUT = (10, 30)


def _norm(t):
    return re.sub(r"[\s\u3000·・～~\-—_（）()【】\[\]!！?？]", "", str(t)).lower()


def _pick(results, title, artist_hint=""):
    """在搜索结果里挑与歌名最匹配的条目。results: [{name, artist, ...}]"""
    tn = _norm(title)
    best, score = None, 0.0
    for r in results:
        name = r.get("name") or ""
        n = _norm(name)
        s = difflib.SequenceMatcher(None, tn, n).ratio()
        if tn and (tn in n or n in tn):
            s = max(s, 0.92)
        if artist_hint:
            an = _norm(r.get("artist") or "")
            if artist_hint and difflib.SequenceMatcher(None, _norm(artist_hint), an).ratio() > 0.5:
                s = min(1.0, s + 0.05)
        if s > score:
            best, score = r, s
    return (best, score) if best and score >= 0.55 else (None, score)


def _lrclib(title, artist_hint=""):
    try:
        p = {"track_name": title}
        if artist_hint:
            p["artist_name"] = artist_hint
        r = requests.get("https://lrclib.net/api/search", params=p,
                         headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code != 200:
            r = requests.get("https://lrclib.net/api/search", params={"q": title},
                             headers={"User-Agent": UA}, timeout=TIMEOUT)
        if r.status_code != 200:
            return None, None, ""
        items = r.json() or []
        best = None
        for it in items:
            if it.get("syncedLyrics"):
                if artist_hint and artist_hint not in (it.get("artistName") or "") and best:
                    continue
                best = best or it
        if best is None:
            for it in items:
                if it.get("syncedLyrics"):
                    best = it
                    break
        if best:
            return (best["syncedLyrics"], "lrclib:%s" % (best.get("artistName") or ""),
                    (best.get("artistName") or "").strip())
    except Exception:
        pass
    return None, None, ""


def _netease(title, artist_hint=""):
    """返回 (lrc, cover_url, source, artist)。搜索用 cloudsearch（旧 api/search/pc 已返回空）。"""
    lrc = cover = src = artist = None
    try:
        r = requests.post("https://music.163.com/api/cloudsearch/pc",
                          data={"s": title, "type": 1, "limit": 8},
                          headers={"User-Agent": UA, "Referer": "https://music.163.com"},
                          timeout=TIMEOUT)
        songs = (r.json().get("result") or {}).get("songs") or []
        cand = [{"name": s.get("name"), "artist": (s.get("ar") or [{}])[0].get("name"),
                 "id": s.get("id"), "cover": (s.get("al") or {}).get("picUrl")} for s in songs]
        if not songs:      # 老接口兜底
            r = requests.post("http://music.163.com/api/search/pc",
                              data={"s": title, "type": 1, "limit": 8, "offset": 0},
                              headers={"User-Agent": UA, "Referer": "https://music.163.com"},
                              timeout=TIMEOUT)
            songs = (r.json().get("result") or {}).get("songs") or []
            cand = [{"name": s.get("name"), "artist": (s.get("artists") or [{}])[0].get("name"),
                     "id": s.get("id"), "cover": (s.get("album") or {}).get("picUrl")} for s in songs]
        hit, score = _pick(cand, title, artist_hint)
        if hit:
            if hit.get("cover"):
                cover = hit["cover"] + "?param=1000y1000"
            rr = requests.get("http://music.163.com/api/song/lyric",
                              params={"id": hit["id"], "lv": 1, "kv": 1, "tv": -1},
                              headers={"User-Agent": UA, "Referer": "https://music.163.com"},
                              timeout=TIMEOUT)
            lrc = ((rr.json().get("lrc") or {}).get("lyric")) or None
            if lrc:
                src = "netease:%s(%s)" % (hit["name"], hit.get("artist") or "")
            artist = (hit.get("artist") or "").strip() or None
    except Exception:
        pass
    return lrc, cover, src, artist


def _t2s(text):
    """繁体 → 简体（LRCLIB 上不少歌词是港台来源的繁体版本）。"""
    try:
        from opencc import OpenCC
        return OpenCC("t2s").convert(text)
    except Exception:
        return text


def _artist_from_source(src):
    """从 lyrics_source 兼容解析原唱歌手（旧缓存无 artist 字段时用）。
    格式：netease:歌名(歌手) / lrclib:歌手"""
    if not src:
        return ""
    if src.startswith("lrclib:"):
        return src.split(":", 1)[1].strip()
    m = re.search(r"\(([^()]*)\)\s*$", src)
    return m.group(1).strip() if m else ""


def clean_lrc(lrc, dur=None):
    """清洗 LRC：去翻译重复段/无效行；超出歌曲时长的行剪掉；去掉 krc 词行。"""
    out = []
    for line in (lrc or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"^\[(ti|ar|al|by|offset|kana):", line, re.I):
            out.append(line)
            continue
        m = re.match(r"^((?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+)(.*)$", line)
        if not m:
            continue
        tags, txt = m.group(1), m.group(2).strip()
        if not txt:                       # 纯时间戳空行 → 保留一个作段落分隔
            out.append("")
            continue
        # 时长校验：第一标签超时长则丢弃该行
        if dur:
            mm = re.match(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]", tags)
            if mm:
                t = int(mm.group(1)) * 60 + int(mm.group(2)) + (float("0." + mm.group(3)) if mm.group(3) else 0)
                if t > dur + 0.5:
                    continue
        out.append(tags + _t2s(txt))
    # 合并连续空行
    merged, blank = [], False
    for l in out:
        if l == "":
            if not blank:
                merged.append("")
            blank = True
        else:
            merged.append(l)
            blank = False
    return "\n".join(merged).strip()


def fetch_lyrics_and_cover(title, artist_hint="", dur=None, cache_dir=None):
    """返回 (lrc:str|None, cover_path:str|None, info:dict)。cover 已下载到 cache_dir。"""
    info = {}
    cache_dir = cache_dir or os.getcwd()
    os.makedirs(cache_dir, exist_ok=True)
    key = re.sub(r"[^\w]", "_", _norm(title))[:40]

    cache_f = os.path.join(cache_dir, "_lrc_%s.json" % key)
    if os.path.exists(cache_f):
        try:
            d = json.load(io.open(cache_f, encoding="utf-8"))
            if d.get("lrc") or d.get("cover_path"):
                if d.get("cover_path") and not os.path.exists(d["cover_path"]):
                    d["cover_path"] = None
                info = d.get("info", {})
                if not info.get("artist"):
                    info["artist"] = _artist_from_source(info.get("lyrics_source"))
                return d.get("lrc"), d.get("cover_path"), info
        except Exception:
            pass

    lrc, lsrc, lartist = _lrclib(title, artist_hint)
    cover_url = None
    artist = ""
    if not lrc:
        lrc, cover_url, lsrc, artist = _netease(title, artist_hint)
    else:
        info["netease_skipped"] = True
        # 封面仍从网易云拿（原唱歌手名也优先用网易云的规范写法）
        _, cover_url, _nsrc, nartist = _netease(title, artist_hint)
        artist = nartist or (lartist or "")
    if artist:
        info["artist"] = artist

    if lrc:
        lrc = clean_lrc(lrc, dur)
        if not re.search(r"\[\d{1,3}:\d{2}", lrc):     # 清洗后没有有效时间轴 → 视为失败
            lrc = None
    if lrc:
        info["lyrics_source"] = lsrc
    else:
        info["lyrics_source"] = "none"

    cover_path = None
    if cover_url:
        try:
            r = requests.get(cover_url, headers={"User-Agent": UA}, timeout=TIMEOUT)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                cover_path = os.path.join(cache_dir, "_cover_%s.img" % key)
                io.open(cover_path, "wb").write(r.content)
                info["cover_source"] = cover_url.split("?")[0]
        except Exception:
            pass
    info["cover_source"] = info.get("cover_source") or "none"

    try:
        json.dump({"lrc": lrc, "cover_path": cover_path, "info": info},
                  io.open(cache_f, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    return lrc, cover_path, info


if __name__ == "__main__":
    import sys
    for t in sys.argv[1:]:
        lrc, cov, info = fetch_lyrics_and_cover(t)
        print("=== %s" % t)
        print("  歌词:", info.get("lyrics_source"), "| 行数:", len((lrc or "").splitlines()))
        print("  封面:", info.get("cover_source"))
