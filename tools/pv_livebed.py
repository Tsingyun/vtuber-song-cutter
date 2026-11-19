# -*- coding: utf-8 -*-
"""把任意一段原录播做成「印在纸上的照片」，用作前奏 / 间奏 / 尾奏的底。

为什么需要：
  JIZURA 的 `interlude` 版式只有一个 glyphLock 装饰 + 几根音量条，在浅灰纸上几乎看不见。
  本片 93.66~106.83（13.17s）和 247.87~269.95（22.08s）都是这个版式 ——
  观众看到的是「十几秒什么都没有」。而这两处音频是有内容的（间奏 −22dB 的乐器）。

  所以间奏/尾奏改走「录播插播」：原画面做双色调 + 网点 + 颗粒 + 缓推，四周留纸色边，
  读作「版面里贴了一张现场照」。和前奏（intro_make.py）同一套处理，只是不叠标题卡。

时间换算：
  录播时间 = 8543.0（mp3 的 0 点）+ 成片时间。这个 8543.0 来自 manifest 的
  cut_start 8544.0 − head_pad 1.0（mp3 开头 1.00s 实测 −180dB 纯静音）。

用法：
  python tools/pv_livebed.py --live 录播.mp4 --offset 8543.0 --at 93.66 --dur 13.17 --out inter_out
  python tools/pv_livebed.py --live 录播.mp4 --offset 8543.0 --at 247.87 --dur 22.08 --out tail_out --fade-out 5
"""
import argparse
import os
import subprocess

import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from songcut import pvlink as PL   # noqa: E402  ffmpeg 与录播源的唯一接入层

# 这里原本写死了某一场录播的绝对路径与偏移（含主播本机目录），
# 既是隐私泄漏，也让这个工具只能在那一场上用。改为必填参数 + pvlink 兜底。
LIVE = ""
OFFSET = 0.0               # 成片 0 点对应的录播时间（= cut_start - head_pad）
FFMPEG = PL.runtime()["ffmpeg"]

W, H = 3840, 2160
FPS = 30
MARGIN = 110               # 纸色边宽（4K 像素）：读作「贴在版面上的一张照片」
PAPER = (208, 207, 203)
FADE_OUT = 0.5             # 末尾淡到纸色的秒数（main 里由 --fade-out 覆盖）

# 源帧裁切：去左侧聊天栏、右侧礼物卡、底部 K 歌进度条
CROP = "crop=1166:990:600:0"


