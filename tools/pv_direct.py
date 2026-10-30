# -*- coding: utf-8 -*-
"""文字 PV 导演层 —— 第二版（JIZURA 版式语法）

与第一版的关系：**推翻重来**。第一版的思路是「一句话 = 一个字块，五档能量逐级加 buff」，
结果是一整片差不多构图的居中大字，靠入场动效制造差别。JIZURA 不是这么工作的：

  1. **段落决定视觉，不是句子决定视觉**。一个 style pack + 一套 scheme 贯穿整段，
     段落边界就是硬切色 —— 观众靠「换了一种纸」来感知「换了一段」。
  2. **版面是有限词汇，不是无限参数**。每句只从 12 个版面里挑一个，且每个版面
     自带规则线/索引/注记/遮带。挑版面看的是**句子形态**（几个词、多长），
     不是「能量档位」。
  3. **字只分三级**：主字（0.11~0.26H）/ 次字（0.27 倍主字）/ 注记（0.023U）。
     没有中间级。断层级差是版面感的一半。
  4. **留白是正片**。一句话平均只占画面 30~45%，其余是空的、有线和注记的「空」。

随机量全部走 seed；同一 seed + 同一输入 → 同一份编排稿。
"""
import argparse
import json
import os
import re
import sys

import numpy as np

# ----------------------------------------------------------------- 版式词汇
# 每个版面能接什么句子，是**形态约束**，不是审美偏好：
#   keySplit   挑一个词撑到 0.5H，其余缩到 0.27 倍 —— 词太多就挤成一排小字
#   cropGiant  巨型副本被画框切掉 + 一段可读的小字 —— 句子太长时小字要三行
#   lowerThird 下部一条 telop —— 超过三行就顶到画面上半区，不再是「下部」
#   cross      十字轴 —— 字少才有轴，字多轴就被盖住了
#   bandHide   一条横带从行间扫过 —— 两行以上才有「行间」可穿
#   indexTable 歌词 + 一张小表 —— 长句会把表挤出去
#   vRuler     竖轴刻度 —— 需要一个相对窄的字块
#   scaleSteps 逐词递减 —— 词必须少且短
#   marginNote 极小字 + 大留白 —— 只能给短句，长句会变成「正文」
#   fullTrack  极端字距铺满整幅 —— 单行才行
#   block      左缘字块 —— 长句主力，任何句子都能接
FIT = {
    "keySplit":   lambda nw, n, cjk: 2 <= nw <= 7,
    # 巨字只做短句：长句本身要占两三行，再配一块巨影就是「两层字互相打架」。
    # 原来的上界 n<=46 几乎放行一切，长句段（副歌 12 镜）里有 5 镜都是巨影。
    "cropGiant":  lambda nw, n, cjk: n <= 40 and nw <= 9,
    "lowerThird": lambda nw, n, cjk: 2 <= nw <= 13,
    "cross":      lambda nw, n, cjk: 2 <= nw <= 6,
    "bandHide":   lambda nw, n, cjk: 2 <= nw <= 9,
    "indexTable": lambda nw, n, cjk: 3 <= nw <= 13,
    "vRuler":     lambda nw, n, cjk: 2 <= nw <= 11,
    "scaleSteps": lambda nw, n, cjk: 3 <= nw <= 6,
    "marginNote": lambda nw, n, cjk: n <= 36,
    "fullTrack":  lambda nw, n, cjk: nw <= 8 and n <= 36,
    "block":      lambda nw, n, cjk: True,
}

# 三种「语气」各自能用的版面池，权重写重复次数。
# 为什么语气只分三种：JIZURA 的 cut 也只有「安静 / 驱动 / 爆发」这三种功能，
# 再多就会变成「每句都在换招」——那是没有编排，不是有编排。
VOICE = {
    # block 是「万能落点」（长句只有它接得住），所以它天然会被抽中偏多。
    # 但它是所有版面里最朴素的一个——左缘块 + 短竖线 + 底细线，没有索引也没有遮蔽。
    # 权重给到 4/3/2 时实测占 29%（14/48），且多次 3~7s 内连着出现两次，
    # 观众读到的就是「又是那一块字」。降到 2/2/1，多出来的份额给带索引与遮蔽的版面。
    "quiet": [("block", 2), ("marginNote", 3), ("indexTable", 3), ("vRuler", 3),
              ("cropGiant", 2)],
    "drive": [("lowerThird", 4), ("block", 2), ("keySplit", 3), ("indexTable", 3),
              ("marginNote", 3), ("vRuler", 2), ("cropGiant", 2)],
    "peak":  [("keySplit", 4), ("bandHide", 3), ("cross", 3), ("cropGiant", 3),
              ("scaleSteps", 2), ("fullTrack", 2), ("block", 1)],
}

# 段落 → (style pack, scheme)。一个 pack 贯穿一段，段落之间换纸。
# 只用了 noir / paper / crimson 三个 pack：
#   noir    黑地米白字 + 琥珀/青  → 前奏、主歌、尾奏
#   paper   奶油纸 + 蓝墨 + 洋红  → 主歌（整片唯一的大面积亮底）
#   crimson 深红地 + 白/黑       → 副歌（整片唯一的饱和色块）
# 三个 pack 已经能给出「黑 / 纸 / 红」三种完全不同的性格；再多就是拼贴。
LOOK = {
    "B1": ("noir", 0),
    "A1": ("paper", 0),
    "A2": ("noir", 0),
    "A3": ("paper", 1),
    "B2": ("crimson", 0),
    "B3": ("crimson", 0),
    "B4": ("noir", 1),
    "A4": ("paper", 2),
    "B5": ("crimson", 0),
    "A5": ("paper", 0),
    "C1": ("noir", 1),
    "C2": ("noir", 0),
}

SCHEME_N = {}                # pack → scheme 套数（运行时从 pv_scheme.js 解析）

# 段内换纸的间隔（秒）。一个 pack 连撑太久会读成「卡住了」——实测 crimson 段
# 13 镜 55s 一镜不换，观众只看到一整片红。换的是**同 pack 的另一套 scheme**：
# 纸换了、字体人格没变，所以段落感还在，节奏却活了。
SWITCH_EVERY = 24.0

# pack → 字体角色。display 是「这套纸的性格」，body 只负责次字与注记。
FONTS = {
    "noir": {"face": "heavy", "body": "sansMed"},
    "crimson": {"face": "grotesk", "body": "sansMed"},
    "paper": {"face": "mincho", "body": "sansMed"},
    "hud": {"face": "grotesk", "body": "sansMed"},
    "mono": {"face": "mincho", "body": "sansMed"},
}

# 语气 → 相机档 / 人物概率 / 版面尺寸基线
# availW 是「版心能有多宽」。上限定在 0.66~0.72：再宽正文块就会横贯画面，
# 从「一张版面」退化成「一条字幕」。
MOOD = {
    "quiet": dict(cam=1, fig=0.42, hK=0.112, availW=0.60, amp=0.85),
    "drive": dict(cam=2, fig=0.58, hK=0.140, availW=0.66, amp=0.95),
    "peak":  dict(cam=3, fig=0.54, hK=0.165, availW=0.72, amp=1.10),
}

# 有人物时必须用的版面：这些版面自带「空的一侧」，人和字不会抢同一片空间。
# 这一条是硬的 —— 一旦让 cropGiant / cross 这类满幅版面带人物，
# 字就必然压在脸上（第一版 43.3s 的 This moment 就是这么盖住眼睛的）。
# 允许与人物同框的版面。lowerThird 之所以能进来：它是一条从画缘伸进版心的横带，
# 只要站到人的另一侧就完全不碰 —— 它曾经因为侧向写死成 'l' 被排除在 51% 的镜头之外。
FIG_SAFE = {"block", "marginNote", "cropGiant", "vRuler", "lowerThird"}

# 相机手法（pv_cam.js 侧实现）
CAM_BY_TIER = {
    1: [("push", 4), ("drift", 2), ("float", 2)],
    2: [("push", 3), ("float", 2), ("orbit", 1), ("tiltDown", 1)],
    3: [("pendulum", 2), ("orbit", 2), ("float", 2), ("push", 1), ("snapPan", 1)],
}
CAM_QUOTA = {"snapPan": 2}

# ----------------------------------------------------------------- 性格标签（P0-2）
# JIZURA 的抽卡是三重加权：基础权重 × 风格偏置(style.bias) × 新颖度惩罚(novelty)。
# 风格偏置给每个 pack 一个「稳定人格」：noir 偏爱竖轴与碎字，crimson 偏爱巨字与故障，
# paper 偏爱留白与柔和。没有它，所有风格看起来都一样（只是换了颜色）——
# 这正是「我们的13 个版式不如官方 862 个零件」的另一半原因：不是数量，是人格。
PERS = {
    "noir": {
        "layout": {"vRuler": 1.8, "keySplit": 1.6, "indexTable": 1.4, "block": 1.0,
                   "cropGiant": 1.2, "marginNote": 1.1},
        "enter":  {"assemble": 2.2, "pAssemble": 1.8, "sliceDown": 1.8, "softIn": 1.1},
        "exit":   {"pExplode": 1.8, "pFall": 1.2, "blurOut": 1.4, "fadeUp": 1.0},
        "hold":   {"drift": 1.6, "sway": 1.4, "breathe": 1.2},
        "trans":  {"wipe": 1.6, "clock": 1.5, "iris": 1.3, "ink": 1.4, "blinds": 1.1},
        "decor":  {"barcode": 1.6, "ruler": 1.4, "cornerTicks": 1.3, "chapter": 1.2},
    },
    "crimson": {
        "layout": {"cropGiant": 2.0, "fullTrack": 1.7, "bandHide": 1.5, "cross": 1.3,
                   "scaleSteps": 1.2, "keySplit": 1.1},
        "enter":  {"slam": 1.8, "typeOn": 1.6, "dropBounce": 1.4, "pAssemble": 1.3},
        "exit":   {"pExplode": 2.0, "pFall": 1.5, "fadeUp": 1.1},
        "hold":   {"jitter": 1.6, "pulse": 1.5, "shimmer": 1.3},
        "trans":  {"flash": 1.8, "zoom": 1.6, "blocks": 1.5, "slice": 1.4, "push": 1.2},
        "decor":  {"speedLines": 1.8, "warnStripe": 1.5, "sparkle": 1.3, "crosshair": 1.2},
    },
    "paper": {
        "layout": {"block": 2.0, "marginNote": 1.8, "lowerThird": 1.4, "indexTable": 1.3,
                   "vRuler": 1.1},
        "enter":  {"softIn": 1.8, "assemble": 1.4, "typeOn": 1.2},
        "exit":   {"fadeUp": 1.7, "fade": 1.5, "blurOut": 1.1},
        "hold":   {"breathe": 1.7, "breatheRow": 1.4, "settle": 1.3, "tiltDrift": 1.2},
        "trans":  {"wipe": 1.5, "ink": 1.6, "iris": 1.3, "blinds": 1.2},
        "decor":  {"tape": 1.7, "chip": 1.4, "ticket": 1.3, "stamp": 1.2, "brackets": 1.2},
    },
    "hud": {
        "layout": {"indexTable": 1.8, "vRuler": 1.5, "cross": 1.4, "lowerThird": 1.2},
        "enter":  {"typeOn": 1.7, "sliceDown": 1.4, "assemble": 1.2},
        "exit":   {"blurOut": 1.6, "fade": 1.3, "pExplode": 1.2},
        "hold":   {"jitter": 1.4, "shimmer": 1.4, "trackIn": 1.2},
        "trans":  {"blinds": 1.6, "clock": 1.5, "slice": 1.4, "wipe": 1.2},
        "decor":  {"crosshair": 1.8, "timeline": 1.5, "barcode": 1.4, "ruler": 1.3},
    },
}
# 未列名的 pack 用中性偏置（等价于无偏置）
PERS_DEFAULT = {g: {} for g in ("layout", "enter", "exit", "hold", "trans", "decor")}


def wkey(obj, k, d=1.0):
    """偏置查表：没有给的键就用基础权重（与 JIZURA 的 wkey 同构）。"""
    return obj.get(k, d) if obj else d


def novelty(hist, val, short=0.22, long=0.62, depth=6):
    """新颖度惩罚：最近 2 次内用过 → 权重 ×0.22；最近 6 次内用过 → ×0.62。

    与 JIZURA 的 novelty 同构。为什么不用「直接排除」：排除会让池子在长句段
    快速枯竭（peak 池只有 7 个版面，排除 2 个再过滤 FIT 就只剩 1~2 个可抽），
    折扣既能压制重复又不会饿死。
    """
    w = 1.0
    for d, v in enumerate(reversed(hist[-depth:])):
        if v == val:
            w *= short if d < 2 else long
    return w


def gacha(r, pool, pers, group, hist):
    """三重加权抽卡：基础权重 × 风格偏置 × 新颖度。pool = [(k, base_w), ...]"""
    out = []
    for k, w in pool:
        ww = w * wkey(pers.get(group), k, 1.0) * novelty(hist, k)
        out.append((k, max(0.02, ww)))
    return weighted(r, out)


# ----------------------------------------------------------------- 转场池（P0-1）
# 转场是「上一镜怎么让位给这一镜」。语气越强，转场越有冲击力。
TRANS_BY_MOOD = {
    "quiet": [("wipe", 3), ("iris", 2), ("ink", 2), ("clock", 2), ("blinds", 1)],
    "drive": [("wipe", 3), ("push", 3), ("blinds", 2), ("slice", 2), ("ink", 1),
              ("flash", 1), ("zoom", 1)],
    "peak":  [("flash", 3), ("zoom", 3), ("push", 2), ("blocks", 2), ("slice", 2),
              ("wipe", 2), ("blinds", 1)],
}

# 保持类（P0-3）：字停留期间的呼吸感。none 不能没有 —— 有些镜头就该一动不动。
HOLD_POOL = [("breathe", 3), ("drift", 2.2), ("sway", 2), ("pulse", 1.8),
             ("tiltDrift", 1.5), ("settle", 1.4), ("breatheRow", 1.5),
             ("counterDrift", 1.3), ("shimmer", 1.0), ("wave", 1.2),
             ("trackIn", 1.0), ("none", 1.2)]

