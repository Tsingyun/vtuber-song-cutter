# -*- coding: utf-8 -*-
"""文字 PV 导演层：音乐段落 + 逐词时间轴 → storyboard.json（可人工编辑的编排稿）。

设计原则（这是本文件存在的理由，不是修辞）
------------------------------------------------
1. **一档一级，不叠buff**：每句只从所属 tier 的白名单里取 1 个 layout / 1 个 enter /
   1 个 hold / 1 个 exit / 1 种字形样式。档与档之间靠**反差**制造节奏，不靠单句堆效果。
2. **字体是主要旋钮**：tier 越高 → 字重越粗、字距越紧、字号越大。这条阶梯是全局一致的，
   所以同一个「粗体压紧」在别处出现时，观众能感到「又上来了」。
3. **重复句必须同处理**（unify）：副歌同一句反复出现，如果每次都换花样，结构就散了。
   以歌词文本为键，第二次出现直接复用第一次的编排。
4. **克制**：整片只有一处**色彩反转**（最高潮的章节），只在少数句子开抖动/色差/放射线。
   这些是「事件」，不是背景噪声。
5. 全部随机量走 seed，同一 seed 与同一输入 → 同一份编排。

用法：
  python tools/pv_direct.py --timing _tmp/pv/timing.json --analysis _tmp/pro_analysis.json \\
      --out _tmp/pv/storyboard.json --start 20 --dur 40 --live _tmp/live_seq/live.json \\
      --title "Innocence" --artist "Avril Lavigne" --vocal "岁己SUI" --date 2026-10-27
"""
import argparse
import json
import os
import re
import sys

import numpy as np

# ----------------------------------------------------------------- 词汇表
# 每档的取件范围。**刻意收窄**：一档里只有 2~4 个候选，才不会每句都长得不一样。
TIERS = {
    0: dict(face=["minchoLt", "sansLt"], track=0.045, sizeK=0.62,
            layout=["center", "corner", "justify"],
            enter=["fadeSlow", "rise"], hold=["none", "breathe"],
            exit=["fade"], treat=["outline", "solid"], fig=0.0),
    1: dict(face=["mincho", "sansMed"], track=0.012, sizeK=0.74,
            layout=["center", "justify", "corner"],
            enter=["sliceDown", "rise", "typeOn"], hold=["drift", "breathe"],
            exit=["fadeUp", "fade"], treat=["solid", "outlineThick"], fig=0.0),
    2: dict(face=["sans", "grotesk"], track=-0.012, sizeK=0.86,
            layout=["stack", "split", "justify", "diagonal"],
            enter=["stretchIn", "sliceUp", "scalePop"], hold=["drift", "wave"],
            exit=["fadeUp", "blurOut"], treat=["fillOutline", "outlineThick", "double"], fig=0.22),
    3: dict(face=["heavy", "grotesk"], track=-0.032, sizeK=1.0,
            layout=["centerBig", "band", "megaCrop", "dense"],
            enter=["slam", "typeOn", "sliceDown"], hold=["wave", "jitter"],
            exit=["scatterOut", "flatten"], treat=["fillOutline", "chroma", "extrude"], fig=0.55),
    4: dict(face=["heavy", "condensed"], track=-0.046, sizeK=1.16,
            layout=["megaCrop", "column", "dense"],
            enter=["scatterIn", "spinIn", "slam"], hold=["jitter", "flutter"],
            exit=["scatterOut", "dissolve"], treat=["chroma", "stripe", "marker"], fig=0.45),
}

PALETTE = {
    "ink": "#F2F6FA", "paper": "#0A0D12", "paper2": "#111720",
    "accent": "#FFB067", "ghostA": "#39C6E0", "ghostB": "#D0453F", "void": "#05070A",
}
# 唯一的反转：最高潮那一章用暖纸 + 墨字（全片只有一次，所以它是事件）
PALETTE_INV = {
    "ink": "#0B0E13", "paper": "#F2E4CE", "paper2": "#E8D5B8",
    "accent": "#C8373A", "ghostA": "#2C7C93", "ghostB": "#B0592F", "void": "#D8C4A6",
}


def rng(seed, *keys):
    """由 seed 与若干整数键派生一个确定性 RNG（同一键永远同一序列）。"""
    h = seed & 0xFFFFFFFF
    for k in keys:
        h = (h ^ (int(k) * 2654435761)) & 0xFFFFFFFF
        h = (h * 40503 + 2246822519) & 0xFFFFFFFF
    return np.random.default_rng(h)


