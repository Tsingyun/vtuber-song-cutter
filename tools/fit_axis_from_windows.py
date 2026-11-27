# -*- coding: utf-8 -*-
"""用「逐窗 ASR 文本 + 官方歌词行序」定轴：模糊序列对齐（不要求字对字）。

为什么不用现有的三条路
--------------------------------------------------------------------------------
· SRT 逐句贴行：ASR 长转写的句边界 ≠ 歌词行边界。一句 SRT 常常覆盖「上一行后半 +
  下一行前半」，按句贴行会**漏行 + 间隔翻倍**。11/26《浴室》实测：她唱 8 句副歌，
  SRT 只给 4 个时间点（7.7s 一句 = 两句歌词），轴里 4 行、间隔翻倍；尾部同样，
  用户看到的就是「2:55 之后歌词越拖越后」。
· 字符级 LCS（char_align / retime_lcs）：要求字符完全相同。嘶吼、气声、伴奏掩盖时
  ASR 吐大量错字（浴室覆盖率 18%、易燃 21%），LCS 匹不到就靠插值，等于在猜。
· 官方 LRC + 首尾两点线性映射：两点都取自 SRT 时，把 SRT 的误差放大成**缩放误差**
  （易燃 k=1.00894 → 半首 +1.3s 漂移），且锚点错一点，全片跟着错。

本工具的做法
--------------------------------------------------------------------------------
窗文本本身是可靠的（时间准确），不可靠的只是「字」。所以：
  1. 每行歌词对每个窗算**模糊匹配分**（difflib 最长匹配块长度 / 行字数），
     错一两个字不影响，关键词对得上就行；
  2. 在「行号单调、窗号单调」约束下做 DP 求全局最优路径，重复副歌只能按顺序
     各归各位，不会互相抢；
  3. 起唱时刻取该行首个高分歧窗的起点（窗是滑动的，首字必然出现在窗口前部）；
  4. 输出每行实测时刻、匹配分、以及「低分/未唱」标记 —— 漏唱的句子会被显式标出来，
     不静默插值。

用法
    python tools/fit_axis_from_windows.py \
        --texts _tmp_drift/<dir>/text.json \
        --lyrics _media_cache/_lrc_浴室.json \
        --cut-start 809.0 --pad 1.0 --out segments/ov_浴室.lrc
"""
import argparse
import difflib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CJK_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff]")


def cjk(s):
    return "".join(CJK_RE.findall(s or ""))


def load_lyric_rows(path):
    """官方 LRC / lrc json → [(官方秒, 正文)]，保持行序，去掉纯伴奏标记行。"""
    if path.endswith(".json"):
        d = json.load(open(path, encoding="utf-8"))
        text = d.get("lrc") if isinstance(d, dict) else None
        if not text:
            raise SystemExit("json 里没有 lrc 字段：%s" % path)
    else:
        text = open(path, encoding="utf-8").read()
    rows = []
    for line in text.splitlines():
        ms = re.findall(r"\[(\d{1,3}):(\d{2}(?:\.\d+)?)\]", line)
        if not ms:
            continue
        body = re.sub(r"\[\d{1,3}:\d{2}(?:\.\d+)?\]", "", line)
        body = re.sub(r"<\d{1,3}:\d{2}(?:\.\d+)?>", "", body).strip()
        if not cjk(body):
            continue
        rows.append((int(ms[0][0]) * 60 + float(ms[0][1]), body))
    return rows


def score(i_text, line):
    """模糊匹配分：最长匹配块总长 / 行字数。错一两个字不掉太多，全不对接近 0。"""
    a = cjk(i_text)
    b = cjk(line)
    if not a or not b:
        return 0.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    m = sum(blk.size for blk in sm.get_matching_blocks())
    return m / float(len(b))


