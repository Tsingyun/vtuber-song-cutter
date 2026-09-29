# -*- coding: utf-8 -*-
"""歌词时间轴对齐（歌切渲染用）。

LRC 时间轴来自原曲（LRCLIB/网易云），而主播翻唱的起唱位置 / 伴奏编排与原曲
存在整体偏移（偶有轻微速度差），直接使用会导致歌词显示滞后或超前。
本模块用 ASR 转写（与录播同一时间轴）中的「演唱行」与 LRC 行做模糊覆盖率匹配，
得到若干 (录播时间, LRC时间) 配对；由于主播跟着原曲伴奏唱（速度比恒为 1），
对配对偏移做聚类后取最大聚类中位数作为常数偏移，统一变换：
    切出音频时间 = LRC时间 + (偏移 - cut_start)

匹配不足或完全无匹配时原样返回并携带 info 说明。
所有函数不抛异常，失败降级并携带 info 说明。
"""
import difflib
import re
import statistics

# 同时匹配行级 [mm:ss.xx] 与词级 <mm:ss.xx> 时间标签
TIME_TAG = re.compile(r"([\[<])(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?([\]>])")
META_TAG = re.compile(r"^\[(ti|ar|al|by|offset|kana):", re.I)


def _norm(t):
    """归一化文本：仅保留中/日/韩/英/数字，转小写（ASR 错字容忍靠模糊匹配）。"""
    return re.sub(r"[^\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", "", str(t)).lower()


def _coverage(a, b):
    """a 在 b 中的近似覆盖率（匹配块字符数 / len(a)）。
    ASR 常把多句歌词合并成一条长句且带谐音错字，整行 ratio 会偏低；
    覆盖率只要求「LRC 行的内容近似出现在 ASR 句中」即可命中。"""
    if not a or not b:
        return 0.0
    sm = difflib.SequenceMatcher(None, a, b)
    return sum(bl.size for bl in sm.get_matching_blocks()) / len(a)


def _tag_time(m):
    frac = m.group(4)
    return int(m.group(2)) * 60 + int(m.group(3)) + (float("0." + frac) if frac else 0.0)


