# -*- coding: utf-8 -*-
"""翻译层（tools/pv_to_z.py）的回归测试。

跑法：
    python tests/test_pv_to_z.py
    python -m pytest tests/test_pv_to_z.py -q

锁住什么
--------
这个脚本的输出是一份**给上游引擎吃的 JS**，所以错了不会报错，只会渲出错的片子。
真正踩过的坑都不在「翻译」本身，而在**判定的优先级**：

  1. `shot.get("fig") or {}.get("art")` 这种写法在 Python 里会变成
     `shot.get("fig") or True` —— 每一镜都被判成「有立绘」，全片变成同一种场景。
     （第一次跑出来 49/51 镜都是 kanjiSlam，就是这么来的。）
  2. 最后一镜要切成「尾镜 + 尾卡」，否则 28 秒的器乐尾会一路黑到底。
  3. `afterimage` 是给 2 秒写的，不能拿去套 28 秒的尾镜。
  4. 空歌词行必须剔掉（JIZURA 会把 `[00:00.00]` 当成一句空歌词画出来）。
  5. `kanjiSlam` / `afterimage` 的预设素材在模板里不存在，必须显式置 null，
     否则 `window.ready` reject、整页不启动（表现为 404 + 白屏）。
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import pv_to_z  # noqa: E402

FAILED = []


def ck(name, cond, detail=""):
    if cond:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s  %s" % (name, detail))
        FAILED.append(name)


PNG_1PX = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00"
    b"\x00IEND\xaeB`\x82"
)


def make_job(tmp):
    """造一个最小的 job 目录：标题镜 / 立绘镜 / 高能镜 / 长尾镜。"""
    job = os.path.join(tmp, "job")
    os.makedirs(job, exist_ok=True)
    art = os.path.join(tmp, "art.png")
    with open(art, "wb") as f:
        f.write(PNG_1PX)
    sb = {
        "version": 2,
        "meta": {"title": "测试曲", "artist": "原唱A", "vocal": "岁己SUI", "dur": 60.0},
        "lines": [],
        "shots": [
            {"i": 0, "t0": 0.0, "t1": 8.0, "kind": "title", "tier": 1, "section": "TITLE", "text": "测试曲"},
            {"i": 1, "t0": 8.0, "t1": 16.0, "tier": 1, "section": "A1",
             "fig": {"art": {"src": art}, "layer": "split", "splitAt": 0.62}},
            {"i": 2, "t0": 16.0, "t1": 24.0, "tier": 3, "section": "B1", "text": "高能句"},
            {"i": 3, "t0": 24.0, "t1": 60.0, "tier": 2, "section": "A7"},
        ],
    }
    with open(os.path.join(job, "storyboard.json"), "w", encoding="utf-8") as f:
        json.dump(sb, f, ensure_ascii=False)
    with open(os.path.join(job, "lyric.lrc"), "w", encoding="utf-8") as f:
        f.write("[ti:测试曲]\n[00:08.00]第一句\n\n[00:16.00]\n[00:20.00]第二句\n")
    return job, art


def test_build(tmp):
    print("[1] 翻译：场景分布 / 尾卡 / 歌词")
    job, art = make_job(tmp)
    dst = os.path.join(tmp, "zrun")
    os.makedirs(os.path.join(dst, "engine"), exist_ok=True)
    rep = pv_to_z.build(job, dst, 60.0, "测试曲", "原唱A", "Vocal 岁己SUI", "原唱A")

    ck("镜数 4 + 尾卡 = 5", rep["shots"] == 5, rep)
    ck("标题镜 → typeCard", rep["scene"].get("typeCard") == 2, rep["scene"])
    ck("高能镜 → converge", rep["scene"].get("converge") == 1, rep["scene"])
    ck("其余 → illust", rep["scene"].get("illust") == 2, rep["scene"])
    ck("没有任何 kanjiSlam / afterimage", "kanjiSlam" not in rep["scene"] and "afterimage" not in rep["scene"], rep["scene"])

    tl = open(os.path.join(dst, "engine", "timeline.js"), encoding="utf-8").read()
    ck("产出 timeline.js 含 Z.SHOTS", "Z.SHOTS = S" in tl)
    ck("内含 __zankoMeta.duration", "__zankoMeta.duration" in tl)
    ck("尾卡 id = s_end", '"s_end"' in tl)
    ck("立绘引用写成 assets/char/", '"assets/char/art.png"' in tl)
    ck("立绘进 fg 层（front:true）", '"front": true' in tl)
    ck("有她的镜头用 cf 版式", '"plan": "cf"' in tl)
    ck("converge 不吃字形素材", '"bg": null' not in tl)   # converge 不写 bg 字段

    lrc = open(os.path.join(dst, "analysis", "lyrics_mv.lrc"), encoding="utf-8").read()
    ck("剔掉 [ti:]", "[ti:" not in lrc)
    ck("剔掉空歌词行", "[00:16.00]" not in lrc)
    ck("保留有效两行", lrc.count("]") == 2, repr(lrc))
    ck("立绘复制到 assets/char", os.path.exists(os.path.join(dst, "assets", "char", "art.png")))

    # 时间轴连续性：镜与镜不重叠、不留缝（尾卡接在尾镜之后）
    spec = json.loads(tl.split("const SPEC = ", 1)[1].split(";\n", 1)[0])
    gaps = []
    for a, b in zip(spec["shots"], spec["shots"][1:]):
        if abs(a["t1"] - b["t0"]) > 1e-6:
            gaps.append((a["id"], a["t1"], b["id"], b["t0"]))
    ck("镜头首尾相接", not gaps, gaps)
    ck("尾卡 7 秒", abs(spec["shots"][-1]["t1"] - spec["shots"][-1]["t0"] - 7.0) < 1e-6)


def test_short_tail(tmp):
    print("[2] 尾镜太短时不硬切尾卡")
    job, _ = make_job(tmp)
    dst = os.path.join(tmp, "zrun2")
    os.makedirs(os.path.join(dst, "engine"), exist_ok=True)
    sb = json.load(open(os.path.join(job, "storyboard.json"), encoding="utf-8"))
    sb["shots"][-1]["t1"] = 28.0          # 尾镜只剩 4 秒
    json.dump(sb, open(os.path.join(job, "storyboard.json"), "w", encoding="utf-8"), ensure_ascii=False)
    rep = pv_to_z.build(job, dst, 28.0, "测试曲", "原唱A", "credit", "sub")
    ck("不切尾卡（镜数仍 4）", rep["shots"] == 4, rep)


def test_limit(tmp):
    print("[3] --shots 区间只翻译区间内的镜")
    job, _ = make_job(tmp)
    dst = os.path.join(tmp, "zrun3")
    os.makedirs(os.path.join(dst, "engine"), exist_ok=True)
    rep = pv_to_z.build(job, dst, 60.0, "测试曲", "原唱A", "credit", "sub", (9.0, 20.0))
    ck("只留 2 镜（8-16 / 16-24）", rep["shots"] == 2, rep)
    ck("区间模式不切尾卡", rep["scene"].get("typeCard", 0) == 0 and rep["scene"].get("converge") == 1, rep["scene"])


def main():
    tmp = tempfile.mkdtemp(prefix="pvz_")
    try:
        test_build(tmp)
        test_short_tail(tmp)
        test_limit(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    if FAILED:
        print("%d 项失败：%s" % (len(FAILED), ", ".join(FAILED)))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
