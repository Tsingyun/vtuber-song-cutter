# -*- coding: utf-8 -*-
"""pv_spans（从编排稿生成人物挂点表）的回归测试。

跑法：
    python tests/test_pv_spans.py

锁住什么
--------
挂点表的键是**句号**、win 是**成片时间**、窗口必须落在片内 —— 这三条错一条，
现象都是「抽到了别的内容」而不是报错。所以这里用假编排稿：

  1. 只挑 kind=line 且 line>=0 的句子（片头/片尾卡与 breath 镜不属于任何一句）
  2. 太短的句子跳过（一闪而过的画面装了也看不出来）
  3. 均匀取 n 处、句号不重复、seq 依次为 s1..sN
  4. win = [t0-lead, 镜头长+lead+tail]，且**不越出片长**（越界会被 pv_live 的闸门拦）
  5. 空编排稿要给出可读报错
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import pv_spans as PS      # noqa: E402

FAILS = []


def ck(cond, name, detail=""):
    if cond:
        print("  ✓ %s" % name)
    else:
        print("  ✗ %s %s" % (name, detail))
        FAILS.append(name)


def mk_sb(dur=100.0):
    """5 句词 + 2 个 card。第 2 句故意只有 0.5s（应被跳过）。"""
    spans = [(0.0, 2.0), (3.0, 3.5), (5.0, 9.0), (12.0, 20.0), (30.0, 45.0), (60.0, 95.0)]
    shots = [{"i": 0, "t0": 0.0, "t1": 2.0, "line": -1, "kind": "card"}]
    for k, (t0, t1) in enumerate(spans):
        shots.append({"i": k + 1, "t0": t0, "t1": t1, "line": k, "kind": "line"})
    shots.append({"i": len(shots), "t0": 95.0, "t1": dur, "line": -1, "kind": "card"})
    # 一个拆镜产生的 breath 镜（line=-1）
    shots.append({"i": len(shots), "t0": 45.0, "t1": 60.0, "line": -1, "kind": "line",
                  "special": "breath"})
    shots.sort(key=lambda s: s["t0"])
    return {"meta": {"dur": dur, "fps": 30}, "shots": shots, "lines": []}


def test_pick():
    print("[pv_spans] 选句")
    sb = mk_sb()
    sel = PS.pick(sb["shots"], 3)
    ck(len(sel) == 3, "取 3 处", len(sel))
    ck(all(s["line"] >= 0 for s in sel), "不会选中 card / breath（line=-1）")
    ck(all(s["t1"] - s["t0"] >= 1.2 for s in sel), "跳过 <1.2s 的短句", sel)
    li = [s["line"] for s in sel]
    ck(len(set(li)) == len(li), "句号不重复", li)
    ck(li == sorted(li), "按时间顺序", li)


def test_build():
    print("[pv_spans] 表结构")
    sb = mk_sb()
    sp = PS.build(sb, n=4, lead=2.0, tail=2.5)
    ck(len(sp) == 4, "4 处", len(sp))
    ck([v["seq"] for v in sp.values()] == ["s1", "s2", "s3", "s4"], "seq 依次 s1..sN")
    for k, v in sp.items():
        w0, wd = v["win"]
        ck(w0 >= 0, "句 %s win 起点不为负（%.2f）" % (k, w0))
        ck(w0 + wd <= sb["meta"]["dur"] + 1e-6,
           "句 %s 窗口不越片长（%.2f+%.2f ≤ %.1f）" % (k, w0, wd, sb["meta"]["dur"]))
        ck(v["mode"] in PS.MODES, "句 %s 形态在支持列表内（%s）" % (k, v["mode"]))

    # 最后一句贴片尾：tail 必须被截断
    sp2 = PS.build(sb, n=6, lead=2.0, tail=2.5)
    last = max(sp2.values(), key=lambda v: v["win"][0])
    ck(last["win"][0] + last["win"][1] <= sb["meta"]["dur"] + 1e-6,
       "贴片尾的窗口被截断（%.2f+%.2f）" % (last["win"][0], last["win"][1]))


def test_empty():
    print("[pv_spans] 空编排稿")
    ck(PS.pick([{"i": 0, "t0": 0, "t1": 1, "line": -1, "kind": "card"}], 3) == [],
       "没有可挂的句子 → 空列表（调用方报错）")


def test_fig_from_spans():
    """轻量表 → shot.fig：几何必须补齐，又不能覆盖完整表里的手写值。

    这是本轮修掉的崩溃点：轻量表只有 mode/seq/win，fig_reserve 直接读 fig["r"]，
    第一句 disc 就 KeyError。
    """
    print("[pv_spans] 轻量表 → fig（pv_direct._fig_from_spans）")
    import pv_direct as PD

    PD.set_fig_spans({"3": {"seq": "s2", "mode": "band", "win": [10.0, 4.0]}})
    f = PD._fig_from_spans(3, 0.0)
    ck(f["seq"] == "s2" and f["mode"] == "band", "seq/mode 用表里的", f)
    ck(f.get("h", 0) > 0, "轻量表缺的几何被补齐（h=%s）" % f.get("h"))
    ck(abs(f["tOff"] + 10.0) < 1e-6, "tOff = w0 - win.start", f.get("tOff"))
    ck(f["wDur"] == 4.0, "wDur 带出来")

    # 完整表（dawn 那种逐句定稿）里的手写几何不能被默认值盖掉
    PD.set_fig_spans({"3": {"seq": "x", "mode": "disc", "win": [10.0, 4.0],
                            "r": 168, "y": 0.4, "a": 0.88}})
    g = PD._fig_from_spans(3, 2.5)
    ck(g["r"] == 168 and g["a"] == 0.88, "完整表不被默认几何覆盖", g)
    ck(abs(g["tOff"] - (2.5 - 10.0)) < 1e-6, "切片窗口 tOff 补 w0", g.get("tOff"))
    PD.set_fig_spans({})


def main():
    test_pick()
    test_build()
    test_empty()
    test_fig_from_spans()
    print("")
    if FAILS:
        print("✗ %d 项失败：%s" % (len(FAILS), "、".join(FAILS)))
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