def fit(texts, rows, hop, win, min_score=0.25):
    """返回 [(行号, 窗号, 分)]，DP 最大化总分且严格单调。"""
    n_win, n_row = len(texts), len(rows)
    sc = [[score(texts[i], rows[j][1]) for i in range(n_win)] for j in range(n_row)]

    NEG = -1e9
    dp = [[NEG] * n_win for _ in range(n_row)]
    bk = [[-1] * n_win for _ in range(n_row)]
    for i in range(n_win):
        dp[0][i] = sc[0][i] if sc[0][i] >= min_score else sc[0][i] - 0.6
    for j in range(1, n_row):
        # 前缀最优：行 j 只能用 > 行 j-1 的窗（允许同窗？不允许：同窗两句会挤在同一时刻）
        best, bidx = NEG, -1
        for i in range(n_win):
            if i > 0 and dp[j - 1][i - 1] > best:
                best, bidx = dp[j - 1][i - 1], i - 1
            v = sc[j][i] if sc[j][i] >= min_score else sc[j][i] - 0.6
            if best > NEG / 2:
                dp[j][i] = v + best
                bk[j][i] = bidx
    # 收尾：最后一行的最优窗
    j = n_row - 1
    i = max(range(n_win), key=lambda k: dp[j][k])
    out = [None] * n_row
    while j >= 0 and i >= 0:
        out[j] = (j, i, sc[j][i])
        i, j = bk[j][i], j - 1
    return out, sc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--texts", required=True, help="逐窗文本 json（数组，按时间序）")
    ap.add_argument("--lyrics", required=True, help="官方 LRC（.lrc 或 _lrc_*.json）")
    ap.add_argument("--hop", type=float, default=0.5)
    ap.add_argument("--win", type=float, default=1.5)
    ap.add_argument("--cut-start", type=float, default=0.0, help="成片起点绝对秒（用于报告绝对时刻）")
    ap.add_argument("--pad", type=float, default=0.0,
                    help="成片片头补静音秒数；输出 override 时会减掉（override 口径不含 pad）")
    ap.add_argument("--out", default="", help="写 override lrc 到这个路径")
    ap.add_argument("--min-score", type=float, default=0.25)
    a = ap.parse_args()

    texts = json.load(open(a.texts, encoding="utf-8"))
    rows = load_lyric_rows(a.lyrics)
    fit_path, sc = fit(texts, rows, a.hop, a.win, a.min_score)

    # 起唱时刻：DP 给的是「最高分窗」，那通常是行中段（字最多、匹配最长）。
    # 真实起唱要往前找该行**首次出现**的窗：向前退，直到分数掉到 30% 以下。
    # 窗是滑动 1.5s，行首字必然同时出现在它之前 1~3 个窗里（视窗内位置）。
    times, firsts = [], []
    for j, (fj, fi, fs) in enumerate(fit_path):
        k = fi
        thr = max(a.min_score, fs * 0.30)
        while k - 1 > (fit_path[j - 1][1] if j else -1) and k - 1 >= 0 and sc[j][k - 1] >= thr:
            k -= 1
        while k + 1 <= fi and k < fi and sc[j][k] < thr:
            k += 1
        firsts.append(k)
        times.append(k * a.hop + a.hop * 0.30)     # 窗内首字大致落在前 1/3

    print("窗 %d 个（hop %.2f / win %.2f）/ 歌词 %d 行" % (len(texts), a.hop, a.win, len(rows)))
    print("\n 行  官方   实测(成片)  间隔   分   文本")
    for (j, i, s), (t_off, body), k, t_film in zip(fit_path, rows, firsts, times):
        flag = "" if s >= 0.55 else ("  ⚠低分" if s >= 0.3 else "  ✗未唱?")
        gap = t_film - times[j - 1] if j else 0.0
        gap_s = "%6.2f" % gap if j else "     -"
        print("%3d %6.2f %8.2f %s  %4.2f  %s%s"
              % (j, t_off, t_film, gap_s, s, body[:22], flag))
    # 单调/间隔体检
    bad = []
    for k in range(1, len(times)):
        if times[k] - times[k - 1] < 0.2:
            bad.append((k, times[k] - times[k - 1]))
    print("\n间隔异常 %d 处%s" % (len(bad), ("：" + str(bad[:5])) if bad else ""))
    low = [(p[0], p[2]) for p in fit_path if p[2] < 0.3]
    print("低分/疑似未唱 %d 行%s" % (len(low), ("：" + str(low)) if low else ""))

    if a.out:
        out = []
        for (t_rel, (_to, body)) in zip(times, rows):
            t = max(0.02, t_rel - a.pad)
            out.append("[%02d:%05.2f]%s" % (int(t // 60), t - int(t // 60) * 60, body))
        open(a.out, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
        print("\n→ %s（%d 行）" % (a.out, len(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
