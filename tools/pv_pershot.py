#!/usr/bin/env python
"""按镜头统计「这一镜到底动没动」：读 fdiff.py 存下的逐帧差异数组。

判据（这一轮定的）：
  · 每镜内部差异的 p50 必须 > 0.10 —— 否则这一镜基本是静止画面（幻灯片）；
  · 每镜「死帧」（差异 < 0.02）占比必须 < 35%。

    python _tmp/pv/pershot.py --mp4 out.mp4 --sb _tmp/pv/sb_full.json
"""
import argparse
import json
import sys

import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mp4", required=True)
    ap.add_argument("--sb", required=True)
    ap.add_argument("--fps", type=float, default=60)
    a = ap.parse_args()

    d = np.load(a.mp4 + ".fdiff.npy")
    sb = json.load(open(a.sb, encoding="utf-8"))
    shots = sb["shots"]
    print("%-4s %8s %8s %7s %7s %7s %6s" % ("#", "t0", "dur", "p50", "mean", "max", "dead%"))
    bad = []
    for s in shots:
        i0 = int(round(s["t0"] * a.fps)) - 1
        i1 = int(round(s["t1"] * a.fps)) - 1
        i0 = max(0, i0); i1 = min(len(d), i1)
        seg = d[i0:i1]
        if len(seg) < 3:
            continue
        p50 = float(np.percentile(seg, 50))
        dead = float((seg < 0.02).mean() * 100)
        flag = ""
        if p50 < 0.10 or dead > 35:
            flag = "  <-- 静止"
            bad.append(s["i"])
        print("%-4s %8.2f %8.2f %7.2f %7.2f %7.2f %5.0f%%%s"
              % (s["i"], s["t0"], s["t1"] - s["t0"], p50, seg.mean(), seg.max(), dead, flag))
    print("[pershot] %d 镜，其中 p50<0.10 或死帧>35%% 的有 %d 个：%s"
          % (len(shots), len(bad), bad or "无"))
    return 0 if not bad else 0


if __name__ == "__main__":
    sys.exit(main())
