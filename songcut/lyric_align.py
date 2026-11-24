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


# ---------------------------- 分段对齐（清唱 / 速度比不为 1） ----------------------------

def _cjk(t):
    return "".join(ch for ch in str(t) if "\u4e00" <= ch <= "\u9fff")


_LA_RUN = re.compile(r"(?:la)+")


def _ono_form(t):
    """拟声词归一：`La-la-la` / `啦啦啦` / `哩哩哩` / `leap frog` 一律折成 `la`。

    歌词站写的是拉丁 "La-la-la"，而 ASR 一律转成「啦啦啦」「哩哩哩」，
    旧版 `_cjk` 把拉丁全剔掉后行文本变成空串 → 整首一半的 La 行永远配不上锚点
    → 只能做长距离插值（2026-11-24《小跳蛙》L24 因此漂了 8s）。
    """
    s = str(t).lower()
    s = re.sub(r"leap\s*frog", "la", s)
    s = s.replace("啦", "la").replace("哩", "la")
    s = re.sub(r"[^a-z\u4e00-\u9fff]+", "", s)
    return _LA_RUN.sub("la", s)



def _lcs_len(a, b):
    """最长公共子串长度（行长短，O(n*m) 足够）。"""
    if not a or not b:
        return 0
    prev, best = [0] * (len(b) + 1), 0
    for ca in a:
        cur = [0] * (len(b) + 1)
        for k, cb in enumerate(b, 1):
            if ca == cb:
                cur[k] = prev[k - 1] + 1
                if cur[k] > best:
                    best = cur[k]
        prev = cur
    return best


def _pair_score(line_txt, seg_txt, min_lcs=None):
    """一行歌词与一句 ASR 的匹配分：归一化后取 LCS / min(两串长)。

    2026-11-24 起改用 `_ono_form`（保留拟声词并折叠），不再只比中文字；
    短 ASR 句（「友情」3 字、「啦啦啦」）也允许作锚点，门槛随长度自适应。
    """
    a, b = _ono_form(line_txt), _ono_form(seg_txt)
    if len(a) < 2 or len(b) < 1:
        return 0.0
    n = min(len(a), len(b))
    need = min_lcs if min_lcs is not None else (2 if n <= 6 else 4)
    l = _lcs_len(a, b)
    if l < need:
        return 0.0
    # 必须至少有一个**汉字**参与匹配：纯「啦啦啦 / La-la-la」之间互相都能 100% 配中，
    # 一旦允许它们当锚点，一段 7s 的「啦啦啦」会把后面 4 行 La 全吸进去
    # （2026-11-24 实测：18 行被压进 7 秒，比原来的漂移更糟）。
    # 纯拟声行一律不产锚点，交给两侧汉字锚点插值。
    if _lcs_len(_cjk(line_txt), _cjk(seg_txt)) < 1:
        return 0.0
    return l / float(n)


def _assign_lines_to_segs(lines, seg, min_score=0.35, lookahead=9):
    """贪心单调配对：一句 ASR 吃掉紧随其后的若干行歌词。

    ASR 会把 4~6 行歌词并成一句（22s 一条），若只给「最像的那一行」锚点，
    首段就会锚到最后一行、把前几行挤成 0 秒（2026-11-24《小跳蛙》实测）。
    返回 [(seg 下标, [行下标...])]，两侧下标均严格递增。
    """
    out, li = [], 0
    for sj, (s, e, txt) in enumerate(seg):
        got, k = [], li
        while k < min(len(lines), li + lookahead):
            if _pair_score(lines[k][1], txt) >= min_score:
                got.append(k)
                k += 1
            elif got:
                break
            else:
                k += 1
        if got:
            out.append((sj, got))
            li = got[-1] + 1
    return out