def pick(r, arr):
    return arr[int(r.integers(0, len(arr)))]


def layout_ok(k, text, nwords, cjk):
    """构图不是万能件：每种构图对「句子形态」有要求，硬套就会得到一堆画外字。
    · stack 一行一个词 —— 词太多会排出画外且字号被压到看不懂
    · diagonal 旋转后水平投影变长 —— 长句直接飞出去
    · column 是竖排 —— 拉丁文只能整体旋 90°，长句会绕出画面
    · dense/megaCrop 靠「重复/裁切」冲击，短句做了没有信息量
    · corner 是「角落小字」——整句塞进左下角就是卡拉OK字幕，只能给短句用"""
    n = len(text)
    if k == "stack":
        return 2 <= nwords <= 5
    if k == "diagonal":
        return nwords <= 7
    if k == "column":
        return cjk and n <= 24
    if k == "dense":
        return n >= 12
    if k == "megaCrop":
        return n >= 8
    if k == "corner":
        # 实测 t=10.6「Waking up I see that everything」被排进左下角，
        # 整句贴底 + 满宽拉满 ⇒ 一眼读作普通歌词条，正好是要避免的那种观感
        return nwords <= 3 and n <= 14
    return True


def pick_layout(r, spec, text, cjk, fallbacks):
    nwords = max(1, len(text.split()))
    for _ in range(6):
        k = pick(r, spec)
        if layout_ok(k, text, nwords, cjk):
            return k
    for k in fallbacks:
        if layout_ok(k, text, nwords, cjk):
            return k
    return "center"


# ----------------------------------------------------------------- 读取分析
def load_series(ana):
    import base64
    N = ana["nframes"]
    rms = np.frombuffer(base64.b64decode(ana["rms_b64"]), dtype=np.uint8).astype(np.float32) / 255.0
    vox = np.frombuffer(base64.b64decode(ana["voiced_b64"]), dtype=np.uint8).astype(np.float32) / 255.0
    flux = np.frombuffer(base64.b64decode(ana["flux_b64"]), dtype=np.uint8).astype(np.float32) / 255.0
    return rms[:N], vox[:N], flux[:N]


def section_features(ana, rms, vox, flux, fps):
    out = []
    for s in ana["sections"]:
        t0, t1 = float(s[0]), float(s[1])
        a, b = int(t0 * fps), int(min(len(rms), t1 * fps))
        if b <= a:
            continue
        out.append({
            "t0": t0, "t1": t1, "name": s[2],
            "rms": float(rms[a:b].mean()),
            "vox": float((vox[a:b] > 0.45).mean()),
            "flux": float(flux[a:b].mean()),
        })
    return out


def assign_tiers(feats, r):
    """用实测能量排序后分档，而不是写死「副歌=3」。

    **不要用 rms**：这条成片是母带过的，全曲 rms p5=0.66 / p95=0.91，段与段之间的
    排名基本是噪声（实测 A1 与 B4 只差 0.008，却分属安静段与最密段）。改用
      0.50 * rank(谱通量) + 0.50 * rank(人声密度)
    谱通量测的是「编曲有多密」，人声密度测的是「她唱得多满」——这两个才对应
    观众感到的「这一段更猛」。分位 → tier；首尾段落降一档（开场收尾天然要留白）。
    """
    if not feats:
        return []
    n = len(feats)
    for k in ("flux", "vox"):
        vals = np.array([f[k] for f in feats])
        order = vals.argsort().argsort()
        for i, f in enumerate(feats):
            f[k + "_r"] = order[i] / max(1, n - 1)
    for f in feats:
        f["score"] = 0.50 * f["flux_r"] + 0.50 * f["vox_r"]
    order = np.argsort([-f["score"] for f in feats])
    for rank, idx in enumerate(order):
        q = rank / max(1, n - 1)
        f = feats[idx]
        f["tier"] = 4 if q < 0.14 else 3 if q < 0.38 else 2 if q < 0.64 else 1 if q < 0.86 else 0
    # 首尾降档：开场第一个段落和收尾最后一个段落不该是最吵的
    feats[0]["tier"] = min(feats[0]["tier"], 1)
    feats[-1]["tier"] = min(feats[-1]["tier"], 1)
    return feats


