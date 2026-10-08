#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auto_cut.py —— 每日「全自动歌切」驱动（不依赖任何人工歌单表）

判定链条全部由内容本身驱动，不看在线表格：
  ① 录播定位   ：录播根目录下按日期找当天文件；
  ② 转写       ：优先复用上游 SRT；缺失时用自建 FunASR 分块管线现转；
  ③ 找演唱窗口 ：把整场转写切成 45 分钟块交给 LLM，问「这段有没有完整唱歌」，
                 有则让它**原样摘出**转写里的歌词行（不让它猜歌名）；
  ④ 定歌名     ：拿摘出的唱词去网易云做**歌词检索**（type=1006），歌名由检索结果给出；
                 另用曲库歌名在转写里的提及做补充候选；
  ⑤ 定区间     ：抓到歌词后放回整场转写滑动比对（字符 2-gram），定位真实演唱起止；
  ⑥ 核验       ：字面重合率 + 歌词覆盖率 + 原曲官方时长（超长按原曲时长收紧出点）；
  ⑦ 出片       ：写识别缓存 → 调 song_cutter.py --no-kdocs 完成精修/渲染/自检；
  ⑧ 报告       ：reports/auto/<日期>.json + LOG.md。

用法：
  python auto_cut.py                       # 处理昨天
  python auto_cut.py --date 2026-10-02     # 指定日期
  python auto_cut.py --no-cut              # 只检测不出片
  python auto_cut.py --no-llm              # 跳过 LLM 分块（只用歌词检索/提及候选）
  python auto_cut.py --asr                 # 忽略上游 SRT，强制自建转写
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import song_cutter as SC                       # noqa: E402
from songcut import lyrics_fetch               # noqa: E402
from songcut import lyric_locate               # noqa: E402
from songcut import asr_auto                   # noqa: E402

MUSIC_PREFIX = asr_auto.MUSIC_PREFIX           # ♪：cue 文本前缀，表示该块被判定为纯音乐/BGM


def is_music_cue(text):
    """cue 是否带音乐事件标记（上游 SRT 无此信息 → 一律 False，只在自建转写时可用）。"""
    return bool(text) and text.startswith(MUSIC_PREFIX)


def strip_music(text):
    return (text or "").lstrip(MUSIC_PREFIX)

OV_OK = getattr(lyrics_fetch, "LRC_TEXT_OV_OK", 0.18)
DUR_UPPER = 1.15            # 区间/原曲时长 上限：超出按原曲时长收紧出点
# ⚠ 时长可信度上限（2026-10-08）：实测演唱跨度超过「官方时长 ×1.35」→ 该时长必错，
#   拒绝自动出片（网易云限流时 LRCLIB 会返回离谱值，如《反方向的钟》131.3 vs 实际 258）。
#   真唱跨度 ≤ 官方时长 + 现场加唱（通常 +10~20%），1.35 留足余量。
DUR_TRUST_MAX = SC.DUR_TRUST_MAX   # 单一真源：检测与出片用同一个可信度上限
# ⚠ 2026-10-06 由0.80 收紧到 0.55：低于此不再自动出片，只标「疑似·待人工确认」。
#   原 0.80 太松 → 「只是放了首 BGM、她跟着哼两句」也能过（span 通常只有原曲 20~40%）。
DUR_LOWER = 0.55
SPAN_RATIO_MIN = 0.40       # 可定位歌词跨度/原曲 时长下限：低于此判「没真正唱」
# 疑似演唱区间前后 ±N 秒内的转写条数（仅记录，不参与否决，见闸门 4c）
NEAR_CUE_SEC = 60.0
BLOCK_SEC = 2700.0          # LLM 分块长度（45 分钟）
BLOCK_OVERLAP = 180.0
MAX_BLOCKS = 10
MENTION_MAX = 8             # 曲库歌名在整场被提及次数上限（超过视为常用词，不作为候选）
# ==== 兜底扫描范围（2026-10-08，为修 10-07《反方向的钟》整首漏切）====
# 旧设计：library_fallback 只在「常规候选一个都没通过」时触发。10-07 常规候选
# 命中了《爱情讯息》→ ok_items 非空 → 兜底**根本没跑**，于是同场第二首
#《反方向的钟》（ASR 报成「反风飒钟」，字面提及匹配失效）永远进不了候选池。
# ⇒ 改成「候选被核验掉的歌曲数 > 0 就跑兜底」：只要有一首歌没被识别出来，
#   就说明歌名生成环节漏了，必须全量扫一遍曲库歌词。
#代价是每场多扫一次曲库（1246 首，走 _media_cache 缓存，第二次起几乎不联网）。
LIB_SCAN_MAX = 1200         # 单场最多扫多少首曲库歌词
LIB_SCAN_TOP = 12           # 粗筛后最多留几个候选做精判
LIB_SCAN_RECALL = 0.30      # 曲库全量粗筛阈值：宁可多选几个，交给 locate 精判
                            # （实测：真歌英文 .57~.76 / 中文 .72，噪声英文 ≤.27 / 中文 ≤.07）
                            # ⚠ 这是**唯一**可靠的漏切信号来源，别再试图用文本特征替代（见下方注释）。

# ==== 「她同时在讲别的事」判据（用户 2026-10-06 提出，BGM 误判治理核心）====
# 播 BGM 时她通常同时在说话，FunASR 会把讲的内容一并转写 → 窗口内非歌词语音占比很高。
# 真唱时窗口内几乎全是歌词。实测：假唱 You(=I) 100% / 173s 一整段；真歌 0~30.5% / 最长 15s。
# ⚠ 只统计**时长占比**与**最长连续段**，不逐句下结论 —— 逐句分类已被实测否决
#   （FunASR 中文错字会把真唱段 16/16 句误判成说话，见 verify_candidate 闸门 4 注释）。
NONLYRIC_RATIO_MAX = 0.55   # 窗口内非歌词语音时长占比上限
NONLYRIC_RUN_MAX = 45.0     # 单段连续非歌词语音上限（秒）

