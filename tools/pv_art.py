# -*- coding: utf-8 -*-
"""立绘素材库 CLI：盘点 / 挑选 / 出接触表。

    python tools/pv_art.py doctor
    python tools/pv_art.py list --kind bust --min-w 900 --top 20
    python tools/pv_art.py pick --kind bust              # 只打印分最高的那张
    python tools/pv_art.py sheet --out _tmp/art            # 出带编号的接触表（人工挑图）

为什么要「接触表」而不是直接自动挑：穿插效果好不好，取决于**她身上的洞在什么高度**。
分数能算出「有没有缝」，算不出「缝是不是正好落在歌词那一行」。所以自动挑只给候选，
最终由人看图定稿 —— 定稿后把文件名写进 --fig-art，渲染就是确定的。
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from songcut import pvlink          # noqa: E402  唯一读口（RULES §120）


def cmd_doctor(a):
    rep = pvlink.art_dir()
    rows = pvlink.art_lib()
    print("[art] 目录 %s（%s）" % (rep, "存在" if os.path.isdir(rep) else "★ 不存在"))
    print("[art] 共 %d 张｜bust %d｜slim %d｜wide %d"
          % (len(rows),
             len([r for r in rows if r["kind"] == "bust"]),
             len([r for r in rows if r["kind"] == "slim"]),
             len([r for r in rows if r["kind"] == "wide"])))
    for r in rows[:5]:
        print("      %-44s %dx%d  边界 %.1f  实心 %.2f  分 %.3f"
              % (r["name"], r["w"], r["h"], r["cuts"], r["mass"], r["score"]))
    return 0


def cmd_list(a):
    rows = pvlink.art_lib(a.kind, min_w=a.min_w)
    rows = rows[:a.top] if a.top else rows
    print(" #   分   边界 实心   尺寸        类型  文件名")
    for i, r in enumerate(rows):
        print("%3d %.3f %5.1f %5.2f %5dx%-5d %-5s %s"
              % (r.get("idx", i), r["score"], r["cuts"], r["mass"],
                 r["w"], r["h"], r["kind"], r["name"]))
    print("（# 号可直接给 --fig-art \"#12\"）")
    return 0


def cmd_pick(a):
    r = pvlink.art_pick(a.kind, min_w=a.min_w)
    if not r:
        print("[art] 没有可用的立绘")
        return 3
    print(r["path"])
    print("[art] %dx%d｜可切边界 %.1f｜实心 %.2f｜分 %.3f"
          % (r["w"], r["h"], r["cuts"], r["mass"], r["score"]), file=sys.stderr)
    return 0


def cmd_sheet(a):
    """接触表：每张缩略图下面标编号 + 尺寸，方便抄 --fig-art "#N"。"""
    from PIL import Image, ImageDraw
    rows = pvlink.art_lib(a.kind, min_w=a.min_w)
    if not rows:
        print("[art] 没有可用立绘")
        return 3
    os.makedirs(a.out, exist_ok=True)
    CELL, COLS, PER = 260, 6, 30
    files = []
    for si in range(0, len(rows), PER):
        chunk = rows[si:si + PER]
        R = (len(chunk) + COLS - 1) // COLS
        sheet = Image.new("RGB", (COLS * CELL, R * (CELL + 18)), (24, 24, 26))
        dr = ImageDraw.Draw(sheet)
        for i, r in enumerate(chunk):
            im = Image.open(r["path"]).convert("RGBA")
            im.thumbnail((CELL - 8, CELL - 8))
            bg = Image.new("RGBA", im.size, (60, 60, 66, 255))
            bg.alpha_composite(im)
            cx, cy = (i % COLS) * CELL, (i // COLS) * (CELL + 18)
            sheet.paste(bg.convert("RGB"), (cx + 4, cy + 4))
            dr.text((cx + 6, cy + CELL - 12),
                    "#%d %.2f %dx%d" % (r.get("idx", si + i), r["score"], r["w"], r["h"]),
                    fill=(255, 220, 120))
        p = os.path.join(a.out, "art_%02d.jpg" % (si // PER))
        sheet.save(p, quality=88)
        files.append(p)
    for p in files:
        print(p)
    return 0


def main():
    ap = argparse.ArgumentParser(description="岁己立绘素材库（只读）")
    sub = ap.add_subparsers(dest="cmd")
    d = sub.add_parser("doctor", help="目录/数量/缓存体检")
    d.set_defaults(fn=cmd_doctor)
    l = sub.add_parser("list", help="列表（按穿插潜力排序）")
    l.add_argument("--kind", default="", choices=["", "bust", "slim", "wide"])
    l.add_argument("--min-w", type=int, default=0)
    l.add_argument("--top", type=int, default=30)
    l.set_defaults(fn=cmd_list)
    p = sub.add_parser("pick", help="自动挑一张（打印路径）")
    p.add_argument("--kind", default="bust", choices=["bust", "slim", "wide", ""])
    p.add_argument("--min-w", type=int, default=700)
    p.set_defaults(fn=cmd_pick)
    s = sub.add_parser("sheet", help="出带编号的接触表")
    s.add_argument("--out", default="_tmp/art")
    s.add_argument("--kind", default="", choices=["", "bust", "slim", "wide"])
    s.add_argument("--min-w", type=int, default=0)
    s.set_defaults(fn=cmd_sheet)
    a = ap.parse_args()
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
