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
# ⚠ 2026-10-06 由0.80 收紧到 0.55：低于此不再自动出片，只标「疑似·待人工确认」。
#   原 0.80 太松 → 「只是放了首 BGM、她跟着哼两句」也能过（span 通常只有原曲 20~40%）。
DUR_LOWER = 0.55
SPAN_RATIO_MIN = 0.40       # 可定位歌词跨度/原曲 时长下限：低于此判「没真正唱」
# 疑似演唱区间前后 ±N 秒内若无任何转写 → 判为孤立纯音频（更像 BGM；用户提出）
NEAR_CUE_SEC = 60.0
BLOCK_SEC = 2700.0          # LLM 分块长度（45 分钟）
BLOCK_OVERLAP = 180.0
MAX_BLOCKS = 10
MENTION_MAX = 8             # 曲库歌名在整场被提及次数上限（超过视为常用词，不作为候选）
LIB_SCAN_RECALL = 0.30      # 曲库全量粗筛阈值：宁可多选几个，交给 locate 精判
                            # （实测：真歌英文 .57~.76 / 中文 .72，噪声英文 ≤.27 / 中文 ≤.07）
LIB_SCAN_MAX = 1200         # 单场最多扫多少首曲库歌词
LIB_SCAN_TOP = 12           # 粗筛后最多留几个候选做精判

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


def llm_windows(cfg, entries, block=BLOCK_SEC, overlap=BLOCK_OVERLAP, max_blocks=MAX_BLOCKS):
    """分块问 LLM：有没有唱歌段落？有则原样摘出歌词行。返回 [(start, end, [lines])]。"""
    if not entries:
        return []
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
    return out, singing_any


def mention_candidates(entries, lib_names):
    """曲库歌名在转写中被提及 → 补充候选（只作候选，区间仍由歌词定位决定）。"""
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
    """把「LLM 摘出的唱词」和「转写提及的曲库歌名」汇成候选歌名列表。"""
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


def library_fallback(entries, workdir, log=print):
    """兜底候选来源：拿曲库歌词回整场转写粗筛，找出真被唱过的歌名。

    用于「LLM 说有唱歌，但所有候选都核验不过」的场景 —— 说明歌名没能生成，
    而不是没唱。歌词走 _media_cache 缓存，第二次起几乎不联网。
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
    for nm, ar in names[:LIB_SCAN_MAX]:
        lrc, _info = fetch_lrc(nm, workdir, ar)
        if not lrc:
            continue
        scanned += 1
        r, _t, _mode = lyric_locate.fast_screen(idx, lrc)
        if r >= LIB_SCAN_RECALL:
            hits.append((r, nm))
    hits.sort(reverse=True)
    log("曲库全量粗筛：扫描 %d 首歌词，%.2f 以上 %d 首 → %s"
        % (scanned, LIB_SCAN_RECALL, len(hits),
           "、".join(n for _r, n in hits[:8]) or "无"))
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
    # ⇒ 改用两条可靠判据：
    #   4a. ASR 音乐事件标签（自建转写可用；上游 SRT 无此信息 → 恒为 0）
    #   4b. 存在性判据（用户提出：放 BGM 时她通常同时在说话）——
    #       真唱段前后一定有她的说话；孤立的纯音频更像在播放 BGM。
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

    # ---- 闸门 4b：前后说话存在性 ----
    if entries:
        LO, HI = max(0.0, loc["start"] - NEAR_CUE_SEC), loc["end"] + NEAR_CUE_SEC
        near = [t for (a_, _b_, t) in entries if LO <= a_ <= HI and t.strip()]
        item["nearby_cues"] = len(near)
        item["gate4_near"] = "PASS" if near else "FAIL"
        if not near:
            item["notes"].append(
                "疑似演唱区间前后 %ds 内没有任何转写内容：孤立的纯音频，"
                "更像在播放 BGM 而非主播演唱" % int(NEAR_CUE_SEC))
            return item, False

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
    orig = round(ms / 1000.0, 1) if ms else lyric_locate.song_duration(title, hint or "")
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
        win, singing = ([], False) if args.no_llm else llm_windows(cfg, entries)
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
        if not ok_items and not args.no_scan:
            # 常规候选一个没通过 → 很可能是歌名压根没进候选（英文歌典型：
            # FunASR 歌词乱码 + 歌名从未被字面提及）。反过来拿曲库歌词回转写筛。
            # 注意：不能只在 LLM 的 singing=True 时才兜底 —— GLM 降级后判定极不稳定
            # （同一份转写两次分别给出 singing=True 与 False）。
            SC.log("常规候选 %d 个全部未通过（LLM singing=%s）→ 启动曲库全量粗筛兜底"
                   % (len(cands), singing))
            for c in library_fallback(entries, workdir, log=SC.log):
                item, ok = verify_candidate(c, entries, workdir)
                if ok:
                    ok_items.append(item)
                    SC.log("  ✓ %s 核验通过（兜底）：%s~%s（%.2f，原曲 %ss）"
                           % (item["title"], item["rough_start_hms"], item["rough_end_hms"],
                              item["overlap"], item.get("orig_duration_s")))
                else:
                    rep["rejected"].append(
                        {"title": item["title"], "source": item["source"],
                         "why": "；".join(item["notes"]) or "核验未通过"})

        for it in dedupe(ok_items):
            it["tag"] = tag
            verified_all.append(it)

    if args.limit:
        verified_all = verified_all[:args.limit]

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
