# -*- coding: utf-8 -*-
"""pv_demo.py —— P0/P1 特性演示片生成器（60 秒）。

产出两样东西：
  1. _tmp/pv/demo_audio.wav  —— 120 BPM 的合成底（鼓 + 铺底和弦 + 踩镲）
  2. _tmp/pv/sb_demo.json    —— 16 镜 showcase 编排稿

演示稿是**手写**的，不是跑 pv_direct.py 出的：演示片要的是「保证每样特性都出镜」，
而抽卡的语义是「按性格随机」——两者目标相反。版面/装饰/背景的参数仍然从
pv_direct 的 decor_spec / bg_spec 取，保证与产线参数同源。

覆盖清单（每镜至少踩中一项新特性）：
  转场 10 种全出镜｜保持 11 种｜装饰 8 个新件｜背景 4 个新件｜字形 8 种
  碎块入场 pAssemble + 碎块退场 pExplode｜绿幕导出 1 镜｜统一感回访 2 组
  全部镜头边界落在 120 BPM 的半拍网格上
"""
import argparse
import json
import math
import os
import struct
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from pv_direct import decor_spec, bg_spec, PERS, PERS_DEFAULT  # noqa: E402

BPM = 120.0
BEAT = 60.0 / BPM          # 0.5s
BAR = BEAT * 4             # 2.0s


# ----------------------------------------------------------------- 音频
def write_demo_audio(path, dur=60.0, sr=44100):
    """120 BPM 合成底：每拍一记底鼓、反拍踩镲、每小节换一个铺底和弦。

    不用真实音乐是为了让演示片可自由分享（无版权）。四个和弦 C-Am-F-G 循环，
    听感是「有节拍的环境底」，足够支撑频谱层与节拍吸附的观感。
    """
    n = int(dur * sr)
    buf = [0.0] * n

    def add(t0, fn, length):
        i0 = int(t0 * sr)
        i1 = min(n, i0 + int(length * sr))
        for i in range(i0, i1):
            buf[i] += fn((i - i0) / sr)

    # 和弦（C3 E3 G3 B3 → A2 C3 E3 G3 → F2 A2 C3 E3 → G2 B2 D3 F3）
    chords = [
        (130.81, 164.81, 196.00, 246.94),
        (110.00, 130.81, 164.81, 196.00),
        (87.31, 110.00, 130.81, 164.81),
        (98.00, 123.47, 146.83, 174.61),
    ]
    for bar in range(int(dur / BAR) + 1):
        t0 = bar * BAR
        ch = chords[bar % 4]
        for f in ch:
            def pad(tt, f=f):
                return 0.055 * math.sin(2 * math.pi * f * tt) * min(1.0, tt * 3) \
                    * max(0.0, 1.0 - tt / BAR)
            add(t0, pad, BAR)

    # 底鼓：每拍
    for b in range(int(dur / BEAT)):
        t0 = b * BEAT

        def kick(tt):
            f = 118 * math.exp(-tt * 26) + 44
            return 0.62 * math.sin(2 * math.pi * f * tt) * math.exp(-tt * 15)
        add(t0, kick, 0.20)

    # 踩镲：反拍，短噪声（用高频正弦近似，避免噪声种子不可控）
    for b in range(int(dur / BEAT)):
        t0 = b * BEAT + BEAT * 0.5

        def hat(tt):
            return 0.10 * math.sin(2 * math.pi * 6300 * tt) * math.exp(-tt * 42)
        add(t0, hat, 0.06)

    # 军鼓：2、4 拍
    for b in range(int(dur / BEAT)):
        if b % 4 not in (1, 3):
            continue
        t0 = b * BEAT

        def snare(tt):
            return 0.22 * (math.sin(2 * math.pi * 196 * tt) * 0.6
                           + math.sin(2 * math.pi * 311 * tt) * 0.4) * math.exp(-tt * 22)
        add(t0, snare, 0.14)

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for v in buf:
            v = max(-1.0, min(1.0, v))
            frames += struct.pack("<h", int(v * 32000))
        w.writeframes(bytes(frames))
    return path


