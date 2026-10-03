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
    sn = norm(s)
    if not sn:
        return set()
    if len(re.findall(r"[A-Za-z]", sn)) / float(max(1, len(sn))) >= 0.5:
        # ⚠️ 必须保留空白分词：norm() 会删空白，英文会塌成一个"词"
        #    （实测《Moon River》→ lg size=1 → locate 直接否决）
        z = re.sub(r"[^A-Za-z0-9'\s]", " ", str(s or "")).lower()
        w = re.findall(r"[a-z0-9']+", z)
        if len(w) >= n:
            return set(tuple(w[i:i + n]) for i in range(len(w) - n + 1))
        return {sn[i:i + n] for i in range(max(0, len(sn) - n + 1))}
    if len(sn) < n:
        return set([sn])
    return set(sn[i:i + n] for i in range(len(sn) - n + 1))


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


def window_index(entries, win=120.0, step=30.0):
    """把整场转写切成重叠窗口，预计算每个窗口的 gram 集与拉丁词集。

    全库扫描（1200+ 首）时用它避免重复计算同一批窗口。
    """
    if not entries:
        return []
    last = max(b for _a, b, _t in entries)
    out = []
    t = 0.0
    while t < last:
        t2 = t + win
        g, w = set(), set()
        for a, _b, x in entries:
            if a >= t and a < t2:
                g |= grams(x)
                w |= set(latin_words(x))
        if g:
            out.append((t, g, w))
        t += step
    return out


def fast_screen(idx, lrc_text):
    """廉价召回：返回 (best_recall, best_t0, mode)。只做粗筛，不用它直接下结论。"""
    lines = lrc_lines(lrc_text)
    if not lines or not idx:
        return (0.0, None, None)
    body = "\n".join(t for _t, t in lines)
    best = (0.0, None)
    if latin_ratio(body) >= LATIN_BODY_TH:
        lw = set()
        for _t, tx in lines:
            lw |= set(latin_words(tx))
        if len(lw) < LATIN_MIN_WORDS:
            return (0.0, None, None)
        for t, _g, w in idx:
            r = len(lw & w) / float(len(lw))
            if r > best[0]:
                best = (r, t)
        return (best[0], best[1], "latin")
    lg = grams(body)
    if len(lg) < 20:
        return (0.0, None, None)
    for t, g, _w in idx:
        r = len(lg & g) / float(len(lg))
        if r > best[0]:
            best = (r, t)
    return (best[0], best[1], "cjk")


def lyric_locate_norm(s):
    return norm(s)


# ---------------- 英文（拉丁）歌词专用定位 ----------------
# 拉丁比值 ≥0.5 视为英文歌词（中文/日文歌词 Beijing/mantra 不会到这个比例）
LATIN_BODY_TH = 0.5
LATIN_MIN_WORDS = 15      # 歌词词集不足这么多个词，召回率统计不可靠，退回通用路径
LATIN_RECALL_OK = 0.45    # 峰值词召回率阈值（真歌 .57~.76 / 噪声 ≤.33）
LATIN_LINE_REC = 0.60     # 行级词召回阈值
LATIN_WIN = 120.0
LATIN_STEP = 30.0


def latin_words(s):
    """抽出拉丁词序列（保留空白分词 —— 详见下方 locate 的坑）。"""
    from .lyrics_fetch import _latin_words
    return _latin_words(s)


def latin_ratio(s):
    sn = norm(s)
    return len(re.findall(r"[A-Za-z]", sn)) / float(max(1, len(sn)))


def _latin_locate(entries, lines):
    """英文歌词定位：词召回率滑窗。

    FunASR（中文模型）转英文歌词时字母序列常常错，但词的表层形式往往部分对上，
    因此用「整首歌词的词汇有多少比例出现在窗口里」远比 n-gram 结构匹配稳固。
    """
    lw_all = set()
    for _t, tx in lines:
        lw_all |= set(latin_words(tx))
    if len(lw_all) < LATIN_MIN_WORDS:
        return None
    ew = [set(latin_words(t)) for _a, _b, t in entries]
    last = max(b for _a, b, _t in entries)
    best = None
    t = 0.0
    while t < last:
        t2 = t + LATIN_WIN
        cw, idx = set(), []
        for i, (a, _b, _x) in enumerate(entries):
            if a >= t and a < t2:
                cw |= ew[i]
                idx.append(i)
        if cw:
            r = len(lw_all & cw) / float(len(lw_all))
            if best is None or r > best[0]:
                best = (r, idx)
        t += LATIN_STEP
    if not best or best[0] < LATIN_RECALL_OK:
        return None
    if not best[1]:
        return None
    # 窗口只有固定 120s，长歌会只框到中间一段 → 向两侧扩张
    # （门槛：相邻 cue 至少有 2 个词属于歌词词集，且允许最多 60s 空档躲过换气/间奏）
    lo, hi = best[1][0], best[1][-1]

    def _hit(i):
        return len(ew[i] & lw_all)

    i = lo
    while i > 0 and _hit(i - 1) >= 2 and (entries[i][0] - entries[i - 1][1]) <= 60.0:
        i -= 1
    lo = i
    i = hi
    while (i < len(entries) - 1 and _hit(i + 1) >= 2
           and (entries[i + 1][0] - entries[i][1]) <= 60.0):
        i += 1
    hi = i
    cw_all = set()
    for i in range(lo, hi + 1):
        cw_all |= ew[i]
    matched = 0
    for _t, tx in lines:
        lw = set(latin_words(tx))
        if len(lw) < 2:
            continue
        if len(lw & cw_all) / float(len(lw)) >= LATIN_LINE_REC:
            matched += 1
    need = max(3, int(0.2 * len(lines)))
    if matched < need:
        return None
    return {"start": round(float(entries[lo][0]), 2),
            "end": round(float(entries[hi][1]), 2),
            "raw_start": round(float(entries[lo][0]), 2),
            "raw_end": round(float(entries[hi][1]), 2),
            "hit_cues": hi - lo + 1,
            "span_s": round(float(entries[hi][1] - entries[lo][0]), 2),
            "mean_prec": round(best[0], 3),
            "latin_recall": round(best[0], 3),
            "matched_lines": matched, "lyric_lines": len(lines),
            "line_hit_ratio": round(matched / float(max(1, len(lines))), 3),
            "intro_s": round(lines[0][0], 2) if lines else 0.0}


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

    # 英文歌词走专用路径（n-gram 对 ASR 错字无能为力，详见 _latin_locate 文档）
    if latin_ratio(body) >= LATIN_BODY_TH:
        return _latin_locate(entries, lines)

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
