#!/usr/bin/env python
"""岁己立绘白底抠图：仅删除「与图像边界连通」的近白区域，衣服内部白色不受影响。

用法：
  python tools/sui_matting.py \
      --src "C:/Users/Tsing/Pictures/管人立绘/岁己立绘" \
      --dst "D:/岁己歌切/_media_cache/sui_figs" [--only 8,9] [--thresh 238]

- RGBA 输入（已有透明底）直接缩放另存，不重复抠。
- 非白底的（角落不近白）跳过，由调用方决定进几何框。
- 输出 <原名>_cut.png + 接触表 _matting_sheet.png（棋盘格底，质检用）。
"""
import argparse
import os

import numpy as np
from PIL import Image
from scipy import ndimage


def corners_white(a, thr):
    h, w = a.shape[:2]
    pts = [(2, 2), (2, w - 3), (h - 3, 2), (h - 3, w - 3)]
    return all(min(a[y, x][:3]) > thr - 8 for y, x in pts)


def cut_white(im, thr):
    """边界连通的近白像素 → alpha 0；羽化 1px。"""
    a = np.asarray(im.convert("RGB"))
    h, w = a.shape[:2]
    near_white = (a.min(axis=2) >= thr)
    lbl, n = ndimage.label(near_white)
    border = set(np.unique(np.concatenate([lbl[0], lbl[-1], lbl[:, 0], lbl[:, -1]])))
    border.discard(0)
    bg = np.isin(lbl, list(border))
    alpha = np.where(bg, 0, 255).astype(np.uint8)
    # 1px 羽化：对 alpha 做一次均值模糊，边缘不过硬
    alpha = ndimage.uniform_filter(alpha.astype(np.float32), size=3).round().clip(0, 255).astype(np.uint8)
    out = np.dstack([a, alpha])
    return Image.fromarray(out, "RGBA")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--dst", required=True)
    ap.add_argument("--thresh", type=int, default=238)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    os.makedirs(a.dst, exist_ok=True)
    only = set(x.strip() for x in a.only.split(",") if x.strip())

    files = sorted(f for f in os.listdir(a.src) if os.path.isfile(os.path.join(a.src, f)))
    done, skipped = [], []
    for f in files:
        if only and not any(o in f for o in only):
            continue
        im = Image.open(os.path.join(a.src, f))
        stem = os.path.splitext(f)[0]
        out = os.path.join(a.dst, stem + "_cut.png")
        if "A" in im.mode:
            im.save(out)
            done.append((f, "alpha-keep", out))
            continue
        a8 = np.asarray(im.convert("RGB"))
        if not corners_white(a8, a.thresh):
            im.save(out)  # 原样转存，几何框里用
            skipped.append((f, "not-white-bg"))
            continue
        cut = cut_white(im, a.thresh)
        # 超大图限边长 3000（4K 母版上单图版面最大 ~2200px，留余量）
        if max(cut.size) > 3000:
            k = 3000 / max(cut.size)
            cut = cut.resize((round(cut.width * k), round(cut.height * k)), Image.LANCZOS)
        cut.save(out)
        frac = (np.asarray(cut)[:, :, 3] < 8).mean()
        done.append((f, "cut transp=%.0f%%" % (frac * 100), out))

    print("[matting] 完成 %d 张，跳过 %d 张" % (len(done), len(skipped)))
    for f, tag, *rest in done:
        print("  ✓ %-42s %s" % (f[:42], tag))
    for f, tag in skipped:
        print("  · %-42s %s" % (f[:42], tag))

    # 质检接触表（棋盘格底）
    cols, cell = 7, 260
    outs = [x[2] for x in done]
    rows = max(1, (len(outs) + cols - 1) // cols)
    sheet = Image.new("RGB", (cols * cell, rows * (cell + 20)), (235, 235, 235))
    yy, xx = np.mgrid[0:cell, 0:cell]
    chk = (((xx // 16) + (yy // 16)) % 2)[..., None]
    tile = np.where(chk, 205, 255).astype(np.uint8) * np.ones(3, np.uint8)
    from PIL import ImageDraw
    dr = ImageDraw.Draw(sheet)
    for i, p in enumerate(outs):
        im = Image.open(p).convert("RGBA")
        im.thumbnail((cell - 8, cell - 8))
        x, y = (i % cols) * cell, (i // cols) * (cell + 20)
        sheet.paste(Image.fromarray(tile.astype(np.uint8)), (x, y))
        sheet.paste(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2), im)
        dr.text((x + 4, y + cell - 4), os.path.basename(p)[:26], fill=(15, 15, 15))
    sheet.save(os.path.join(a.dst, "_matting_sheet.png"))
    print("[sheet] %s" % os.path.join(a.dst, "_matting_sheet.png"))


if __name__ == "__main__":
    main()