# 装饰池（P1-4）：宁可没有也不要多 —— 一个镜头最多一件。
DECOR_POOL = [("cropMarks", 3), ("barcode", 2.2), ("dots", 1.2), ("cornerTicks", 2),
              ("crosshair", 1.6), ("tape", 1.8), ("chip", 1.5), ("ruler", 1.5),
              ("arrows", 1.3), ("brackets", 1.4), ("sparkle", 1.1), ("warnStripe", 1.2),
              ("stamp", 1.1), ("ticket", 1.2), ("timeline", 1.3), ("chapter", 1.4),
              ("speedLines", 1.0), ("progress", 1.0)]

# 背景图形池（P1-4）：平涂底上只给一层极淡图形。
BG_POOL = [("rings", 2), ("halftone", 1.8), ("wash", 1.6), ("grid", 1.3),
           ("hatch", 1.2), ("diagonal", 1.4), ("waveBand", 1.3), ("noiseField", 1.2),
           ("spotlight", 1.2), ("contour", 1.1), ("sunburst", 1.0), ("mesh", 1.2),
           ("gradWash", 1.3), ("topo", 1.0), ("dust", 1.0)]

# 节拍吸附（P1-6）：镜头边界与动效时长向节拍网格靠拢。
def beat_snap(t, beat, tol):
    """把 t 吸附到最近的节拍；偏差超过 tol 就保持原值（宁可不吸也不能拖拍）。

    为什么要 tol：歌词 PV 的字必须贴着人声出现。把 0.35s 的偏差硬吸到节拍上，
    字会比唱词早/晚三分之一拍出来 —— 观众读作「字幕对不上」，比不吸更糟。
    """
    if beat <= 0:
        return t
    q = round(t / beat) * beat
    return round(q, 3) if abs(q - t) <= tol else t


# ----------------------------------------------------------------- 装饰 / 背景参数
def decor_spec(dk, r, i):
    """给装饰件填参数。位置只做「构图」，不做「贴纸」——永远贴边或落在空白处。"""
    if dk == "cropMarks":
        return {"k": dk, "m": 46, "len": 18, "a": 0.30}
    if dk == "barcode":
        return {"k": dk, "x": 1500, "y": 64, "w": 300, "h": 26, "a": 0.42, "seed": 11 + i}
    if dk == "dots":
        return {"k": dk, "x0": 0, "y0": 0, "x1": 1920, "y1": 1080, "step": 44,
                "r": 1.0, "a": 0.07}
    if dk == "cornerTicks":
        return {"k": dk, "m": 54, "len": 12, "n": 3, "gap": 9, "a": 0.20}
    if dk == "crosshair":
        return {"k": dk, "x": int(1920 * (0.22 + 0.56 * r.random())),
                "y": int(1080 * (0.24 + 0.52 * r.random())), "r": 24, "a": 0.26}
    if dk == "tape":
        return {"k": dk, "x": int(60 + 260 * r.random()), "y": int(50 + 120 * r.random()),
                "w": 220 + int(120 * r.random()), "h": 30, "rot": -8 + int(16 * r.random()),
                "a": 0.15, "col": "accent" if r.random() < 0.4 else "ink"}
    if dk == "chip":
        return {"k": dk, "x": int(80 + 220 * r.random()), "y": int(70 + 120 * r.random()),
                "h": 30, "text": pick(r, ["VERSE", "CHORUS", "TAKE 02", "REC", "A/B"]),
                "size": 13, "a": 0.8, "fill": "accent" if r.random() < 0.35 else "ink"}
    if dk == "ruler":
        return {"k": dk, "x0": 110, "x1": 1810, "y": 986, "n": 24, "a": 0.15,
                "numbers": r.random() < 0.5}
    if dk == "arrows":
        return {"k": dk, "x": int(120 + 300 * r.random()), "y": int(200 + 400 * r.random()),
                "n": 3, "dir": 1 if r.random() < 0.5 else -1, "a": 0.22}
    if dk == "brackets":
        return {"k": dk, "x": int(1920 * (0.18 + 0.28 * r.random())),
                "y": int(1080 * (0.20 + 0.26 * r.random())),
                "w": int(1920 * (0.30 + 0.22 * r.random())),
                "h": int(1080 * (0.30 + 0.24 * r.random())), "len": 28, "a": 0.20}
    if dk == "sparkle":
        return {"k": dk, "x": int(1920 * (0.12 + 0.76 * r.random())),
                "y": int(1080 * (0.12 + 0.76 * r.random())), "r": 16, "a": 0.42,
                "col": "accent"}
    if dk == "warnStripe":
        return {"k": dk, "x": 0, "y": 1036 if r.random() < 0.6 else 26,
                "w": 1920, "h": 16, "a": 0.15, "step": 14}
    if dk == "stamp":
        return {"k": dk, "x": int(1920 * (0.72 + 0.18 * r.random())),
                "y": int(1080 * (0.14 + 0.22 * r.random())), "r": 50,
                "rot": -14 + int(28 * r.random()), "a": 0.17,
                "text": pick(r, ["OK", "PASS", "01", "LIVE"])}
    if dk == "ticket":
        return {"k": dk, "x": 130, "y": int(1080 * (0.72 + 0.10 * r.random())),
                "w": 340, "h": 50, "a": 0.13, "text": "ADMIT ONE"}
    if dk == "timeline":
        return {"k": dk, "x0": 120, "x1": 1800, "y": 1010, "n": 8,
                "p": round(0.15 + 0.7 * r.random(), 3), "a": 0.17}
    if dk == "chapter":
        return {"k": dk, "x": 96, "y": 120 + int(760 * r.random()),
                "text": pick(r, ["SEC 01", "SEC 02", "B-SIDE", "LIVE", "CUT"]),
                "size": 15, "a": 0.55}
    if dk == "speedLines":
        return {"k": dk, "cx": 960, "cy": 540, "n": 22, "a": 0.09, "phase": float(r.random())}
    if dk == "progress":
        return {"k": dk, "x": 120, "y": 1016, "w": 1680, "p": round(0.1 + 0.8 * r.random(), 3)}
    return {"k": dk, "a": 0.2}


def bg_spec(bk, r):
    """给背景图形填参数。一律极淡（a ≤ 0.10）——它是「层次」不是「滤镜」。"""
    if bk == "rings":
        return {"k": bk, "x": 0.5, "y": 0.5, "n": 4, "a": 0.10, "spd": 0.20, "rMax": 1.10}
    if bk == "halftone":
        return {"k": bk, "x": 0.55, "y": 0.10, "w": 0.45, "h": 0.80, "a": 0.07, "step": 22}
    if bk == "wash":
        return {"k": bk, "n": 3, "a": 0.10}
    if bk == "grid":
        return {"k": bk, "step": 40, "a": 0.05}
    if bk == "hatch":
        return {"k": bk, "step": 18, "a": 0.045}
    if bk == "diagonal":
        return {"k": bk, "step": 26, "a": 0.055, "w": 1.2}
    if bk == "waveBand":
        return {"k": bk, "n": 3, "a": 0.065}
    if bk == "noiseField":
        return {"k": bk, "n": 240, "a": 0.05}
    if bk == "spotlight":
        return {"k": bk, "x": round(0.3 + 0.4 * r.random(), 3), "r": 0.85, "a": 0.09}
    if bk == "contour":
        return {"k": bk, "x": round(0.3 + 0.4 * r.random(), 3),
                "y": round(0.3 + 0.36 * r.random(), 3), "n": 4, "a": 0.075}
    if bk == "sunburst":
        return {"k": bk, "x": 0.5, "y": 0.5, "n": 18, "a": 0.05}
    if bk == "mesh":
        return {"k": bk, "step": 34, "a": 0.05}
    if bk == "gradWash":
        return {"k": bk, "ang": int(90 + 60 * r.random()), "a": 0.075}
    if bk == "topo":
        return {"k": bk, "a": 0.06}
    if bk == "dust":
        return {"k": bk, "a": 0.05}
    return {"k": bk, "a": 0.06}


def rng(seed, *keys):
    """由 seed 与若干整数键派生的确定性 RNG。"""
    h = seed & 0xFFFFFFFF
    for k in keys:
        h = (h ^ (int(k) * 2654435761)) & 0xFFFFFFFF
        h = (h * 40503 + 2246822519) & 0xFFFFFFFF
    return np.random.default_rng(h)


def pick(r, arr):
    return arr[int(r.integers(0, len(arr)))]


def weighted(r, pairs):
    """[(值, 权重)] → 值"""
    tot = sum(w for _, w in pairs)
    x = float(r.random()) * tot
    for v, w in pairs:
        x -= w
        if x <= 0:
            return v
    return pairs[-1][0]


def key_index(words, kw):
    """挑「主字」：标题词优先，其次最长的实词，最后退回最长的词。

    JIZURA 的 tyKeySplit 也是这么挑的（最长词 / 首个汉字串）。
    为什么要显式挑：随便取中间那个词，等于版面上最重要的位置交给了随机数。
    """
    best, bi = -1, 0
    for i, w in enumerate(words):
        t = re.sub(r"[^A-Za-z']", "", w).lower()
        score = len(t) + (3.0 if t in kw else 0) + (1.2 if 0 < i < len(words) - 1 else 0)
        if score > best:
            best, bi = score, i
    return bi


def scheme_counts():
    """从 pv_scheme.js 解析每个 pack 有几套 scheme。

    不在这里写死，是为了避免与前端漂移：pv_scheme.js 增删 scheme 之后轮转会自己跟上，
    不会出现「idx 越界 → 解算出 undefined 色 → 整镜画成黑」这种零报错事故。
    """
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "renderer", "player", "pv", "pv_scheme.js")
    packs, cur = {}, None
    for line in open(p, encoding="utf-8"):
        m = re.match(r"\s{4}(\w+):\s*\{\s*$", line)
        if m:
            cur = m.group(1); packs[cur] = 0; continue
        if cur and re.search(r"bg: '#\w+'", line):
            packs[cur] += 1
    return packs


def fig_reserve(fig, W=1920):
    """人物占掉的一侧宽度（px）。

    为什么必须精确算：`fixed=False` 时 placeFigure 会在六个锚点里挑「离字最远」的一个，
    于是导演层以为人在右、引擎把她放到了左边（实测 t=14 的圆窗压在 SEC/BAR 表上）。
    改成 fixed 之后人的位置是确定的，文字的版心也就能算准 —— 两边都不靠猜。
    """
    if fig["mode"] == "disc":
        return 2 * int(fig["r"]) + 48
    return int(int(fig["h"]) * 0.752) + 24


def layout_params(layk, mood, fig, r, nwords, key=None, avoid=None):
    """给版面填参数。参数只做「位置与比例」，字号一律交给版面自己反解 ——
    字号写在编排稿里，等于把排版交给导演层，长句必然溢出。"""
    m = MOOD[mood]
    p = {"k": layk, "availW": round(m["availW"], 3)}
    frac = (avoid or {}).get("frac", 0.0)
    free_left = (avoid is None) or avoid.get("side") != "l"
    # 版心：字永远从「空的那一侧」的边距起排。
    # 人在右 → 字从 0.075W 起；人在左 → 字从 (0.075 + frac)W 起。
    # 这里一度写反了：人站右边、字却从 0.36W 起排，整块字直接压到人身上。
    xL = 0.075 if free_left else 0.075 + frac
    # 上限取 MOOD 给的版心宽（它表达「这一段能排多宽」），再被人物占位压窄。
    # 原来写成 min(0.80, ...) —— 那是个硬顶，MOOD.availW 从来没生效过，
    # 长句因此一路排到 0.80W，折行怎么算都排不出均衡的三行。
    aw = max(0.30, min(m["availW"], 1.0 - 0.15 - frac))
    p["availW"] = round(aw, 3)

    if layk in ("block", "marginNote", "cropGiant", "vRuler"):
        if layk == "block":
            p.update(hK=m["hK"], x=round(xL, 3), cy=0.48, lead=1.16, maxRows=3)
            p["no"] = int(r.integers(1, 99))
        elif layk == "marginNote":
            p.update(pos="b" if r.random() < 0.7 else "t", x=round(xL, 3),
                     availW=round(min(aw, 0.52), 3),
                     note=("NOTE %02d" % int(r.integers(1, 99))),
                     no=int(r.integers(1, 99)))
        elif layk == "cropGiant":
            p.update(edge="b" if r.random() < 0.62 else "t",
                     mode="dim" if r.random() < 0.76 else "outline",
                     giantK=round(0.56 + 0.10 * r.random(), 3),
                     no=int(r.integers(1, 99)), x=round(xL, 3))
            # 巨型字占下半屏时，可读块必须在上半屏（反之亦然）
            p["side"] = "t" if p["edge"] == "b" else "b"
        elif layk == "vRuler":
            p.update(side="l" if free_left else "r", n=18)
    elif layk == "indexTable":
        p.update(side="r", rows=[["SEC", "B"], ["BAR", "%02d" % int(r.integers(1, 99))],
                                 ["KEY", pick(r, ["D", "A", "Bm", "G"])]])
    elif layk == "lowerThird":
        # 横带永远从「没人」的那侧伸进来：人在左 → 条从右缘伸，反之从画缘伸
        p.update(side="r" if (avoid and avoid.get("side") == "l") else "l",
                 no=int(r.integers(1, 99)),
                 meta=pick(r, ["VERSE", "CHORUS", "BRIDGE", "A", "B"]))
    elif layk == "keySplit":
        p.update(key=key, cx=0.5)
    elif layk == "cross":
        p.update(no=int(r.integers(1, 99)), cx=0.5)
    elif layk == "bandHide":
        p.update(bandK=round(0.30 + 0.10 * r.random(), 3),
                 bandCol=pick(r, ["accent", "ink", "accent"]),
                 dir=1 if r.random() < 0.5 else -1,
                 tag=pick(r, ["CHORUS", "B", "REFRAIN", "FULL"]))
    elif layk == "fullTrack":
        p.update(note=pick(r, ["CHORUS 04", "VERSE", "OUTRO"]))
    return p