def sm(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def cover(im, w, h, zoom=1.0, fy=0.5):
    iw, ih = im.size
    k = max(w / float(iw), h / float(ih)) * zoom
    sw, sh = max(1, int(round(iw * k))), max(1, int(round(ih * k)))
    im = im.resize((sw, sh), Image.LANCZOS)
    return im.crop(((sw - w) // 2, int((sh - h) * fy),
                    (sw - w) // 2 + w, int((sh - h) * fy) + h))


def duotone(im, lo, hi):
    g = im.convert("L")
    a = np.asarray(g, dtype=np.float32)
    a = (a - lo) / max(1.0, hi - lo) * 255.0
    a = 128.0 + (np.clip(a, 0, 255) - 128.0) * 1.16
    g = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    return ImageOps.colorize(g, black=(24, 22, 20), white=(216, 214, 207))


def build_masks(w, h):
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    dot = (np.sin(xx * np.pi / 8.0) ** 2) * (np.sin(yy * np.pi / 8.0) ** 2)
    nx = (xx - w / 2) / (w / 2)
    ny = (yy - h / 2) / (h / 2)
    r = np.sqrt(nx * nx + ny * ny) / 1.4142
    vig = 1.0 - 0.30 * np.clip((r - 0.45) / 0.55, 0, 1) ** 1.8
    return dot, vig


def grab(at, dur, src_dir):
    os.makedirs(src_dir, exist_ok=True)
    n = int(round(dur * FPS))
    cmd = [FFMPEG, "-v", "error", "-y", "-ss", "%.3f" % (at + OFFSET), "-i", LIVE,
           "-t", "%.3f" % (dur + 0.5), "-vf", "fps=%d,%s" % (FPS, CROP),
           "-q:v", "2", os.path.join(src_dir, "%05d.jpg")]
    subprocess.run(cmd, check=True)
    files = sorted(f for f in os.listdir(src_dir) if f.endswith(".jpg"))
    if len(files) < n:
        raise SystemExit("源帧不够：%d < %d" % (len(files), n))
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", required=True, help="录播源 mp4")
    ap.add_argument("--offset", type=float, default=0.0,
                    help="成片 0 点在录播里的秒数（= cut_start - head_pad）；不传就按 --at 直接取")
    ap.add_argument("--at", type=float, required=True, help="成片里的起始秒")
    ap.add_argument("--dur", type=float, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fade-out", type=float, default=0.5, help="末尾淡到纸色的秒数")
    ap.add_argument("--test", action="store_true")
    a = ap.parse_args()

    global LIVE, OFFSET
    LIVE = a.live
    OFFSET = a.offset
    src_dir = os.path.abspath(a.out) + "_src"
    n = grab(a.at, a.dur, src_dir)
    print("[bed] %.2fs 起 %.2fs → %d 帧" % (a.at, a.dur, n))

    # 全段统一黑白场（逐帧算会闪）
    los, his = [], []
    for i in range(0, n, max(1, n // 8)):
        im = Image.open(os.path.join(src_dir, "%05d.jpg" % (i + 1))).convert("L")
        ar = np.asarray(im, dtype=np.float32)
        los.append(float(np.percentile(ar, 2)))
        his.append(float(np.percentile(ar, 98)))
    los.sort(); his.sort()
    lo, hi = los[len(los) // 2], his[len(his) // 2]
    print("[bed] 黑白场 %.0f ~ %.0f" % (lo, hi))

    iw, ih = W - 2 * MARGIN, H - 2 * MARGIN
    dot, vig = build_masks(iw, ih)
    n = int(round(a.dur * FPS))      # 输出帧数精确对齐时间轴
    global FADE_OUT
    FADE_OUT = a.fade_out

    if a.test:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        for k, i in enumerate((int(n * 0.1), int(n * 0.5), int(n * 0.9))):
            render(i, n, src_dir, iw, ih, dot, vig, lo, hi).save(
                a.out + "_t%d.jpg" % k, quality=93)
        print("[bed] 预览 %s_t0..t2.jpg" % a.out)
        return

    os.makedirs(a.out, exist_ok=True)
    for i in range(n):
        render(i, n, src_dir, iw, ih, dot, vig, lo, hi).save(
            os.path.join(a.out, "j_%05d.jpg" % (i + 1)), quality=95, subsampling=1)
        if (i + 1) % 60 == 0:
            print("[bed] %d / %d" % (i + 1, n), flush=True)
    print("[bed] → %s（%d 帧）" % (a.out, n))


def render(i, n, src_dir, iw, ih, dot, vig, lo, hi):
    """第 i 帧：源帧 → 双色调 → 网点/颗粒/渐晕 → 四周贴纸色边 → 两端交叉淡化。"""
    src = Image.open(os.path.join(src_dir, "%05d.jpg" % (i + 1))).convert("RGB")
    p = i / float(max(1, n - 1))
    # 缓推 3%：静中有动
    pic = cover(src, iw, ih, zoom=1.01 + 0.03 * p, fy=0.62)
    pic = duotone(pic, lo, hi)

    arr = np.asarray(pic).astype(np.float32)
    lum = arr.mean(axis=2, keepdims=True) / 255.0
    arr *= (1.0 - 0.11 * dot[..., None] * (1.0 - lum))
    rng = np.random.default_rng(7000 + i)
    arr += rng.normal(0, 3.0, (ih, iw, 1)).astype(np.float32)
    arr *= vig[..., None]
    pic = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

    canvas = Image.new("RGB", (W, H), PAPER)
    canvas.paste(pic, (MARGIN, MARGIN))
    d = ImageDraw.Draw(canvas)          # 细墨线，像照片压边
    d.rectangle((MARGIN, MARGIN, W - MARGIN - 1, H - MARGIN - 1),
                outline=(26, 26, 26), width=4)

    # 首 0.5s 与 JIZURA 的纸色交叉淡化，避免硬切；尾段按 FADE_OUT 长度淡到纸色
    e = 0.5 * FPS
    if i < e:
        canvas = Image.blend(Image.new("RGB", (W, H), PAPER), canvas, sm(i / e))
    fo = max(e, FADE_OUT * FPS)
    if i > n - 1 - fo:
        canvas = Image.blend(canvas, Image.new("RGB", (W, H), PAPER), sm((i - (n - 1 - fo)) / fo))
    return canvas


if __name__ == "__main__":
    main()