def align_lrc_piecewise(lrc, srt_entries, cut_start, cut_end):
    """分段对齐：ASR 锚点 + 句内铺开 + 句间线性插值，逐行平移（行级 + 逐字）。

    返回 (aligned_lrc, info)；锚点不足 3 个返回 (None, info) 交由上游降级。
    """
    info = {}
    lines = _parse_lrc(lrc)
    if not lines:
        info["summary"] = "无有效歌词行"
        return None, info
    seg = [(s, e, txt) for (s, e, txt) in (srt_entries or [])
           if cut_start - 15 <= s <= cut_end + 25]
    if len(seg) < 3:
        info["summary"] = "片段内 ASR 行不足（%d）" % len(seg)
        return None, info
    anchors = []
    for sj, idxs in _assign_lines_to_segs(lines, seg):
        s, e, _t = seg[sj]
        # 段内按「原曲节奏」铺开，而不是按行数均分：一句 ASR 吞 4~7 行时，
        # 各行字数/长短本来就不等，均分会把长句压短、短句拉长并在段尾累积漂移。
        t0 = lines[idxs[0]][0]
        if len(idxs) > 1:
            t1 = lines[idxs[-1]][0]
            d_last = (t1 - t0) / float(len(idxs) - 1)   # 末行按平均行距估宽，避免落在段尾
            span = max(1e-6, (t1 - t0) + d_last)
        else:
            span = max(1e-6, e - s)
        for li in idxs:
            anchors.append((lines[li][0],
                            s + (lines[li][0] - t0) / span * (e - s)))
    info["anchors"] = len(anchors)
    # 锚点空窗体检：两锚点之间空得越久，中间行越只能靠插值猜。
    # 空窗 > 15s 一律记进 info，供上游决定是否放行（RULES §150）。
    if len(anchors) >= 2:
        gaps = [(anchors[k + 1][1] - anchors[k][1], anchors[k][1])
                for k in range(len(anchors) - 1)]
        g_max, g_at = max(gaps)
        info["max_gap_s"] = round(g_max, 1)
        info["max_gap_at"] = round(g_at - cut_start, 1)
    if len(anchors) < 3:
        info["summary"] = "锚点仅 %d 个，不足以分段" % len(anchors)
        return None, info
    clean = []
    for t, a in anchors:                       # 保证两侧都严格单调
        if clean and (a <= clean[-1][1] or t <= clean[-1][0]):
            continue
        clean.append((t, a))
    anchors = clean
    dur = cut_end - cut_start

    def _slope(a, b):
        return min(2.0, max(0.5, (b[1] - a[1]) / max(1e-6, b[0] - a[0])))

    def to_abs(t):
        if t <= anchors[0][0]:
            k = _slope(anchors[0], anchors[1]) if len(anchors) > 1 else 1.0
            return anchors[0][1] - (anchors[0][0] - t) * k
        if t >= anchors[-1][0]:
            k = _slope(anchors[-2], anchors[-1]) if len(anchors) > 1 else 1.0
            return anchors[-1][1] + (t - anchors[-1][0]) * k
        for k in range(len(anchors) - 1):
            (t0, a0), (t1, a1) = anchors[k], anchors[k + 1]
            if t0 <= t <= t1:
                return a0 + (t - t0) / max(1e-6, t1 - t0) * (a1 - a0)
        return anchors[-1][1]

    out, last, kept = [], -1.0, 0
    rows = []                                  # [(行文本, 原时间, 新时间)]
    for line in (lrc or "").splitlines():
        if META_TAG.match(line.strip()):
            continue
        m = TIME_TAG.search(line)
        if not m:
            if not re.search(r"<\d{1,3}:\d{2}", line):
                out.append("")
            continue
        t_old = _tag_time(m)
        rows.append([line, t_old, to_abs(t_old) - cut_start])
    # 片尾兜底：插值/外推可能越过片长，不能简单截断——那会把末尾若干行压成同一时刻
    # （2026-11-24《小跳蛙》实测 5 行全落在 139.40s，画面上等于整段闪一下）。
    # 改成把越界的那一撮在「上一行 ~ 片尾前 0.3s」之间均分。
    limit = max(0.05, dur - 0.30)
    over = [k for k, r in enumerate(rows) if r[2] > limit]
    if over:
        p = over[0]
        base = rows[p - 1][2] if p > 0 else 0.0
        n = len(rows) - p
        # 片尾这一撮至少每行 0.55s（QC L05 的 BLOCK 线是 0.4s），够了再往外铺
        limit = min(max(0.05, dur - 0.25), max(base + 0.55 * n, dur - 1.0))
        if limit <= base:
            limit = min(max(0.05, dur - 0.10), base + 0.55 * n)
        for k in range(p, len(rows)):
            rows[k][2] = base + (limit - base) * ((k - p + 1) / float(n))
    for line, t_old, t_new in rows:
        t_new = max(last + 0.02, t_new)
        last = t_new
        kept += 1
        d = t_new - t_old
        out.append(TIME_TAG.sub(lambda mm: _shift_tag(mm, 1.0, d), line))
    info["summary"] = ("分段对齐 %d 行（%d 个锚点，句内铺开 + 句间插值）"
                       % (kept, len(anchors)))
    return "\n".join(out).strip(), info


def audit_timeline(lrc, srt_entries, cut_start, head_pad=0.0, max_seg=6.0,
                   min_score=0.45):
    """时间轴体检：用「短 ASR 句」当独立裁判，判断歌词行与真实演唱是否对得上。

    原理：VAD 短句（<=6s）的起止是语音活动真值，与任何对齐算法无关。
    取句中点，看该时刻高亮的是哪一行：
      · 文本命中该行              → 通过
      · 文本命中前/后一行         → **错位**（用户看到的「唱到下一句还停在上一句」）
      · 都命不中（ASR 吐垃圾）    → 无法判定，跳过
    返回 {ok, bad, skip, rate, fails}。rate 只统计「判得出」的样本。

    2026-11-24 加此函数：分段对齐把《小跳蛙》L24 漂了 8s 却仍 0 BLOCK 出片，
    QC 只查行时长/重叠、不查「这一刻是否真在唱这一行」。此函数补上这个洞。
    """
    info = {"ok": 0, "bad": 0, "skip": 0, "rate": None, "fails": []}
    rows = _parse_lrc(lrc)
    if len(rows) < 4 or not srt_entries:
        return info

    def _score(a, b):
        a, b = _ono_form(a), _ono_form(b)
        if not a or not b:
            return 0.0
        return _lcs_len(a, b) / float(min(len(a), len(b)))

    end_cut = rows[-1][0]
    for s, e, txt in srt_entries:
        if e - s > max_seg or e - s < 0.4:
            continue
        if not (cut_start - 2.0 <= s <= cut_start + end_cut - head_pad + 6.0):
            continue
        if len(_ono_form(txt)) < 2:
            continue
        tm = (s + e) / 2.0 - cut_start + head_pad
        k = -1
        for i in range(len(rows)):
            if rows[i][0] <= tm:
                k = i
            else:
                break
        if k < 0:
            continue
        cur = _score(rows[k][1], txt)
        if cur >= min_score:
            info["ok"] += 1
            continue
        nb = max(_score(rows[j][1], txt) for j in (k - 1, k + 1)
                 if 0 <= j < len(rows)) if (k > 0 or k + 1 < len(rows)) else 0.0
        if nb >= min_score:
            info["bad"] += 1
            info["fails"].append((round(tm, 1), rows[k][1][:12], txt[:12]))
        else:
            info["skip"] += 1
    judged = info["ok"] + info["bad"]
    info["rate"] = round(info["ok"] / float(judged), 3) if judged else None
    return info


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