# ----------------------------------------------------------------- 文案辅助
def make_type(line, tier, spec, phase_seed, idx):
    """字号 / 字距 / 字体 + 构图参数。长句自动缩字号，否则必然撞边。"""
    txt = line["text"]
    n = max(1, len(txt.replace(" ", "")))
    base = {"heavy": 300, "grotesk": 280, "condensed": 320, "sans": 250,
            "sansMed": 230, "sansLt": 210, "mincho": 220, "minchoLt": 200,
            "mono": 150, "monoB": 150}
    face = pick(phase_seed, spec["face"])
    size = int(min(base.get(face, 240) * spec["sizeK"], 1500 / max(6, n) * 3.2))
    size = max(56, size)
    return face, size, spec["track"]


def emphasis_of(line, keywords):
    """挑要强调的词：命中关键词优先，否则取音节最多（信息量最大）的那个词。"""
    ws = line.get("words") or []
    if not ws:
        return []
    hits = [i for i, w in enumerate(ws) if re.sub(r"[^a-z]", "", w["text"].lower()) in keywords]
    if hits:
        return hits[:2]
    best = max(range(len(ws)), key=lambda i: (len(re.sub(r"[^a-z]", "", ws[i]["text"].lower())), -i))
    # 太短的句子不必强调
    if len(re.sub(r"\s", "", line["text"])) < 8:
        return []
    return [best]