SYS_LYRIC = (
    "你是直播录播分析助手。任务：判断一段语音转写里是否存在主播完整演唱一首歌的段落，"
    "并从转写中**原样摘出**歌词行。输出必须是严格合法的 JSON，不要多余文本或代码围栏。"
)
USER_LYRIC = """下面是一场直播语音转写的一个片段（每行形如 [HH:MM:SS] 文本，时间为该句在录播中的起点）。

请判断其中是否存在「主播完整演唱一首歌」的段落：
  - 算：跟着伴奏唱完整一首、清唱整首；
  - 不算：随口哼一两句、只是聊到歌名、只放原曲不唱、合唱一两句。

若存在，对每个段落给出：
  - start / end：真正开唱与唱完收尾的时刻（HH:MM:SS，与转写同一时间轴；报歌名/互动/唱完后的闲聊都不算）；
  - lines：从下面转写里**原样摘出** 3~6 行你认为属于歌词的文本（必须照抄，不要改写、不要补字、不要自己编）。

严格输出 JSON：
{"singing": true, "segments": [{"start": "HH:MM:SS", "end": "HH:MM:SS", "lines": ["...", "..."]}]}
若不存在：{"singing": false, "segments": []}

转写片段：
%s"""


def yesterday(days=1):
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def hms(t):
    t = float(t)
    return "%02d:%02d:%02d" % (int(t // 3600), int((t % 3600) // 60), int(t % 60))


def probe_video(ffprobe, path):
    info = SC.ffprobe_json(ffprobe, path)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    return {"duration_s": round(float(info["format"]["duration"]), 2),
            "size_mb": round(int(info["format"]["size"]) / 1048576),
            "width": v.get("width"), "height": v.get("height")}


def find_upstream_srt(date):
    if not (SC.SRT_DIR and os.path.isdir(SC.SRT_DIR)):
        return []
    ymd = date.replace("-", "")
    out = [os.path.join(SC.SRT_DIR, f) for f in os.listdir(SC.SRT_DIR)
           if f.startswith(ymd) and f.lower().endswith(".srt")]
    out.sort()
    return out


def parse_block_json(raw):
    """解析「演唱窗口」块回复：{"singing": bool, "segments":[{start,end,lines}]}。

    注意：不能用 SC.extract_json —— 那是 song_cutter 的 {"songs":[…]} 专用解析器，
    会把 {"singing":…,"segments":[…]} 整体丢弃（singing=false 时直接报「找不到合法 JSON」，
    segments 非空时又把单个 segment 误当成 songs 项），导致本步骤永远拿不到结果。
    """
    t = re.sub(r"```+(?:json)?", "", (raw or "").strip())
    dec = json.JSONDecoder(strict=False)
    for i, ch in enumerate(t):
        if ch != "{":
            continue
        try:
            obj, _end = dec.raw_decode(t, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and ("segments" in obj or "singing" in obj):
            return obj
    return None


def llm_windows(cfg, entries, block=BLOCK_SEC, overlap=BLOCK_OVERLAP, max_blocks=MAX_BLOCKS,
                srt_path=None, workdir=None):
    """分块问 LLM：有没有唱歌段落？有则原样摘出歌词行。返回 [(start, end, [lines])]。

    结果按「SRT 指纹 + 分块参数 + 模型」缓存（2026-10-09）：
    这是每场**唯一的常驻 Token 开销**（整场约 7 万字转写切 10 块喂进去，≈44k token），
    而同一份 SRT 在调试/重渲/补片时会被反复处理 —— 不改判据、只复用结果，
    第二次起直接读盘。SRT 一变（mtime/size）指纹就变，自动失效。
    """
    if not entries:
        return []
    cache_f = None
    if srt_path and workdir:
        try:
            st = os.stat(srt_path)
            sm = (cfg.get("summarize") or {})
            model = str(sm.get("model") or sm.get("provider") or "")
            fp = "%s|%d|%d|%s|%d|%d|%d" % (os.path.basename(srt_path), int(st.st_mtime),
                                           st.st_size, re.sub(r"[^\w]", "", model)[:24],
                                           int(block), int(overlap), max_blocks)
            d = os.path.join(workdir, "_cache", "llmwin")
            os.makedirs(d, exist_ok=True)
            cache_f = os.path.join(d, "%s.json" % re.sub(r"[^\w.-]", "_", fp)[:120])
            if os.path.exists(cache_f):
                try:
                    c = json.load(io.open(cache_f, encoding="utf-8"))
                    SC.log("LLM 分块：复用缓存（%s，%d 个窗口，saved ~%d 次调用）"
                           % (os.path.basename(cache_f), len(c.get("windows") or []),
                              c.get("calls", len(c.get("windows") or []))))
                    return [tuple(w) for w in (c.get("windows") or [])], bool(c.get("singing"))
                except Exception:
                    pass
        except Exception:
            cache_f = None
    total = max(e for _s, e, _t in entries)
    out, t, n, singing_any = [], 0.0, 0, False
    while t < total and n < max_blocks:
        t1 = min(t + block, total)
        sub = [(a, b, x) for (a, b, x) in entries if a >= t and a < t1]
        n += 1
        if len(sub) >= 10:
            body = "\n".join("[%s] %s" % (SC.sec_to_hms(a), x.replace("\n", " "))
                             for a, _b, x in sub)
            try:
                raw = SC.call_llm(cfg["summarize"], SYS_LYRIC, USER_LYRIC % body)
                data = parse_block_json(raw)
            except Exception as e:
                SC.log("    块 %d LLM 失败：%s" % (n, str(e)[:120]))
                data = None
            for sg in ((data or {}).get("segments") or []):
                try:
                    st = SC.hms_to_sec(SC._clean_time_field(sg.get("start", "")))
                    en = SC.hms_to_sec(SC._clean_time_field(sg.get("end", "")))
                except Exception:
                    continue
                if en <= st or (en - st) > SC.MAX_SEC:
                    continue
                lines = [str(x) for x in (sg.get("lines") or []) if str(x).strip()]
                out.append((st + t, en + t, lines))
            if (data or {}).get("singing"):
                singing_any = True
        t = t1 - overlap if t1 < total else total
    if cache_f:
        try:
            io.open(cache_f, "w", encoding="utf-8", newline="").write(json.dumps(
                {"windows": out, "singing": singing_any, "calls": n,
                 "at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False))
        except Exception:
            pass
    return out, singing_any


def mention_candidates(entries, lib_names):
    """曲库歌名在转写中被提及 → 补充候选（只作候选，区间仍由歌词定位决定）。

    ⚠ 2026-10-08 教训：**不要在这里做模糊/同音容错**。
    为修 10-07《反方向的钟》漏切（她报「我有反风飒钟」，字面匹配失效），
    实测过三种模糊方案全部失败：
      ① 字符集覆盖 ≥75%           → 候选 42 → 255（星星/大风吹 到处命中）
      ② 顺序子序列 ≥60%（≥3字）   → 候选 42 → 128（长闲聊句乱凑）
      ③ 歌单序列（同句≥3 首）     → 3720s 那句因夹大量英文电影解说反而没被认出，
                                      13840s 英文长句反而「命中 24 首」
    根本原因：文本相似度不是识别判据，**歌词定位才是**。
    同音错字问题由 `library_fallback`（拿曲库歌词回全曲定位）统一解决 ——
    见`main` 里「兜底常开」的触发条件修复。
    """
    hits = {}
    for nm in lib_names:
        if len(nm) < 2:
            continue
        for a, _b, t in entries:
            if nm in t:
                hits.setdefault(nm, 0)
                hits[nm] += 1
    return [n for n, c in hits.items() if 1 <= c <= MENTION_MAX]


def fetch_lrc(title, workdir, artist_hint=None):
    try:
        lrc, _cov, info = lyrics_fetch.fetch_lyrics_and_cover(
            title, artist_hint=artist_hint or "", dur=None, ref_text=None,
            cache_dir=os.path.join(workdir, "_media_cache"))
    except Exception:
        return None, {}
    return (lrc if (info.get("lyrics_source") not in (None, "none")) else None), info


def build_candidates(entries, llm_win, workdir, use_llm=True, cfg=None):
    """把「LLM 摘出的唱词」和「转写提及的曲库歌名」汇成候选歌名列表。

    ⚠ 2026-10-08：提及候选分两档，避免 ASR 同音错字造成的漏切被噪声淹没。
      模糊命中（mention-fuzzy）**先用本地歌词缓存做 locate 快筛**：
      唱词在整场转写里定位不出来的，直接丢弃 —— 这让「你在玩你的游戏」
      凑出「如果的事」这类噪声在**零联网成本**下被筛掉。
    """
    cands, seen = [], set()

    def add(name, src, lines=None):
        key = lyric_locate.norm(name)
        if not key or key in seen:
            return
        seen.add(key)
        cands.append({"name": name, "src": src, "lines": lines or []})

    if use_llm:
        for _st, _en, lines in llm_win:
            for c in lyric_locate.identify_by_lines(lines)[:3]:
                add(c["name"], "lyric-search", lines)
    try:
        lib = json.load(io.open(SC.SONG_LIB_JSON, encoding="utf-8"))
        names = [x.get("song_name") for x in lib if x.get("song_name")]
    except Exception:
        names = []
    for nm in mention_candidates(entries, names):
        add(nm, "mention")
    return cands


def _overlaps(a1, a2, b1, b2, min_overlap=30.0):
    """两个演唱区间是否**实质性**重叠（重叠时长须≥ min_overlap 秒才算同一段）。

    兜底常会把同一段识别成不同歌名（同名不同版/剪辑版），需要去重；
    但**擦边不算重叠**：两首歌前后紧接、或同一首歌被间奏切成两段时，
    边界只有十几秒交叠，应当视为两首不同的歌各出各的片。
    ⇒ 判据用「交叠时长」而不是「区间相交」：
         overlap = min(a2,b2) - max(a1,b1)，> 0 时取该值；≤0 视为不相交。
    """
    ov = min(a2, b2) - max(a1, b1)
    return ov >= min_overlap


# ⚠ 已否决的漏切信号（2026-10-08 实测，勿再尝试，代码已移除）：
#   「转写里存在未被已识别歌曲覆盖的成段歌词样文字 → 必有漏切」
#   想法：用转写自身结构特征（连续≥4 条、总时长≥45s、平均每条≥11 字、
#   句间隔≤14s）找出没被认出来的演唱。
#   实测：10-07 整场跑出 **31 段全部误报**（闲聊段同样长句密集）。
#   ⇒ 与 10-06 的教训一致：FunASR 转写里「歌词」与「闲聊」在**文本结构上
#     不可区分**（34.9 字/句 vs 50.9 字/句都是长句），逐句/分段结构判据原理性失效。
#   ⇒ 唯一可靠的漏切信号是 `library_fallback`：**歌词定位**天然过滤噪声。


def _local_cached_lrc(name, workdir, artist_hint=""):
    """直接读本地歌词缓存正文（不联网）。全库粗筛只需要歌词正文做 fast_screen。

    背景：全库粗筛原本逐首调 fetch_lyrics_and_cover，而它每首都要走
    歌手一致性 / 字面重合率 / 残缺复检 —— 任一条不符就判 stale 重新联网，
    实测 631 ms/首 × 1200 首 ≈ 12.6 min/场；而真正下判据的 fast_screen 只要 1.9 ms/首。

    ⚠ 歌手一致性这**一条**必须在这里保留（其余两条粗筛用不到）：
    2026-10-09 实测《Blessing》缓存里存的是 FictionJunction 版，与曲库原唱
    halyosy/初音ミク 不符 —— 拿错版本的歌词去粗筛，recall 从 0.652 掉到 0.042，
    **直接跌破 0.30 阈值造成漏召**（漏切是最贵的事故，见 MEMORY）。
    所以：歌手不符 → 返回 None，交给 fetch_lrc 走原路径复核纠正，
    纠正结果会写回缓存，下一场起这首歌就转为本地直读（自愈）。
    """
    f = os.path.join(workdir, "_media_cache",
                     "_lrc_%s.json" % lyrics_fetch.cache_key(name))
    if not os.path.exists(f):
        return None
    try:
        d = json.load(io.open(f, encoding="utf-8"))
    except Exception:
        return None
    lrc = d.get("lrc")
    if not lrc:
        return None
    info = d.get("info") or {}
    if artist_hint:
        cached_artist = (info.get("artist")
                         or lyrics_fetch._artist_from_source(info.get("lyrics_source"))
                         or "").strip()
        if not lyrics_fetch.artist_matches(artist_hint, cached_artist):
            # 歌手不符 → 缓存可能是翻唱版（同名不同歌时粗筛会漏召，见 Blessing）。
            # 但「已经用同一个 hint 纠正过」的，说明歌词站确实拿不到更匹配的版本
            # （网易云常无原唱条目，如《反方向的钟》只有「乐乐仔」版），
            # 再纠正一万次也是同一个结果 —— 记下已检查，后续直接放行。
            # ⚠ 这个标记是**性能**手段，不是正确性判据：粗筛漏召才是致命的，
            #   而放行只可能多给候选，精判 verify_candidate 仍会用正确版本复核。
            if info.get("artist_checked_hint") == artist_hint:
                return lrc
            return None      # 未检查过 → 交给 fetch_lrc 纠正一次
    return lrc


def _mark_artist_checked(name, workdir, artist_hint):
    """给歌词缓存打「已用该歌手提示纠正过」的标记，避免每场重复纠正同一批翻唱缓存。"""
    if not artist_hint:
        return
    f = os.path.join(workdir, "_media_cache",
                     "_lrc_%s.json" % lyrics_fetch.cache_key(name))
    if not os.path.exists(f):
        return
    try:
        d = json.load(io.open(f, encoding="utf-8"))
        info = d.setdefault("info", {})
        if info.get("artist_checked_hint") == artist_hint:
            return
        info["artist_checked_hint"] = artist_hint
        info["artist_checked_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        io.open(f, "w", encoding="utf-8", newline="").write(
            json.dumps(d, ensure_ascii=False))
    except Exception:
        pass      # 打标失败不影响结果，只是下次还会再纠正一次


def library_fallback(entries, workdir, log=print):
    """兜底候选来源：拿曲库歌词回整场转写粗筛，找出真被唱过的歌名。

    用于「LLM 说有唱歌，但所有候选都核验不过」的场景 —— 说明歌名没能生成，
    而不是没唱。

    2026-10-09 提速：粗筛阶段**本地歌词优先**（实测 1201 首 / 2.3 s，
    对比原先逐首 fetch_lrc 约 12.6 min）。只有本地没缓存的歌名才走联网，
    联网新拉到的写回缓存，下一场起同样免联网。
    """
    idx = lyric_locate.window_index(entries)
    if not idx:
        return []
    try:
        lib = json.load(io.open(SC.SONG_LIB_JSON, encoding="utf-8"))
    except Exception:
        return []
    names, seen = [], set()
    for x in lib:
        nm = (x.get("song_name") or "").strip()
        ar = (x.get("artist") or "").strip()
        if nm and nm not in seen:
            seen.add(nm)
            names.append((nm, ar))
    hits = []
    scanned = 0
    fetched = 0          # 本地没缓存、必须联网的歌名数
    t0 = time.time()
    for nm, ar in names[:LIB_SCAN_MAX]:
        lrc = _local_cached_lrc(nm, workdir, ar)
        if lrc is None:
            # 本地无缓存 / 缓存是错版本（歌手不符且未检查过）→ 走原路径，
            # 会联网纠正并写回；纠正完打标记，后续场次不再重复纠正。
            lrc, _info = fetch_lrc(nm, workdir, ar)
            _mark_artist_checked(nm, workdir, ar)
            fetched += 1
        if not lrc:
            continue
        scanned += 1
        r, _t, _mode = lyric_locate.fast_screen(idx, lrc)
        if r >= LIB_SCAN_RECALL:
            hits.append((r, nm))
    hits.sort(reverse=True)
    log("曲库全量粗筛：扫描 %d 首歌词（本地 %d + 联网 %d），%.2f 以上 %d 首，耗时 %.1fs → %s"
        % (scanned, scanned - fetched, fetched, LIB_SCAN_RECALL, len(hits),
           time.time() - t0, "、".join(n for _r, n in hits[:8]) or "无"))
    return [{"name": nm, "src": "library-scan"} for _r, nm in hits[:LIB_SCAN_TOP]]


def verify_candidate(cand, entries, workdir):
    """抓歌词 → 转写定位 → 重合率/时长核验。返回 (item, ok)。"""
    title = cand["name"]
    hint = SC.lookup_library_artist(title)
    lrc, info = fetch_lrc(title, workdir, hint)
    item = {"title": title, "source": cand["src"], "artist_hint": hint or None,
            "lyrics_source": info.get("lyrics_source"), "notes": []}
    if not lrc:
        item["notes"].append("未检索到该歌名歌词")
        return item, False
    loc = lyric_locate.locate(entries, lrc)
    if not loc:
        item["notes"].append("歌词在整场转写中未找到匹配片段（本场很可能没唱这首）")
        return item, False
    need_lines = max(5, int(0.2 * loc["lyric_lines"]))
    if loc["matched_lines"] < need_lines:
        item["notes"].append(
            "转写中只唱到 %d/%d 行歌词（需 ≥%d 行），判为未真正演唱"
            % (loc["matched_lines"], loc["lyric_lines"], need_lines))
        return item, False
    ref_text = "\n".join(t for (a, b, t) in entries if loc["start"] - 2 <= a <= loc["end"] + 2)
    if loc.get("latin_recall") is not None:
        # 英文歌词：2-gram 重合率被 FunASR 错字打成 0.00，改看 locate 已算的词召回率
        ov = loc["latin_recall"]
        item["match_metric"] = "latin_recall"
    else:
        ov = lyrics_fetch.text_overlap_ratio(lrc, ref_text)
        item["match_metric"] = "text_overlap"
    if (ov or 0.0) < OV_OK:
        item["notes"].append(
            "歌词与演唱内容重合度 %.2f < %.2f（判据=%s），判为不匹配"
            % (ov or 0.0, OV_OK, item["match_metric"]))
        return item, False

    # ==== 闸门 4：区分「她唱」与「只放 BGM」 ====
    # 实测教训（10-01/10-02/10-05 三场真实数据）：**逐句文本分类原理性失效**
    #   · BGM 里的歌词与她唱的歌词在转写文本上完全一样，无法区分；
    #   · FunASR 中文错字多（守候→守住/发芽→发茅），单句 2-gram 命中率极低
    #     （《泡泡》真唱段 16/16 句被误判成「说话」）；
    #   · 字幕句长/间隔结构也无区分度（真唱 34.9 字 vs 闲聊 50.9 字，都是长句）。
    # ⇒ 改用两条**时长级**判据（不逐句下结论）：
    #   4a. ASR 音乐事件标签（自建转写可用；上游 SRT 无此信息 → 恒为 0）
    #   4b.「她同时在讲别的事」（用户提出）：播 BGM 时她通常同时说话，那段时间的
    #       转写内容是**她在讲的事**而非歌词 → 窗口内非歌词语音时长占比 + 最长连续段。
    win = [(a_, b_, strip_music(t)) for (a_, b_, t) in entries
           if loc["start"] - 2 <= a_ <= loc["end"] + 2]

    # ---- 闸门 4a：纯音乐标签 ----
    music_ratio = (sum(1 for (a_, _b_, t) in entries
                       if loc["start"] - 2 <= a_ <= loc["end"] + 2 and is_music_cue(t))
                   / float(len(win))) if win else 0.0
    item["music_ratio"] = round(music_ratio, 3)
    item["gate4_music"] = "PASS"
    if music_ratio >= 0.8:
        item["gate4_music"] = "FAIL"
        item["notes"].append(
            "区间内 %.0f%% 的转写块被 ASR 标记为纯音乐（<|BGM|>）：只有伴奏没有主播人声"
            % (music_ratio * 100))
        return item, False

    # ---- 闸门 4b：她同时在讲别的事（区分真唱 / 只放 BGM）----
    # 只看时长占比与最长连续段，不逐句判定（逐句已被实测否决，见上方注释）。
    prof = lyric_locate.speech_profile(entries, lrc, loc["start"], loc["end"])
    item["nonlyric_ratio"] = prof["nonlyric_ratio"]
    item["nonlyric_max_run_s"] = prof["max_run_s"]
    item["nonlyric_runs"] = prof["runs"][:6]
    item["window_speech_s"] = prof["speech_s"]
    item["gate4_speech"] = "PASS"
    ratio, run = prof["nonlyric_ratio"], prof["max_run_s"]
    if prof["speech_s"] <= 0:
        # 窗口内一条转写都没有 → 上游 SRT 缺这段，只能交给音频判据，不在这里否决
        item["gate4_speech"] = "SKIP"
    elif ratio > NONLYRIC_RATIO_MAX:
        item["gate4_speech"] = "FAIL"
        item["notes"].append(
            "演唱窗口内 %.0f%% 的语音（%.0f/%.0fs）匹配不上歌词，最长连续 %.0fs："
            "这段时间她在**讲别的事**（播 BGM 时的解说/闲聊），不是在唱这首歌"
            % (ratio * 100, prof["nonlyric_s"], prof["speech_s"], run))
        return item, False
    elif run > NONLYRIC_RUN_MAX:
        item["gate4_speech"] = "FAIL"
        item["notes"].append(
            "演唱窗口内有一段连续 %.0fs 的非歌词语音：她在讲别的事，不是只放 BGM 就是在唱别的"
            % run)
        return item, False
    elif ratio > NONLYRIC_RATIO_MAX * 0.6 or run > NONLYRIC_RUN_MAX * 0.6:
        # 逼近阈值 → 不否决，但写进报告供人工判断（宁可疑似不可漏）
        item["gate4_speech"] = "WARN"
        item["notes"].append(
            "窗口内非歌词语音 %.0f%%、最长连续 %.0fs，接近 BGM 阈值（%d%% / %ds），"
            "已标为待人工确认" % (ratio * 100, run, int(NONLYRIC_RATIO_MAX * 100),
                                  int(NONLYRIC_RUN_MAX)))

    # ---- 闸门 4c：前后说话存在性（弱信号，仅记录）----
    # ⚠ 早期版本把这条当否决闸门（前后无转写→判BGM），方向与用户判据相反且会误杀：
    #   真唱段前后本来就可能是纯伴奏。保留为记录项，不参与否决。
    if entries:
        LO, HI = max(0.0, loc["start"] - NEAR_CUE_SEC), loc["end"] + NEAR_CUE_SEC
        item["nearby_cues"] = len([t for (a_, _b_, t) in entries
                                   if LO <= a_ <= HI and t.strip()])

    # ---- 闸门 5：歌词版本一致性 ----
    # 网易云同一首歌可能返回完全不同的版本（实测搜「泡泡」拿到片头曲
    # "我们吹呀吹"，主播唱的是 "告诉我吧告诉我吧"）→ 文本判据全部失真，
    # 会「真歌唱不出、假歌反而通过」。用区间转写与歌词的整体重合做版本核对。
    if win:
        g_win = lyric_locate.grams("\n".join(t for (_a, _b, t) in win))
        lg_all = lyric_locate.grams("\n".join(t for (_t, t) in lyric_locate.lrc_lines(lrc)))
        if len(g_win) >= 5 and len(lg_all) >= 20:
            ver = len(g_win & lg_all) / float(len(g_win))
            item["lyric_version_ov"] = round(ver, 3)
            item["gate5_version"] = "PASS"
            if ver < 0.06:
                item["gate5_version"] = "FAIL"
                item["notes"].append(
                    "区间转写与歌词整体重合仅 %.0f%%：抓到的歌词很可能是**别的版本**，"
                    "判为不可核验（避免真歌漏/假歌过）" % (ver * 100))
                return item, False

    # 原曲官方时长：优先取歌词抓取时的网易云元数据，缺失再单独查一次
    ms = info.get("netease_duration_ms")
    src_dur = "netease"
    orig = round(ms / 1000.0, 1) if ms else lyric_locate.song_duration(title, hint or "")
    if not orig and not ms:
        src_dur = "lrclib/guess"
    item["orig_duration_src"] = src_dur
    if orig:
        item["orig_duration_s"] = round(orig, 1)
    # 起点：首句唱到的时刻 − 原曲前奏（伴奏进来的那一段）；终点：起点 + 原曲时长
    rough_start = max(0.0, loc["start"] - (loc["intro_s"] or 0.0) - 1.5)
    rough_end = (rough_start + orig) if orig else (loc["end"] + 2.0)
    item.update({"overlap": round(ov or 0.0, 3), "mean_prec": loc["mean_prec"],
                 "locate": {"hit_cues": loc["hit_cues"], "span_s": loc["span_s"],
                            "matched_lines": loc["matched_lines"],
                            "lyric_lines": loc["lyric_lines"], "intro_s": loc["intro_s"],
                            "raw": [loc["raw_start"], loc["raw_end"]]},
                 "rough_start": round(rough_start, 2), "rough_end": round(rough_end, 2),
                 "rough_start_hms": hms(rough_start), "rough_end_hms": hms(rough_end)})

    # ==== 时长可信度闸门（2026-10-08，为修「原曲时长错误导致切短」）====
    # 实测：网易云限流时 orig 来自 LRCLIB，对《反方向的钟》返回 **131.3s**
    #   （真实 4:18=258s）。于是 rough_end = 3756.5+131.3 = 3887.8，
    #   而开唱在 3782.9 → **成片只剩 105s，一半歌被切掉**，且全程无任何报错。
    #
    # 判据：实测演唱跨度 span_s 超过「官方时长 × (1 + DUR_TRUST_MAX)」→ 该时长
    #   不可能是官方时长。真唱跨度必然 ≤ 官方时长 + 少量加唱，DUR_TRUST_MAX=0.35
    #   已留足余量（现场加唱副歌通常 +10~20%）。标定见 tests：
    #   《反方向的钟》198.3s vs 131.3s → 1.51✗ 拒绝（比值 1.51 > 1.35）
    #   《爱情讯息》219.7/280.7=0.78 ✓  《泡泡》217.3/214.0=1.02 ✓  全部正常放行。
    if orig and loc["span_s"] > orig * (1.0 + DUR_TRUST_MAX):
        item["notes"].append(
            "实测演唱 %.0fs 明显长于查到的原曲时长 %.0fs（来源 %s，比值 %.2f > %.2f）——"
            "该时长不可能是官方时长（真唱跨度应接近官方时长），已拒绝自动出片，"
            "须人工核定切点后用 segments 的 cut_start_abs/cut_end_abs 重跑"
            % (loc["span_s"], orig, src_dur, loc["span_s"] / max(1.0, orig),
               1.0 + DUR_TRUST_MAX))
        item["duration_suspect"] = True
        return item, False
    if orig:
        ratio = loc["span_s"] / orig
        item["duration_ratio"] = round(ratio, 3)
        if ratio > DUR_UPPER:
            item["notes"].append("演唱区 %.0fs 长于原曲 %.0fs（%.0f%%）→ 已按原曲时长定出点，"
                                 "尾部的加唱/闲聊不计入" % (loc["span_s"], orig, (ratio - 1) * 100))
        elif ratio < DUR_LOWER:
            # 闸门 3：比例过低不再自动出片（以前只告警 → 纯 BGM 假歌蒙混过关）
            item["notes"].append(
                "可辨认演唱区 %.0fs / 原曲 %.0fs = %.0f%% < %d%%：判为未真正演唱，"
                "不自动出片（可能是只放 BGM 或仅哼唱）"
                % (loc["span_s"], orig, ratio * 100, int(DUR_LOWER * 100)))
            return item, False
        elif ratio < SPAN_RATIO_MIN:
            item["notes"].append("演唱跨度 %.0f%% 偏低，仅作备注" % (ratio * 100))
    return item, True


def dedupe(items):
    """同名去重；不同名但区间重叠 >60% 时保留重合率更高的。"""
    best = {}
    for it in items:
        k = lyric_locate.norm(it["title"])
        cur = best.get(k)
        if cur is None or (it.get("overlap") or 0) > (cur.get("overlap") or 0):
            best[k] = it
    out = list(best.values())
    out.sort(key=lambda x: x["rough_start"])
    keep = []
    for it in out:
        drop = False
        for jt in keep:
            a1, b1 = it["rough_start"], it["rough_end"]
            a2, b2 = jt["rough_start"], jt["rough_end"]
            inter = max(0.0, min(b1, b2) - max(a1, a2))
            if inter > 0.6 * min(b1 - a1, b2 - a2):
                drop = True
                if (it.get("overlap") or 0) > (jt.get("overlap") or 0):
                    keep[keep.index(jt)] = it
                break
        if not drop:
            keep.append(it)
    return keep


def write_report(rep_dir, date, rep):
    os.makedirs(rep_dir, exist_ok=True)
    p = os.path.join(rep_dir, "%s.json" % date)
    io.open(p, "w", encoding="utf-8").write(json.dumps(rep, ensure_ascii=False, indent=1))
    songs = rep.get("songs") or []
    md = os.path.join(rep_dir, "LOG.md")
    new = not os.path.exists(md)
    with io.open(md, "a", encoding="utf-8") as f:
        if new:
            f.write("| 日期 | 录播 | 唱歌环节 | 歌名 | 输出 |\n|---|---|---|---|---|\n")
        if not rep["recording"]["found"]:
            f.write("| %s | 无 | - | - | - |\n" % date)
        elif not songs:
            f.write("| %s | 有 | %s | - | - |\n" % (date, rep["singing"]["detected"]))
        else:
            for s in songs:
                f.write("| %s | 有 | 是 | %s | %s |\n" % (
                    date, s.get("title"), (s.get("output") or "未出片")))
    return p


def main():
    ap = argparse.ArgumentParser(description="每日全自动歌切")
    ap.add_argument("--date", default=None, help="直播日期 YYYY-MM-DD（默认昨天）")
    ap.add_argument("--workdir", default=SC.DEFAULT_WORKDIR)
    ap.add_argument("--no-cut", action="store_true", help="只检测不出片")
    ap.add_argument("--asr", action="store_true", help="忽略上游 SRT，强制自建转写")
    ap.add_argument("--no-llm", action="store_true", help="跳过 LLM 分块找演唱窗口")
    ap.add_argument("--no-scan", action="store_true",
                    help="禁用曲库全量粗筛兜底（快，但英文歌场景下可能召回为 0）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--chunk", type=float, default=30.0, help="自建转写分块秒数")
    ap.add_argument("--qc-strict", action="store_true", help="自检不通过则整体失败")
    args = ap.parse_args()

    date = args.date or yesterday()
    t_begin = time.time()
    workdir = args.workdir
    rep_dir = os.path.join(workdir, "reports", "auto")
    os.makedirs(workdir, exist_ok=True)
    SC.open_log(workdir, date)
    SC.log("=== 全自动歌切开始：%s ===" % date)

    rep = {"date": date, "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "recording": {"found": False}, "transcript": {}, "singing": {"detected": False},
           "songs": [], "rejected": [], "outputs": [], "errors": []}

    for k in ("CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR", "CODEBUDDY_TOOL_CALL_ID"):
        os.environ.pop(k, None)
    if not os.environ.get("NODE_PATH"):
        SC.log("提示：NODE_PATH 未设置（渲染依赖 node_modules，建议在运行环境导出）")

    cfg = SC.load_sum_cfg()
    paths = cfg.get("paths") or {}
    ffmpeg = paths.get("ffmpeg") or SC.CFG.ffmpeg()
    ffprobe = paths.get("ffprobe") or SC.CFG.ffprobe()

    # ── 1. 找录播 ──────────────────────────────────────────────
    try:
        src = SC.find_video(date)
    except FileNotFoundError as e:
        SC.log("未找到录播：%s" % e)
        rep["errors"].append("未找到录播：" + str(e))
        p = write_report(rep_dir, date, rep)
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        print("报告 → %s" % p)
        return 0
    vinfo = probe_video(ffprobe, src)
    rep["recording"] = {"found": True, "path": src, **vinfo}
    SC.log("录播：%s（%.0fs / %dMB）" % (os.path.basename(src), vinfo["duration_s"], vinfo["size_mb"]))

    # ── 2. 转写 ────────────────────────────────────────────────
    srts = [] if args.asr else find_upstream_srt(date)
    if not srts:
        tag = os.path.splitext(os.path.basename(src))[0][:14]
        out_dir = SC.SRT_DIR or os.path.join(workdir, "transcript")
        os.makedirs(out_dir, exist_ok=True)
        out_srt = os.path.join(out_dir, "%s-%s.srt" % (date.replace("-", ""), tag))
        SC.log("无上游转写 → 自建 FunASR 分块转写（chunk=%.0fs）" % args.chunk)
        try:
            st = asr_auto.transcribe_to_srt(src, out_srt, chunk=args.chunk, ffmpeg=ffmpeg)
        except Exception as e:
            SC.log("转写失败：%s" % e)
            rep["errors"].append("转写失败：" + str(e)[:300])
            p = write_report(rep_dir, date, rep)
            print("报告 → %s" % p)
            return 1
        rep["transcript"] = {"source": "asr", **st}
        srts = [out_srt]
        SC.log("转写完成：%d 句 / %d 字，音乐块 %d（%.0fs），耗时 %.0fs"
               % (st["cues"], st["chars"], st["music_chunks"], st["music_sec"], st["elapsed_s"]))
    else:
        rep["transcript"] = {"source": "upstream",
                             "files": [os.path.basename(s) for s in srts],
                             "cues": sum(len(SC.parse_srt(s)) for s in srts)}
        SC.log("复用上游转写 %d 份：%s" % (len(srts), "；".join(os.path.basename(s) for s in srts)))

    # ── 3~6. 找窗口 / 定歌名 / 定区间 / 核验 ─────────────────────
    verified_all, llm_singing = [], False
    for srt_path in srts:
        entries = SC.parse_srt(srt_path)
        tag = os.path.splitext(os.path.basename(srt_path))[0][:14]
        SC.log("--- 场次 %s：%d 句转写 ---" % (tag, len(entries)))
        win, singing = ([], False) if args.no_llm else llm_windows(
            cfg, entries, srt_path=srt_path, workdir=workdir)
        llm_singing = llm_singing or singing
        SC.log("LLM 分块：演唱窗口 %d 个（singing=%s）" % (len(win), singing))
        for st_, en_, lines in win:
            SC.log("  窗口 %s~%s，摘出 %d 行唱词" % (hms(st_), hms(en_), len(lines)))
        cands = build_candidates(entries, win, workdir, use_llm=not args.no_llm)
        SC.log("候选歌名 %d 个：%s" % (len(cands), "、".join(c["name"] for c in cands[:12]) or "无"))
        ok_items = []
        for c in cands:
            item, ok = verify_candidate(c, entries, workdir)
            if ok:
                ok_items.append(item)
                SC.log("  ✓ %s 核验通过：%s~%s（重合率 %.2f，原曲 %ss）"
                       % (item["title"], item["rough_start_hms"], item["rough_end_hms"],
                          item["overlap"], item.get("orig_duration_s")))
            else:
                rep["rejected"].append({"title": item["title"], "source": item["source"],
                                        "why": "；".join(item["notes"]) or "核验未通过"})

        # ==== 兜底：默认执行（2026-10-08 修复 10-07《反方向的钟》整首漏切）====
        # 三个版本都被实测推翻，这是最终形态：
        #  ① 旧版 `if not ok_items`（常规候选全军覆没才扫）
        #     10-07 实测：常规候选命中了《爱情讯息》→ ok_items 非空 → 兜底**根本没跑**，
        #     而她同场唱的第二首《反方向的钟》因 ASR 把歌名听成「反风飒钟」（同音错字），
        #     字面提及匹配 0 命中、LLM 摘词检索也没覆盖到 → 永久漏切。
        #  ② 改为 `or len(win) > len(ok_items)`（LLM 窗口数 > 识别数就扫）
        #     复测再次漏切：GLM 只报了 **1 个**窗口（首次报 2 个），1 > 1 为 False。
        #     ⇒ **LLM 窗口数本身不可靠**（Google 429 降级到 GLM 后判定极不稳定）。
        #  ③ 试过「转写里存在未被覆盖的成段歌词样文字 → 必定漏切」
        #     实测 31 段误报 —— FunASR 转写里歌词与闲聊的**文本结构无法区分**
        #     （长句密集这一特征闲聊同样满足），已实测否决，见 MEMORY。
        #
        # 最终：**兜底默认执行**。它是唯一靠「歌词定位」而非「文本相似度」的判据，
        # 噪声天然被 locate 过滤（实测 1174 首里只 108 首入围粗筛、12 首进精判）。
        # 代价：每场约 17 分钟（首次；歌词走 _media_cache，第二次起几乎不联网）。
        # --no-scan 仍可显式关闭。
        if not args.no_scan:
            SC.log("启动曲库全量粗筛兜底（常规候选已通过 %d 首，LLM singing=%s）"
                   % (len(ok_items), singing))
            known = {lyric_locate.norm(i["title"]) for i in ok_items}
            known_spans = [(i["rough_start"], i["rough_end"]) for i in ok_items]
            for c in library_fallback(entries, workdir, log=SC.log):
                # 与已识别歌曲去重：同一首歌的重复版/同曲不同版本不重复出片
                if lyric_locate.norm(c["name"]) in known:
                    continue
                item, ok = verify_candidate(c, entries, workdir)
                if not ok:
                    rep["rejected"].append(
                        {"title": item["title"], "source": item["source"],
                         "why": "；".join(item["notes"]) or "核验未通过"})
                    continue
                # 时间轴重叠检查：兜底常会把同一段识别成不同歌名
                if any(_overlaps(item["rough_start"], item["rough_end"], s, e)
                       for s, e in known_spans):
                    SC.log("  ✗ %s 区间与已识别歌曲重叠，跳过" % item["title"])
                    rep["rejected"].append(
                        {"title": item["title"], "source": item["source"],
                         "why": "演唱区间与已识别歌曲重叠（同一段被识别成不同歌名）"})
                    continue
                ok_items.append(item)
                known.add(lyric_locate.norm(c["name"]))
                known_spans.append((item["rough_start"], item["rough_end"]))
                SC.log("  ✓ %s 核验通过（兜底）：%s~%s（%.2f，原曲 %ss）"
                       % (item["title"], item["rough_start_hms"], item["rough_end_hms"],
                          item["overlap"], item.get("orig_duration_s")))

        for it in dedupe(ok_items):
            it["tag"] = tag
            # 闸门 4b 判为 WARN（非歌词语音接近 BGM 阈值）→ 只标疑似，不自动出片。
            # 与 10-02「泡泡」定调一致：宁可标疑似留人工确认，也不出一片错歌。
            if it.get("gate4_speech") == "WARN":
                it["needs_review"] = True
            verified_all.append(it)

    if args.limit:
        verified_all = verified_all[:args.limit]

    auto_list = [it for it in verified_all if not it.get("needs_review")]
    if auto_list:
        verified_all = auto_list
    elif verified_all:
        rep["singing"]["note"] = ("%d 首核验通过但「非歌词语音占比」接近 BGM 阈值，"
                                  "已全部标为待人工确认，本次不出片" % len(verified_all))

    rep["singing"]["method"] = ("LLM 分块找演唱窗口（只摘唱词不猜歌名）→ 网易云歌词检索定歌名 "
                                "→ 歌词回整场转写定位区间 → 重合率/原曲时长核验")
    if verified_all:
        rep["singing"]["detected"] = True
    elif llm_singing or rep["rejected"]:
        rep["singing"]["detected"] = "疑似"
        rep["singing"]["note"] = "检测到演唱/候选但未能匹配到可核验的歌名，未出片，建议人工复核"
    else:
        rep["singing"]["note"] = "未检测到唱歌环节"

    # ── 7. 出片 ────────────────────────────────────────────────
    if verified_all and not args.no_cut:
        seg_dir = os.path.join(workdir, "segments")
        os.makedirs(seg_dir, exist_ok=True)
        by_tag = {}
        for it in verified_all:
            by_tag.setdefault(it["tag"], []).append(it)
        for tag, items in by_tag.items():
            seg_json = os.path.join(seg_dir, "%s-%s.json" % (date.replace("-", ""), tag))
            io.open(seg_json, "w", encoding="utf-8").write(json.dumps({
                "version": 1, "date": date, "songs": [{
                    "seq": i + 1, "title_guess": it["title"],
                    "start": it["rough_start"], "end": it["rough_end"], "lang": "zh",
                    "confidence": 0.9, "start_hms": it["rough_start_hms"],
                    "end_hms": it["rough_end_hms"], "title_source": "lyric-search",
                    "evidence": "全自动：歌词检索定名 + 转写回定位（重合率 %.2f，原曲 %ss）"
                                % (it.get("overlap") or 0, it.get("orig_duration_s")),
                } for i, it in enumerate(items)]}, ensure_ascii=False, indent=1))
            cmd = [sys.executable, os.path.join(_HERE, "song_cutter.py"),
                   "--date", date, "--workdir", workdir, "--no-kdocs"]
            if args.qc_strict:
                cmd.append("--qc-strict")
            SC.log("调用出片：%s" % " ".join(cmd))
            p = subprocess.run(cmd, capture_output=True, text=True, cwd=workdir,
                               encoding="utf-8", errors="replace")
            for ln in (p.stdout or "")[-2000:].splitlines():
                if ("规格" in ln or "自检" in ln or "✗" in ln or "完成" in ln or "跳过" in ln):
                    SC.log("    | " + ln.strip())
            if p.returncode != 0:
                rep["errors"].append("song_cutter 退出码 %d：%s"
                                     % (p.returncode, (p.stdout or "")[-500:]))
        mf_path = os.path.join(workdir, "cuts", date, "manifest.json")
        if os.path.exists(mf_path):
            mf = json.load(io.open(mf_path, encoding="utf-8"))
            for e in mf.get("songs", []):
                rep["outputs"].append({
                    "title": e.get("title"), "mp4": e.get("mp4"),
                    "duration_s": round((e.get("specs") or {}).get("duration") or 0, 2),
                    "cut_start": e.get("cut_start"), "cut_end": e.get("cut_end"),
                    "specs_ok": e.get("specs_ok"), "qc": e.get("qc"),
                    "orig_duration_s": e.get("orig_duration_s")})
            for it in verified_all:
                for o in rep["outputs"]:
                    if o["title"] and lyric_locate.norm(o["title"]).startswith(
                            lyric_locate.norm(it["title"])):
                        it["output"] = o["mp4"]
                        it["duration_s"] = o["duration_s"]
                        it["qc_passed"] = (o.get("qc") or {}).get("passed")
                        break
    elif verified_all and args.no_cut:
        SC.log("--no-cut：跳过出片")

    rep["songs"] = verified_all
    rep["elapsed_s"] = round(time.time() - t_begin, 1)
    p = write_report(rep_dir, date, rep)
    SC.log("=== 全自动歌切结束：唱歌环节=%s，核验通过 %d 首，出片 %d 个 ==="
           % (rep["singing"]["detected"], len(verified_all), len(rep["outputs"])))
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    print("报告 → %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