def fig_script(mood, r, pack):
    """人物剧本（JIZURA 没有人物层，这一层是我们的加法）。

    平涂底上的照片必须像「印刷品里切好的一块」，不像贴纸：
      · **fixed**：贴边放置。位置确定，版心才能算准（auto 锚点会在六个位置里乱跳）
      · 硬边（feather = 0）+ 2px 实线框（引擎在 S.flat 时自己加）
      · 去饱和（mono）或轻双色调（duo），色温必须服从纸
      · 尺寸按语气给：quiet 只给一个圆窗，peak 才允许整块大幅
      · 只保留 disc / band / slab 三种：crop 是「半边出画」，宽度不可控，
        和平涂版面抢版心时算不准要让多少
    """
    modes = {"quiet": [("disc", 6), ("band", 4)],
             "drive": [("band", 6), ("disc", 3)],
             "peak": [("slab", 4), ("band", 4)]}[mood]
    mode = weighted(r, modes)
    side = "r" if r.random() < 0.60 else "l"
    fig = {
        "mode": mode, "side": side,
        "tone": "duo" if mode == "disc" else "mono",
        "motion": round(0.6 + 0.7 * r.random(), 2),
        "ph": round(r.random(), 3),
        "layer": "avoid",
        "fixed": True,
        "seq": "head" if mode == "disc" else "body",
        "feather": 0,
        "frame": 2,
    }
    if mode == "disc":
        fig.update(r=int(158 + 30 * r.random()), y=round(0.36 + 0.12 * r.random(), 3), a=0.94)
    elif mode == "band":
        fig.update(h=int(600 + 110 * r.random()), y=round(0.16 + 0.08 * r.random(), 3), a=0.90)
    else:  # slab
        fig.update(h=int(880 + 90 * r.random()), y=round(0.07 + 0.05 * r.random(), 3), a=0.76)
    return fig


def fig_band_fix(shots, w=1920):
    """兼容旧调用（tools/pv_figband.py）。

    第二版把「文字让开人物」做在导演层（layout_params 里按 fig.side 改 availW / x / cx），
    不再需要事后补丁。这个函数保留为稽核口：只报告「还有哪些镜头不符合新规则」。
    """
    bad = []
    for s in shots:
        f, lay = s.get("fig"), s.get("layout") or {}
        if f and lay.get("k") not in FIG_SAFE:
            bad.append((s.get("i"), round(s.get("t0", 0), 2), lay.get("k"), f.get("mode")))
    return bad


# ----------------------------------------------------------------- 主流程
# =====================================================================
# 晨光绘本（Innocence v2）：意象驱动模式（--concept dawn）
# 与默认的「抽卡」模式相反：逐句视觉**由歌词意象决定**，不是摇出来的。
# 输入 = docs/innocence-v2-imagery-map.json（47 句逐句：意象标签 / 版面 /
# 背景件 / 装饰件 / 入退场 / special 特判），本文件只做「字符串 → 引擎参数」翻译。
# =====================================================================
D_ENTER = {
    # 抒情曲的入场是「浮现/托起」，时长比 v1 长一档（0.85~1.2s），不做甩炸
    "softIn":     {"k": "softIn",     "dur": 0.85, "stagger": 0.045, "lead": 0.12},
    "fadeSlow":   {"k": "fadeSlow",   "dur": 1.20, "stagger": 0.060, "lead": 0.20},
    "typeOn":     {"k": "typeOn",     "dur": 0.34, "stagger": 0.050, "lead": 0.06},
    "dropBounce": {"k": "dropBounce", "dur": 0.72, "stagger": 0.050, "lead": 0.08},
}
D_EXIT = {
    "fade":      {"k": "fade",   "dur": 0.85},
    "fadeUp":    {"k": "fadeUp", "dur": 1.05},
    "pFallSlow": {"k": "pFall",  "dur": 1.25},   # 碎块慢落（时间流逝语义，全片仅 4 处）
}
D_BG = {
    "sunrise":     {"a": 0.17, "y": 0.74},
    "windowLight": {"a": 0.12, "n": 3, "ang": -28, "motes": 12},
    "halo":        {"a": 0.20, "r": 0.58, "x": 0.5, "y": 0.45},
    "watercolor":  {"a": 0.14, "n": 4},
    "arch":        {"a": 0.22},
    "petal":       {"a": 0.15, "n": 14},
    # 复用件（极淡）
    "dust":        {"a": 0.11},
    "leak":        {"a": 0.070},
    "rays":        {"a": 0.095},
}
D_DECOR = {
    "stitchLines":  {"a": 0.13, "m": 64},
    "photoCorners": {"a": 0.20, "m": 56},
    "tinyStars":    {"a": 0.28, "n": 10},
    "teardrop":     {"a": 0.24, "n": 5},
    "softCorners":  {"a": 0.15, "m": 48},
}
D_TRANS_POOL = [("wipe", 3), ("dissolve", 3), ("grad", 3), ("ink", 2), ("flash", 2),
                ("iris", 2), ("push", 2), ("mask", 2), ("zoom", 1)]
D_TRANS_DUR = {"wipe": 0.42, "ink": 0.48, "flash": 0.34, "iris": 0.44, "push": 0.40,
               "zoom": 0.40, "dissolve": 0.58, "grad": 0.50, "mask": 0.46}


def _dawn_face(spec):
    """字重/字族：正文 Nunito，情感短句 Fraunces 斜体，weight:max 上 Nunito 最粗。"""
    img = set(spec.get("img") or [])
    nw = len((spec.get("text") or "").split())
    if (img & {"brill", "cry"}) and nw <= 7:
        return "frauncesItB"
    if spec.get("weight") == "max":
        return "nunitoB"
    return "nunitoSemi"


def _fig_foot(fig):
    """画框在设计坐标里的占位（宽, 高）+ 是否横贯全宽。
    横贯全宽（wash/strip）⇒ 文字只能上下让位；其余按侧位让出整条版心。
    这里必须和 pv_engine.placeFigure 的几何一致，否则「让位」是假的、字照样压脸。"""
    m = fig.get("mode")
    sc = float(fig.get("scale") or 1)
    if m == "wash":
        return 1920, 1080, True
    if m == "strip":
        return 1920, int(fig.get("h", 380) * sc), True
    if m == "split":
        return int(1920 * fig.get("wf", 0.46)) + 10, 1080, False
    if m in ("pill", "round"):
        return int(fig.get("w", 760) * sc) + 24, int(fig.get("h", 300) * sc), False
    if m == "arch":
        h = int(fig.get("h", 640) * sc)
        return int(h * fig.get("wRatio", 0.62)) + 24, h, False
    if m == "soft":
        return int(1920 * fig.get("wf", 0.48)) + 24, int(1080 * fig.get("hf", 0.68)), False
    if m == "diag":
        wf = fig.get("wf", 0.44)
        sk = abs(fig.get("skew") if fig.get("skew") is not None else int(1920 * wf * 0.14))
        return int(1920 * wf) + sk + 24, int(1080 * fig.get("hf", 0.88)), False
    if m in ("disc", "duo"):
        r = int(fig.get("r", 215) * sc)
        return 2 * r + 40, 2 * r + 40, False
    if m == "crop":
        # 半边出画：占版面的只有**露出来的那一半**，不是整块。
        # 按整块宽让位会白留一大片 —— 实测 line 46 的框只露到 333px，
        # 版心却从 821 起排，中间空 488px（缩略图上读作「这块版面没排完」）。
        # 0.54 = 1 - 0.46（placeFigure 里 crop 的出画比例）。
        return int(int(fig.get("h", 620) * sc * fig.get("wRatio", 0.752)) * 0.54) + 24, \
            int(fig.get("h", 620) * sc), False
    return (int(fig.get("h", 620) * sc * fig.get("wRatio", 0.752)) + 24,
            int(fig.get("h", 620) * sc), False)


def _dawn_layout(spec, fig=None):
    sp = spec.get("special") or ""
    face = _dawn_face(spec)
    layk = spec.get("layout") or "block"
    if fig:
        # 现场画面入画面时，**文字必须让位**：center/scaleSteps 不支持版心参数，
        # 一律换 block 并把版心挤到「没图的那一侧」。只靠 placeFigure 的 bbox
        # 避让是不够的 —— 文字块本身占满中段时，六锚点里没有一个能躲开
        # （实测 t=24.5 圆窗压住 "I wouldn't change"、t=30.5 方框压住 "feeling"）。
        fw, fh, full = _fig_foot(fig)
        if full:
            if fig.get("mode") == "wash":
                # 满幅淡底不吃版面：字照常全宽。纵向渐隐（veilK）时下半是她，
                # 版心必须上移到 0.32，否则最重的那一行正好落在她脸上。
                if fig.get("veilK"):
                    return {"k": "block", "face": face, "x": 0.075, "availW": 0.86,
                            "cy": 0.30, "hK": 0.120, "lead": 1.16, "maxRows": 3}
                return {"k": "block", "face": face, "x": 0.075, "availW": 0.86,
                        "cy": 0.50, "hK": 0.130, "lead": 1.18, "maxRows": 3}
            top = fig.get("at") == "top"
            return {"k": "block", "face": face, "x": 0.075, "availW": 0.86,
                    "cy": 0.64 if top else 0.34, "hK": 0.115, "lead": 1.14, "maxRows": 3}
        frac = fw / 1920.0
        if fig.get("side") == "l":
            x = round(0.075 + frac + 0.02, 3)
            aw = round(max(0.30, 0.94 - x), 3)
        else:
            x = 0.075
            aw = round(max(0.30, 0.94 - frac - 0.075), 3)
        # 字号：先按句长给「无人物时该有的字号」，再被栏宽压住。
        # 早先这里写死 0.125 / 0.112 / 0.102 —— 于是六段人物镜里最长的句子
        # （"It's so beautiful,it makes you wanna cry"，40 字符）只有 0.102H，
        # 与相邻无人物镜的 0.168H 并排看，像从另一首歌里剪进来的。
        nch = len(spec.get("text") or "")
        base = 0.200 if nch <= 16 else (0.168 if nch <= 24 else
                                        (0.140 if nch <= 32 else 0.118))
        # 栏宽每多 0.10 才允许长一档，最多到 base
        hk = min(base, 0.118 + max(0.0, aw - 0.40) * 0.34)
        return {"k": "block", "face": face, "x": x, "availW": aw,
                "cy": 0.47, "hK": round(hk, 3), "lead": 1.16, "maxRows": 3}
    nw = len((spec.get("text") or "").split())
    # scaleSteps 是 bleed=true（不过安全网），词多时堆叠总高会顶出画外：
    # 8 词 × 0.17H 起步 ≈ 1.03H，首词直接被裁（实测桥段 t=127）。
    # bigK 随词数收：词越多起始越小，总高压进 0.72H 内。
    step_k = round(0.17 * 6.0 / max(6, nw), 3)
    if "offsetPair" in sp:
        # 双行错位的近似：scaleSteps 的逐词台阶读作「词在错位」，dir 逐句交替
        return {"k": "scaleSteps", "face": face, "bigK": step_k,
                "dir": "up" if (spec.get("line", 0) % 2 == 0) else "down"}
    if "threeWordCascade" in sp:
        return {"k": "scaleSteps", "face": face, "bigK": step_k, "dir": "up"}
    if sp == "cryGiant":
        return {"k": "center", "face": "frauncesItB", "track": -0.015}
    return {"k": layk, "face": face}