# ----------------------------------------------------------------- 唱词行
def make_line(i, text, t0, t1):
    """把一句文本切成 words / chars，时间在 [t0, t1] 内均匀铺开。

    演示片没有人声，均匀铺开即可；产线的行来自 timing.json（贴人声）。
    """
    words = text.split()
    nw = len(words)
    span = max(0.2, t1 - t0)
    wspan = span / max(1, nw)
    ws, cs = [], []
    nch = sum(len(w) for w in words)
    per = span / max(1, nch)
    ci = 0
    for wi, w in enumerate(words):
        wt0 = t0 + wi * wspan
        ws.append({"text": w, "t0": round(wt0, 3), "t1": round(wt0 + wspan, 3)})
        for ch in w:
            ct0 = t0 + ci * per
            cs.append({"c": ch, "t0": round(ct0, 3), "t1": round(ct0 + per, 3)})
            ci += 1
    return {"i": i, "t0": round(t0, 3), "t1": round(t1, 3),
            "text": text, "words": ws, "chars": cs}


class R:
    """确定性 RNG（与 pv_direct 同构），保证演示稿可复现。"""

    def __init__(self, seed):
        self.s = seed & 0xFFFFFFFF

    def random(self):
        self.s = (self.s * 1664525 + 1013904223) & 0xFFFFFFFF
        return self.s / 0xFFFFFFFF

    def integers(self, lo, hi):
        """兼容 numpy Generator.integers(lo, hi) 的半开区间。"""
        return lo + int(self.random() * (hi - lo))

    def __call__(self, a, b):
        return self.random()


# ----------------------------------------------------------------- 编排稿
# (文本, 入场, 保持, 退场, 转场, 版面, 字形, 装饰, 背景, 备注)
# 装饰/背景写的是 decor_spec / bg_spec 的键，参数在生成时补。
SHOWCASE = [
    ("MAKE IT COUNT",        "assemble",   "breathe",      "fadeUp",    "wipe",
     "block",       "solid",     "cornerTicks", None,       "转场 wipe · 呼吸"),
    ("DRAWN NOT FILTERED",   "softIn",     "sway",         "fade",      "blinds",
     "marginNote",  "poster",    "tape",        None,       "百叶窗 · 摆 · 胶带"),
    ("SIGNAL IN THE NOISE",  "sliceDown",  "pulse",        "blurOut",   "clock",
     "vRuler",      "gradFill",  None,          "diagonal", "钟表扫 · 脉冲 · 渐层填"),
    ("KEY IT GREEN",         "typeOn",     "counterDrift", "fade",      "iris",
     "indexTable",  "emboss",    "crosshair",   None,       "绿幕导出 · 反漂 · 浮雕"),
    ("EVERY FRAME MATTERS",  "dropBounce", "settle",       "fadeUp",    "blocks",
     "lowerThird",  "inline",    "warnStripe",  None,       "方格消解 · 沉降 · 内线"),
    ("ONE DOZEN FRAMES",     "slam",       "jitter",       "pExplode",  "flash",
     "fullTrack",   "glow",      None,          "sunburst", "闪白 · 抖动 · 碎块退场"),
    ("CUT ON THE BEAT",      "assemble",   "tiltDrift",    "fade",      "push",
     "cross",       "cutout",    "ticket",      None,       "推走 · 倾漂 · 票券"),
    ("PIECES COME HOME",     "pAssemble",  "breatheRow",   "fadeUp",    "zoom",
     "keySplit",    "solid",     "sparkle",     None,       "碎块入场 · 整行呼吸"),
    ("TWELVE DRAWINGS",      "softIn",     "shimmer",      "fade",      "slice",
     "scaleSteps",  "noiseFill", None,          "waveBand", "短册错位 · 微闪 · 噪声填"),
    ("INK BLOOMS SLOW",      "assemble",   "drift",        "pFall",     "ink",
     "cropGiant",   "solid",     "stamp",       None,       "墨晕 · 漂移 · 碎块塌落"),
    ("DRAWN NOT FILTERED",   "softIn",     "sway",         "fade",      "blinds",
     "marginNote",  "poster",    "tape",        None,       "统一感回访 ← 第 2 镜"),
    ("STAY ON THE GRID",     "sliceDown",  "trackIn",      "blurOut",   "blinds",
     "vRuler",      "outline",   "ruler",       "contour",  "标尺 · 缓推 · 等高线"),
    ("BACK TO THE PICTURE",  "assemble",   "breathe",      "fadeUp",    "wipe",
     "block",       "solid",     "cornerTicks", None,       "统一感组 A 首次"),
    ("BACK TO THE PICTURE",  "assemble",   "breathe",      "fadeUp",    "wipe",
     "block",       "solid",     "cornerTicks", None,       "统一感回访 ← 第 13 镜"),
    ("P0 AND P1 DONE",       "slam",       "pulse",        "fade",      "flash",
     "cross",       "double",    "chapter",     "gradWash", "收尾 · 双层 · 章节"),
]


