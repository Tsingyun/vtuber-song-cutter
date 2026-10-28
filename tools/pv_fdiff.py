#!/usr/bin/env python
"""逐帧差异曲线：用来判断「切镜/过渡是否生硬」，不看绝对画质。

思路：把成片解码成 160x90 灰度，算相邻帧的平均绝对差（0~255）。这条曲线有两个用途：
  1. 切镜处会有尖峰 —— 尖峰过高说明硬切（观众感到「跳」）；
  2. 镜头内部若长期接近 0 —— 说明这一镜根本不动（幻灯片），这才是「生硬」的主因。

    python _tmp/pv/fdiff.py --mp4 out.mp4 [--sb _tmp/pv/sb_full.json] [--fps 60]

输出：整体分位数 + 尖峰列表（对齐到编排稿的镜头边界）。
"""
import argparse
import json
import subprocess
import sys

import numpy as np

FF = "D:/ffmpeg/bin/ffmpeg.exe"
W, H = 160, 90


def frames(mp4, fps):
    cmd = [FF, "-v", "error", "-i", mp4, "-vf", "fps=%g,scale=%d:%d,format=gray" % (fps, W, H),
           "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    n = W * H
    while True:
        b = p.stdout.read(n)
        if len(b) < n:
            break
        yield np.frombuffer(b, dtype=np.uint8).reshape(H, W).astype(np.int16)
    p.stdout.close()
    p.wait()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mp4", required=True)
    ap.add_argument("--sb", default="")
    ap.add_argument("--fps", type=float, default=60)
    ap.add_argument("--top", type=int, default=12)
    a = ap.parse_args()

    ds, ds_full = [], []
    prev = None
    for f in frames(a.mp4, a.fps):
        if prev is not None:
            ds_full.append(float(np.abs(f - prev).mean()))
            # 内容区差异：底色平移后颜色不变，只有字/人物/标记的像素会产生真实差异。
            # 用**两帧的内容并集**做掩膜，避免入场时内容从无到有把分母抹成 0。
            base = float(np.median(f))
            m = (np.abs(f - base) > 25) | (np.abs(prev - base) > 25)
            k = int(m.sum())
            ds.append(float(np.abs(f - prev)[m].mean()) if k >= 12 else 0.0)
        prev = f
    if not ds:
        print("[err] 没有解出帧")
        return 1
    d = np.array(ds)
    with open(a.mp4 + ".fdiff.npy", "wb") as fh:
        np.save(fh, d)
    print("[fdiff] 帧数 %d  时长 %.2fs" % (len(d) + 1, (len(d) + 1) / a.fps))
    print("[fdiff] 内容区 mean %.2f  p50 %.2f  p90 %.2f  p95 %.2f  p99 %.2f  max %.2f"
          % (d.mean(), np.percentile(d, 50), np.percentile(d, 90), np.percentile(d, 95),
             np.percentile(d, 99), d.max()))
    df = np.array(ds_full)
    print("[fdiff] 全画面 mean %.2f  p50 %.2f（平涂底上会被稀释，仅供参考）"
          % (df.mean(), np.percentile(df, 50)))

    # 尖峰：明显高于本片自身的高分位
    thr = max(np.percentile(d, 99.0), d.mean() * 3.0)
    idx = np.where(d >= thr)[0]
    # 合并相邻（同一次切镜会连续 2~3 帧高）
    groups = []
    for i in idx:
        if groups and i - groups[-1][-1] <= 2:
            groups[-1].append(i)
        else:
            groups.append([i])
    groups = sorted(groups, key=lambda g: -max(d[g]))[: a.top]
    groups.sort(key=lambda g: g[0])
    print("[peak] 阈值 %.2f，尖峰 %d 处（列前 %d）" % (thr, len(idx), min(a.top, len(groups))))
    for g in groups:
        t = (g[0] + 1) / a.fps
        print("   %7.2fs  max %5.2f  (%d 帧)" % (t, max(d[g]), len(g)))

    if a.sb:
        sb = json.load(open(a.sb, encoding="utf-8"))
        cuts = [s["t0"] for s in sb.get("shots", [])][1:]
        print("[cut] 镜头边界处（边界前 0.05s ~ 后 0.35s 内）的最大帧差：")
        for c in cuts:
            lo = int((c - 0.05) * a.fps) - 1
            hi = int((c + 0.35) * a.fps) - 1
            lo = max(0, lo); hi = min(len(d) - 1, hi)
            seg = d[lo:hi + 1]
            if len(seg):
                print("   %7.2fs  max %5.2f  mean %5.2f" % (c, seg.max(), seg.mean()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
