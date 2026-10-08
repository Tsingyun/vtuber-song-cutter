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

# 歌词完整性：末行时间戳 / 歌曲时长 低于此值时，判定「疑似残缺」并触发重新拉取。
# 流行歌的正常 outro 一般在 0.85~0.99；低于 0.80 基本可以断定尾部歌词掉了。
LRC_SUSPECT_COVER = 0.80
# 歌词正文 vs 演唱转写的最低字面重合率（低于即判定「疑似不是同一首歌」）
LRC_TEXT_OV_OK = 0.18


def _norm(t):
    return re.sub(r"[\s\u3000·・～~\-—_（）()【】\[\]!！?？]", "", str(t)).lower()


# 原唱一致性的最小相似度：低于此值视为「不是原唱的那一版」。
# 标定依据：张惠妹 vs en（王翊恩）= 0.0（完全不同的名字）；
# aMEI vs 张惠妹 ≈ 0.5（英文/中文写法差异，需靠 ARTIST_ALIASES 兜），
# 初音ミク vs 初音未来 ≈ 0.5（同上）。取 0.62 既容忍写法差异又能挡住翻唱者。
ARTIST_MATCH_MIN = 0.62
# 歌手名的等价写法（英文名/中文名/昵称），命中任一即视为同一人。
ARTIST_ALIASES = {
    "张惠妹": ("amei", "张惠妹amei"),
    "初音未来": ("初音ミク", "初音miku", "hatsune miku"),
    "初音ミク": ("初音未来", "初音miku", "hatsune miku"),
    "周杰伦": ("jay chou", "jaychou", "周杰倫"),
    "蔡健雅": ("tanya chua", "tanyachua"),
    "田馥甄": ("hebe tien", "hebe"),
    "郭静": ("jessie kuo", "郭靜"),
}


def artist_matches(a, b):
    """两个歌手名是否指同一人。容忍中英文写法/昵称差异；缺一侧信息则不拦。"""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return True
    if na == nb or na in nb or nb in na:
        return True
    for key, al in ARTIST_ALIASES.items():
        ka = _norm(key)
        ka_hit = (ka in na) or any(_norm(x) in na for x in al)
        kb_hit = (ka in nb) or any(_norm(x) in nb for x in al)
        if ka_hit and kb_hit:
            return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= ARTIST_MATCH_MIN


def _pick(results, title, artist_hint="", reject_artist_mismatch=False):
    """在搜索结果里挑与歌名最匹配的条目。results: [{name, artist, ...}]

    reject_artist_mismatch=True 时，**原唱对不上的候选直接淘汰**而不是靠加权竞争。
    ⚠ 必须开这个开关的原因（2026-10-06《连名带姓》）：同名不同版时歌名分完全相同，
    翻唱版歌手只是「+0.05 加分」，等于没有约束 → 谁的搜索排序靠前谁赢，
    于是封面/歌手名/时长dt 一起被翻唱版污染（详见 ARTIST_MATCH_MIN 注释）。
    仅对「元数据可信度要求高」的字段（封面、官方时长）启用该模式。
    """
    tn = _norm(title)
    best, score = None, 0.0
    for r in results:
        if reject_artist_mismatch and artist_hint:
            if not artist_matches(artist_hint, r.get("artist")):
                continue# 原唱不符 → 否决（不进候选池）
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


