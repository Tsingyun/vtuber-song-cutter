#!/usr/bin/env python
"""逐帧接触表：把一段连续帧铺成网格，每格标注帧号与时间。

为什么需要它：闪帧只有 1~3 帧，pv_stills 抽的是镜头中点，pv_trans 抽的是转场四相，
两者都会漏掉「中间插进来的一张画」。要判断「这三帧是不是同一张画」，
必须看**连续帧**，而且不能缩放——缩略图会把 1 像素的位移吃掉。

用法：
    python tools/pv_strip.py --mp4 x.mp4 --t0 11.60 --n 18 [--cols 6] [--w 640] [--out a.png]
    python tools/pv_strip.py --mp4 x.mp4 --frame 352 --n 12

输出：接触表 PNG + 每帧与前一帧的差异（打印在终端，也印在格子上）。
"""
import argparse
import os
import subprocess
import sys

from PIL import Image, ImageDraw

FF = "D:/ffmpeg/bin/ffmpeg.exe"


def grab(mp4, t0, n, fps, w, tmp):
    """抽 n 帧连续帧（按输出帧率对齐），返回路径列表"""
    os.makedirs(tmp, exist_ok=True)
    for f in os.listdir(tmp):
        os.remove(os.path.join(tmp, f))
    cmd = [FF, "-v", "error", "-ss", "%.4f" % t0, "-i", mp4,
           "-frames:v", str(n), "-vf", "fps=%g,scale=%d:-2" % (fps, w),
           os.path.join(tmp, "f%03d.png")]
    subprocess.run(cmd, check=True)
    return [os.path.join(tmp, "f%03d.png" % i) for i in range(1, n + 1)
            if os.path.exists(os.path.join(tmp, "f%03d.png" % i))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mp4", required=True)
    ap.add_argument("--t0", type=float, default=0.0)
    ap.add_argument("--frame", type=int, default=-1)
    ap.add_argument("--n", type=int, default=18)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--cols", type=int, default=6)
    ap.add_argument("--w", type=int, default=640)
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    t0 = a.frame / a.fps if a.frame >= 0 else a.t0
    tmp = os.path.join(os.path.dirname(a.out or "x.png") or ".", "_strip_frames")
    files = grab(a.mp4, t0, a.n, a.fps, a.w, tmp)
    if not files:
        print("[err] 没抽到帧")
        return 1
    base = float(_first_frame_index(a.mp4, t0, a.fps))

    ims = [Image.open(f).convert("RGB") for f in files]
    cw, ch = ims[0].size
    pad, lab = 6, 26
    rows = (len(ims) + a.cols - 1) // a.cols
    sheet = Image.new("RGB", (a.cols * (cw + pad) + pad, rows * (ch + lab + pad) + pad), (24, 24, 26))
    dr = ImageDraw.Draw(sheet)
    prev = None
    print("[strip] t0=%.3fs  帧号 %d..%d  每格 %d×%d" % (t0, base, base + len(ims) - 1, cw, ch))
    for k, im in enumerate(ims):
        c, r = k % a.cols, k // a.cols
        x, y = pad + c * (cw + pad), pad + r * (ch + lab + pad)
        sheet.paste(im, (x, y))
        # 与前一帧的差（缩到 96×54 灰度算，够用且稳）
        if prev is not None:
            g0 = prev.resize((96, 54)).convert("L")
            g1 = im.resize((96, 54)).convert("L")
            d = sum(abs(p - q) for p, q in zip(g0.getdata(), g1.getdata())) / (96 * 54)
        else:
            d = 0.0
        prev = im
        dr.text((x + 3, y + ch + 5), "f%05d  %.3fs  d=%.2f" % (base + k, (base + k) / a.fps, d),
                fill=(235, 235, 240))
        print("   f%05d %.3fs d=%5.2f" % (base + k, (base + k) / a.fps, d))

    out = a.out or os.path.join(tmp, "strip.png")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    sheet.save(out)
    print("[strip] %s" % out)
    return 0


def _first_frame_index(mp4, t0, fps):
    return round(t0 * fps)


if __name__ == "__main__":
    sys.exit(main())
