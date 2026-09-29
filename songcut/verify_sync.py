# -*- coding: utf-8 -*-
"""歌词同步偏差验证器（批量回归用）。

裁判 = vocal_activity.py 的「人声乐句起点」——纯音频、全程adium无文字参与，
与任何对齐算法（ASR / DTW / whisper forced-align）都独立，可以用来打分量产的每一首。

做法是「单调锚点匹配」：一行歌词最多配一个人声乐句、一对一且顺序不变；
只有当歌词行前面存在真实的换气间隙时，该行才算**可验证锚点**
（连唱 legato 的句子处于同一个乐句里，单点精度无法测量，必须排除，否则会虚报良好）。
"""
import argparse
import io
import json
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vocal_activity as VA   # noqa: E402


def parse_lrc(text_or_path):
    if os.path.exists(text_or_path) and not text_or_path.startswith("["):
        raw = io.open(text_or_path, encoding="utf-8").read()
        if text_or_path.endswith(".json"):
            j = json.loads(raw)
            raw = j["lrc"] if isinstance(j, dict) else "\n".join(j)
    else:
        raw = text_or_path
    out = []
    for line in raw.splitlines():
        m = re.match(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\](.*)$", line)
        if not m:
            continue
        t = int(m.group(1)) * 60 + int(m.group(2)) + \
            (float("0." + m.group(3)) if m.group(3) else 0.0)
        txt = re.sub(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>", "", m.group(4)).strip()
        if txt:
            out.append({"t": t, "text": txt})
    out.sort(key=lambda d: d["t"])
    return out


def match_anchors(lines, runs, win=2.5, skip_line=6.0, skip_run=3.0, gap_min=0.45):
    """单调一对一匹配。返回 list[(行号, 乐句号 or None, 偏差 or None, 是否锚点)]"""
    NL, NR = len(lines), len(runs)
    lt = [l["t"] for l in lines]
    rs = [r[0] for r in runs]
    INF = 1e9
    dp = np.full((NL + 1, NR + 1), INF)
    bt = np.zeros((NL + 1, NR + 1), np.int8)
    for j in range(NR + 1):
        dp[NL][j] = 0.0
    for i in range(NL - 1, -1, -1):
        for j in range(NR - 1, -1, -1):
            dt = abs(rs[j] - lt[i])
            c1 = dp[i + 1][j + 1] + (dt if dt <= win else win + 8.0)
            c2 = dp[i + 1][j] + skip_line
            c3 = dp[i][j + 1] + skip_run
            best = min((c1, 1), (c2, 2), (c3, 3), key=lambda kv: kv[0])
            dp[i][j], bt[i][j] = best[0], best[1]
    pairs = [None] * NL
    i = j = 0
    while i < NL and j < NR:
        k = bt[i][j]
        if k == 1:
            pairs[i] = j
            i, j = i + 1, j + 1
        elif k == 2:
            i += 1
        else:
            j += 1
    out = []
    for i, jn in enumerate(pairs):
        if jn is None:
            out.append((i, None, None, False))
            continue
        d = rs[jn] - lt[i]
        # 是否「可验证锚点」：该乐句之前是否存在真实换气间隙
        prev_end = runs[jn - 1][1] if jn > 0 else 0.0
        gap = rs[jn] - prev_end
        out.append((i, jn, d, gap >= gap_min))
    return out


def report(name, lines, res, verbose=True, runs=None):
    dev_all = [d for (_i, _j, d, _a) in res if d is not None]
    # legato 内部行排除: 行起点深入某乐句 run 内部(>0.35s)时, run 起点
    # 不是该行的真实起唱点, 用它打分属于错误测量 —— 纯音频判据
    runs = runs if runs is not None else []

    def measurable(i, d):
        t = lines[i]["t"]
        for (s, e) in runs:
            if s + 0.35 < t <= e + 0.3:
                return False
        return True

    anch = [(i, d) for (i, j, d, a) in res
            if a and abs(d) <= 2.5 and measurable(i, d)]
    if verbose:
        print("%-4s %-9s %-9s %-8s %-5s %s" % ("行", "LRC起点", "实测起唱", "偏差", "锚点", "文本"))
        for (i, j, d, a) in res:
            if j is None:
                print("%-4d %8.2fs %9s %8s        %s" % (i, lines[i]["t"], "—", "—", lines[i]["text"][:24]))
            else:
                f = "✅" if (a and abs(d) < 0.3) else ("⚠️" if abs(d) < 1.0 else "❌")
                print("%-4d %8.2fs %9.2fs %+7.2fs %-6s %s %s" % (
                    i, lines[i]["t"], VA_runs_first(res, i, d, lines), d,
                    ("锚点" if a else "—"), f, lines[i]["text"][:22]))
    print("\n=== %s ===" % name)
    print("可配对 %d/%d 行；其中可验证锚点 %d 个" % (len(dev_all), len(lines), len(anch)))
    if anch:
        a = np.array([d for (_i, d) in anch])
        print("锚点偏差：MAE %.3fs | 最大 %.2fs | 中位 %+.2fs | 有符号均值 %+.2fs | 标准差 %.2fs"
              % (np.abs(a).mean(), np.abs(a).max(), np.median(a), a.mean(), a.std()))
        print("锚点 |偏差| < 0.30s 的比例：%.0f%%（目标 ≥ 90%%）"
              % (100 * (np.abs(a) < 0.30).mean()))
    return {"n_anchor": len(anch),
            "mae": float(np.abs(a).mean()) if anch else None,
            "max": float(np.abs(a).max()) if anch else None,
            "median": float(np.median(a)) if anch else None,
            "mean": float(a.mean()) if anch else None,
            "std": float(a.std()) if anch else None,
            "pass_ratio": float((np.abs(a) < 0.30).mean()) if anch else None,
            "pass03": int((np.abs(a) < 0.30).sum()) if anch else 0,
            "devs": [round(d, 3) for (_i, d) in anch]}


_V = {}


def VA_runs_first(res, i, d, lines):
    return lines[i]["t"] + d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--lrc", required=True, help="lrc 文本文件或 _lrc_*.json")
    ap.add_argument("--name", default="")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--th", type=float, default=VA.DEFAULT["th_db"])
    ap.add_argument("--no-verbose", action="store_true")
    args = ap.parse_args()

    lines = parse_lrc(args.lrc)
    print("歌词 %d 行" % len(lines))
    x = VA.load_audio(args.audio)
    print("音频 %.1fs" % (len(x) / VA.SR))
    runs, sal, times = VA.detect_phrases(x, th_db=args.th)
    print("人声乐句 %d 个" % len(runs))
    res = match_anchors(lines, runs)
    name = args.name or os.path.basename(args.lrc)
    stat = report(name, lines, res, verbose=not args.no_verbose, runs=runs)
    if args.json_out:
        io.open(args.json_out, "w", encoding="utf-8", newline="").write(json.dumps(
            {"lines": lines, "runs": [list(r) for r in runs], "stat": stat},
            ensure_ascii=False, indent=1))
        print("→", args.json_out)
    # 退出码用于批量回归：未达标返回 1
    if stat["n_anchor"] < 6 or (stat["pass_ratio"] or 0) < 0.9:
        sys.exit(1 if not args.no_verbose else 0)


if __name__ == "__main__":
    main()