# ★ LEGACY（2026-10-30）：这张表已**不再被任何代码引用**，保留只作形态参数参考。
#   现行方案是下面的 LIVE_SPANS（六段 / 16.3%）。为什么废弃：29 处占了 63% 的句镜，
#   观感上她从「被看见」降级成「贴纸底纹」，而且每挂一处就把版心挤窄一次。
#   要恢复密排（例如做纯人物向的短切片），把 LIVE_SPANS 换回这张表即可 —— 参数都还在。
#
# 1027 演唱原视频的「入画面」节点（2026-10-29 三版：扩容 + 换画法）。
# ★ 一律 layer="avoid"：图先画、字后画 ⇒ 字永远在图之上；侧位面板还会按文字
#   bbox 避让选位（placeFigure），版面层再把版心挤到没图的那一侧（_dawn_layout）。
# ★ 画法不再只有圆窗/竖幅。九种语汇，全部用同一份 1027 原片序列：
#   disc  圆窗        脸，最轻，给短句
#   duo   双圆        两格「前一刻」，给需要呼吸的短句
#   band  3:4 竖幅    半身，主力
#   arch  拱窗        圆顶竖窗，比方框软、比圆窗「建筑」
#   pill  横胶囊      横向切一条脸，读作版面上的「印章」
#   split 纵向分屏    全高出血，占一侧；split2 再在内部切两格（此刻/刚才）
#   strip 横向满幅条  贴顶/贴底出血，字被推到另一半（长句专用，全宽覆盖）
#   soft  羽化大面    无边框，人像化进纸里
#   wash  满幅淡底    整帧压成一层水彩，字照常全宽（最长句专用）
# 入场/退场也不再是硬切：reveal（fade 叠化 / wipe 擦开 / grow 遮罩长开）+ out 叠出，
# 框内还有 zoom0→zoom1 的推镜 —— 素材自己在动，框才不会读作「贴上去的一张图」。
# 参数是 1920×1080 设计坐标（4K 渲染整体 scale×2）；y 在 disc/arch/pill/soft 里是中心，
# 在 band/strip 里是顶边（沿用旧语义）。
# ★ 静态 PNG 立绘（photoCircle/Square/Sticker/Strip）已整套移除：
#   固定坐标必然压字，抠图贴纸的质感也与晨光绘本语法不合。原视频入框才是正解。
DAWN_LIVE_DENSE_LEGACY = {
    # ==================== V1 晨醒 ====================
    # 0：37 字符长句 → 纵向渐隐底景（上半是纸，下半是她）。比横条稳：
    #    横条把脸切成一条缝，内容取决于那一秒她的姿势，十次有三次读作「切坏了」。
    0:  dict(mode="wash", seq="wide", layer="avoid", veilK=0.74, a=0.86, fy=0.46,
             fadeIn=0.9, out=0.7, zoom0=1.03, zoom1=0.10, motion=1.0, ph=0.05),
    # 2：45 字符 → 满幅淡底，整帧化进纸里，字照常全宽
    2:  dict(mode="wash", seq="wide", layer="avoid", a=0.40, lift=0.22, fy=0.50,
             fadeIn=1.2, out=1.1, zoom0=1.02, zoom1=0.06, motion=0.8, ph=0.31),
    4:  dict(mode="disc", seq="head", layer="avoid", side="r", r=215, y=0.42, a=0.92,
             edge="hard", motion=1.0, ph=0.12,
             reveal={"k": "grow", "dur": 0.55}, zoom0=1.08, zoom1=0.10),
    # 5：7.2s 长镜 → 纵向分屏 + 内部两格，推镜走得最慢
    5:  dict(mode="split", seq="body", layer="avoid", side="r", wf=0.44, hf=1.10,
             dt=0.9, fy=0.34, a=0.93, edge="hard",
             reveal={"k": "wipe", "dir": "R", "dur": 0.7}, out=0.8,
             zoom0=1.03, zoom1=0.09, motion=0.9, ph=0.37),
    6:  dict(mode="arch", seq="head", layer="avoid", side="r", h=650, wRatio=0.62,
             y=0.48, a=0.92, edge="hard",
             reveal={"k": "grow", "dur": 0.5}, out=0.5, zoom0=1.06, zoom1=0.08,
             motion=1.0, ph=0.61),
    7:  dict(mode="disc", seq="head", layer="avoid", side="l", r=186, y=0.40, a=0.90,
             edge="hard", motion=1.0, ph=0.74,
             reveal={"k": "grow", "dur": 0.45}, zoom0=1.06, zoom1=0.06),
    # ==================== C1 副歌一 ====================
    8:  dict(mode="pill", seq="head", layer="avoid", side="r", w=780, h=344, y=0.47,
             a=0.92, edge="hard", tick=True, fy=0.56,
             reveal={"k": "wipe", "dir": "R", "dur": 0.45}, out=0.45,
             zoom0=1.05, zoom1=0.08, motion=1.0, ph=0.90),
    10: dict(mode="band", seq="body", layer="avoid", side="l", h=520, y=0.19, a=0.90,
             edge="hard", motion=0.9, ph=0.83,
             reveal={"k": "wipe", "dir": "L", "dur": 0.5}, out=0.5,
             zoom0=1.05, zoom1=0.07),
    # 12：10.7s 全片最长的镜头 → 分屏 + 两格，推镜极慢（近景里她在动）
    12: dict(mode="split", seq="body", layer="avoid", side="l", wf=0.46, hf=1.10,
             dt=0.9, fy=0.30, a=0.93, edge="hard",
             reveal={"k": "fade", "dur": 1.0}, out=0.9,
             zoom0=1.02, zoom1=0.10, motion=0.9, ph=0.22),
    # ==================== V2 上下画框 ====================
    13: dict(mode="wash", seq="wide", layer="avoid", veilK=0.80, a=0.88, fy=0.44,
             fadeIn=0.85, out=0.65, zoom0=1.03, zoom1=0.09, motion=1.0, ph=0.44),
    14: dict(mode="wash", seq="wide", layer="avoid", veilK=0.70, a=0.82, fy=0.50,
             fadeIn=0.85, out=0.65, zoom0=1.04, zoom1=0.09, motion=1.0, ph=0.55),
    17: dict(mode="arch", seq="head", layer="avoid", side="l", h=620, wRatio=0.62,
             y=0.50, a=0.92, edge="hard",
             reveal={"k": "grow", "dur": 0.5}, out=0.5, zoom0=1.05, zoom1=0.08,
             motion=1.0, ph=0.68),
    # ==================== C2 副歌二 ====================
    18: dict(mode="split", seq="body", layer="avoid", side="r", wf=0.44, hf=1.10,
             dt=0.9, fy=0.32, a=0.93, edge="hard", tick=True,
             reveal={"k": "wipe", "dir": "R", "dur": 0.6}, out=0.7,
             zoom0=1.03, zoom1=0.09, motion=0.9, ph=0.15),
    20: dict(mode="disc", seq="head", layer="avoid", side="l", r=196, y=0.40, a=0.90,
             edge="hard", motion=1.0, ph=0.28,
             reveal={"k": "grow", "dur": 0.45}, zoom0=1.07, zoom1=0.07),
    # 21：双圆（主窗 + 下方小窗 = 同一口气的前后两格）
    21: dict(mode="duo", seq="head", layer="avoid", side="l", r=196, y=0.36, a=0.92,
             edge="hard", motion=1.0, ph=0.52,
             reveal={"k": "grow", "dur": 0.5}, out=0.5, zoom0=1.06, zoom1=0.07),
    23: dict(mode="band", seq="body", layer="avoid", side="r", h=508, y=0.20, a=0.90,
             edge="hard", motion=0.9, ph=0.05,
             reveal={"k": "wipe", "dir": "R", "dur": 0.5}, out=0.5,
             zoom0=1.05, zoom1=0.07),
    25: dict(mode="arch", seq="head", layer="avoid", side="r", h=600, wRatio=0.62,
             y=0.50, a=0.92, edge="hard",
             reveal={"k": "grow", "dur": 0.5}, out=0.5, zoom0=1.05, zoom1=0.08,
             motion=1.0, ph=0.79),
    # ==================== Br 桥段 ====================
    28: dict(mode="wash", seq="wide", layer="avoid", veilK=0.76, a=0.86, fy=0.46,
             fadeIn=0.95, out=0.7, zoom0=1.03, zoom1=0.10, motion=1.0, ph=0.13),
    29: dict(mode="wash", seq="wide", layer="avoid", a=0.38, lift=0.24, fy=0.50,
             fadeIn=1.2, out=1.1, zoom0=1.02, zoom1=0.06, motion=0.8, ph=0.66),
    31: dict(mode="wash", seq="wide", layer="avoid", veilK=0.72, a=0.84, fy=0.48,
             fadeIn=0.95, out=0.7, zoom0=1.03, zoom1=0.10, motion=1.0, ph=0.88),
    # ==================== FC 末段副歌 ====================
    33: dict(mode="arch", seq="head", layer="avoid", side="l", h=632, wRatio=0.62,
             y=0.49, a=0.92, edge="hard",
             reveal={"k": "grow", "dur": 0.5}, out=0.5, zoom0=1.05, zoom1=0.08,
             motion=1.0, ph=0.34),
    34: dict(mode="disc", seq="head", layer="avoid", side="l", r=200, y=0.41, a=0.92,
             edge="hard", motion=1.0, ph=0.19,
             reveal={"k": "wipe", "dir": "L", "dur": 0.4}, zoom0=1.08, zoom1=0.08),
    37: dict(mode="split", seq="body", layer="avoid", side="l", wf=0.42, hf=1.10,
             dt=0.9, fy=0.30, a=0.93, edge="hard",
             reveal={"k": "fade", "dur": 0.9}, out=0.8,
             zoom0=1.03, zoom1=0.09, motion=0.9, ph=0.47),
    38: dict(mode="disc", seq="head", layer="avoid", side="r", r=210, y=0.42, a=0.92,
             edge="hard", motion=1.0, ph=0.91,
             reveal={"k": "grow", "dur": 0.45}, zoom0=1.07, zoom1=0.08),
    39: dict(mode="pill", seq="head", layer="avoid", side="r", w=744, h=336, y=0.47,
             a=0.92, edge="hard", tick=True, fy=0.56,
             reveal={"k": "wipe", "dir": "R", "dur": 0.45}, out=0.45,
             zoom0=1.05, zoom1=0.09, motion=1.0, ph=0.60),
    42: dict(mode="arch", seq="head", layer="avoid", side="r", h=610, wRatio=0.62,
             y=0.49, a=0.92, edge="hard",
             reveal={"k": "grow", "dur": 0.5}, out=0.5, zoom0=1.05, zoom1=0.08,
             motion=1.0, ph=0.07),
    44: dict(mode="band", seq="body", layer="avoid", side="l", h=500, y=0.20, a=0.90,
             edge="hard", motion=0.9, ph=0.73,
             reveal={"k": "wipe", "dir": "L", "dur": 0.5}, out=0.5,
             zoom0=1.05, zoom1=0.07),
    45: dict(mode="disc", seq="head", layer="avoid", side="r", r=202, y=0.43, a=0.90,
             edge="hard", motion=1.1, ph=0.18,
             reveal={"k": "grow", "dur": 0.45}, zoom0=1.07, zoom1=0.07),
    # 46：7.3s 收尾长镜 → 分屏；两格 = 此刻与片刻前，最后一句留一个动作的延续
    46: dict(mode="split", seq="body", layer="avoid", side="r", wf=0.46, hf=1.10,
             dt=0.9, fy=0.30, a=0.93, edge="hard",
             reveal={"k": "fade", "dur": 1.0}, out=1.0,
             zoom0=1.02, zoom1=0.10, motion=0.9, ph=0.84),
}


# ==================== 直播画面入画「叙事表」（2026-10-30 v2）====================
# 上一版 DAWN_LIVE 挂了 29 处：46 个句镜里 63% 都有人。密到「每镜一个大头贴」之后，
# 她从「被看见」降级成「贴纸底纹」，而且每个镜头的版心都被挤窄、长句被迫缩成小字。
# 这一版收成**六段**（37.9s / 全片 16.3%），一次单向的接近 —— 距离越来越近，
# 框越来越不可忽略，每次出现都比上一次更「在场」：
#
#   ① 23.4s V1 末   disc 圆窗（最小）        第一次看见她，像纸上戳开一个孔
#   ② 50.4s C1 末   arch 拱窗（拉开）        窗有了建筑感，她走进画面
#   ③ 91.9s C2 起   split 竖幅分屏           占住右手一侧，纸变成两栏
#   ④ 146.6s Br     band 竖幅 + 角标         桥段最高点，最近的一次对视
#   ⑤ 171.0s FC     duo 双圆（此刻/片刻前）  同一口气的前后两格
#   ⑥ 208.0s 末句   crop 半边出画（左侧）    「don't you let it pass you by」——
#                                            她被画框切掉一半，正是挽留的形状
#
# 左右六次交替（r l r l r l）：连起来读是「视线在她与纸之间来回」；
# 全放同一侧的话，六次出现会读成同一个模板换六句话。
#
# win=(成片起点秒, 时长秒)：抽片窗口，前后各留 ~2s —— reveal/out 要走完，
# getFrame 还有 ±0.25s 的缺帧回落半径。**时间窗只在这里定义一次**：
# tools/pv_live.py 照它抽素材，导演层照它挂 fig 并推 tOff，两边不会各自漂。
LIVE_SPANS = {
    4: dict(mode="disc", seq="s1", layer="avoid", side="r", r=200, y=0.42,
            a=0.94, paper=1.0, ph=0.12,
            reveal={"k": "grow", "dur": 0.55}, out=0.55,
            zoom0=1.06, zoom1=0.10, motion=1.0, win=(21.41, 9.89)),
    10: dict(mode="arch", seq="s2", layer="avoid", side="l", h=620, wRatio=0.66,
             y=0.46, a=0.93, edge="hard", paper=0.95, ph=0.37,
             reveal={"k": "wipe", "dir": "L", "dur": 0.55}, out=0.55,
             zoom0=1.05, zoom1=0.09, motion=1.0, win=(48.39, 9.50)),
    18: dict(mode="split", seq="s3", layer="avoid", side="r", wf=0.30, hf=1.06,
             fy=0.30, a=0.94, edge="hard", paper=0.82, ph=0.61,
             reveal={"k": "wipe", "dir": "R", "dur": 0.60}, out=0.70,
             zoom0=1.03, zoom1=0.09, motion=0.9, win=(89.88, 10.75)),
    31: dict(mode="band", seq="s4", layer="avoid", side="l", h=760, y=0.13,
             a=0.94, edge="hard", tick=True, fy=0.26, paper=0.90, ph=0.83,
             reveal={"k": "grow", "dur": 0.50}, out=0.60,
             zoom0=1.05, zoom1=0.08, motion=0.9, win=(144.62, 10.79)),
    37: dict(mode="duo", seq="s5", layer="avoid", side="r", r=196, y=0.38,
             a=0.93, paper=1.0, ph=0.28,
             reveal={"k": "grow", "dur": 0.50}, out=0.55,
             zoom0=1.06, zoom1=0.08, motion=1.0, win=(168.98, 9.63)),
    46: dict(mode="crop", seq="s6", layer="avoid", side="l", h=820, y=0.09,
             a=0.94, edge="hard", tick=True, fy=0.28, paper=0.92, ph=0.44,
             reveal={"k": "wipe", "dir": "D", "dur": 0.70}, out=0.80,
             zoom0=1.03, zoom1=0.10, motion=0.9, win=(206.00, 11.30)),
}


# 演唱写法 vs 官方写法的规范化：同一句歌词在官方歌词里是 "wanna"，
# 在实唱对齐的 cut.lrc 里可能是 "want to"（反之亦然）。只做「去掉标点/大小写差异 +
# 合并已知同音缩写」，不做模糊匹配 —— 真错行时相似度会低到 0.2 以下。
_LYR_ALIAS = [
    (r"\bwant\s+to\b", "wanna"), (r"\bgoing\s+to\b", "gonna"),
    (r"\bbecause\b", "cause"), (r"\byou\s+all\b", "yall"),
    (r"\bgonna\b", "gonna"), (r"\bcannot\b", "cant"),
]