def lrclib_candidates(title, artist_hint=""):
    """返回 LRCLIB 上所有带时间轴的候选 [{lrc, source, artist}]，按匹配度降序。
    供上层做「多源择优」——旧的「取第一个有歌词的」会把翻唱/重制版当成原版。"""
    out = []
    for req in ({"track_name": title, "artist_name": artist_hint} if artist_hint else {"track_name": title},
                {"q": title}):
        try:
            r = requests.get("https://lrclib.net/api/search", params=req,
                             headers={"User-Agent": UA}, timeout=TIMEOUT)
            if r.status_code == 200:
                out = list(r.json() or [])
                if out:                      # 空结果不能 break：否则带歌手精确搜无命中时
                    break                    # 整个源直接失效，只能拿到同名他人的歌词
        except Exception:
            continue
    res = []
    for it in (out or []):
        if not it.get("syncedLyrics"):
            continue
        nm = it.get("trackName") or ""
        an = (it.get("artistName") or "").strip()
        s = difflib.SequenceMatcher(None, _norm(title), _norm(nm)).ratio()
        if _norm(title) and _norm(title) in _norm(nm):
            s = max(s, 0.92)
        if artist_hint:
            s += 0.15 if difflib.SequenceMatcher(
                None, _norm(artist_hint), _norm(an)).ratio() > 0.5 else 0.0
        else:
            s -= 0.05          # 无歌手提示时轻微惩罚，让网易云的权威元数据占优
        res.append({"lrc": it["syncedLyrics"], "source": "lrclib:%s" % an,
                    "artist": an, "score": s, "duration": it.get("duration")})
    res.sort(key=lambda x: -x["score"])
    return res


def _lrclib(title, artist_hint=""):
    c = lrclib_candidates(title, artist_hint)
    if not c:
        return None, None, ""
    b = c[0]
    return b["lrc"], b["source"], b["artist"]


def _netease(title, artist_hint=""):
    """返回 (lrc, cover_url, source, artist)。搜索用 cloudsearch（旧 api/search/pc 已返回空）。"""
    lrc = cover = src = artist = None
    dur_ms = None
    relaxed = False          # True= 未命中原唱版，已退回宽松模式（元数据存疑）
    try:
        r = requests.post("https://music.163.com/api/cloudsearch/pc",
                          data={"s": title, "type": 1, "limit": 8},
                          headers={"User-Agent": UA, "Referer": "https://music.163.com"},
                          timeout=TIMEOUT)
        songs = (r.json().get("result") or {}).get("songs") or []
        cand = [{"name": s.get("name"), "artist": (s.get("ar") or [{}])[0].get("name"),
                 "id": s.get("id"), "cover": (s.get("al") or {}).get("picUrl"),
                 "dur": s.get("dt")} for s in songs]
        if not songs:      # 老接口兜底
            r = requests.post("http://music.163.com/api/search/pc",
                              data={"s": title, "type": 1, "limit": 8, "offset": 0},
                              headers={"User-Agent": UA, "Referer": "https://music.163.com"},
                              timeout=TIMEOUT)
            songs = (r.json().get("result") or {}).get("songs") or []
            cand = [{"name": s.get("name"), "artist": (s.get("artists") or [{}])[0].get("name"),
                     "id": s.get("id"), "cover": (s.get("album") or {}).get("picUrl"),
                     "dur": s.get("dt")} for s in songs]
        # 原唱否决模式：封面/官方时长必须来自原唱那一版，否则会污染画面。
        hit, score = _pick(cand, title, artist_hint, reject_artist_mismatch=True)
        if not hit and artist_hint:
            # 网易云候选里没有原唱版（歌手字段缺失/写法差异过大）→ 退回宽松模式，
            # 但明确标记「元数据可能来自翻唱版」，由上层决定是否出片。
            hit, score = _pick(cand, title, artist_hint)
            if hit:
                relaxed = True
        if hit:
            dur_ms = hit.get("dur")
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
    return lrc, cover, src, artist, dur_ms, relaxed