def build(dur=60.0, seed=20261029):
    r = R(seed)
    lines, shots = [], []
    # 片头卡占前 4s（BEAT 的整数倍）
    intro_end = 4.0
    n = len(SHOWCASE)
    slot = (dur - intro_end - 2.0) / n     # 留 2s 给收尾卡
    for i, (text, entk, holdk, exitk, transk, layk, treatk, dk, bk, note) in enumerate(SHOWCASE):
        t0 = intro_end + i * slot
        t1 = t0 + slot
        # 边界吸到半拍网格（演示片没有真唱词，可以吸得狠一点）
        t0 = round(round(t0 / (BEAT / 2)) * (BEAT / 2), 3)
        t1 = round(round(t1 / (BEAT / 2)) * (BEAT / 2), 3)
        if t1 - t0 < 2.4:
            t1 = round(t0 + 2.4, 3)
        ln = make_line(i, text, t0 + 0.24, t1 - 0.18)
        lines.append(ln)

        pers = PERS.get("noir", PERS_DEFAULT)
        decor = [decor_spec(dk, r, i)] if dk else []
        bgl = [bg_spec(bk, r)] if bk else []
        # 绿幕：第 4 镜（KEY IT GREEN）单镜色键导出
        keybg = "green" if "KEY IT GREEN" in text else None
        shots.append({
            "i": len(shots) + 1, "t0": t0, "t1": t1,
            "line": i, "kind": "line", "tier": 2, "section": "DEMO",
            "energy": 0.7,
            "layout": {"k": layk, "availW": 0.66, "x": 0.075, "cy": 0.48,
                       "hK": 0.140, "lead": 1.16, "maxRows": 3,
                       "no": 10 + i, "key": 0, "cx": 0.5,
                       "note": "NOTE %02d" % (10 + i),
                       "side": "l", "n": 18, "bandK": 0.34, "bandCol": "accent",
                       "dir": 1, "tag": "DEMO", "meta": "DEMO",
                       "rows": [["SEC", "B"], ["BAR", "%02d" % (10 + i)], ["KEY", "C"]],
                       "pos": "b", "edge": "b", "mode": "dim", "giantK": 0.6,
                       "side2": "t"},
            "type": {"face": "heavy", "body": "sansMed", "squeeze": 1.0},
            "enter": {"k": entk, "dur": 0.52, "stagger": 0.026, "lead": 0.07},
            "hold": {"k": holdk, "amp": 1.0},
            "exit": {"k": exitk, "dur": 0.38},
            "trans": {"k": transk},
            "treat": {"k": treatk},
            "pal": {"pack": "noir", "idx": i % 2},
            "bgl": bgl, "bg": [], "decor": decor, "post": [],
            "cam": {"k": ["push", "float", "orbit", "pendulum"][i % 4], "amp": 0.9},
            "fig": None, "sync": True, "wobble": 0,
            "unified": text in ("DRAWN NOT FILTERED", "BACK TO THE PICTURE")
            and i in (10, 13),
            "keyBg": keybg,
            "note": note,
        })

    # 片头卡
    shots.insert(0, {
        "i": 0, "t0": 0.0, "t1": intro_end - 0.26, "line": -1,
        "kind": "title", "tier": 1, "section": "TITLE", "energy": 0.2,
        "layout": {"k": "titleCard", "kicker": "P0 + P1 SHOWCASE",
                   "artist": "JIZURA STANDARD", "shadow": True,
                   "meta": "TRANS 10 · HOLD 11 · DECOR 8 · TREAT 8 · KEYBG · UNITY · BEAT"},
        "type": {"face": "heavy", "body": "sansMed", "squeeze": 1.0},
        "text": "TEXT PV ENGINE",
        "enter": {"k": "fadeSlow", "dur": 1.0, "stagger": 0.05, "lead": 0.20},
        "hold": {"k": "breathe", "amp": 0.8},
        "exit": {"k": "fade", "dur": 0.62},
        "trans": {"k": "wipe"},
        "treat": {"k": "solid"},
        "pal": {"pack": "noir", "idx": 0},
        "bgl": [], "bg": [], "decor": [], "post": [],
        "cam": {"k": "push", "amp": 0.7},
        "fig": None, "sync": False, "wobble": 0, "unified": False,
        "note": "title card",
    })
    # 收尾卡：占最后 2s
    shots.append({
        "i": len(shots), "t0": round(dur - 2.0, 3), "t1": round(dur, 3), "line": -1,
        "kind": "title", "tier": 1, "section": "END", "energy": 0.2,
        "layout": {"k": "titleCard", "kicker": "ALL FEATURES LIVE",
                   "artist": "48 SHOTS · 10 TRANS · 12 HOLD", "shadow": True,
                   "meta": "RENDERED AT 3840x2160 @ 60 · KOMA 12"},
        "type": {"face": "heavy", "body": "sansMed", "squeeze": 1.0},
        "text": "COMPLETE",
        "enter": {"k": "fadeSlow", "dur": 0.8, "stagger": 0.05, "lead": 0.10},
        "hold": {"k": "breathe", "amp": 0.8},
        "exit": {"k": "fade", "dur": 0.5},
        "trans": {"k": "flash"},
        "treat": {"k": "solid"},
        "pal": {"pack": "noir", "idx": 0},
        "bgl": [], "bg": [], "decor": [], "post": [],
        "cam": {"k": "push", "amp": 0.7},
        "fig": None, "sync": False, "wobble": 0, "unified": False,
        "note": "end card",
    })
    shots.sort(key=lambda s: s["t0"])
    for k, s in enumerate(shots):
        s["i"] = k
    # 首尾相接
    for k in range(len(shots) - 1):
        shots[k]["t1"] = round(max(shots[k]["t0"] + 0.30, shots[k + 1]["t0"]), 3)
        shots[k + 1]["t0"] = shots[k]["t1"]
    shots[-1]["t1"] = round(dur, 3)

    sb = {
        "version": 2,
        "meta": {"title": "P0+P1 Showcase", "artist": "JIZURA STANDARD",
                 "vocal": "岁己SUI", "source": "", "date": "20261029",
                 "seed": seed, "dur": dur, "fps": 60, "start": 0.0,
                 "koma": 12, "keyBg": None, "mark": "demo"},
        "palette": {"pack": "noir", "idx": 0},
        "audio": {"bpm": BPM, "barSec": BAR, "sections": []},
        "lines": lines,
        "shots": shots,
    }
    return sb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "_tmp", "pv", "sb_demo.json"))
    ap.add_argument("--audio", default=os.path.join(ROOT, "_tmp", "pv", "demo_audio.wav"))
    ap.add_argument("--dur", type=float, default=60.0)
    ap.add_argument("--seed", type=int, default=20261029)
    a = ap.parse_args()

    write_demo_audio(a.audio, a.dur)
    print("[audio] %s  %.1fs" % (a.audio, a.dur))

    sb = build(a.dur, a.seed)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    json.dump(sb, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    from collections import Counter
    sh = sb["shots"]
    print("[shots] %d 镜  %.2fs" % (len(sh), sb["meta"]["dur"]))
    print("[trans] %s" % dict(Counter(s["trans"]["k"] for s in sh if s.get("trans")).most_common()))
    print("[hold] %s" % dict(Counter(s["hold"]["k"] for s in sh if s.get("hold")).most_common()))
    print("[enter] %s" % dict(Counter(s["enter"]["k"] for s in sh if s.get("enter")).most_common()))
    print("[treat] %s" % dict(Counter(s["treat"]["k"] for s in sh if s.get("treat")).most_common()))
    dec = Counter(d["k"] for s in sh for d in (s.get("decor") or []))
    bg = Counter(b["k"] for s in sh for b in (s.get("bgl") or []))
    print("[decor] %s" % dict(dec))
    print("[bg] %s" % dict(bg))
    print("[keyBg] %s" % [s["i"] for s in sh if s.get("keyBg")])
    print("[unity] %s" % [s["i"] for s in sh if s.get("unified")])
    print("[out] %s  %.0fKB" % (a.out, os.path.getsize(a.out) / 1024.0))


if __name__ == "__main__":
    sys.exit(main())
