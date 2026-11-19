# -*- coding: utf-8 -*-
"""pv_timing_patch.py —— 修补 pv_timing 的「行内词时间塌缩」。

为什么需要
----------
`pv_timing.py` 对**行起点**很可靠（LRC + 全局校正 + 起音吸附，实测中位位移 0.09s），
但对**行内词边界**会整段塌缩：某行第一两个字还正常，后面几个字全变成 `t0 == t1`
（零时长）。判据很隐蔽 —— 行的 `t0/t1` 看上去「有值」，渲染不报错，只是那一行的
字在一瞬间全闪出来，或者干脆只显示前两个字。

2026-11-01《画心》实测：34 行里 #2 #9 #10 #11 #12 #13 #18 #19 #20 共 9 行塌缩，
其中 #13/#18 每字只有 0.049s（正常 0.67s），13 倍差。

修法
----
**只修行内词，不动行起点**（行起点是全片对齐的锚，动它会连带毁掉相邻行）。
对判定为塌缩的行，按「目标跨度 ÷ 字权重」重新分配：

    target = 下一行 t0 - 本行 t0        （间距 ≤8.5s，正常两句之间）
           = 本曲中位句长               （间距 >8.5s ⇒ 中间有间奏，不能拉满）

判据不只看「每字时长」，而是**与它自己的空隙比**：span < gap × 0.8 即为塌缩。
只看绝对阈值会放过 #19（3.46s/13 字 = 0.266s，看着还行，其实只有应有的一半）。

权重：汉字 1.0，句尾字 1.35，句首字 1.15，标点/空白 0（不占时间）。

用法
----
    python tools/pv_timing_patch.py --timing cuts/<d>/<s>/_work/timing.json
    python tools/pv_timing_patch.py --timing ... --check      # 只体检不写
"""
import argparse
import json
import shutil
import statistics
import sys

MIN_PER_CHAR = 0.20       # 兜底：短于此必然塌缩（无 gap 可参照时用）
GAP_IS_INTERLUDE = 8.5    # 行间距超过它 ⇒ 中间是间奏
SPAN_IS_SHORT = 0.80      # span < gap × 0.8 ⇒ 塌缩
TAIL_DUR = 28.76          # 末行到音频尾的兜底跨度（无下一行时）

PUNCT = set("，。、；：？！…—·「」『』（）()《》〈〉,.!?;:\"'“”‘’ \t\u3000")


def char_weight(ch, idx, total):
    if ch in PUNCT:
        return 0.0
    if idx == total - 1:
        return 1.35           # 句尾拖长
    if idx == 0:
        return 1.15           # 句首起音
    return 1.0


def rebuild(line, target):
    ws = line.get("words") or []
    if not ws:
        return ws
    total = len(ws)
    wts = [char_weight(w["text"], i, total) for i, w in enumerate(ws)]
    s = sum(wts)
    if s <= 0:
        return ws
    t = line["t0"]
    out = []
    for w, ww in zip(ws, wts):
        d = target * ww / s
        out.append(dict(w, t0=round(t, 3), t1=round(t + d, 3)))
        t += d
    for k in range(len(out) - 1, -1, -1):
        if wts[k] > 0:
            out[k]["t1"] = round(line["t0"] + target, 3)
            break
    return out


def diagnose(line, gap, med):
    """返回 (是否塌缩, 说明)。gap = 到下一行起点的秒数，med = 本曲中位句长。

    参照系不是固定阈值，而是**这一行本该有多长**：间距正常时用间距，间距里夹着
    间奏时用中位句长。只看绝对「每字秒数」会漏掉 #12（2.34s / 10 字 = 0.234s，
    看着及格，实际只有应有长度的一半）。
    """
    ws = [w for w in (line.get("words") or []) if w["text"].strip()]
    if not ws:
        return False, "空行"
    zero = sum(1 for w in ws if w["t1"] - w["t0"] <= 0.001)
    span = line["t1"] - line["t0"]
    per = span / len(ws)
    if zero:
        return True, "零时长 %d/%d" % (zero, len(ws))
    ref = gap if gap <= GAP_IS_INTERLUDE else med
    if span < ref * SPAN_IS_SHORT:
        return True, "span %.2f / 应 %.2f" % (span, ref)
    if per < MIN_PER_CHAR:
        return True, "每字 %.3fs" % per
    return False, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timing", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    with open(a.timing, encoding="utf-8") as f:
        raw = f.read()
    t = json.loads(raw)
    L = t["lines"]

    spans = [L[i + 1]["t0"] - L[i]["t0"] for i in range(len(L) - 1)]
    good = [s for s in spans if 2.0 < s < GAP_IS_INTERLUDE]
    med = round(statistics.median(good), 3) if good else 6.9
    print("[patch] 中位句长 %.3fs（%d 个样本）" % (med, len(good)))

    fixed, skipped = [], []
    for i, ln in enumerate(L):
        gap = (L[i + 1]["t0"] - ln["t0"]) if i + 1 < len(L) else TAIL_DUR
        bad, why = diagnose(ln, gap, med)
        if not bad:
            continue
        target = gap if gap <= GAP_IS_INTERLUDE else med
        if target <= 0.2:
            skipped.append((i, why))
            continue
        old = round(ln["t1"] - ln["t0"], 2)
        ln["words"] = rebuild(ln, target)
        ln["t1"] = round(ln["t0"] + target, 3)
        fixed.append((i, why, old, round(target, 2), (ln.get("text") or "")[:18]))

    print("[patch] 修补 %d / %d 行" % (len(fixed), len(L)))
    for i, why, old, new, txt in fixed:
        print("  #%2d %-20s %.2fs → %.2fs  %s" % (i, why, old, new, txt))
    for i, why in skipped:
        print("  ! #%d 跳过（%s）" % (i, why))

    if a.check:
        return 0
    if not fixed:
        print("[patch] 无需修补")
        return 0

    out = a.out or a.timing
    if out == a.timing and not a.check:
        bak = a.timing + ".bak"
        with open(bak, "w", encoding="utf-8") as f:
            f.write(raw)
        print("[bak] %s" % bak)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(t, f, ensure_ascii=False)
    print("[out] %s" % out)

    with open(out, encoding="utf-8") as f:
        L2 = json.load(f)["lines"]
    left = []
    for i, ln in enumerate(L2):
        gap = (L2[i + 1]["t0"] - ln["t0"]) if i + 1 < len(L2) else TAIL_DUR
        if diagnose(ln, gap, med)[0]:
            left.append(i)
    print("[verify] 仍有塌缩：%s" % (left or "无"))
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main())