def _t2s(text):
    """繁体 → 简体。歌词库上不少条目是港台来源的繁体版本，必须统一转简，
    否则同一首歌会出现「進化成更好的人」这类繁体字形。
    依赖见 requirements.txt：opencc-python-reimplemented（首选）或 zhconv。
    两者都缺失时保留原文并向 _t2s_warned 登记一次，由上层提示安装。"""
    global _T2S_OK
    if not text:
        return text
    if _T2S_OK is None:
        _T2S_OK = False
        try:
            from opencc import OpenCC
            _t2s._cc = OpenCC("t2s")
            _T2S_OK = True
        except Exception:
            try:
                import zhconv
                _t2s._zh = zhconv.convert
                _T2S_OK = True
            except Exception:
                _t2s._cc = _t2s._zh = None
    if not _T2S_OK:
        return text
    try:
        if getattr(_t2s, "_cc", None):
            return _t2s._cc.convert(text)
        return _t2s._zh(text, "zh-cn")
    except Exception:
        return text


_T2S_OK = None      # None=未探测 True=已就绪 False=缺依赖


def _artist_from_source(src):
    """从 lyrics_source 兼容解析原唱歌手（旧缓存无 artist 字段时用）。
    格式：netease:歌名(歌手) / lrclib:歌手"""
    if not src:
        return ""
    if src.startswith("lrclib:"):
        return src.split(":", 1)[1].strip()
    m = re.search(r"\(([^()]*)\)\s*$", src)
    return m.group(1).strip() if m else ""


# 创作/制作署名行（网易云 LRC 首部常见）。这些不是歌词，若混进时间轴会被当成歌词渲染。
META_RE = re.compile(
    r"^\s*(作词|作曲|编曲|制作人|监制|混音|母带|和声|和音|吉他|贝斯|鼓|钢琴|弦乐|录音|"
    r"后期|统筹|策划|出品|发行|翻唱|原唱|演唱|词|曲|歌名|专辑|歌手|曲名|歌词|"
    r"歌曲营销|联合营销|总顾问|营销|文案|视觉|封面|设计|企划|OP|SP|ISRC)"
    r"\s*[:：]|^\s*(?:\[\d{1,3}:\d{2}[.:]?\d{0,3}\])*\s*(?:作词|作曲|编曲|制作人|歌曲营销|联合营销|总顾问)\s*[:：]|"
    r"^\s*(?:OP|SP|ISRC)")

# 段落标记 / 无效占位
JUNK_RE = re.compile(r"^\[(?:00:00\.00)\]\s*$|^\s*(?:~+|End|music|Music|--+|…)\s*$")


def lrc_tail_sec(lrc):
    """最后一个「有文本」歌词行的秒数；无则返回 0.0。
    用于歌词完整性评估：末行越接近歌曲结束，说明 outro / 重复副歌没被漏掉。"""
    last = 0.0
    for line in (lrc or "").splitlines():
        m = re.match(r"^\s*\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]\s*(\S.*)?$", line)
        if not m or not (m.group(4) or "").strip():
            continue
        t = int(m.group(1)) * 60 + int(m.group(2))
        if m.group(3):
            t += float("0." + m.group(3))
        last = max(last, t)
    return last


def clean_lrc(lrc, dur=None):
    """清洗 LRC：去元数据署名行/无效行/翻译重复段；超出歌曲时长的行剪掉。"""
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
        if META_RE.search(txt) or JUNK_RE.match(txt):      # 署名/占位，非歌词
            continue
        # 时长校验：远超片段时长才丢弃。
        # ⚠ 阈值必须宽松（dur*1.2+5）：歌切片段的结束点由波形/DTW 决定，常略早于
        #   原曲 outro；若用 dur 硬剪，尾部歌词会被连带裁掉 ——「最后一段歌词不完整」
        #   的第二个成因。宁可多留一行，不可漏一段。
        if dur:
            mm = re.match(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]", tags)
            if mm:
                t = int(mm.group(1)) * 60 + int(mm.group(2)) + (float("0." + mm.group(3)) if mm.group(3) else 0)
                if t > dur * 1.2 + 5:
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


