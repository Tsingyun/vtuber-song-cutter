#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""lyric_locate.py —— 「歌词文本 → 联网搜歌名 → 在转写里定位演唱窗口」

全自动歌切的核心判据：不依赖任何人工歌单，全程由内容本身决定唱没唱、唱的什么、从哪到哪。

三条能力：
  1. search_by_lyric(phrase)  网易云歌词检索（cloudsearch type=1006）：输入一句唱词，返回歌名候选；
  2. locate(entries, lrc)     把某首歌的歌词放回整场转写里滑动比对，定位实际演唱区间；
  3. overlap(a, b)            字符 2-gram 重合率（判定「这段转写是不是在唱这首歌」）。

设计取舍：
  - 歌名由**联网歌词检索**给出，不由 LLM 凭印象猜 —— LLM 猜歌名有实测翻车史
    （把闲聊「小岁来了」当歌名、把歌词首句当歌名）；
  - 区间由**歌词在转写中的实际出现位置**给出，不由歌名被提到的时刻给出
    （实测：歌名可能在开唱前 4 分钟就被提到，也可能唱完才提）。
"""
import re

from .lyrics_fetch import UA, TIMEOUT

NETEASE_CLOUDSEARCH = "https://music.163.com/api/cloudsearch/pc"
BAD_NAME_RE = re.compile(r"伴奏|伴奏版|纯音乐|DEMO|demo|翻唱|cover|remix|Remix|"
                         r"串烧|剪辑|铃声|清唱版| instrumental|Inst\.", re.I)
PUNCT_RE = re.compile(r"[\s，。！？、；：,.!?;:~～\"'()（）\[\]【】《》…—\-_|/\\]+")


def norm(s):
    return PUNCT_RE.sub("", s or "")


def grams(s, n=2):
    """拉丁文本用词级 n-gram，中文/日文用字符 n-gram（同 lyrics_fetch._grams_of）。"""
    s = norm(s)
    if not s:
        return set()
    if len(re.findall(r"[A-Za-z]", s)) / float(max(1, len(s))) >= 0.5:
        w = re.findall(r"[a-z0-9']+", s.lower())
        if len(w) >= n:
            return set(tuple(w[i:i + n]) for i in range(len(w) - n + 1))
        return set(w)
    if len(s) < n:
        return set([s])
    return set(s[i:i + n] for i in range(len(s) - n + 1))


def overlap(a, b):
    """字符 2-gram 重合率（较短文本为分母），0~1。"""
    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / float(min(len(ga), len(gb)))


# ---------------- 歌词联网检索：唱词 → 歌名 ----------------
def search_by_lyric(phrase, limit=8, timeout=None):
    """网易云歌词检索（type=1006）。返回 [{"name","artist","id"}]，失败返回 []。"""
    import requests
    phrase = (phrase or "").strip()
    if len(phrase) < 4:
        return []
    try:
        r = requests.post(NETEASE_CLOUDSEARCH,
                          data={"s": phrase, "type": 1006, "limit": limit},
                          headers={"User-Agent": UA, "Referer": "https://music.163.com"},
                          timeout=timeout or TIMEOUT)
        songs = ((r.json() or {}).get("result") or {}).get("songs") or []
    except Exception:
        return []
    out = []
    for s in songs:
        nm = (s.get("name") or "").strip()
        if not nm:
            continue
        ar = ", ".join((a.get("name") or "") for a in (s.get("ar") or []) if a.get("name"))
        out.append({"name": nm, "artist": ar, "id": s.get("id"),
                    "bad": bool(BAD_NAME_RE.search(nm) or BAD_NAME_RE.search(ar))})
    return out


def identify_by_lines(lines, try_lines=3, limit=8):
    """对候选唱词逐行检索，合并出最可能的歌名（按命中次数排序，过滤伴奏/DEMO 变体）。"""
    picked = []
    for ln in (lines or []):
        ln = (ln or "").strip()
        if len(norm(ln)) < 6:              # 太短的行检索噪声大
            continue
        picked.append(ln)
        if len(picked) >= try_lines:
            break
    score = {}
    for ln in picked:
        for c in search_by_lyric(ln, limit=limit):
            if c["bad"]:
                continue
            key = c["name"]
            d = score.setdefault(key, {"name": key, "artist": c["artist"], "id": c["id"],
                                       "hits": 0})
            d["hits"] += 1
    return sorted(score.values(), key=lambda x: -x["hits"])


# ---------------- 歌词 → 转写定位演唱区间 ----------------
def lrc_lines(lrc_text):
    """解析 LRC → [(秒, 文本)]，剥离行内 <mm:ss.xx> 逐字标签与时间标签。"""
    out = []
    for ln in (lrc_text or "").splitlines():
        m = re.match(r"\s*\[(\d+):(\d+(?:\.\d+)?)\](.*)", ln)
        if not m:
            continue
        t = int(m.group(1)) * 60 + float(m.group(2))
        txt = re.sub(r"<\d+:\d+(?:\.\d+)?>", "", m.group(3)).strip()
        txt = re.sub(r"\[\d+:\d+(?:\.\d+)?\]", "", txt).strip()
        if txt:
            out.append((t, txt))
    out.sort(key=lambda x: x[0])
    return out


def song_duration(title, artist="", timeout=None):
    """网易云单曲检索取官方时长（秒）。失败返回 None。"""
    import requests
    try:
        r = requests.post(NETEASE_CLOUDSEARCH,
                          data={"s": title, "type": 1, "limit": 8},
                          headers={"User-Agent": UA, "Referer": "https://music.163.com"},
                          timeout=timeout or TIMEOUT)
        songs = ((r.json() or {}).get("result") or {}).get("songs") or []
    except Exception:
        return None
    norm_title = norm(title)
    for s in songs:
        nm = (s.get("name") or "").strip()
        if not nm or BAD_NAME_RE.search(nm):          # 伴奏/DEMO/remix 变体不算原曲
            continue
        if norm_title not in norm(nm):
            continue
        ar = ", ".join((a.get("name") or "") for a in (s.get("ar") or []) if a.get("name"))
        if artist and artist not in ar:
            continue
        d = s.get("dt") or s.get("duration")           # cloudsearch 单曲时长字段是 dt
        if d:
            return round(d / 1000.0, 1)
    return None


def lyric_locate_norm(s):
    return norm(s)


def locate(entries, lrc_text, hit_thr=0.45, gap=15.0, min_run=40.0, line_jac=0.7):
    """在整场转写里定位「真正在唱这首歌」的那一段。

    判据（逐句，而非整窗 —— 整窗会被长段闲聊里的常用字偶然命中）：
      1. 每句转写与整首歌词的 2-gram 命中率 ≥ hit_thr 视为「唱到的一句」；
      2. 把命中句按 ≤gap 秒的空隙串成最长的一段（容忍换气/伴奏间奏）；
      3. 该段内被实际唱到的歌词行数 ≥ 一定比例，才算真的在唱这首歌。
    """
    if not entries or not lrc_text:
        return None
    lines = lrc_lines(lrc_text)
    body = "\n".join(t for _t, t in lines)
    lg = grams(body)
    if len(lg) < 20:
        return None

    eg, prec = [], []
    for _a, _b, t in entries:
        g = grams(t)
        eg.append(g)
        prec.append(len(g & lg) / float(len(g)) if len(g) >= 3 else None)
    hits = [i for i, p in enumerate(prec) if p is not None and p >= hit_thr]
    if not hits:
        return None

    runs, cur = [], [hits[0]]
    for i in hits[1:]:
        if entries[i][0] - entries[cur[-1]][1] <= gap:
            cur.append(i)
        else:
            runs.append(cur)
            cur = [i]
    runs.append(cur)
    best = max(runs, key=lambda r: entries[r[-1]][1] - entries[r[0]][0])
    span = entries[best[-1]][1] - entries[best[0]][0]
    if span < min_run:
        return None

    # 该段实际唱到的歌词行：要求整句歌词被某一句（或相邻两句）转写覆盖 ≥ line_rec。
    # 用「覆盖率」而不是 Jaccard：ASR 常把多句歌词并成一句，Jaccard 会被长句稀释；
    # 但只在单句/相邻两句上比对，不会像「比对整窗文本」那样被长段闲聊里的常用字偶然命中。
    win_cues = [eg[i] for i in range(best[0], best[-1] + 1)]
    matched = 0
    for _t, tx in lines:
        gl = grams(tx)
        if len(gl) < 2:
            continue
        bj = 0.0
        for i, cg in enumerate(win_cues):
            u = cg
            if i + 1 < len(win_cues):
                u = cg | win_cues[i + 1]
            if not u:
                continue
            r = len(gl & u) / float(len(gl))
            if r > bj:
                bj = r
        if bj >= line_jac:
            matched += 1
    mp = sum(prec[i] for i in best) / float(len(best))
    return {"start": round(float(entries[best[0]][0]), 2),
            "end": round(float(entries[best[-1]][1]), 2),
            "raw_start": round(float(entries[best[0]][0]), 2),
            "raw_end": round(float(entries[best[-1]][1]), 2),
            "hit_cues": len(best), "span_s": round(span, 2),
            "mean_prec": round(mp, 3),
            "matched_lines": matched, "lyric_lines": len(lines),
            "line_hit_ratio": round(matched / float(max(1, len(lines))), 3),
            "intro_s": round(lines[0][0], 2) if lines else 0.0}