# ----------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timing", required=True)
    ap.add_argument("--analysis", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", type=float, default=0.0, help="样片起点（秒，相对成片）")
    ap.add_argument("--dur", type=float, default=0.0, help="样片时长（0=整首）")
    ap.add_argument("--seed", type=int, default=20261027)
    ap.add_argument("--title", default="")
    ap.add_argument("--artist", default="")
    ap.add_argument("--vocal", default="")
    ap.add_argument("--source", default="")
    ap.add_argument("--date", default="")
    ap.add_argument("--live", default="", help="tools/prepare_live.py 的 live.json")
    ap.add_argument("--lrc", default="", help="只用于逐字核对行数")
    a = ap.parse_args()

    tdoc = json.load(open(a.timing, encoding="utf-8"))
    ana = json.load(open(a.analysis, encoding="utf-8"))
    fps = 60.0
    total = float(ana["dur"])
    w0 = max(0.0, a.start)
    w1 = min(total, w0 + a.dur) if a.dur > 0 else total

    rms, vox, flux = load_series(ana)
    feats = assign_tiers(section_features(ana, rms, vox, flux, fps), rng(a.seed, 1))

    print("[sections] 段落能量与档位")
    for f in feats:
        print("  %-3s %7.2f→%7.2f  rms %.3f vox %.3f  score %.3f  tier %d"
              % (f["name"], f["t0"], f["t1"], f["rms"], f["vox"], f["score"], f["tier"]))

    def tier_at(t):
        for f in feats:
            if f["t0"] <= t < f["t1"]:
                return f["tier"], f
        return 1, (feats[0] if feats else {"name": "?", "tier": 1})

    # ---- 窗口内的歌词行（时间归零）
    lines = []
    for l in tdoc["lines"]:
        if l["t1"] < w0 + 0.25 or l["t0"] > w1 - 0.25:
            continue
        lines.append(json.loads(json.dumps(l)))
    if not lines:
        raise SystemExit("窗口 %.1f~%.1f 内没有歌词行" % (w0, w1))
    for l in lines:
        l["t0"] = round(l["t0"] - w0, 3)
        l["t1"] = round(min(l["t1"] - w0, w1 - w0), 3)
        for w in l["words"]:
            w["t0"] = round(w["t0"] - w0, 3)
            w["t1"] = round(min(w["t1"] - w0, w1 - w0), 3)
        for c in l["chars"]:
            c["t0"] = round(c["t0"] - w0, 3)
            c["t1"] = round(min(c["t1"] - w0, w1 - w0), 3)
    dur = round(w1 - w0, 3)

    # ---- 关键词（用于强调）
    kw = set(re.findall(r"[a-z]{3,}", (a.title + " " + " ".join(
        l["text"] for l in lines)).lower()))
    # 只保留标题词 + 出现 ≥3 次的内容词，避免「什么都强调」
    freq = {}
    for l in lines:
        for w in re.findall(r"[a-z]{3,}", l["text"].lower()):
            freq[w] = freq.get(w, 0) + 1
    title_words = set(re.findall(r"[a-z]{3,}", a.title.lower()))
    kw = {w for w, c in freq.items() if c >= 3} | title_words
    kw -= {"the", "and", "that", "this", "you", "your", "with", "for", "you're", "it's", "was", "are"}

    # ---- 统一表：同一句歌词复用同一套编排
    unify = {}

    shots = []
    for i, l in enumerate(lines):
        tier, sec = tier_at(l["t0"] + w0)
        nxt = lines[i + 1]["t0"] if i + 1 < len(lines) else dur
        t0 = max(0.0, l["t0"] - 0.10)
        t1 = min(dur, max(l["t1"] + 0.22, min(nxt - 0.02, l["t1"] + 3.0)))
        if t1 - t0 < 0.5:
            t1 = t0 + 0.5
        spec = TIERS[tier]
        key = re.sub(r"[^a-z]", "", l["text"].lower())
        r = rng(a.seed, 100 + i)

        if key in unify and len(key) > 6:
            base = unify[key]
            layk, entk, holdk, exitk, treatk = (base["layout"]["k"], base["enter"]["k"],
                                                base["hold"]["k"], base["exit"]["k"],
                                                base["treat"]["k"])
            face, size, track = base["type"]["face"], base["type"]["size"], base["type"]["track"]
            unified = True
        else:
            cjk = any("\u3040" <= ch <= "\u9fff" for ch in l["text"])
            layk = pick_layout(r, spec["layout"], l["text"], cjk,
                               ["justify", "split", "center"])
            entk = pick(r, spec["enter"])
            holdk = pick(r, spec["hold"])
            exitk = pick(r, spec["exit"])
            treatk = pick(r, spec["treat"])
            face, size, track = make_type(l, tier, spec, r, i)
            uniform = dict(layout={"k": layk}, enter={"k": entk}, hold={"k": holdk},
                           exit={"k": exitk}, treat={"k": treatk},
                           type={"face": face, "size": size, "track": track})
            if len(key) > 6:
                unify[key] = uniform
            unified = False

        # 构图参数：不同 layout 需要不同的锚点，统一在这里给
        lay = {"k": layk}
        if layk == "center":
            lay.update(cy=0.50 if tier <= 1 else 0.52)
        elif layk == "centerBig":
            lay.update(fillH=0.60 + 0.05 * (tier - 3), cy=0.66)
        elif layk == "corner":
            lay.update(pos="bl" if (i % 2 == 0) else "tr")
        elif layk == "edge":
            lay.update(side="left" if i % 2 == 0 else "right", bleed=0.20)
        elif layk == "megaCrop":
            lay.update(focus=0.30 + 0.12 * (i % 3), cy=0.72)
        elif layk == "band":
            lay.update(cy=0.50)
        elif layk == "column":
            lay.update(x=0.86, y=0.16, colH=0.70)
        elif layk == "stack":
            # 垂直位置交给排版层按行数自己算（词多时整体居中最稳）
            lay.update(stair=(tier >= 4))
        elif layk == "diagonal":
            lay.update(angle=-8 if i % 2 else 7)
        # 冲击型构图整块出现，不跟逐词时间
        sync = layk in ("dense", "megaCrop", "centerBig", "band")

        # 入场：节奏越强，错开越小（重拍要「一起砸下来」）
        ent = {"k": entk}
        if entk in ("slam", "typeOn"):
            ent.update(dur=0.16, stagger=0.0, lead=0.02)
        elif entk in ("sliceDown", "sliceUp", "stretchIn"):
            ent.update(dur=0.30, stagger=0.045, lead=0.05)
        elif entk in ("scatterIn", "spinIn"):
            ent.update(dur=0.42, stagger=0.030, lead=0.10)
        else:
            ent.update(dur=0.52, stagger=0.020, lead=0.08)

        xit = {"k": exitk, "dur": 0.30 if tier >= 3 else 0.46}
        hold = {"k": holdk, "amp": 0.8 + 0.35 * tier}

        tr = {"k": treatk}
        if treatk == "chroma":
            tr.update(chromaAmp=0.020 + 0.010 * (tier - 3))
        if treatk == "extrude":
            tr.update(extrudeN=6, extrudeDX=0.05, extrudeDY=0.045)
        tre = None
        emph = emphasis_of(l, kw)
        if layk == "dense":
            emph = []          # 密排墙把同一句重复七行，逐字强调会变成一片随机色块
        if emph and tier >= 1:
            tre = {"k": "marker" if tier >= 3 else "fillOutline"}

        # 人物层：只有高档段落出现，且由 tier 的 fig 概率决定
        fig = None
        if spec["fig"] > 0 and r.random() < spec["fig"]:
            use_body = (tier >= 4) and (r.random() < 0.45)
            # 满幅构图（密排墙/极限裁切）没有「空白处」可避，必须让字真的从她身上过
            full = layk in ("dense", "megaCrop", "centerBig")
            fig = {
                "mode": "band" if use_body else "disc",
                "seq": "body" if use_body else "head",
                # front = 文字压在她前面（穿插感）；avoid = 人躲在字后面
                "layer": "front" if (full or (tier >= 4 and r.random() < 0.5)) else "avoid",
                "r": int(250 + 55 * r.random()),
                "h": 620, "wRatio": 0.752,
                "side": "r" if (full or r.random() >= 0.5) else "l",
                "y": 0.26 if use_body else 0.44,
                "a": 0.92,
                "fixed": bool(full),        # 满幅构图不做避让，按 side 定点
            }

        # 装饰：按档位递进，不叠加
        decor, bg = [], []
        if tier <= 1:
            decor.append({"k": "rule", "x0": 132, "x1": 1788, "y": 856, "a": 0.13, "ticks": 12})
            decor.append({"k": "chapter", "x": 132, "y": 900, "text": sec["name"], "size": 14, "a": 0.40})
        elif tier == 2:
            bg.append({"k": "vrule", "x": 96, "y0": 120, "y1": 960, "a": 0.10})
            decor.append({"k": "cropMarks", "m": 52, "len": 20, "a": 0.18})
        else:
            decor.append({"k": "barcode", "x": 1500, "y": 856, "w": 290, "h": 34,
                          "a": 0.30, "seed": 7 + i})
            if tier >= 4 and r.random() < 0.55:
                bg.append({"k": "speedLines", "cx": 960, "cy": 520, "r0": 300,
                           "r1": 1250, "n": 30, "a": 0.07, "phase": 0.2})
        decor.append({"k": "progress", "x": 132, "y": 1008, "w": 1656,
                      "p": round((t0 / dur) if dur else 0, 4)})

        # 画面效果：只在高档、且限次
        post = []
        if tier >= 3 and r.random() < 0.30:
            post.append({"k": "flash", "at": round(t0 + 0.02, 3), "decay": 0.10, "a": 0.22})

        shake = round((0.6 + 1.6 * r.random()) * (tier >= 3), 2) if tier >= 3 else 0.0
        cam = {"shake": shake} if shake else {"zoom": 0.012 * (tier - 1) if tier >= 2 else 0}

        shots.append({
            "i": len(shots), "t0": round(t0, 3), "t1": round(t1, 3),
            "line": i, "kind": "line", "tier": tier, "section": sec["name"],
            "energy": round(float(min(1.0, sec["score"])), 3),
            "layout": lay,
            "type": {"face": face, "size": size, "track": track},
            "enter": ent, "hold": hold, "exit": xit,
            "treat": tr, "treatEmph": tre, "emphasis": emph,
            "bg": bg, "decor": decor, "post": post, "cam": cam,
            "fig": fig, "sync": bool(sync),
            "wobble": 0.9 if tier >= 4 else (0.5 if tier == 3 else 0),
            "unified": unified,
            "note": "%s / %s / %s / %s" % (sec["name"], face, layk, entk),
        })

    # ---- 片头标题卡
    # 铁律：标题卡**占据整个前奏**（0 → 首句前 0.3s），并且绝不移动歌词时间。
    # 早先的写法是「标题卡 3.4s + 把后面所有镜头整体后移」，两个错：前奏剩下的
    # 7 秒是纯空画面；歌词被平移后与音频错位（PV 的时间轴必须锁死在实际演唱上）。
    intro_end = shots[0]["t0"] if shots else 0.0
    if intro_end >= 2.4:
        title_card = [{
            "i": -1, "t0": 0.0, "t1": round(intro_end - 0.30, 3), "line": -1,
            "kind": "title", "tier": 0,
            "section": "TITLE", "energy": 0.2,
            "layout": {"k": "justify", "targetW": 1180, "cy": 0.46, "maxSize": 132},
            "type": {"face": "mincho", "size": 116, "track": 0.02},
            "text": a.title,
            "enter": {"k": "fadeSlow", "dur": 1.1, "stagger": 0.055, "lead": 0.25},
            "hold": {"k": "breathe"}, "exit": {"k": "fade", "dur": 0.85},
            "treat": {"k": "solid"},
            "bg": [],
            # 海报式的信息层级：细线 → ORIGINAL → 原唱歌手 → 演唱来源/日期，逐条渐显
            "decor": [
                {"k": "rule", "x0": 370, "x1": 1550, "y": 604, "a": 0.20, "at": 0.55, "fade": 0.7},
                {"k": "chapter", "x": 370, "y": 660, "text": "ORIGINAL", "size": 13,
                 "a": 0.44, "at": 0.80, "fade": 0.6},
                {"k": "chapter", "x": 370, "y": 700, "text": a.artist or "",
                 "size": 22, "a": 0.66, "at": 1.05, "fade": 0.7},
                {"k": "chapter", "x": 370, "y": 730,
                 "text": ("VOCAL " + a.vocal if a.vocal else "") + ("  " + a.date if a.date else ""),
                 "size": 14, "a": 0.36, "at": 1.35, "fade": 0.7},
            ],
            "post": [], "cam": {"zoom": -0.006}, "fig": None, "wobble": 0,
            "unified": False, "note": "title card (整段前奏)",
        }]
        shots = title_card + shots
        for k, s2 in enumerate(shots):
            s2["i"] = k        # 片头插进来之后必须重排，否则 seeded rng 的键会撞

    # ---- 色彩反转：只给最高档章节，且只给其中一段
    tiers_present = [s["tier"] for s in shots if s["kind"] == "line"]
    if 4 in tiers_present:
        idx4 = [k for k, s in enumerate(shots) if s["kind"] == "line" and s["tier"] == 4]
        # 取该章节里连续的一段（不超过 3 句），让反转有「开始与结束」
        inv = idx4[:min(3, len(idx4))]
        for k in inv:
            shots[k]["pal"] = PALETTE_INV
            shots[k]["note"] += " · inverted"

    # ---- 直播序列
    live = None
    if a.live and os.path.exists(a.live):
        lj = json.load(open(a.live, encoding="utf-8"))
        live = {}
        for k in ("head", "body", "wide"):
            if lj.get(k):
                q = dict(lj[k])
                q["dir"] = os.path.abspath(q["dir"]).replace("\\", "/")
                live[k] = q
        if lj.get("face"):
            live["face"] = lj["face"]

    sb = {
        "version": 1,
        "meta": {
            "title": a.title, "artist": a.artist, "vocal": a.vocal,
            "source": a.source, "date": a.date,
            "seed": a.seed, "dur": dur, "fps": 60, "start": w0,
            # 角落极淡标记：信息交给片头标题卡之后，全片只留这一行
            "mark": " · ".join(x for x in (a.vocal, a.title) if x),
        },
        "palette": PALETTE,
        "audio": {"bpm": ana["bpm"], "barSec": ana["bar_sec"],
                  "sections": [[f["t0"] - w0, f["t1"] - w0, f["name"], f["tier"]] for f in feats]},
        "lines": lines,
        "shots": shots,
    }
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    json.dump(sb, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # ---- 核对：行数、遮挡、档次分布
    nl = sum(1 for s in shots if s["kind"] == "line")
    print("[shots] %d 句 → %d 个镜头（含片头 %d），时长 %.2fs"
          % (len(lines), len(shots), len(shots) - nl, dur))
    from collections import Counter
    c = Counter(s["tier"] for s in shots if s["kind"] == "line")
    print("[tier] %s" % dict(sorted(c.items())))
    cu = sum(1 for s in shots if s.get("unified"))
    print("[unify] 复用同一编排的重复句 %d 个" % cu)
    nf = sum(1 for s in shots if s.get("fig"))
    print("[figure] 出现人物层的镜头 %d 个（%.0f%%）" % (nf, 100.0 * nf / max(1, nl)))
    print("[out] %s  %.0fKB" % (a.out, os.path.getsize(a.out) / 1024.0))


if __name__ == "__main__":
    sys.exit(main())