def lyric_body_text(lrc):
    """抽出歌词正文（去时间轴标签与常见标点），用于和演唱转写做字面比对。"""
    t = re.sub(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]", "", lrc or "")
    return re.sub(r"[\s\u3000\u00b7\u30fb\uff5e~\-—_（）()【】\[\]!！?？,，.。'\u201c\u201d\u2018\u2019]", "", t)


def _latin_ratio(x):
    x = x or ""
    return len(re.findall(r"[A-Za-z]", x)) / float(max(1, len(x)))


def _latin_words(x):
    """从文本里抽出拉丁词序列。

    ⚠️ 绝不能先过 _norm —— 它连空白一起删，英文歌词会被压成一个巨型"词"
    （实测《Moon River》: "Moon river wider than a mile" → "moonriverwiderthanamile"，
    词级 2-gram 只剩 1 个元素，locate 的 len(lg)<20 判据会把所有英文歌一票否决）。
    """
    # 标点/符号一律替换为空格，空白保留，用于分词
    z = re.sub(r"[^A-Za-z0-9'\s]", " ", str(x or "")).lower()
    return re.findall(r"[a-z0-9']+", z)


def _grams_of(x, n=2):
    """拉丁文本 → 词级 n-gram；中文/日文 → 字符 n-gram。

    英文若用字符 2-gram，任意两段文字都会因 th/he/in/er 等常见字母对而高度重合
    （实测：《Lover》歌词 vs《奇异博士》英文对白 重合率 0.83，纯属噪声）。
    """
    xn = _norm(x or "")
    if _latin_ratio(xn) >= 0.5:
        w = _latin_words(x)
        if len(w) >= n:
            return set(tuple(w[i:i + n]) for i in range(len(w) - n + 1))
        # 词太少（短句/单词）→ 退化到字符 n-gram，避免只剩 0~1 个 gram
        return {xn[i:i + n] for i in range(max(0, len(xn) - n + 1))}
    return {xn[i:i + n] for i in range(max(0, len(xn) - n + 1))}


def text_overlap_ratio(lrc, ref_text, n=2):
    """歌词正文与演唱转写的字符 n-gram 重合率。

    用途：拦截「同名不同歌」的错版本。典型案例（2026-10-01《泡泡》）——
    LRCLIB 上《泡泡》排在首位的是娃娃 Waa Wei 的版本（"吹我们吹呀吹"），
    而实际演唱的是牛佳钰《泡泡》（"是不是嘛 对不对嘛"）。两版标题完全同名、
    时长也接近（3:38 / 3:40），按「覆盖率×行数」择优仍可能选错，
    但正文字面重合率近 0，一测即出。

    返回 0~1：同版本通常 >0.20；错版本通常 <0.05。
    ref_text 为空则返回 None（表示「无参照，未执行」）。
    """
    if not ref_text:
        return None
    body = lyric_body_text(lrc)
    # 英文歌词：2-gram 结构被 FunASR 错字打得只剩噪声（实测 0.00），
    # 改用「歌词的词有多少出现在演唱里」——字母序错但词的表层形式常有对上的部分。
    if _latin_ratio(body) >= 0.5:
        # lyric_body_text 会把空白也删掉，取词必须用保留空白的原文
        raw = re.sub(r"\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\]", "", lrc or "")
        lw, rw = set(_latin_words(raw)), set(_latin_words(ref_text))
        if not lw or not rw:
            return 0.0
        return len(lw & rw) / float(len(lw))
    a, b = _grams_of(body, n), _grams_of(ref_text, n)
    if not a or not b:
        return 0.0
    return len(a & b) / float(min(len(a), len(b)))


