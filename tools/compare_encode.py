# -*- coding: utf-8 -*-
"""编码路线画质三方比对（同一帧 f3010，4K）：
  源 = 画布直出 PNG（无压缩上限）
  A  = 浏览器 WebCodecs 编码（实测 ~4.2 Mbps，码率参数被忽略）
  B  = ffmpeg libx264 CBR 18 Mbps（本次改造目标路线）
指标：对源帧的 PSNR + 分区 Tenengrad 锐度保留率 + 文字区 1:1 对照图。
"""
import numpy as np
from PIL import Image
import os

import sys
_HERE = os.path.dirname(os.path.abspath(__file__))
T = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(_HERE), "_tmp")
SRC = os.path.join(T, "src", "f3010.png")
A = os.path.join(T, "br_f10.png")     # 浏览器编码
B = os.path.join(T, "ff_f10.png")     # ffmpeg 18 Mbps


def gray(a):
    return 0.299 * a[..., 0] + 0.587 * a[..., 1] + 0.114 * a[..., 2]


def sharp(g):
    gx = np.diff(g, axis=1); gy = np.diff(g, axis=0)
    return float((gx[:-1, :] ** 2).mean() + (gy[:, :-1] ** 2).mean())


def psnr(x, y):
    mse = float(np.mean((x.astype(np.float64) - y.astype(np.float64)) ** 2))
    return 99.0 if mse <= 1e-12 else 10 * np.log10(255.0 ** 2 / mse)


s = np.asarray(Image.open(SRC).convert("RGB")).astype(np.float32)
a = np.asarray(Image.open(A).convert("RGB")).astype(np.float32)
b = np.asarray(Image.open(B).convert("RGB")).astype(np.float32)
print(f"尺寸 源 {s.shape[1]}x{s.shape[0]}  A {a.shape[1]}x{a.shape[0]}  B {b.shape[1]}x{b.shape[0]}")

print("\n=== 全画面 PSNR（对源帧）===")
print(f"  A 浏览器编码       {psnr(s, a):6.2f} dB")
print(f"  B ffmpeg 18Mbps    {psnr(s, b):6.2f} dB   （提升 {psnr(s, b) - psnr(s, a):+.2f} dB）")

# 设计坐标 ×2 → 4K 像素
Z = {
    "右下角日期":   (1700, 1042, 1840, 1070),
    "左下角档案号": (88, 1042, 500, 1070),
    "歌词当前行":   (980, 320, 1820, 420),
    "标题区":       (88, 150, 700, 300),
    "底部进度条":   (0, 1000, 1920, 1040),
}
gs = gray(s); ga = gray(a); gb = gray(b)
print("\n=== 分区锐度（Tenengrad，越大越锐）与保留率 ===")
print(f"  {'区域':<14}{'源':>10}{'A(浏览器)':>12}{'保留':>8}{'B(ffmpeg)':>12}{'保留':>8}")
for n, (x0, y0, x1, y1) in Z.items():
    X0, Y0, X1, Y1 = x0 * 2, y0 * 2, x1 * 2, y1 * 2
    ss = sharp(gs[Y0:Y1, X0:X1]); sa = sharp(ga[Y0:Y1, X0:X1]); sb = sharp(gb[Y0:Y1, X0:X1])
    print(f"  {n:<14}{ss:10.2f}{sa:12.2f}{100*sa/max(ss,1e-9):7.0f}%{sb:12.2f}{100*sb/max(ss,1e-9):7.0f}%")

print("\n=== 文字区像素差（对源帧）===")
print(f"  {'区域':<14}{'A 均值':>9}{'A p99':>8}{'B 均值':>9}{'B p99':>8}")
for n, (x0, y0, x1, y1) in Z.items():
    X0, Y0, X1, Y1 = x0 * 2, y0 * 2, x1 * 2, y1 * 2
    da = np.abs(s[Y0:Y1, X0:X1] - a[Y0:Y1, X0:X1]).max(axis=2)
    db = np.abs(s[Y0:Y1, X0:X1] - b[Y0:Y1, X0:X1]).max(axis=2)
    print(f"  {n:<14}{da.mean():9.2f}{np.percentile(da,99):8.1f}{db.mean():9.2f}{np.percentile(db,99):8.1f}")

# ── 1:1 对照图：源 / 浏览器 / ffmpeg 三层堆叠 ──
print("\n=== 对照图（1:1 像素，上=源 中=浏览器编码 下=ffmpeg 18Mbps）===")


def strip(name, box, gap=6):
    x0, y0, x1, y1 = [v * 2 for v in box]
    w, h = x1 - x0, y1 - y0
    tiles = [Image.open(p).convert("RGB").crop((x0, y0, x1, y1)) for p in (SRC, A, B)]
    cv = Image.new("RGB", (w, h * 3 + gap * 2), (255, 0, 0))
    for i, t in enumerate(tiles):
        cv.paste(t, (0, i * (h + gap)))
    p = os.path.join(T, f"enc_{name}.png")
    cv.save(p)
    print(f"  {p}  ({w}x{h} ×3)")


strip("date", (1690, 1040, 1850, 1072))
strip("footer_left", (88, 1040, 560, 1072))
strip("lyric", (980, 330, 1500, 380))
print("\n完成")
