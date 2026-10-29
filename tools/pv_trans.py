#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从**成片**里抽转场帧，做接触表。

为什么要从成片抽，而不是 pv_stills：
  pv_stills 用 __pvShot 单帧渲染，直接跳到镜头中点；而转场（saveCross 的快照 A）
  只是在**顺序播放**到切点那一刻才被拍下来的，单帧渲染根本不经过那个窗口 →
  在 pv_stills 里永远看不到真转场。必须从编码好的 mp4 里取帧。

用法：
  python tools/pv_trans.py --video X.mp4 --sb _tmp/pv/sb.json --spread 9
  python tools/pv_trans.py --video X.mp4 --sb _tmp/pv/sb.json --shots 5,6,7,22

产物：
  <out>/s05_wipe_0.42_10.12.png   单镜四相条（一行四格，含标签）
  <out>/sheet.png                 所有镜的纵向接触表（最后一张给人眼扫）

标签一律 ASCII（避免字体缺字），格式：  #5 wipe .42 | p0 10.02s
"""
import argparse
import json
import os
import subprocess
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FFMPEG = os.environ.get("FFMPEG_BIN", "D:/ffmpeg/bin/ffmpeg.exe")


def die(msg):
    print("[err] " + msg)
    sys.exit(1)


def grab(video, t, out_png, w):
    """抽单帧。输入侧 seek（-ss 在 -i 前）+ 默认 accurate_seek：先跳关键帧再解码到精确位置。"""
    cmd = [
        FFMPEG, "-v", "error", "-ss", "%.3f" % t, "-i", video,
        "-frames:v", "1", "-vf", "scale=%d:-2" % w, "-y", out_png,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out_png):
        return None
    return out_png


def pick_spread(shots, n):
    """按转场种类各挑一个（优先带 fig 的镜，即原片素材镜），凑不超过 n 个。"""
    order, seen = [], set()
    for s in shots:
        tr = s.get("trans")
        if not tr:
            continue
        k = tr.get("k")
        if k in seen:
            continue
        if s.get("fig") or True:                     # 全都要，只要种类没出现过
            seen.add(k)
            order.append(s)
        if len(order) >= n:
            break
    return order


def phases(shot):
    """四相：切点前 1 帧 / 转场 1/3 / 2/3 / 转场结束后。"""
    tr = shot.get("trans") or {}
    dur = float(tr.get("dur") or 0.42)
    t0 = float(shot["t0"])
    return [
        ("pre", t0 - 0.05),
        ("1/3", t0 + dur * 0.33),
        ("2/3", t0 + dur * 0.66),
        ("post", t0 + dur + 0.16),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--sb", required=True)
    ap.add_argument("--shots", default="")
    ap.add_argument("--spread", type=int, default=0)
    ap.add_argument("--out", default="_tmp/pv/trans")
    ap.add_argument("--w", type=int, default=960, help="单帧抽取宽度（4K 片给 960 够看）")
    ap.add_argument("--cell", type=int, default=480, help="接触表格宽")
    a = ap.parse_args()

    video = a.video if os.path.isabs(a.video) else os.path.join(ROOT, a.video)
    sbp = a.sb if os.path.isabs(a.sb) else os.path.join(ROOT, a.sb)
    out = a.out if os.path.isabs(a.out) else os.path.join(ROOT, a.out)
    if not os.path.exists(video):
        die("找不到视频 " + video)
    if not os.path.exists(FFMPEG) and not FFMPEG.endswith("ffmpeg"):
        die("找不到 ffmpeg " + FFMPEG)

    sb = json.load(open(sbp, encoding="utf-8"))
    shots = sb["shots"]
    byi = {int(s["i"]): s for s in shots}

    if a.shots:
        sel = [byi[int(x)] for x in a.shots.split(",") if x.strip()]
    elif a.spread:
        sel = pick_spread(shots, a.spread)
    else:
        sel = [s for s in shots if s.get("trans") and s.get("fig")]

    if not sel:
        die("没挑到镜头")
    os.makedirs(out, exist_ok=True)

    pad, lab_h = 6, 22
    cw, ch = a.cell, int(round(a.cell * 9 / 16))
    rows, done, miss = [], 0, 0

    for s in sel:
        i = int(s["i"])
        tr = s.get("trans") or {}
        kind = tr.get("k") or "cut"
        dur = float(tr.get("dur") or 0.0)
        fm = (s.get("fig") or {}).get("mode") or "-"
        tag = "#%02d %s %.2f %s" % (i, kind, dur, fm)
        cells = []
        for ph, t in phases(s):
            p = os.path.join(out, "_f_%02d_%s.png" % (i, ph.replace("/", "")))
            if grab(video, max(0.0, t), p, a.w):
                im = Image.open(p).convert("RGB")
                im = im.resize((cw, ch), Image.LANCZOS)
                os.remove(p)
                cells.append((im, "%s %.2fs" % (ph, t)))
            else:
                miss += 1
        if not cells:
            print("[skip] #%02d 抽帧失败" % i)
            continue

        strip = Image.new("RGB", (len(cells) * (cw + pad) + pad, lab_h + ch + pad), (18, 18, 20))
        d = ImageDraw.Draw(strip)
        d.text((pad, 5), tag, fill=(235, 225, 200))
        for j, (im, lb) in enumerate(cells):
            x = pad + j * (cw + pad)
            strip.paste(im, (x, lab_h))
            d.text((x + 4, lab_h - 15), lb, fill=(150, 200, 235))
        name = os.path.join(out, "s%02d_%s_%.2f_%.2f.png" % (i, kind, dur, float(s["t0"])))
        strip.save(name)
        rows.append(strip)
        done += 1
        print("[ok] %s" % os.path.basename(name))

    if not rows:
        die("一张都没抽到")

    sw = max(r.width for r in rows)
    sheet = Image.new("RGB", (sw, sum(r.height for r in rows)), (12, 12, 14))
    y = 0
    for r in rows:
        sheet.paste(r, (0, y))
        y += r.height
    sp = os.path.join(out, "sheet.png")
    sheet.save(sp)
    print("\n[sheet] %s  (%d 镜 × %d 相, %d×%d)" % (sp, done, len(phases(shots[0])), sheet.width, sheet.height))
    if miss:
        print("[warn] 有 %d 帧抽失败（时间越界或 seek 失败）" % miss)


if __name__ == "__main__":
    main()