def fetch_lyrics_and_cover(title, artist_hint="", dur=None, cache_dir=None, ref_text=None):
    """返回 (lrc:str|None, cover_path:str|None, info:dict)。cover 已下载到 cache_dir。

    ref_text：本片段的演唱转写原文（SRT 文本）。给了就启用「字面一致性」判据，
    把同名不同歌的错版本候选排到后面，避免成片配着完全不相干的歌词。
    """
    info = {}
    cache_dir = cache_dir or os.getcwd()
    os.makedirs(cache_dir, exist_ok=True)
    key = re.sub(r"[^\w]", "_", _norm(title))[:40]

    cache_f = os.path.join(cache_dir, "_lrc_%s.json" % key)
    if os.path.exists(cache_f):
        try:
            d = json.load(io.open(cache_f, encoding="utf-8"))
            if d.get("lrc") or d.get("cover_path"):
                info = d.get("info", {})
                # 缓存可能是旧逻辑写下的残缺版本 → 完整性复检；疑似残缺则弃用重拉。
                # （达尔文 2026-09-30：缓存里存着 26 行的苡慧翻唱版，成了持续污染源）
                stale = False
                if d.get("lrc") and dur and dur > 1:
                    cov = info.get("lyrics_coverage")
                    if cov is None:
                        cov = lrc_tail_sec(d["lrc"]) / dur
                    stale = cov < LRC_SUSPECT_COVER
                # 缓存 key 只含歌名：同名不同歌的错版本会长期驻留（持续污染源）。
                # 已知歌手时做「歌手一致性」复核，给出参照文本时再做「字面一致性」复核。
                if not stale and artist_hint and d.get("lrc"):
                    cached_artist = (info.get("artist") or _artist_from_source(
                        info.get("lyrics_source")) or "").strip()
                    if not artist_matches(artist_hint, cached_artist):
                        stale = True
                        info["cache_invalidated"] = "歌手不一致（缓存 %s vs 曲库 %s）" % (
                            cached_artist, artist_hint)
                if not stale and ref_text and d.get("lrc"):
                    c_ov = text_overlap_ratio(d["lrc"], ref_text)
                    info["lyrics_overlap"] = round(c_ov or 0.0, 3)
                    if (c_ov or 0.0) < LRC_TEXT_OV_OK:
                        stale = True
                        info["cache_invalidated"] = (
                            "歌词与演唱内容字面重合率仅 %.1f%%（阈值 %.0f%%）"
                            % ((c_ov or 0.0) * 100, LRC_TEXT_OV_OK * 100))
                if stale:
                    # 歌手不符 →旧缓存里的封面图同样属于那个错误歌手，必须作废，
                    # 否则重抓失败时会继续用翻唱版专辑图出片（2026-10-06 连名带姓事故）。
                    d["cover_path"] = None
                    info.pop("cover_source", None)
                if not stale:
                    if d.get("cover_path") and not os.path.exists(d["cover_path"]):
                        d["cover_path"] = None
                    if not info.get("artist"):
                        info["artist"] = _artist_from_source(info.get("lyrics_source"))
                    return d.get("lrc"), d.get("cover_path"), info
        except Exception:
            pass

    # ---- 多源获取 + 歌词完整性择优 ----
    # 教训：单一源「取第一个有歌词的」会把翻唱/重制短版当成原版。
    #   达尔文（2026-09-30）：LRCLIB 只有苡慧《达尔文·2022》26 行/末行 183s，
    #   而蔡健雅原版 34 行/末行 248s —— 尾部 outro「有过竞争…进化成更好的人」
    #   整段丢失，且 LRCLIB 那份还是繁体。
    # 这里 LRCLIB 与网易云都取，按 ①末行时间戳占歌曲时长的比例 ②行数 择优。
    cands = []
    try:
        for c in lrclib_candidates(title, artist_hint)[:4]:
            cands.append({"lrc": c["lrc"], "source": c["source"],
                          "artist": c["artist"], "cover_url": None})
    except Exception:
        pass
    cover_url, nartist = None, ""
    try:
        nl, ncover, nsrc, nart, ndur, nrelaxed = _netease(title, artist_hint)
        cover_url, nartist = ncover, (nart or "")
        if ndur:
            info["netease_duration_ms"] = ndur
        if nrelaxed:
            # 没拿到原唱版本的条目 → 封面与 dt 很可能属于某个翻唱版，不可信。
            # 不静默：dt直接不采信（原曲时长铁律：缺失必须联网核实，不允许猜）。
            info.pop("netease_duration_ms", None)
            info["warn_artist_mismatch"] = (
                "网易云候选中未找到原唱（%s）版本，封面与官方时长不可信，"
                "已丢弃 dt、需人工指定封面" % (artist_hint or "?"))
        if nl:
            cands.append({"lrc": nl, "source": nsrc, "artist": nart,
                          "cover_url": ncover})
    except Exception:
        pass

    scored = []
    for c in cands:
        body = clean_lrc(c["lrc"], dur)
        if not re.search(r"\[\d{1,3}:\d{2}", body):     # 清洗后无有效时间轴 → 淘汰
            continue
        lines = len([x for x in body.splitlines() if x.strip()])
        tail = lrc_tail_sec(body)
        cov = (tail / dur) if (dur and dur > 1) else (tail / 240.0)
        ov = text_overlap_ratio(body, ref_text)
        scored.append({"cov": cov, "lines": lines, "tail": tail, "body": body,
                       "source": c["source"], "artist": c["artist"],
                       "cover_url": c["cover_url"], "ov": ov})
    # 有转写参照时：先按「与演唱内容字面一致」分档，再比覆盖率/行数。
    # 只按覆盖率会选到同名不同歌的错版本（见 text_overlap_ratio 的《泡泡》案例）。
    if ref_text:
        scored.sort(key=lambda x: (1 if (x["ov"] or 0.0) >= LRC_TEXT_OV_OK else 0,
                                   round(x["ov"] or 0.0, 3),
                                   round(x["cov"], 3), x["lines"]), reverse=True)
    else:
        scored.sort(key=lambda x: (round(x["cov"], 3), x["lines"]), reverse=True)

    lrc, artist = None, ""
    if scored:
        b = scored[0]
        lrc = b["body"]
        if ref_text:
            info["lyrics_overlap"] = round(b["ov"] or 0.0, 3)
            if (b["ov"] or 0.0) < LRC_TEXT_OV_OK:
                info["warn_lyrics_mismatch"] = (
                    "歌词与演唱内容字面重合率仅 %.1f%%（阈值 %.0f%%）——很可能是同名不同歌的"
                    "错误版本（演唱：%s / 抓到：%s）"
                    % ((b["ov"] or 0.0) * 100, LRC_TEXT_OV_OK * 100,
                       (ref_text or "")[:24], b["source"]))
        info["lyrics_source"] = b["source"]
        info["lyrics_lines"] = b["lines"]
        info["lyrics_tail_sec"] = round(b["tail"], 2)
        info["lyrics_coverage"] = round(b["cov"], 3)
        info["lyrics_rejected"] = [{"source": s["source"], "lines": s["lines"],
                                    "tail_sec": round(s["tail"], 2),
                                    "coverage": round(s["cov"], 3),
                                    "overlap": (round(s["ov"], 3) if s.get("ov") is not None else None)}
                                   for s in scored[1:]]
        artist = b["artist"] or ""
    else:
        info["lyrics_source"] = "none"

    cover_url = cover_url or next((s["cover_url"] for s in scored if s["cover_url"]), None)
    artist = (nartist or "").strip() or artist       # 网易云的歌手写法优先（权威元数据）
    if artist:
        info["artist"] = artist
    if _T2S_OK is False:
        info["warn_t2s"] = "繁简转换依赖缺失（pip install opencc-python-reimplemented），歌词可能保留繁体"

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
    if not cover_url and info.get("cover_source") in (None, "none"):
        # 没有任何可信封面来源时，尝试退回 LRCLIB 的专辑图（若有）
        cover_url = next((s["cover_url"] for s in scored if s.get("cover_url")), None)
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
