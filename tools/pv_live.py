#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按 LIVE_SPANS 抽直播画面序列，并合成引擎能读的 live.json。

为什么不是「一次抽满整首」：
  231s × 30fps 的头窗序列是 6900 帧 JPEG（约 400MB），而引擎是按镜头滑动装载的
  （pv_engine.holdShot），一次性铺满既浪费磁盘也让首帧加载变慢。六段各抽 9~11s，
  总计约 110MB，正好是「用到多少抽多少」。

为什么要写 tOff：
  引擎的 getFrame(name, t) 用**成片时间**直接索引序列（i = (t + tOff) * fps）。
  分段抽出的序列帧 0 对应成片 win.start，不是 0s —— 所以每个 fig 必须带
  tOff = -win.start，否则索引会整体漂到片子开头，画面取到的是「第一句的姿势」。

用法：
  python tools/pv_live.py                     # 抽全部六段（已有则复用）
  python tools/pv_live.py --force             # 全量重抽
  python tools/pv_live.py --only 3,4          # 只重抽第 3、4 段
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from pv_direct import LIVE_SPANS  # noqa: E402  时间窗与画法的唯一真源

# 裁切框（1920×1080 源坐标）。不是拍脑袋定的 —— 这一版录播的版面实测：
#   左侧聊天栏右缘 x≈590；右下礼物卡左缘 x≈1370；顶部弹幕在 y<40。
# head 取 620..1340 × 0..720（脸中心实测 965,372，居中偏上）；
# body 取 592..1340 × 0..997（3:4 竖幅，左右刚好把两处 UI 关在框外）。
HEAD = dict(x=620, y=0, s=720)
BODY = dict(x=592, y=0, w=748, h=997)
FACE = dict(x=965, y=372)      # 脸中心（源坐标），写进 face 供圆窗对齐


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write((p.stdout or "") + (p.stderr or ""))
        raise SystemExit("[live] 抽取失败 (%d)" % p.returncode)
    return p.stdout


def one(span_idx, fig, src, cut_start, out_root, force):
    name = fig["seq"]                       # s1..s6
    mode = fig["mode"]
    # 抽哪种序列由画法决定：小窗（圆/拱/双圆）要头的方形近景，
    # 竖幅族（band/crop/split）要 3:4 半身。抽错的话圆窗里会出现一条腰。
    seqk = "body" if mode in ("band", "crop", "split", "slab", "soft", "ghost") else "head"
    start, dur = fig["win"]
    out = os.path.join(out_root, name)
    jp = os.path.join(out, "live.json")

    if not force and os.path.isfile(jp):
        try:
            old = json.load(open(jp, encoding="utf-8"))
            if old.get(seqk) and old[seqk].get("n"):
                if os.path.isdir(old[seqk]["dir"]):
                    print("[live] %s 复用（%d 帧）" % (name, old[seqk]["n"]))
                    return name, old[seqk]
        except (ValueError, OSError):
            pass

    cmd = [sys.executable, os.path.join(HERE, "prepare_live.py"),
           "--src", src, "--cut-start", "%.2f" % cut_start,
           "--start", "%.3f" % start, "--dur", "%.3f" % dur,
           "--out", out, "--jobs", jp, "--mode", seqk,
           "--head-fps", "30", "--body-fps", "30",
           "--head-x", str(HEAD["x"]), "--head-y", str(HEAD["y"]),
           "--head-s", str(HEAD["s"]), "--head-out", str(HEAD["s"]),
           "--body-x", str(BODY["x"]), "--body-y", str(BODY["y"]),
           "--body-w", str(BODY["w"]), "--body-h", str(BODY["h"]),
           "--face-x", str(FACE["x"]), "--face-y", str(FACE["y"])]
    run(cmd)
    info = json.load(open(jp, encoding="utf-8"))
    ent = info.get(seqk)
    if not ent:
        raise SystemExit("[live] %s 没抽出来（%s）" % (name, jp))
    # 目录改成相对仓库根的写法，换盘符/换机也能读
    d = ent["dir"].replace("\\", "/")
    if os.path.isabs(d):
        try:
            d = os.path.relpath(d, ROOT).replace("\\", "/")
        except ValueError:
            pass
    ent["dir"] = d
    return name, ent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="", help="录播源；默认读 config.json 的 recording_root + date")
    ap.add_argument("--date", default="2026-10-27")
    ap.add_argument("--cut-start", type=float, default=4474.10, help="成片 0s 对应的录播绝对时间")
    ap.add_argument("--out", default=os.path.join(ROOT, "_tmp", "live"))
    ap.add_argument("--only", default="", help="只抽指定段（seq 名，逗号分隔，如 s3,s4）")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    src = a.src
    if not src:
        cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
        d = os.path.join(cfg["recording_root"], a.date)
        cand = [f for f in sorted(os.listdir(d)) if f.endswith(".mp4")]
        if not cand:
            raise SystemExit("[live] %s 下没有 mp4" % d)
        src = os.path.join(d, cand[0])
    print("[live] 源 %s" % os.path.basename(src))

    os.makedirs(a.out, exist_ok=True)
    only = {s.strip() for s in a.only.split(",") if s.strip()}
    seqs = {}
    for idx in sorted(LIVE_SPANS):
        fig = LIVE_SPANS[idx]
        if only and fig["seq"] not in only:
            continue
        name, ent = one(idx, fig, src, a.cut_start, a.out, a.force)
        seqs[name] = ent

    # 合并：进度里已有的段要保留（--only 重抽某段时不能把别的段抹掉，
    # 否则播放器静默不画那几镜，画面看起来只是「这一段没人」）
    lj = os.path.join(a.out, "live.json")
    if os.path.isfile(lj):
        try:
            old = json.load(open(lj, encoding="utf-8"))
            for k, v in old.items():
                if k not in ("face", "cut_start") and isinstance(v, dict) and k not in seqs and v:
                    seqs[k] = v
        except (ValueError, OSError):
            pass

    hx, hy, hs = HEAD["x"], HEAD["y"], HEAD["s"]
    db = {"face": {"cx": round((FACE["x"] - hx) / float(hs), 4),
                   "cy": round((FACE["y"] - hy) / float(hs), 4)},
          "cut_start": a.cut_start}
    db.update(seqs)
    with open(lj, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

    tot = 0
    for k, v in sorted(seqs.items()):
        d = v["dir"]
        p = d if os.path.isabs(d) else os.path.join(ROOT, d)
        if os.path.isdir(p):
            tot += sum(os.path.getsize(os.path.join(p, f)) for f in os.listdir(p))
    print("[live] 合并 %d 段 → %s（%.0f MB）" % (len(seqs), lj, tot / 1e6))
    for k in sorted(seqs):
        v = seqs[k]
        print("  %-3s %3d 帧 @%dfps  win=%.2fs → tOff=%.2f"
              % (k, v["n"], v["fps"], LIVE_SPANS[[i for i in LIVE_SPANS
                                                 if LIVE_SPANS[i]["seq"] == k][0]]["win"][0],
                 -LIVE_SPANS[[i for i in LIVE_SPANS
                              if LIVE_SPANS[i]["seq"] == k][0]]["win"][0]))


if __name__ == "__main__":
    main()