def _lyr_norm(s):
    s = (s or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    s = re.sub(r"[^a-z0-9']+", " ", s)
    for pat, rep in _LYR_ALIAS:
        s = re.sub(pat, rep, s)
    return re.sub(r"\s+", " ", s.replace("'", "")).strip()


def _lyr_sim(a, b):
    """两句的相似度 0~1（规范化后按字符相似）。错行时接近 0，写法差异时 ≥0.9。"""
    import difflib
    x, y = _lyr_norm(a), _lyr_norm(b)
    if x == y:
        return 1.0
    return difflib.SequenceMatcher(None, x, y).ratio()



# ---------------------------------------------------------------------------
# 置景（dress）：把「排版卡」变成「有场景的版面」
#
# 问题（2026-10-30 复盘）：dawn 模式跑出来的成片，47 句里有 12 句一个背景件都没有，
# 其余多数只有一枚 alpha 0.20 的 halo —— 整片读作「大字贴在奶油纸上」，
# 也就是「不像手书 PV」。器件库并不缺（PV.BG 19 件 / PV.DECOR 25 件），缺的是
# **每镜的最低置景配额**。这里按段落给一套场景（底色 / 结构 / 光 / 活物），
# 只补缺项，不覆盖映射表已经写好的意象。
#
# 纪律：
#   · 全部走 seeded rng（a.seed ^ 镜头号）→ 同 seed 同结果，确定性验收不受影响；
#   · alpha 上限：底 0.18 / 结构 0.07 / 光 0.20 / 活物 0.16 ——「留白是正片」，
#     置景是让留白看起来有纸、有空气，不是把画面填满；
#   · 有人的镜头（live 生效）只补光与活物，不动版心，避免和 placeFigure 打架。
SEC_SCENE = {
    "intro": dict(base=("watercolor", {"a": 0.34, "n": 4}),
                  light=("sunrise", {"a": 0.30, "y": 0.78}),
                  struct=("grid", {"a": 0.130, "step": 96}),
                  life=("dust", {"a": 0.20})),
    "V1": dict(base=("watercolor", {"a": 0.30, "n": 3}),
               light=("windowLight", {"a": 0.24, "n": 2, "ang": -26}),
               struct=("hatch", {"a": 0.115}),
               life=("dust", {"a": 0.19})),
    "C1": dict(base=("wash", {"a": 0.38, "n": 3, "col": "accent"}),
               light=("rays", {"a": 0.26, "n": 3, "rot": -0.22}),
               struct=("halftone", {"a": 0.145, "step": 20}),
               life=("petal", {"a": 0.22, "n": 10})),
    "V2": dict(base=("watercolor", {"a": 0.29, "n": 3}),
               light=("windowLight", {"a": 0.23, "n": 2, "ang": 22}),
               struct=("grid", {"a": 0.120, "step": 120}),
               life=("dust", {"a": 0.18})),
    "C2": dict(base=("wash", {"a": 0.36, "n": 3, "col": "accent"}),
               light=("rays", {"a": 0.26, "n": 4, "rot": 0.18}),
               struct=("halftone", {"a": 0.140, "step": 18}),
               life=("petal", {"a": 0.23, "n": 12})),
    "Br": dict(base=("wash", {"a": 0.26, "n": 2, "col": "ghost"}),
               light=("arch", {"a": 0.30, "h": 760, "w": 520, "x": 0.5, "y": 0.52}),
               struct=("topo", {"a": 0.140, "rings": 9, "rot": -0.12}),
               life=("dust", {"a": 0.20})),
    "FC": dict(base=("wash", {"a": 0.40, "n": 4, "col": "accent"}),
               light=("sunrise", {"a": 0.32, "y": 0.80}),
               struct=("halftone", {"a": 0.155, "step": 16}),
               life=("petal", {"a": 0.24, "n": 14})),
    "outro": dict(base=("watercolor", {"a": 0.32, "n": 4}),
                  light=("sunrise", {"a": 0.26, "y": 0.90}),
                  struct=("grid", {"a": 0.110, "step": 140}),
                  life=("petal", {"a": 0.22, "n": 16})),
}
# 「已有同类」判据：按 k 归类，避免同一类叠两层
_K_BASE = {"watercolor", "wash", "mega", "panel"}
_K_STRUCT = {"grid", "hatch", "halftone", "topo", "rings", "ink"}
_K_LIGHT = {"sunrise", "windowLight", "arch", "rays", "halo", "leak"}

# ---------------------------------------------------------------------------
# 字后图形层（shot.bg）
# ---------------------------------------------------------------------------
# 引擎的绘制顺序：paper → bgl（背景）→ bg（字后装饰）→ 人物 → 字 → decor → post。
# `shot.bg` 是**唯一能压在字下面、当版面骨架**的那一层。它一直是空的，
# 于是 49 帧里 47 帧只有「一行字 + 一根发丝下划线」，12 帧接触表全部同构 ——
# 不是字体或版式不行，是画面里没有笔。
#
# 这一层只放 stroke / 低 alpha 色块，全部按「版心之外」或「字行正下方」落位，
# 保证不压歌词（RULES：歌词不得被任何画框遮挡）。
_SHAPE_RECIPE = {
    "intro": ["line", "circle", "marker"],
    "V1":    ["line", "marker", "scrawl"],
    "C1":    ["band", "circle", "line"],
    "V2":    ["line", "scrawl", "marker"],
    "C2":    ["band", "circle", "marker"],
    "Br":    ["scrawl", "line"],
    "FC":    ["band", "circle", "line", "marker"],
    "outro": ["circle", "line", "scrawl"],
}
_DW, _DH = 1920.0, 1080.0          # 设计坐标系（PV.W / PV.H）


def _mk_shape(kind, sh, rnd):
    """造一个「字后」图形件。坐标一律 1920×1080 设计坐标。"""
    lay = sh.get("layout") or {}
    cy = float(lay.get("cy") or 0.47)
    lx = float(lay.get("x") or 0.06)
    aw = float(lay.get("availW") or 0.88)
    seed = 1000 + int(sh.get("i", 0)) * 37 + rnd.randrange(97)
    accent = rnd.random() < 0.45

    if kind == "line":
        # 手绘长线：**走向必须散开**。第一版全是 x0≈100→x1≈1830 的满幅横线，
        # 12 帧里 8 帧都是「中间一行字 + 上方一根横线」——单看每帧不丑，
        # 连起来就是同一张版换了字。三种走向按 3:2:5 分：
        #   竖（贴边，当分割线） / 斜（一角到另一角） / 横（长短位置都随机）
        r = rnd.random()
        accent = rnd.random() < 0.45
        seed2 = seed
        if r < 0.30:
            x = (_DW * (0.045 if rnd.random() < 0.5 else 0.955))
            return {"k": "brush",
                    "x0": round(x), "y0": round((0.05 + rnd.random() * 0.10) * _DH),
                    "x1": round(x + (rnd.random() - 0.5) * 26),
                    "y1": round((0.86 + rnd.random() * 0.09) * _DH),
                    "bow": round((rnd.random() - 0.5) * 46),
                    "w": round(13 + rnd.random() * 8), "wob": 2.0,
                    "a": round(0.34 + rnd.random() * 0.22, 3),
                    "grow": round(0.70 + rnd.random() * 0.50, 2),
                    "seed": seed2, "col": ("accent" if accent else None)}
        if r < 0.50:
            flip = rnd.random() < 0.5
            y0 = (0.10 + rnd.random() * 0.16) * _DH
            y1 = (0.74 + rnd.random() * 0.16) * _DH
            xa = (0.06 + rnd.random() * 0.14) * _DW
            xb = (0.80 + rnd.random() * 0.13) * _DW
            return {"k": "brush",
                    "x0": round(xb if flip else xa), "y0": round(y0),
                    "x1": round(xa if flip else xb), "y1": round(y1),
                    "bow": round((rnd.random() - 0.5) * 90),
                    "w": round(12 + rnd.random() * 9), "wob": 2.2,
                    "a": round(0.30 + rnd.random() * 0.20, 3),
                    "grow": round(0.85 + rnd.random() * 0.60, 2),
                    "seed": seed2, "col": ("accent" if accent else None)}
        up = cy <= 0.54
        y = (0.150 + rnd.random() * 0.080) if up else (0.762 + rnd.random() * 0.080)
        dy = (rnd.random() - 0.5) * 0.014
        # 横向：长度 0.35~1.0 幅宽，起点随机 —— 不再每条都顶到两边
        ln = 0.35 + rnd.random() * 0.65
        x0 = (0.04 + rnd.random() * (1.0 - ln - 0.06)) * _DW
        return {"k": "brush",
                "x0": round(x0), "y0": round(y * _DH),
                "x1": round(x0 + ln * _DW), "y1": round((y + dy) * _DH),
                "bow": round((rnd.random() - 0.5) * 58),
                "w": round(15 + rnd.random() * 9), "wob": 2.4,
                "a": round(0.40 + rnd.random() * 0.24, 3),
                "grow": round(0.55 + rnd.random() * 0.45, 2),
                "seed": seed, "col": ("accent" if accent else None)}

    if kind == "circle":
        # 手画圈：圈在版心的对侧留白里（右上 / 左下），像「圈住了一个词」
        right = rnd.random() < 0.5
        cx = (0.845 if right else 0.155) * _DW + (rnd.random() - 0.5) * 70
        cyy = (0.215 if cy > 0.5 else 0.795) * _DH + (rnd.random() - 0.5) * 60
        return {"k": "circleLoose", "x": round(cx), "y": round(cyy),
                "rx": round(150 + rnd.random() * 105),
                "ry": round((150 + rnd.random() * 105) * 0.62),
                "w": round(10 + rnd.random() * 6),
                "a": round(0.40 + rnd.random() * 0.22, 3),
                "grow": round(0.60 + rnd.random() * 0.50, 2),
                "seed": seed, "col": ("accent" if accent else None)}

    if kind == "marker":
        # 马克笔痕：压在**字行正下方**当加粗下划线（不压字，安全）
        h = round(38 + rnd.random() * 22)
        return {"k": "marker",
                "x": round(lx * _DW), "y": round((cy + 0.082) * _DH),
                "w": round(aw * _DW * (0.55 + rnd.random() * 0.34)),
                "h": h, "a": round(0.24 + rnd.random() * 0.14, 3),
                "grow": round(0.40 + rnd.random() * 0.40, 2),
                "seed": seed, "col": "accent"}

    if kind == "band":
        # 竖直色带：贴一侧边缘，给构图一条「硬边」
        right = rnd.random() < 0.5
        bw = round((0.055 + rnd.random() * 0.085) * _DW)
        return {"k": "block",
                "x": round((_DW - bw - 40) if right else 40),
                "y": round((0.06 + rnd.random() * 0.10) * _DH),
                "w": bw, "h": round((0.62 + rnd.random() * 0.28) * _DH),
                "a": round(0.085 + rnd.random() * 0.075, 3),
                "col": None}

    if kind == "scrawl":
        # 长波浪：贯穿画面的一根「画」，给最空的段压出层次。
        # 必须落在**版心之外**的带子里：细线（w≈3、alpha≈0.2）穿字不会读作
        # 层次，只会读作「画面脏了」。原来 ±0.15 的抖动半径在 cy=0.47 时
        # 直接压到字行上（实测 y=536/1080，amp=88 能扫到 448~624）。
        band = 1 if rnd.random() < 0.5 else -1
        return {"k": "scrawl",
                "y": round((cy + band * (0.245 + rnd.random() * 0.135)) * _DH),
                "amp": round(44 + rnd.random() * 46),
                "ph": round(rnd.random() * 6.28, 2),
                "w": round(2.8 + rnd.random() * 2.0),
                "a": round(0.16 + rnd.random() * 0.14, 3),
                "grow": round(1.1 + rnd.random() * 0.9, 2),
                "col": ("accent" if accent else None)}
    return None



def _pick_layout(text, base_k, rnd, prev_k, special):
    """版式轮转：映射表把 28/47 句都写成 block（同一块版心重复 28 次 = 读作字幕）。
    只对**没有人物**的镜头轮换，且按句长/词数做形态约束（同本文件头部的版式词汇
    表：词多的不能上 keySplit、句子长的不能上「下部 telop」）。"""
    if special in ("cryGiant", "offsetPair", "threeWordCascade"):
        return base_k                      # 特别句的版式是刻意选的，不动
    nw = len((text or "").split())
    nc = len(text or "")
    # 只允许「整句仍然可读」的版式。keySplit / scaleSteps 会把次要词压到 0.27 倍，
    # 6 词以上的句子缩成 10px 的小字（实测 t=75「The first time…」那条完全读不了），
    # 所以它们只在 ≤4 词的短句里出现。
    if nc >= 34:
        pool = ["block", "cropGiant", "lowerThird", "block"]
    elif nc >= 20:
        pool = ["block", "lowerThird", "bandHide", "indexTable", "block"]
    elif nw <= 4:
        pool = ["keySplit", "center", "cross", "scaleSteps", "fullTrack", "block"]
    elif nw <= 6:
        pool = ["center", "cross", "fullTrack", "block"]
    else:
        pool = ["block", "fullTrack", "lowerThird", "block"]
    if rnd.random() > 0.30:
        return base_k                      # 70% 尊重映射表的原版面
    for _ in range(4):
        k = pool[rnd.randrange(len(pool))]
        if k != prev_k and k != base_k:
            return k
    return base_k


def dress_shots(shots, seed, lines=None):
    """按段落给每镜补置景，并对无人物镜头做版式轮转。原地修改并返回。"""
    import random as _random
    prev_k = None
    for sh in shots:
        if sh.get("kind") != "line":
            continue
        rnd = _random.Random((seed ^ (sh["i"] * 2654435761)) & 0xFFFFFFFF)
        scene = SEC_SCENE.get(sh.get("section", ""), SEC_SCENE["V1"])
        bgl = list(sh.get("bgl") or [])
        have = {d.get("k") for d in bgl}

        if not (have & _K_BASE):
            k, d = scene["base"]
            bgl.insert(0, dict(d, k=k))            # 底色：整段一致
        if not (have & _K_STRUCT) and rnd.random() < 0.75:
            k, d = scene["struct"]
            bgl.append(dict(d, k=k, a=round(d["a"] * (0.8 + 0.5 * rnd.random()), 3)))
        if not (have & _K_LIGHT):
            k, d = scene["light"]
            bgl.append(dict(d, k=k))               # 光：每镜都要有方向光
        if not (have & {"dust", "petal"}) and rnd.random() < 0.60:
            k, d = scene["life"]
            bgl.append(dict(d, k=k, a=round(d["a"] * (0.85 + 0.3 * rnd.random()), 3)))
        sh["bgl"] = bgl

        # 字后图形层：引擎里 drawDecor(shot.bg) 排在字**之前**，
        # 是唯一能压字当版面骨架的位置。这一层空着 = 只有字和渐变。
        if not (sh.get("bg") or []):
            pool = _SHAPE_RECIPE.get(sh.get("section", ""), _SHAPE_RECIPE["V1"])
            picks = list(pool)
            rnd.shuffle(picks)
            n = 1 if rnd.random() < 0.42 else 2
            shapes = []
            for kd in picks[:n]:
                one = _mk_shape(kd, sh, rnd)
                if one:
                    shapes.append(one)
            # 出现次序错开：第二个迟一点「补画上去」，读作手在画而不是贴图
            if len(shapes) == 2:
                shapes[1]["at"] = round(0.25 + rnd.random() * 0.55, 2)
                shapes[1]["fade"] = 0.3
            sh["bg"] = shapes

        if not (sh.get("decor") or []):
            picks = ["cropMarks", "photoCorners", "stitchLines", "vrule", "brackets"]
            k = picks[rnd.randrange(len(picks))]
            d = {"k": k, "m": 56, "seed": 11 + sh["i"] * 7, "a": 0.16}
            if k == "vrule":
                d["side"] = "l" if float((sh.get("layout") or {}).get("x") or 0.075) >= 0.5 else "r"
                d["a"] = 0.16
            elif k == "brackets":
                d["a"] = 0.18
            sh["decor"] = [d]

        if not sh.get("fig"):
            # 判据只看**这一镜自己有没有图**，不看全局「这一版有没有开 live」。
            # 早先写 `not has_live and not sh.get("fig")`：一旦启用直播画面，
            # 46 个镜头全部跳过版式轮转与主字分级，退回映射表的 0.102H「小字幕」——
            # 于是六段人物镜之外的那 40 镜，画面整体退回上一版之前的样子，
            # 而且因为没人报错，会误以为是「加了人之后变丑了」。
            lay = dict(sh.get("layout") or {})
            base_k = lay.get("k") or "block"
            # 逐句文本不在 shot 里（shot 只存 line 下标），必须回 lines 取 ——
            # 拿空串去算词数会把每一句都判成「短句」，于是长句也被分到 keySplit
            # （实测 t=75 那句被压成 10px 小字，整句读不了）。
            _ln = sh.get("line")
            _txt = ""
            if lines and isinstance(_ln, int) and 0 <= _ln < len(lines):
                _txt = (lines[_ln].get("text") or "")
            nk = _pick_layout(_txt, base_k, rnd, prev_k, sh.get("special") or "")
            if nk != base_k:
                lay = {"k": nk, "face": lay.get("face")}
                if nk == "indexTable":
                    lay["meta"] = sh.get("section", "")
                if nk == "lowerThird":
                    lay["label"] = True
                sh["layout"] = lay
            # 主字大小：映射表一律用 0.102H（≈110px/1080），连起来读是「小字幕」。
            # 手书 PV 的主字按句长分级：短句 0.20H、中句 0.15H、长句 0.12H。
            # 只改 block（其余版式自己算字号），且只在没有人物时改。
            # 形态否决：映射表也会给长句排窄栏（实测第 4 句 9 个词排成 8 行竖条，
            # 读作「坏版」）。版面词汇表本身就是**形态约束**，先否决再谈轮转。
            cur0 = sh.get("layout") or {}
            if cur0.get("k") in ("vRuler", "bandHide", "keySplit", "scaleSteps", "cross") \
                    and (len(_txt) > 30 or len((_txt or "").split()) > 7):
                sh["layout"] = {"k": "block", "face": cur0.get("face")}

            cur = sh.get("layout") or {}
            if cur.get("k") == "block" and cur.get("hK") is None:
                nc2 = len(_txt)
                hk = 0.200 if nc2 <= 16 else (0.168 if nc2 <= 24 else
                                              (0.140 if nc2 <= 32 else 0.118))
                # 版心纵向三档：26 镜全用 cy=0.47 时，连起来看是「同一块版换了字」。
                # 手书 PV 的版面是**在画面里跳的**（贴上/居中/压低），
                # 三档都在安全区（0.345~0.575，加半行高后仍在 0.70 以内）。
                cyv = rnd.choice([0.345, 0.455, 0.575])
                cur = dict(cur, hK=hk, cy=cyv, availW=0.88, lead=1.12, maxRows=3)
                sh["layout"] = cur
            prev_k = (sh.get("layout") or {}).get("k")
    return shots


def _live_fig(i, a):
    """第 i 句要不要放直播画面。空串 --live-json 时一律不放（抽卡测试用）。

    tOff 是从 win 推出来的：分段抽的序列帧 0 对应成片 win.start 秒，
    引擎按 (t + tOff) 索引，所以 tOff = -win.start。两边共用一个 win，
    不会出现「抽了 9s、引擎却按 12s 取」这种静默错位。
    """
    if not getattr(a, "live_json", "") or i not in LIVE_SPANS:
        return None
    f = dict(LIVE_SPANS[i])
    start, _dur = f.pop("win")
    f["tOff"] = round(-start, 3)
    return f


def build_dawn_shots(a, lines, m, dur):
    """把意象映射表翻译成编排稿镜头（不抽卡：一切由歌词意象决定）。"""
    from collections import Counter
    ml = m["lines"]
    if len(ml) != len(lines):
        raise SystemExit("[dawn] 映射 %d 句 ≠ 窗口内 %d 句，先对齐 timing.json"
                         % (len(ml), len(lines)))
    for i, (spec, l) in enumerate(zip(ml, lines)):
        t1 = (spec.get("text") or "").strip()
        t2 = (l.get("text") or "").strip()
        # 判据是「这两句是不是同一句」，不是「字面是否逐字相等」：
        #   映射表按官方歌词写（"wanna" / 逗号后带空格），timing 来自实唱对齐的 cut.lrc
        #   （"want to" / 逗号后不带空格）。同一句在歌里两种写法都出现，逐字比较会把
        #   整份编排稿挡回去（第 28/34 句实测）。改成「规范化 + 相似度」，仍然能抓住
        #   真正的错行 —— 错行的词重叠接近 0，ratio 会远低于阈值。
        if _lyr_sim(t1, t2) < 0.85:
            raise SystemExit("[dawn] 第 %d 句文本不一致：映射=%r 时机=%r" % (i, t1, t2))

    secs = m.get("sections") or []
    def sec_at(t):
        for s in secs:
            if s["t0"] <= t < s["t1"]:
                return s
        return secs[-1] if secs else {"name": "V1", "scheme": 0}

    outro_t0 = None
    for s in secs:
        if s.get("name") == "outro":
            outro_t0 = s["t0"]
    if outro_t0 is None:
        outro_t0 = ml[-1]["t1"]

    # pal 字符串 "2→4"：同标签的第 1 镜取左值、之后取右值（跨镜渐溶由转场承担）
    pal_span = {}

    shots = []
    recall_trans = {}
    seen_key = Counter()
    # 转场用「确定性轮转」而不是逐镜抽签：回访组会把同一次抽签放大 3~5 倍
    # （实测 zoom 权重 1/14 却占了 32% 的镜头）。轮转按权重铺开一个序列，
    # 每出现一个**新**分组就前进一格 —— 分布精确、同组复用、双渲一致。
    _tpool = []
    for _k, _w in D_TRANS_POOL:
        _tpool += [_k] * _w
    _tseq = rng(a.seed, 777, 3)          # 只洗一次牌（确定性）
    _perm = list(range(len(_tpool)))
    for _i in range(len(_perm) - 1, 0, -1):
        _j = int(_tseq.integers(0, _i + 1))
        _perm[_i], _perm[_j] = _perm[_j], _perm[_i]
    _tpool = [_tpool[_p] for _p in _perm]
    _tpos = [0]

    def pick_trans(key):
        if key and key in recall_trans:
            return json.loads(json.dumps(recall_trans[key]))
        k = _tpool[_tpos[0] % len(_tpool)]
        _tpos[0] += 1
        tr = {"k": k, "dur": D_TRANS_DUR[k]}
        if key:
            recall_trans[key] = json.loads(json.dumps(tr))
        return tr

    def bg_items(spec):
        out = []
        for name in spec.get("bg") or []:
            if name not in D_BG:
                continue
            d = dict(D_BG[name])
            # boost / haloR：统一感回访时 halo 递进（记忆点越来越亮/越来越大）
            if name == "halo":
                if spec.get("haloR"):
                    d["r"] = round(d["r"] * spec["haloR"], 3)
                if spec.get("boost"):
                    d["a"] = round(min(0.30, d["a"] * spec["boost"]), 3)
            out.append(dict(d, k=name))
        return out

    def decor_items(spec, i):
        out = []
        for name in spec.get("decor") or []:
            if name not in D_DECOR:
                continue
            out.append(dict(D_DECOR[name], k=name, seed=11 + i * 7))
        return out

    def pal_idx(spec, sec):
        pv = spec.get("pal")
        if pv is None:
            return int(sec.get("scheme", 0))
        if isinstance(pv, str) and "→" in pv:
            lo, hi = pv.split("→", 1)
            if pv not in pal_span:
                pal_span[pv] = 0
            pal_span[pv] += 1
            return int(lo) if pal_span[pv] == 1 else int(hi)
        return int(pv)

    # ---- 片头卡（钢琴前奏 0 ~ 第一句）
    shots.append({
        "i": 0, "t0": 0.0, "t1": round(ml[0]["t0"], 3),
        "line": -1, "kind": "card", "tier": 1, "section": "intro",
        "text": a.title or m.get("_meta", {}).get("song", "Innocence"),
        "layout": {"k": "titleCard", "face": "frauncesItB",
                   "kicker": "ORIGINAL SONG", "artist": a.artist,
                   "meta": "VOCAL %s · %s" % (a.vocal, a.date), "shadow": True},
        "enter": {"k": "fadeSlow", "dur": 1.2, "stagger": 0.05, "lead": 0.20},
        "hold": {"k": "breathe", "amp": 0.55},
        "exit": {"k": "fade", "dur": 0.85},
        "type": {}, "treat": {"k": "solid"},
        "pal": {"pack": "dawn", "idx": 0},
        "bgl": [dict(D_BG["sunrise"], k="sunrise"),
                dict(D_BG["windowLight"], k="windowLight", n=2, a=0.10)],
        "bg": [], "decor": [dict(D_DECOR["softCorners"], k="softCorners", seed=3)],
        "post": [], "cam": {"k": "push", "amp": 0.5},
        "energy": 0.30, "sync": False, "wobble": 0, "unified": False,
        "special": "intro-card", "note": "intro / 破晓 / titleCard",
    })

    # ---- 逐句
    for i, spec in enumerate(ml):
        t0 = round(spec["t0"], 3)
        t1 = round(ml[i + 1]["t0"], 3) if i + 1 < len(ml) else round(outro_t0, 3)
        t1 = max(t1, t0 + 0.30)
        sec = sec_at(spec["t0"])
        sp = spec.get("special") or ""
        key = spec.get("recallKey")
        img = spec.get("img") or []

        ent = dict(D_ENTER.get(spec.get("enter") or "softIn", D_ENTER["softIn"]))
        xit = dict(D_EXIT.get(spec.get("exit") or "fade", D_EXIT["fade"]))
        hold = {"k": "none"} if spec.get("hold") == "none" else {"k": "breathe", "amp": 0.55}
        cam = {"k": "float", "amp": 0.45}
        if spec.get("cam") == "push":
            cam = {"k": "push", "amp": 0.62}
        elif spec.get("cam") == "drift" and sp != "slowPan":
            cam = {"k": "drift", "amp": 0.9}
        if sp == "slowPan":
            cam = {"k": "drift", "amp": 2.6}      # 全片唯一横移长镜
        if "freeze2s" in sp:
            hold = {"k": "none"}                  # 「不变」= 字完全静止
        if "drag3px" in sp:
            hold = {"k": "counterDrift", "amp": 0.40}   # 挽留的拉扯感
        wob = 0.0
        if sp.startswith("wobble"):
            try:
                wob = float(sp.replace("wobble", ""))
            except ValueError:
                wob = 0.5

        bgl = bg_items(spec)
        if sp == "mirrorDust":
            bgl += [{"k": "halo", "x": 0.22, "y": 0.5, "r": 0.26, "a": 0.13},
                    {"k": "halo", "x": 0.78, "y": 0.5, "r": 0.26, "a": 0.13}]
        post = []
        if sp == "ghostFlash":
            post.append({"k": "chromaBurst", "a": 0.55, "at": round(t0 + 0.06, 3),
                         "decay": 0.32})
        if spec.get("desat"):
            post.append({"k": "desat", "a": float(spec["desat"])})

        energy = 0.40
        if "brill" in img:
            energy = 0.85
        elif "cry" in img:
            energy = 0.70
        elif "hold" in img:
            energy = 0.62

        fg = _live_fig(i, a)
        sh = {
            "i": len(shots), "t0": t0, "t1": t1,
            "line": i, "kind": "line",
            "tier": 1 if energy < 0.6 else (2 if energy < 0.8 else 3),
            "section": sec.get("name", ""),
            "energy": energy,
            "layout": _dawn_layout(spec, fg),
            "type": {},
            "enter": ent, "hold": hold, "exit": xit,
            "treat": {"k": "solid"},
            "pal": {"pack": "dawn", "idx": pal_idx(spec, sec)},
            "bgl": bgl, "bg": [],
            "decor": decor_items(spec, i),
            "post": post, "cam": cam,
            # 没有 live 序列时**不能**保留画框：画框会把版心挤到 0.42 宽、右边
            # 留一块空白给一个不存在的人（实测 t=29.3 整块字挤在左上、右侧空一半）。
            # 反过来，挂了 fig 的镜头由 _dawn_layout 把字排到没图的一侧，两者必须同进同退。
            "fig": fg,
            "sync": False, "wobble": wob,
            "unified": False,
            "special": sp,
            "note": "%s / %s / %s" % (sec.get("name", ""), "+".join(img), sp or "-"),
        }
        if spec.get("scale"):
            sh["sc"] = float(spec["scale"])
        if "pieces" in sp:
            sh["exit"] = dict(D_EXIT["pFallSlow"])
        if key:
            if seen_key[key]:
                sh["unified"] = True
            seen_key[key] += 1
        if i > 0:   # 片头卡→第一句不带转场（从黑柔溶进来）
            sh["trans"] = pick_trans(key)
        shots.append(sh)

    # ---- 尾奏卡（字退去后只剩花瓣与署名）
    # 置景：每镜的最低配额 + 无人物时的版式轮转（见 dress_shots 头注）
    dress_shots(shots, a.seed, lines)

    shots.append({
        "i": len(shots), "t0": round(outro_t0, 3), "t1": round(dur, 3),
        "line": -1, "kind": "card", "tier": 1, "section": "outro",
        "text": "",
        "layout": {"k": "titleCard", "face": "frauncesItB",
                   "kicker": "", "artist": "",
                   "meta": "%s — %s ｜ 歌切：%s ｜ %s"
                           % (m.get("_meta", {}).get("song", "Innocence"),
                              a.artist, a.vocal, a.date), "shadow": False},
        "enter": {"k": "fadeSlow", "dur": 1.4, "stagger": 0.05, "lead": 0.20},
        "hold": {"k": "breathe", "amp": 0.4},
        "exit": {"k": "fade", "dur": 1.6},
        "type": {}, "treat": {"k": "solid"},
        "pal": {"pack": "dawn", "idx": 4},
        "bgl": [dict(D_BG["petal"], k="petal", n=16, a=0.16),
                dict(D_BG["sunrise"], k="sunrise", y=0.9, a=0.10)],
        "bg": [], "decor": [],
        "post": [], "cam": {"k": "float", "amp": 0.3},
        "energy": 0.20, "sync": False, "wobble": 0, "unified": False,
        "special": "outro-card", "note": "outro / 暮 / petal+credits",
    })

    # 半拍吸附在 dawn_main 里做（只吸动效时长；镜头边界锁人声，不动）
    return shots


def dawn_main(a, tdoc, ana, lines, secs, dur, w0):
    """意象驱动模式的出口：建镜头 → 吸半拍 → 组 sb → 写盘 → 报告。"""
    from collections import Counter
    path = a.imagery or os.path.join("docs", "innocence-v2-imagery-map.json")
    m = json.load(open(path, encoding="utf-8"))
    mm = m.get("_meta", {})
    # 元数据：CLI 空着时用映射表的曲目信息（原唱必须是 Avril Lavigne）
    if not a.title:
        a.title = mm.get("song", "Innocence")
    if not a.artist:
        a.artist = mm.get("artist", "Avril Lavigne")
    if not a.vocal:
        a.vocal = mm.get("vocal", "")

    shots = build_dawn_shots(a, lines, m, dur)

    # 1027 演唱原视频的序列描述（静帧工具靠 storyboard.meta.live 兜底装载）
    live_desc = None
    if getattr(a, "live_json", ""):
        lj = json.load(open(a.live_json, encoding="utf-8"))
        root = os.path.dirname(os.path.abspath(a.live_json)).replace("\\", "/")
        def _seq(e):
            if not e:
                return None
            d = (e.get("dir") or "").replace("\\", "/")
            if d and not os.path.isabs(d):
                # prepare_live 写的 dir 是「相对项目根」，也可能「相对 live.json」——
                # 两个候选都试，取真实存在的那个（之前直接拼 root 导致路径重复一层）
                cands = [os.path.abspath(d).replace("\\", "/"), root + "/" + d]
                d = next((c for c in cands if os.path.isdir(c)), cands[0])
            return dict(e, dir=d)
        live_desc = {"face": lj.get("face") or {"cx": 0.5, "cy": 0.5}}
        # 序列名不再固定 head/body/wide：LIVE_SPANS 是六段各一条序列（s1..s6）。
        # 写死三个名字的话，新表的六个 seq 一条都载不进去 —— 而且不报错，
        # 画面只是「这一段没有人」，会被误判成素材没抽上。
        for _k, _v in lj.items():
            if _k in ("face", "cut_start") or not isinstance(_v, dict):
                continue
            s = _seq(_v)
            if s:
                live_desc[_k] = s

    # 半拍吸附（只吸动效时长；镜头边界锁人声，不动）
    beat = 60.0 / ana["bpm"] if ana.get("bpm") else 0.0
    if beat > 0:
        half = beat * 0.5
        for s in shots:
            for kk in ("enter", "exit"):
                d = s.get(kk) or {}
                if d.get("dur"):
                    d["dur"] = round(max(half, round(d["dur"] / half) * half), 3)

    sb = {
        "version": 2,
        "meta": {
            "title": a.title, "artist": a.artist, "vocal": a.vocal,
            "source": a.source, "date": a.date,
            "seed": a.seed, "dur": dur, "fps": (getattr(a, "fps", 0) or 60), "start": w0,
            "koma": a.koma, "keyBg": a.keyBg or None,
            "concept": "dawn", "imagery": path.replace("\\", "/"),
            "live": live_desc,   # head 圆窗 / body 竖幅（1027 演唱原视频）
            # 绘本纸底非平涂（bg 件柔光/水彩会进四角）→ pv_check 四角判据豁免
            "paper_ok": True,
            "mark": "",
        },
        "palette": {"pack": "dawn", "idx": 0},
        "audio": {"bpm": ana["bpm"], "barSec": ana["bar_sec"],
                  "sections": [[s["t0"], s["t1"], s["name"]] for s in secs]},
        "lines": lines,
        "shots": shots,
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    json.dump(sb, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # ---- 报告
    nl = sum(1 for s in shots if s["kind"] == "line")
    print("[concept] dawn 意象驱动（不抽卡，逐句由映射表决定）")
    print("[shots] %d 句 → %d 镜（片头/尾各 1）  %.2fs" % (len(lines), len(shots), dur))
    print("[imagery] %s" % dict(Counter(
        i for s in shots if s["kind"] == "line" for i in (s["note"].split(" / ")[1].split("+")) if i and i != "-"
    ).most_common()))
    print("[layout] %s" % dict(Counter(s["layout"]["k"] for s in shots if s["kind"] == "line").most_common()))
    print("[face] %s" % dict(Counter(s["layout"].get("face", "") for s in shots if s["kind"] == "line").most_common()))
    print("[pal] %s" % dict(Counter("dawn/%d" % s["pal"]["idx"] for s in shots).most_common()))
    print("[trans] %s" % dict(Counter(s["trans"]["k"] for s in shots if s.get("trans")).most_common()))
    print("[hold] %s" % dict(Counter(s["hold"]["k"] for s in shots).most_common()))
    nuni = sum(1 for s in shots if s.get("unified"))
    print("[unity] 统一感回访 %d 镜" % nuni)
    npieces = sum(1 for s in shots if "pieces" in (s.get("special") or ""))
    print("[pieces] 碎块慢落 %d 处" % npieces)
    nsc = sum(1 for s in shots if s.get("sc"))
    print("[scale] 整镜缩放 %d 处  [desat] %d 处"
          % (nsc, sum(1 for s in shots if any(f.get("k") == "desat" for f in (s.get("post") or [])))))
    print("[beat] 动效时长吸半拍  bpm=%.1f  一拍=%.3fs" % (ana.get("bpm") or 0, beat))
    print("[out] %s  %.0fKB" % (a.out, os.path.getsize(a.out) / 1024.0))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timing", required=True)
    ap.add_argument("--analysis", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--dur", type=float, default=0.0)
    ap.add_argument("--seed", type=int, default=20261027)
    ap.add_argument("--title", default="")
    ap.add_argument("--artist", default="")
    ap.add_argument("--vocal", default="")
    ap.add_argument("--source", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--live", default="")
    ap.add_argument("--lrc", default="")
    ap.add_argument("--fps", type=int, default=30,
                    help="成片帧率（写进 meta；30fps + koma12 = 每张画 2~3 个输出帧）")
    ap.add_argument("--koma", type=int, default=12,
                    help="二帧量化：每秒画几张（JIZURA 默认 12，0=不量化）")
    ap.add_argument("--keyBg", default="", choices=["", "green", "black", "white"],
                    help="绿幕/黑幕导出（JIZURA 的 keyBg），空=正常纸底")
    ap.add_argument("--concept", default="", choices=["", "dawn"],
                    help="dawn=意象驱动（读 --imagery 映射表，不抽卡）")
    ap.add_argument("--live-json", default="",
                    help="1027 原视频序列的 live.json（prepare_live.py 产出）")
    ap.add_argument("--imagery", default="",
                    help="逐句意象映射 JSON（--concept dawn 用），默认 docs/innocence-v2-imagery-map.json")
    a = ap.parse_args()

    tdoc = json.load(open(a.timing, encoding="utf-8"))
    ana = json.load(open(a.analysis, encoding="utf-8"))
    total = float(ana["dur"])
    w0 = max(0.0, a.start)
    w1 = min(total, w0 + a.dur) if a.dur > 0 else total
    dur = round(w1 - w0, 3)

    # ---- 段落（曲式）：只用于决定「这一段用什么纸」与语气
    secs = [{"t0": t0 - w0, "t1": t1 - w0, "name": name} for t0, t1, name in ana["sections"]]
    if not secs:
        secs = [{"t0": 0.0, "t1": dur, "name": "A1"}]

    def sec_at(t):
        for s in secs:
            if s["t0"] <= t < s["t1"]:
                return s
        return secs[-1]

    def mood_of(name):
        if name.startswith("B"):
            return "peak" if name in ("B3", "B5") else "drive"
        if name.startswith("C"):
            return "quiet"
        return "quiet" if name in ("A1", "A2") else "drive"

    # ---- 窗口内的歌词行
    lines = []
    for l in tdoc["lines"]:
        if l["t1"] < w0 + 0.25 or l["t0"] > w1 - 0.25:
            continue
        q = json.loads(json.dumps(l))
        q["t0"] = round(max(0.0, q["t0"] - w0), 3)
        q["t1"] = round(min(q["t1"] - w0, w1 - w0), 3)
        for w in q["words"]:
            w["t0"] = round(max(0.0, w["t0"] - w0), 3)
            w["t1"] = round(min(w["t1"] - w0, w1 - w0), 3)
        for c in q["chars"]:
            c["t0"] = round(max(0.0, c["t0"] - w0), 3)
            c["t1"] = round(min(c["t1"] - w0, w1 - w0), 3)
        lines.append(q)
    if not lines:
        raise SystemExit("窗口 %.1f~%.1f 内没有歌词行" % (w0, w1))

    # ---- 关键词（标题词 + 高频实词）
    freq = {}
    for l in lines:
        for w in re.findall(r"[a-z']{3,}", l["text"].lower()):
            freq[w] = freq.get(w, 0) + 1
    kw = {w for w, c in freq.items() if c >= 3} | set(re.findall(r"[a-z']{3,}", a.title.lower()))
    kw -= {"the", "and", "that", "this", "you", "your", "with", "for", "was", "are", "its"}

    # ---- 意象驱动模式：跳过全部抽卡，直接由映射表建镜头
    if a.concept == "dawn":
        return dawn_main(a, tdoc, ana, lines, secs, dur, w0)

    print("[look] 段落 → 纸")
    for s in secs:
        pk = LOOK.get(s["name"], ("noir", 0))
        print("  %-3s %7.2f→%7.2f  %-8s scheme %d  %s"
              % (s["name"], s["t0"], s["t1"], pk[0], pk[1], mood_of(s["name"])))

    SCHEME_N.update(scheme_counts())
    SEC_SW = {}              # 段名 → [上次换纸时刻, 当前 scheme]
    shots = []
    last_cam = [""]
    cam_used = {}
    recent_lay = []          # 最近用过的版面（避免连续重复）
    recent_ent = []          # 最近用过的入场（P0-2 新颖度）
    recent_exit = []         # 最近用过的退场
    recent_hold = []         # 最近用过的保持
    recent_trans = []        # 最近用过的转场
    recent_decor = []        # 最近用过的装饰
    recall = {}              # 统一感（P1-5）：唱词 → (版面, 入场, 转场) 首次出现
    beat = 60.0 / ana["bpm"] if ana.get("bpm") else 0.0   # 一拍的秒数（P1-6）

    for i, l in enumerate(lines):
        sec = sec_at(l["t0"])
        mood = mood_of(sec["name"])
        m = MOOD[mood]
        # 镜头首尾相接：起点提前 0.10s（人声起点略晚于字），终点顶到下一句
        t0 = max(0.0, l["t0"] - 0.10)
        nxt = max(0.0, lines[i + 1]["t0"] - 0.10) if i + 1 < len(lines) else dur
        t1 = min(dur, max(nxt, l["t1"] + 0.12)) if nxt > l["t1"] else min(dur, l["t1"] + 2.4)
        if t1 - t0 < 0.6:
            t1 = t0 + 0.6
        r = rng(a.seed, 200 + i)

        pack, sch0 = LOOK.get(sec["name"], ("noir", 0))
        # 段内轮转：每累计 SWITCH_EVERY 秒换同 pack 的下一套 scheme。
        # 旧写法是「过中点 且 sch==0 才换成 1」——于是只有 crimson 段能触发，
        # 而且 13 镜里只换后半段一次，纸还是读成连着的一整片。
        st = SEC_SW.setdefault(sec["name"], [sec["t0"], sch0])
        nsc = SCHEME_N.get(pack, 1)
        if nsc > 1 and (sec["t1"] - sec["t0"]) > 34 and l["t0"] - st[0] >= SWITCH_EVERY:
            st[1] = (st[1] + 1) % nsc
            st[0] = l["t0"]
        sch = st[1]
        fonts = FONTS.get(pack, FONTS["noir"])

        words = [w["text"] for w in l["words"]]
        nw = len(words)
        n = len(l["text"])
        cjk = any("\u3040" <= ch <= "\u9fff" for ch in l["text"])
        key = key_index(words, kw)

        # ---- 人物：先决定有没有人，再决定版面（版面必须为人让位）
        fig = None
        if r.random() < m["fig"]:
            fig = fig_script(mood, r, pack)

        # ---- 版面：语气池 → 形态过滤 → 三重加权抽卡（P0-2）
        pool = [(k, w) for k, w in VOICE[mood] if FIT[k](nw, n, cjk)]
        if fig:
            safe = [(k, w) for k, w in pool if k in FIG_SAFE]
            if safe:
                pool = safe
        # 新颖度改由 gacha() 内的 novelty() 做折扣（不直接排除，避免池子枯竭），
        # 但仍然硬排除最近 1 个 —— 连着两镜同一版面怎么调权重都读作「卡住了」。
        if recent_lay:
            fresh = [(k, w) for k, w in pool if k != recent_lay[-1]]
            if fresh:
                pool = fresh
        # 兜底：长句在 peak 池里可能一个版面都接不住（keySplit/cross/scaleSteps 都有词数上限），
        # 这时必须退回 block —— 空池会让抽签直接崩，而不是「排得难看」。
        if not pool:
            pool = [("block", 1)]
        pers = PERS.get(pack, PERS_DEFAULT)
        layk = gacha(r, pool, pers, "layout", recent_lay)
        recent_lay.append(layk)
        if len(recent_lay) > 6:
            recent_lay.pop(0)

        # 人占掉多宽 → 版心从哪儿起。人太宽时先缩人（字是主角，人不能让版心消失）
        avoid = None
        if fig:
            res = fig_reserve(fig)
            for _ in range(4):
                if res <= 1920 * 0.40:
                    break
                if fig["mode"] == "disc" and fig["r"] > 140:
                    fig["r"] = int(fig["r"] * 0.90)
                elif fig["mode"] != "disc" and fig["h"] > 520:
                    fig["h"] = int(fig["h"] * 0.90)
                else:
                    break
                res = fig_reserve(fig)
            avoid = {"side": fig["side"], "frac": round(res / 1920.0, 3)}
        lay = layout_params(layk, mood, fig, r, nw, key, avoid)

        # ---- 类型：只有三级（主字 / 次字 / 注记），字体由 pack 决定
        typ = {"face": fonts["face"], "body": fonts["body"], "squeeze": 1.0}

        # ---- 入场 / 保持 / 退场（P0-2 三重加权抽卡）
        # pAssemble 是 piece 级入场：字被拆成零件后各自飞回来拼成整体。
        #   给它的权重不高（2）—— 它的信息量比整字位移大得多，48 镜里出现 6~8 次就够，
        #   用多了「拆字」会从签名变成套路。
        entpool = [("assemble", 3), ("softIn", 3), ("sliceDown", 2),
                   ("dropBounce", 2), ("typeOn", 1), ("pAssemble", 2)]
        if mood == "peak":
            entpool = [("assemble", 3), ("slam", 2.4), ("dropBounce", 2),
                       ("pAssemble", 3), ("sliceDown", 1.4)]
        entk = gacha(r, entpool, pers, "enter", recent_ent)
        recent_ent.append(entk)
        if len(recent_ent) > 6:
            recent_ent.pop(0)
        ent = {"k": entk}
        if entk == "slam":
            ent.update(dur=0.22, stagger=0.0, lead=0.02)
        elif entk == "assemble":
            ent.update(dur=0.46, stagger=0.026, lead=0.08)
        elif entk == "softIn":
            ent.update(dur=0.60, stagger=0.018, lead=0.10)
        elif entk == "dropBounce":
            ent.update(dur=0.52, stagger=0.030, lead=0.05)
        elif entk == "typeOn":
            ent.update(dur=0.24, stagger=0.030, lead=0.02)
        elif entk == "pAssemble":
            # 零件飞行需要时间铺开：太短会读作「抖了一下」，太长会抢唱词。
            # 0.62 是 48 镜里能保住「堆起来」这个过程而不拖沓的下限。
            ent.update(dur=0.62, stagger=0.024, lead=0.06)
        else:
            ent.update(dur=0.34, stagger=0.040, lead=0.04)

        # pExplode / pFall 同为 piece 级：前者「炸」，后者「塌」。
        #   pFall 放在长句/情绪落点上，pExplode 放在宰制的cut before 断点。
        exitk = gacha(r, [("fadeUp", 4), ("blurOut", 2), ("fade", 2),
                          ("pFall", 1.2), ("pExplode", 1.2)], pers, "exit", recent_exit)
        recent_exit.append(exitk)
        if len(recent_exit) > 6:
            recent_exit.pop(0)
        xit = {"k": exitk, "dur": 0.26 if mood == "peak" else 0.40}
        if exitk in ("pExplode", "pFall"):
            xit["dur"] = 0.34 if mood == "peak" else 0.46
        holdk = gacha(r, HOLD_POOL, pers, "hold", recent_hold)
        recent_hold.append(holdk)
        if len(recent_hold) > 6:
            recent_hold.pop(0)
        hold = {"k": holdk, "amp": round(0.7 + 0.2 * m["cam"], 2)}

        # ---- 转场（P0-1）：这一镜「怎么进来」。片头卡不给转场（它是硬切进第一句的）。
        transk = gacha(r, TRANS_BY_MOOD[mood], pers, "trans", recent_trans)
        recent_trans.append(transk)
        if len(recent_trans) > 6:
            recent_trans.pop(0)
        trans = {"k": transk}

        # ---- 相机
        poolc, alt = [], []
        for k, wgt in CAM_BY_TIER[m["cam"]]:
            alt += [k] * wgt
            if k != last_cam[0]:
                poolc += [k] * wgt
        if not poolc:
            poolc = alt
        camk = pick(r, poolc)
        if camk in CAM_QUOTA:
            if cam_used.get(camk, 0) >= CAM_QUOTA[camk]:
                camk = "push"
            else:
                cam_used[camk] = cam_used.get(camk, 0) + 1
        last_cam[0] = camk
        cam = {"k": camk, "amp": round(m["amp"] * (0.86 + 0.28 * r.random()), 3)}

        # ---- 装饰（P1-4 扩充池）：JIZURA 的 decor 组，宁可不要多 —— 一镜最多一件
        decor, bgl, bg = [], [], []
        if not fig and r.random() < 0.38:
            dk = gacha(r, DECOR_POOL, pers, "decor", recent_decor)
            recent_decor.append(dk)
            if len(recent_decor) > 6:
                recent_decor.pop(0)
            decor.append(decor_spec(dk, r, i))
        # 背景：平涂底上只允许一层极淡的图形，且只给驱动/爆发段 —— 这是「层次」不是「滤镜」
        if mood != "quiet" and r.random() < (0.34 if mood == "peak" else 0.20):
            bgl.append(bg_spec(gacha(r, BG_POOL, pers, "decor", []), r))

        # ---- 统一感（P1-5）：同一句唱词再次出现时，回到它上次的画面
        # JIZURA 的 recap 只是「往几张版面上加权」，我们做得更硬：直接复用
        # 版面 / 入场 / 转场三件套。观众在第二次副歌读到的是「啊，是那个画面」，
        # 这正是编排「有呼应」而不是「每句都在换招」的分界线。
        txt = re.sub(r"\s+", " ", l["text"].strip().lower())
        recap = False
        if txt in recall:
            layk2, entk2, transk2 = recall[txt]
            # 回访也要过形态过滤 + 人物安全过滤：同一句唱词在第二次出现时可能带人物，
            # 把满幅版面硬塞回去会让字压在脸上（43.3s This moment 的老事故）。
            if (FIT.get(layk2, lambda *_: True)(nw, n, cjk)
                    and (not fig or layk2 in FIG_SAFE)):
                layk, entk, transk = layk2, entk2, transk2
                # 版面参数必须按回访的 layk 重算 —— 否则引擎读到的还是抽签那个
                # 版面（lay 是个 dict，里面自带 k），「回访」等于没发生。
                lay = layout_params(layk, mood, fig, r, nw, key, avoid)
                ent = {"k": entk}
                if entk == "slam":
                    ent.update(dur=0.22, stagger=0.0, lead=0.02)
                elif entk == "assemble":
                    ent.update(dur=0.46, stagger=0.026, lead=0.08)
                elif entk == "softIn":
                    ent.update(dur=0.60, stagger=0.018, lead=0.10)
                elif entk == "dropBounce":
                    ent.update(dur=0.52, stagger=0.030, lead=0.05)
                elif entk == "typeOn":
                    ent.update(dur=0.24, stagger=0.030, lead=0.02)
                elif entk == "pAssemble":
                    ent.update(dur=0.62, stagger=0.024, lead=0.06)
                else:
                    ent.update(dur=0.34, stagger=0.040, lead=0.04)
                trans = {"k": transk}
                recap = True
        else:
            recall[txt] = (layk, entk, transk)

        shots.append({
            "i": len(shots), "t0": round(t0, 3), "t1": round(t1, 3),
            "line": i, "kind": "line", "tier": m["cam"], "section": sec["name"],
            "energy": 0.35 if mood == "quiet" else (0.62 if mood == "drive" else 0.9),
            "layout": lay, "type": typ,
            "enter": ent, "hold": hold, "exit": xit,
            "trans": trans,
            "treat": {"k": "solid"},
            "pal": {"pack": pack, "idx": sch},
            "bgl": bgl, "bg": bg, "decor": decor, "post": [],
            "cam": cam, "fig": fig, "sync": False, "wobble": 0,
            "unified": recap,
            "note": "%s / %s / %s / %s%s" % (sec["name"], pack, layk, entk,
                                             " / recap" if recap else ""),
        })

    # ---- 片头标题卡：占满整个前奏（绝不移动歌词时间轴）
    intro_end = shots[0]["t0"] if shots else 0.0
    if intro_end >= 2.4:
        shots.insert(0, {
            "i": -1, "t0": 0.0, "t1": round(intro_end - 0.26, 3), "line": -1,
            "kind": "title", "tier": 1, "section": "TITLE", "energy": 0.2,
            "layout": {"k": "titleCard", "kicker": "ORIGINAL SONG",
                       "artist": a.artist or "", "shadow": True,
                       "meta": " ".join(x for x in (("VOCAL " + a.vocal) if a.vocal else "",
                                                    ("·  " + a.date) if a.date else "") if x)},
            "type": {"face": "heavy", "body": "sansMed", "squeeze": 1.0},
            "text": a.title,
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
        for k, s2 in enumerate(shots):
            s2["i"] = k

    # ---- 收口：镜头首尾相接
    shots.sort(key=lambda s: s["t0"])
    for i in range(len(shots) - 1):
        shots[i]["t1"] = round(max(shots[i]["t0"] + 0.30, shots[i + 1]["t0"]), 3)
        shots[i + 1]["t0"] = shots[i]["t1"]
        shots[i + 1]["i"] = i + 1
    if shots:
        shots[-1]["t1"] = round(dur, 3)

    # ---- 节拍吸附（P1-6）：镜头边界 + 动效时长向节拍网格靠拢
    # 边界是「换镜的时机」，时长是「动作的快慢」。这两样贴上节拍，观感才会有
    # 「跟音乐长在一起」的感觉；文字自身的时间轴**不动**（那是人声，不是节拍）。
    if beat > 0:
        half = beat * 0.5
        for s in shots:
            for kk in ("enter", "exit"):
                d = s.get(kk) or {}
                if d.get("dur"):
                    nd = round(max(half, round(d["dur"] / half) * half), 3)
                    if nd != d["dur"]:
                        d["dur"] = nd
            hd = s.get("hold") or {}
            # 保持类的 amp 不吸（它是幅度不是时间）
        for i in range(len(shots) - 1):
            b = shots[i]["t1"]
            nb = beat_snap(b, beat, 0.18)
            if nb != b and nb > shots[i]["t0"] + 0.30 and nb < shots[i + 1]["t1"] - 0.30:
                shots[i]["t1"] = nb
                shots[i + 1]["t0"] = nb
                shots[i]["sync"] = shots[i + 1]["sync"] = True

    # ---- 直播序列
    live = None
    if a.live and os.path.exists(a.live):
        lj = json.load(open(a.live, encoding="utf-8"))
        live = {}
        for k, _v in lj.items():
            # 序列名由 live.json 决定（s1..s6），写死三档会一条都装不进去
            if k in ("face", "cut_start") or not isinstance(_v, dict) or not _v.get("dir"):
                continue
            if lj.get(k):
                q = dict(lj[k])
                q["dir"] = os.path.abspath(q["dir"]).replace("\\", "/")
                live[k] = q
        if lj.get("face"):
            live["face"] = lj["face"]

    sb = {
        "version": 2,
        "meta": {
            "title": a.title, "artist": a.artist, "vocal": a.vocal,
            "source": a.source, "date": a.date,
            "seed": a.seed, "dur": dur, "fps": (getattr(a, "fps", 0) or 60), "start": w0,
            "koma": a.koma, "keyBg": a.keyBg or None,
            "mark": "",
        },
        "palette": {"pack": "noir", "idx": 0},
        "audio": {"bpm": ana["bpm"], "barSec": ana["bar_sec"],
                  "sections": [[s["t0"], s["t1"], s["name"]] for s in secs]},
        "lines": lines,
        "shots": shots,
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    json.dump(sb, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # ---- 核对
    from collections import Counter
    nl = sum(1 for s in shots if s["kind"] == "line")
    print("[shots] %d 句 → %d 镜（含片头 %d），%.2fs" % (len(lines), len(shots), len(shots) - nl, dur))
    print("[layout] %s" % dict(Counter(s["layout"]["k"] for s in shots if s["kind"] == "line").most_common()))
    print("[pal] %s" % dict(Counter("%s/%d" % (s["pal"]["pack"], s["pal"]["idx"]) for s in shots).most_common()))
    # 换纸节奏：统计的是 **scheme**（pack/idx）连续段，不是 pack。
    # pack 一个贯穿一整段是设计（noir 段就该一直是 noir 的字体人格）；
    # 观众感知到的「换纸」来自 scheme —— 同一个 pack 里红纸与黑纸的切换。
    segs, curpk, cnt = [], None, 0
    for s in shots:
        pk = "%s/%d" % (s["pal"]["pack"], s["pal"]["idx"])
        if pk == curpk:
            cnt += 1
        else:
            if curpk:
                segs.append((curpk, cnt))
            curpk, cnt = pk, 1
    segs.append((curpk, cnt))
    worst = max(c for _, c in segs)
    # 阈值定 8 而不是 6：换纸只能发生在镜头边界，SWITCH_EVERY=24s 落地后
    # 必然带一个镜头的余量（实测 24s 规则 → 最长 7 镜 ≈ 30s）。6 会恒定误报。
    print("[paper runs] 最长同纸连续 %d 镜  共换纸 %d 次  %s"
          % (worst, len(segs) - 1, "OK" if worst <= 8 else "WARN 换纸节奏偏慢"))
    print("[paper runs] %s" % segs)
    nf = sum(1 for s in shots if s["fig"])
    print("[figure] 人物镜头 %d 个（%.0f%%）" % (nf, 100.0 * nf / max(1, nl)))
    # ---- P0/P1 稽核：转场覆盖、保持分布、统一感回访、节拍吸附
    ntr = sum(1 for s in shots if s.get("trans"))
    print("[trans] 带转场 %d/%d 镜  %s"
          % (ntr, len(shots),
             dict(Counter(s["trans"]["k"] for s in shots if s.get("trans")).most_common())))
    print("[hold] %s"
          % dict(Counter(s["hold"]["k"] for s in shots if s.get("hold")).most_common()))
    nuni = sum(1 for s in shots if s.get("unified"))
    print("[unity] 统一感回访 %d 镜（同唱词复用版面/入场/转场）" % nuni)
    nsync = sum(1 for s in shots if s.get("sync"))
    print("[beat] 节拍吸附 %d 处边界  bpm=%.1f  一拍=%.3fs"
          % (nsync, ana.get("bpm") or 0, beat))
    bad = fig_band_fix(shots)
    if bad:
        print("[warn] 有人物却用了满幅版面：%s" % bad)
    print("[out] %s  %.0fKB" % (a.out, os.path.getsize(a.out) / 1024.0))


if __name__ == "__main__":
    sys.exit(main())
