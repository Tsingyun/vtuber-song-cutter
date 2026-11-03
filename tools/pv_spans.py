#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从编排稿生成「人物挂点表」（LIVE_SPANS / --spans 消费的那张表）。

为什么要这个工具
----------------
挂点表决定「哪一句有人、什么形态、用哪条序列」。以前它是一段写死在
`pv_direct.py` 里的字面量（1027 那场的六段），换一首歌只能整段手抄一遍时间，
抄错一位不会报错，只会「该有人的那句没有人」。

现在从编排稿反推：镜头时间是编排稿定稿，挂点按句号挂上去，`win` 按
「镜头起点前 lead 秒」的约定算 —— 与 `_check_live_spans` 的判据同源，
所以抽出来的序列天然对得上。

用法
----
    python tools/pv_spans.py --sb _tmp/mvg/storyboard.json --n 6 --out _tmp/mvg/spans.json
    python tools/pv_live.py --date 2026-11-01 --from-segments --song 小丑的品格 \\
        --spans _tmp/mvg/spans.json --out _tmp/mvg/live

注意：键是**句号**（编排稿 shot.line），不是镜头号 —— 片头/片尾卡与 breath 镜
不属于任何一句，写错一位就整片错一格（见 pv_direct._check_live_spans 的注释）。
"""
import argparse
import json
import os
import sys

# 只放 fig_script 有固定几何的三种形态（pv_direct.FIG_GEOM0）。
# arch/wash/split 那种「逐句定稿」的几何只能手写完整表 —— 轻量表里没有 r/h/wf，
# 引擎侧的宽度推导算不出来，硬塞会得到「让位算错」而不是「画得难看」。
MODES = ["disc", "band", "slab"]


def pick(shots, n, min_dur=1.2):
    """均匀挑 n 个句子。太短的句子跳过（画面一闪而过，装了也看不出来）。"""
    cand = [s for s in shots
            if s.get("kind") == "line" and isinstance(s.get("line"), int) and s["line"] >= 0
            and float(s["t1"]) - float(s["t0"]) >= min_dur]
    if not cand:
        return []
    if n >= len(cand):
        return cand
    out, step = [], len(cand) / float(n)
    for k in range(n):
        lo, hi = int(k * step), max(int(k * step) + 1, int((k + 1) * step))
        seg = cand[lo:hi] or [cand[min(lo, len(cand) - 1)]]
        # 段内取最长的那个：同样位置，长句给的画面更站得住
        out.append(max(seg, key=lambda s: float(s["t1"]) - float(s["t0"])))
    return out


def build(sb, n=6, modes=None, lead=2.0, tail=2.5):
    modes = modes or MODES
    shots = sorted(sb.get("shots") or [], key=lambda s: s["t0"])
    dur = float((sb.get("meta") or {}).get("dur") or 0) or \
        max((float(s.get("t1") or 0) for s in shots), default=0.0)
    sel = pick(shots, n)
    spans, used = {}, set()
    for k, sh in enumerate(sel):
        li = int(sh["line"])
        if li in used:
            continue
        used.add(li)
        t0, t1 = float(sh["t0"]), float(sh["t1"])
        w0 = max(0.0, t0 - lead)
        # 窗口要够长：镜头本身 + 前置 lead + 一点余量（拆镜后的 breath 镜会用到后面）
        wd = round(t1 - w0 + tail, 3)
        # 但不能越出成片：pv_live 的闸门会拦（win 落在片外 = 抽到别的内容），
        # 在生成端就截断，比让用户去猜为什么被拦好。
        if dur > 0:
            wd = round(min(wd, dur - w0), 3)
        spans[li] = {"seq": "s%d" % (k + 1), "mode": modes[k % len(modes)],
                     "win": [round(w0, 3), max(0.5, wd)]}
    return spans


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sb", required=True, help="编排稿 storyboard.json")
    ap.add_argument("--n", type=int, default=6, help="挂几处（默认 6，与 dawn 表一致）")
    ap.add_argument("--modes", default=",".join(MODES),
                    help="画法轮转（disc/band/split/arch/wash/pill/strip/duo/soft/ghost/crop）")
    ap.add_argument("--lead", type=float, default=2.0, help="窗口起点比镜头早多少秒（约定 2.0）")
    ap.add_argument("--tail", type=float, default=2.5, help="窗口末尾余量")
    ap.add_argument("--out", default="", help="写到文件（缺省打印）")
    a = ap.parse_args()

    with open(a.sb, encoding="utf-8") as f:
        sb = json.load(f)
    modes = [m.strip() for m in a.modes.split(",") if m.strip()]
    spans = build(sb, a.n, modes, a.lead, a.tail)
    if not spans:
        raise SystemExit("[spans] 编排稿里没有可挂的句子（kind=line 且 line>=0）")
    txt = json.dumps(spans, ensure_ascii=False, indent=1)
    if a.out:
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(txt)
    else:
        print(txt)
    tot = sum(float(v["win"][1]) for v in spans.values())
    dur = float((sb.get("meta") or {}).get("dur") or 0)
    print("[spans] %d 处 / 窗口合计 %.1fs%s（片子 %.1fs，覆盖 %.1f%%）"
          % (len(spans), tot, " → " + a.out if a.out else "", dur,
             (tot / dur * 100) if dur else 0))


if __name__ == "__main__":
    sys.exit(main())
