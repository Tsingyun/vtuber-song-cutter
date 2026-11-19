#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""立绘素材体检：alpha 包围盒 / 触边裁切 / 构图形态判定。

只读扫描，不改动源文件。输出 reports/art_audit.json 与 CSV。

用法（路径走参数或环境变量 ART_DIR，不写死在仓库里）：
    set ART_DIR=<立绘素材目录>
    python tools/art_audit.py [<立绘素材目录>]
"""
from __future__ import annotations

import csv
import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

EDGE_BAND = 3          # 触边判定的像素带宽度
ALPHA_MIN = 12         # 视为"有内容"的 alpha 阈值
TOUCH_RATIO = 0.004    # 触边列/行占比超过该值 ⇒ 判为被裁切


def audit(p: Path) -> dict:
    im = Image.open(p)
    mode0, size0 = im.mode, im.size
    if im.mode not in ("RGBA", "LA"):
        im = im.convert("RGBA")
    a = np.array(im.getchannel("A"), dtype=np.uint8)
    h, w = a.shape
    m = a > ALPHA_MIN

    ys, xs = np.nonzero(m)
    if xs.size == 0:
        return dict(name=p.name, path=str(p), w=w, h=h, empty=True)

    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    bw, bh = x1 - x0 + 1, y1 - y0 + 1

    # 四边触边占比（按边长归一）
    colsum = m.sum(axis=0) / max(1, h)
    rowsum = m.sum(axis=1) / max(1, w)
    left = float(colsum[:EDGE_BAND].max())
    right = float(colsum[-EDGE_BAND:].max())
    top = float(rowsum[:EDGE_BAND].max())
    bot = float(rowsum[-EDGE_BAND:].max())

    # 头部（上 12%~32% 区间）的横向跨度，用于判断"竖屏裁切"
    hy0, hy1 = y0 + int(bh * 0.10), y0 + int(bh * 0.34)
    head = m[hy0:hy1]
    hx = np.nonzero(head.any(axis=0))[0]
    head_span = (int(hx.max() - hx.min() + 1) / w) if hx.size else 0.0

    # 底部是否"平切"（半身图下缘是一条直线）：最后 1% 行内宽度接近整幅
    tail = m[max(0, y1 - 2):y1 + 1]
    tail_span = float(tail.any(axis=0).sum() / max(1, bw))

    alpha_cov = float(m.mean())
    bbox_fill = float(m[y0:y1 + 1, x0:x1 + 1].mean())

    # 形态粗判
    ar = bh / max(1, bw)
    if ar > 1.55:
        form = "全身竖构"
    elif ar > 1.15:
        form = "半身竖构"
    elif ar > 0.80:
        form = "近方"
    else:
        form = "横构/特写"

    clipped = left > TOUCH_RATIO or right > TOUCH_RATIO
    return dict(
        name=p.name, path=str(p), w=w, h=h, mode=mode0,
        bbox=[x0, y0, x1, y1], bw=bw, bh=bh,
        margin_l=x0 / w, margin_r=(w - 1 - x1) / w,
        margin_t=y0 / h, margin_b=(h - 1 - y1) / h,
        touch_l=round(left, 4), touch_r=round(right, 4),
        touch_t=round(top, 4), touch_b=round(bot, 4),
        head_span=round(head_span, 3), tail_span=round(tail_span, 3),
        alpha_cov=round(alpha_cov, 4), bbox_fill=round(bbox_fill, 4),
        ar=round(ar, 3), form=form,
        clipped_lr=bool(clipped),
        clipped_any=bool(clipped or top > TOUCH_RATIO or bot > TOUCH_RATIO),
        opaque=bool(a.min() > 250),
        semi=bool(((a > 8) & (a < 200)).mean() > 0.004),  # 有半透明抗锯齿/发丝
    )


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ART_DIR", "")
    if not arg:
        print("用法: python tools/art_audit.py <立绘素材目录>  或先 set ART_DIR=...")
        return 2
    root = Path(arg)
    seen, files = set(), []
    for q in sorted(root.iterdir()):
        if q.suffix.lower() not in (".png", ".webp"):
            continue
        if q.name.lower() in seen:
            continue
        seen.add(q.name.lower())
        files.append(q)
    rows = []
    for i, p in enumerate(files, 1):
        try:
            rows.append(audit(p))
        except Exception as e:  # noqa: BLE001
            rows.append(dict(name=p.name, path=str(p), error=str(e)))
        if i % 40 == 0:
            print(f"  {i}/{len(files)}", flush=True)

    out = Path("reports")
    out.mkdir(exist_ok=True)
    (out / "art_audit.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")

    keys = ["name", "w", "h", "form", "ar", "bw", "bh", "margin_l", "margin_r",
            "touch_l", "touch_r", "head_span", "tail_span", "alpha_cov",
            "bbox_fill", "clipped_lr", "clipped_any", "opaque", "semi", "mode"]
    with (out / "art_audit.csv").open("w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        wr.writeheader()
        for r in rows:
            wr.writerow(r)

    ok = [r for r in rows if "error" not in r and not r.get("empty")]
    tot = len(ok)
    print(f"\n共 {tot} 张有效立绘")
    for k in ("form",):
        from collections import Counter
        print(f"  {k}: {Counter(r[k] for r in ok).most_common()}")
    cl = [r for r in ok if r["clipped_lr"]]
    print(f"  左右被裁切: {len(cl)} ({len(cl)/max(1,tot)*100:.1f}%)")
    noside = [r for r in ok if r["margin_l"] > 0.22 or r["margin_r"] > 0.22]
    print(f"  单侧留白>22%: {len(noside)}")
    op = [r for r in ok if r.get("opaque")]
    print(f"  完全不含透明(alpha=255满幅): {len(op)}")
    sq = [r for r in ok if abs(r["w"] / r["h"] - 1) < 0.02]
    print(f"  正方形画布: {len(sq)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