def _parse_lrc(lrc):
    """→ [(首标签时间, 行文本, 原始行)]，仅保留带时间戳且有文字的行。"""
    out = []
    for line in (lrc or "").splitlines():
        m = re.search(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]", line)
        if not m:
            continue
        t = int(m.group(1)) * 60 + int(m.group(2)) + (float("0." + m.group(3)) if m.group(3) else 0)
        txt = re.sub(r"\[[^\]]*\]", "", line)
        txt = re.sub(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>", "", txt).strip()
        if txt:
            out.append((t, txt, line))
    out.sort(key=lambda x: x[0])
    return out


def _best_offset(diffs):
    """从配对偏移序列中估计最优常数偏移。
    ASR 分段起点带有 ±2~8s 的量化/合并噪声且可能随时间漂移，直接最小二乘
    或全局中位数都会被带偏。改为：排序后按 >2.5s 间隙聚类，取最大聚类
    （最一致的一批配对）的中位数——抗离群、抗漂移。"""
    vals = sorted(diffs)
    clusters, cur = [], [vals[0]]
    for v in vals[1:]:
        if v - cur[-1] <= 2.5:
            cur.append(v)
        else:
            clusters.append(cur)
            cur = [v]
    clusters.append(cur)
    best = max(clusters, key=len)
    return statistics.median(best), len(best), len(clusters)


def _shift_tag(m, a, delta):
    """单个时间标签整体位移：t' = a*t + delta，保留原括号类型。"""
    t = int(m.group(2)) * 60 + int(m.group(3)) + (float("0." + m.group(4)) if m.group(4) else 0)
    nt = max(0.0, a * t + delta)
    mm = int(nt // 60)
    ss = nt - mm * 60
    return "%s%02d:%05.2f%s" % (m.group(1), mm, ss, m.group(5))


def align_lrc(lrc, srt_entries, cut_start, cut_end):
    """对齐 LRC 到切出音频 0 点。
    @param lrc          原始/清洗后的 LRC 文本
    @param srt_entries  [(start_sec, end_sec, text)]，录播时间轴（秒）
    @param cut_start    波形精修切点起点（录播时间轴秒）
    @param cut_end      波形精修切点终点
    @return (aligned_lrc, info dict)；info.summary 供日志输出
    """
    info = {}
    lines = _parse_lrc(lrc)
    if not lines:
        info["summary"] = "LRC 无有效时间轴，跳过对齐"
        return lrc, info
    seg = [(s, e, txt) for (s, e, txt) in (srt_entries or [])
           if cut_start - 12 <= s <= cut_end + 20]
    if not seg:
        info["summary"] = "片段内无 ASR 行，跳过对齐"
        return lrc, info

    # 1. 顺序匹配：每条 ASR 句在「未消费的 LRC 行」窗口里找第一条被其覆盖的行。
    #    ASR 句起点 ≈ 其覆盖的第一条歌词行的演唱时刻 → 配对 (ASR时间, LRC时间)。
    pairs = []
    n = len(lines)
    li = 0                                   # 下一个待匹配的 LRC 行下标（单调推进）
    for s, e, txt in sorted(seg, key=lambda x: x[0]):
        nt = _norm(txt)
        if len(nt) < 2:
            continue
        hit = None
        for i in range(li, min(n, li + 8)):
            if _coverage(_norm(lines[i][1]), nt) >= 0.55:
                hit = i
                break
        if hit is not None:
            pairs.append((s, lines[hit][0]))
            li = hit + 1
            if li >= n:
                break
    # 2. 合理性过滤：映射到切出音频后的相对时间差应在 90s 内（s 为录播绝对时间，
    #    不能直接与 LRC 时间比较，必须先减去切点）
    mono = [(s, t) for (s, t) in pairs if abs((s - cut_start) - t) <= 90]

    info["matched"] = len(mono)
    info["lrc_lines"] = len(lines)
    if mono:
        # 主播跟着原曲伴奏唱 → 速度比恒为 1，歌词整体只需一个常数偏移。
        # 用聚类中位数估计偏移（抗 ASR 分段噪声与漂移）。
        a = 1.0
        b, cluster_n, cluster_total = _best_offset([s - t for s, t in mono])
        rms = (sum(((s - t) - b) ** 2 for s, t in mono if abs((s - t) - b) <= 2.5)
               / max(1, cluster_n)) ** 0.5
        info.update(offset_in_cut=round(b - cut_start, 2), cluster=cluster_n, rms=round(rms, 2))
        info["summary"] = ("匹配 %d/%d 行 · 偏移 %+.2fs（%d/%d 一致）· RMS=%.2fs" % (
            len(mono), len(lines), b - cut_start, cluster_n, cluster_total, rms))
    else:
        a, b = 1.0, 0.0
        info["summary"] = "ASR 与 LRC 无匹配行，保持原时间轴（%d 行）" % len(lines)

    if a == 1.0 and b == 0.0:
        return lrc, info

    # 3. 全量时间标签位移（行级 + 词级统一处理），去掉元数据标签，保留段落空行
    delta = b - cut_start
    out_lines = []
    for line in (lrc or "").splitlines():
        if META_TAG.match(line.strip()):
            continue
        if not re.search(r"\[(\d{1,3}):(\d{2})", line) and not re.search(
                r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>", line):
            out_lines.append("")              # 段落空行原样保留
            continue
        out_lines.append(TIME_TAG.sub(lambda m: _shift_tag(m, a, delta), line))
    return "\n".join(out_lines).strip(), info


if __name__ == "__main__":
    import json
    import sys
    # 简易自测：python lyric_align.py <lrc文件> <cut_start> <cut_end> <srt文件>
    lrc_text = io_open = open(sys.argv[1], encoding="utf-8").read()
    cs, ce = float(sys.argv[2]), float(sys.argv[3])
    srt = open(sys.argv[4], encoding="utf-8-sig").read()
    pat = re.compile(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\s*\n(.*?)(?=\n\n|\Z)", re.S)

    def h2s(x):
        p = [q.replace(",", ".") for q in x.strip().split(":")]
        return int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2]) if len(p) == 3 else float(p[0])
    entries = [(h2s(m.group(1)), h2s(m.group(2)), m.group(3).strip())
               for m in pat.finditer(srt) if m.group(3).strip()]
    aligned, inf = align_lrc(lrc_text, entries, cs, ce)
    print(json.dumps(inf, ensure_ascii=False))
    print("--- 前 8 行 ---")
    print("\n".join(aligned.splitlines()[:8]))
