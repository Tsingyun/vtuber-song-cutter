#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成一段**确定性的**「三维场景」图片序列，用于验证 bg3d 层通路。

为什么需要它
------------
`shot.bg3d` 的消费侧只认「目录 + 帧数 + 帧率 + 摆放」，产出侧是不是 Blender 不重要。
但「装没装 Blender」不该决定这一层能不能测 —— 本机没有 Blender 时，三维层的
接入必须仍然可验证，否则它就是一段没人跑过的代码。

这里用 PIL 画一段有透视地面、逐帧推进的网格、以及三层不同视差的前景柱：
足以验证四件事：
  1) 序列帧随时间前进（不是一张静图重复）
  2) 透视/视差真的在动（不同深度位移不同）
  3) 帧率与帧数被编排稿正确消费（时间定位对得上）
  4) cover/contain 裁切与缓慢推移不炸边

要真三维（EEVEE / Cycles / 卡通着色），用 tools/blender_bg3d.py；
两者产出同一种东西（PNG 序列），编排稿不必区分。

用法
----
    python tools/bg3d_seq.py --out _tmp/b3d --sec 6 --fps 12 --w 960 --h 540
    # 然后：
    python tools/pv_prepare.py ... --bg3d _tmp/b3d --bg3d-fps 12
"""
import argparse
import math
import os
import sys

from PIL import Image, ImageDraw


def lerp(a, b, k):
    return a + (b - a) * k


def frame(w, h, t, dur):
    """画第 t 秒的一帧。纯函数：同样的 (t, w, h, dur) 永远得到同一张图。"""
    im = Image.new("RGB", (w, h), (18, 20, 28))
    d = ImageDraw.Draw(im, "RGBA")
    hor = int(h * 0.46)                     # 地平线

    # ── 天空：从夜的蓝到地平线的暖灰（缓慢呼吸，6s 一个周期）
    for y in range(hor):
        k = y / float(hor)
        warm = 0.10 + 0.10 * math.sin(t * 0.9)
        col = (int(lerp(14, 96 + 40 * warm, k)),
               int(lerp(18, 92 + 30 * warm, k)),
               int(lerp(30, 104, k)))
        d.line([(0, y), (w, y)], fill=col)

    # ── 地面
    d.rectangle([0, hor, w, h], fill=(12, 14, 20))

    # ── 透视网格：横向线随时间向观众推进（速度=1 格/秒）
    vx = w * 0.5
    span = dur * 1.0
    for i in range(-6, 26):
        # 每条横线有自己的「深度相位」，t 增大时相位减小 → 视觉上是向镜头来
        ph = ((i / 6.0) - (t / max(0.2, span))) % 1.0
        if ph <= 0.02:
            continue
        y = hor + (h - hor) * (ph ** 2.2)
        if y > h:
            continue
        d.line([(0, y), (w, y)], fill=(70, 120, 150, int(30 + 120 * ph)))

    # ── 纵向线：向灭点收敛
    for k in range(-10, 11):
        x0 = vx + k * w * 0.16
        d.line([(vx, hor), (x0, h)], fill=(60, 100, 130, 70))

    # ── 三个不同深度的柱（视差：越远越慢）
    for depth, (cx, wid, col) in enumerate((
            (0.24, 0.055, (150, 205, 235, 210)),
            (0.62, 0.085, (240, 195, 120, 225)),
            (0.82, 0.125, (200, 120, 150, 235)))):
        speed = 0.06 + 0.10 * depth
        x = ((cx + t * speed) % 1.25) - 0.12
        px = x * w
        ph = h * (0.30 + 0.26 * depth)
        base = hor + (h - hor) * (0.10 + 0.28 * depth)
        d.rectangle([px - wid * w * 0.5, base - ph, px + wid * w * 0.5, base],
                    fill=col)
        # 顶面，让「三维」不是纯色块
        d.polygon([(px - wid * w * 0.5, base - ph),
                   (px - wid * w * 0.28, base - ph - h * 0.035 * (0.6 + depth)),
                   (px + wid * w * 0.72, base - ph - h * 0.035 * (0.6 + depth)),
                   (px + wid * w * 0.5, base - ph)],
                  fill=(255, 255, 255, 40))

    # ── 一颗缓慢横移的月（给「整帧在动」一个参照物）
    mx = w * (0.72 + 0.06 * math.sin(t * 0.5))
    my = h * 0.18
    r = h * 0.055
    d.ellipse([mx - r, my - r, mx + r, my + r], fill=(250, 240, 210, 235))
    return im


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--sec", type=float, default=6.0, help="时长（秒）")
    ap.add_argument("--fps", type=float, default=12.0)
    ap.add_argument("--w", type=int, default=960)
    ap.add_argument("--h", type=int, default=540)
    a = ap.parse_args()

    if a.sec <= 0 or a.fps <= 0:
        raise SystemExit("[bg3d] --sec / --fps 必须 > 0")
    os.makedirs(a.out, exist_ok=True)
    n = int(round(a.sec * a.fps))
    for i in range(n):
        im = frame(a.w, a.h, i / a.fps, a.sec)
        im.save(os.path.join(a.out, "%05d.jpg" % (i + 1)), quality=92,
                optimize=True)
    print("[bg3d] %d 帧 → %s（%dx%d @ %.1ffps，%.1fs）"
          % (n, a.out, a.w, a.h, a.fps, n / a.fps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
