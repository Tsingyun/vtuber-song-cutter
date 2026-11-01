#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把一份编排稿切成一个时间窗口，用于「只重渲 20 秒」的局部验证。

为什么需要它
------------
整片 4K 一轮 28 分钟（12915 帧），而门禁 `pv_flash.py` 报的是**某个切点**。
调一个切点参数却要重渲全片，等于把一次 A/B 试错做成了半天。

切片的正确性取决于三个量一起平移，少一个就会「画面正常但内容错位」：
  · 镜头 t0/t1   -= start
  · fig.tOff     += start      （tOff = -win.start，是**成片时间轴**上的锚点）
  · meta.dur      = 窗口长度
歌词与版式都在镜头里，跟着走；音频由调用方从 start 处截。

用法：
  python tools/pv_slice.py --sb sb.json --out _tmp/slice.json --start 240 --dur 30
"""
import argparse
import json
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sb", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--dur", type=float, required=True)
    ap.add_argument("--pad", type=float, default=1.0,
                    help="窗口两侧各多留多少秒（切点前的转场要看得到）")
    a = ap.parse_args()

    sb = json.load(open(a.sb, encoding="utf-8"))
    w0, w1 = a.start - a.pad, a.start + a.dur + a.pad
    keep = []
    for s in sb["shots"]:
        if s["t1"] <= w0 or s["t0"] >= w1:
            continue
        q = json.loads(json.dumps(s))
        q["t0"] = round(max(0.0, q["t0"] - a.start), 3)
        q["t1"] = round(min(a.dur, q["t1"] - a.start), 3)
        if q["t1"] - q["t0"] < 0.05:
            continue
        f = q.get("fig")
        if f and f.get("tOff") is not None:
            # 保持 (t + tOff) 不变 ⇒ 取到的还是原来那几帧
            f["tOff"] = round(float(f["tOff"]) + a.start, 3)
        keep.append(q)
    if not keep:
        raise SystemExit("[slice] 窗口 %.1f~%.1f 里没有镜头" % (w0, w1))
    for i, q in enumerate(keep):
        q["i"] = i
    # 片段首镜不带上一个镜头，转场改成 fade，否则会去取一个不存在的快照
    keep[0]["trans"] = {"k": "fade", "dur": 0.0}
    sb["shots"] = keep
    sb["meta"]["dur"] = round(a.dur, 3)
    sb["meta"]["start"] = 0.0
    if sb.get("audio"):
        sb["audio"]["dur"] = round(a.dur, 3)
    json.dump(sb, open(a.out, "w", encoding="utf-8"), ensure_ascii=False)
    print("[slice] %d 镜  %.2f~%.2fs  →  %s"
          % (len(keep), keep[0]["t0"], keep[-1]["t1"], a.out))


if __name__ == "__main__":
    sys.exit(main())
