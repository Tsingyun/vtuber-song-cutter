# -*- coding: utf-8 -*-
"""翻译层（tools/pv_to_z.py）的回归测试。

跑法：
    python tests/test_pv_to_z.py

锁住什么
--------
这个脚本的输出是一份**给上游引擎吃的 JS**，所以错了不会报错，只会渲出错的片子。
真正踩过的坑都不在「翻译」本身，而在**判定的优先级**：

  1. 最后一镜要切成「尾镜 + 尾卡」，否则 28 秒的器乐尾会一路黑到底。
  2. `afterimage` 是给 2~3 秒写的，不能拿去套 28 秒的尾镜。
  3. 空歌词行必须剔掉（JIZURA 会把 `[00:00.00]` 当成一句空歌词画出来）。
  4. `kanjiSlam` / `afterimage` 的预设素材在模板里不存在，必须显式置 null，
     否则 `window.ready` reject、整页不启动（表现为 404 + 白屏）。
  5. 母版补丁（片名 / 配色 / END / LOOKS 映射 / HUD）必须**每条恰好命中一次**：
     上游改版后静默失配，成片会照常渲出来，只是参数全部落空。
"""
import json
import os
import re
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


def make_job(tmp, with_audio=False):
    """最小 job：标题镜 / 立绘镜 / 高能镜 / 长尾镜。"""
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


def put_audio(dst, sections):
    os.makedirs(os.path.join(dst, "analysis"), exist_ok=True)
    with open(os.path.join(dst, "analysis", "audio.json"), "w", encoding="utf-8") as f:
        json.dump({"bars": [{"index": 0, "start": 0.0}], "beats": [0.0], "duration": 60.0,
                   "music_cut": 60.0, "sections": sections}, f, ensure_ascii=False)


def shots_from(tl):
    """从生成的 JS 里抓 shot(...) 的 (id, t0, t1)。"""
    return [(m.group(1), float(m.group(2)), float(m.group(3)))
            for m in re.finditer(r"shot\('([\w]+)', ([\d.]+), ([\d.]+),", tl)]


def test_build(tmp):
    print("[1] 翻译：场景分布 / 尾卡 / 歌词 / 母版补丁")
    job, art = make_job(tmp)
    dst = os.path.join(tmp, "zrun")
    os.makedirs(os.path.join(dst, "engine"), exist_ok=True)
    rep = pv_to_z.build(job, dst, 60.0, "测试曲", "原唱A", "Vocal 岁己SUI", "原唱A", None, [art])

    ck("镜数 4 + 尾卡 = 5", rep["shots"] == 5, rep)
    ck("标题镜 → typeCard", rep["scene"].get("typeCard") == 1, rep["scene"])
    ck("尾卡走 afterimage", rep["scene"].get("afterimage") == 1, rep["scene"])
    ck("其余 → illust", rep["scene"].get("illust") == 3, rep["scene"])
    ck("无 audio.json 时不砸巨字", "kanjiSlam" not in rep["scene"], rep["scene"])
    ck("立绘挂上了", rep["art_refs"] >= 2, rep["art_refs"])

    tl = open(os.path.join(dst, "engine", "timeline.js"), encoding="utf-8").read()
    ck("产出含 Z.SHOTS", "Z.SHOTS = S;" in tl)
    ck("母版 helpers 保留（stars/ridge）", "const stars =" in tl and "const ridge =" in tl)
    ck("母版 DENY 策展保留", "const DENY = {" in tl)
    ck("四个 JIZURA plan 保留", all(("JZ.addPlan('%s'" % p) in tl for p in ("main", "cf", "noir", "mono")))
    ck("片名注入", 'Z.TITLE = "测试曲";' in tl)
    ck("END 换成秒数", "const END = 60;" in tl)
    ck("duration 不再多加 TAIL", "window.__zankoMeta.duration = END;" in tl)
    ck("LOOKS 按能量映射", "s.energy || 0) >= 0.78 ? 'chorus'" in tl)
    ck("尾卡 id = s_end", "'s_end'" in tl)
    ck("立绘引用写成 assets/char/", '"assets/char/art.png"' in tl)
    ck("立绘进 fg 层（front:true）", "front: true" in tl)
    ck("有她的镜头用 cf 版式", 'plan: "cf"' in tl)
    ck("kanjiSlam 的素材显式关掉", "bg: null" not in tl)     # 无 energy 数据时不产生 kanjiSlam

    lrc = open(os.path.join(dst, "analysis", "lyrics_mv.lrc"), encoding="utf-8").read()
    ck("剔掉 [ti:]", "[ti:" not in lrc)
    ck("剔掉空歌词行", "[00:16.00]" not in lrc)
    ck("保留有效两行", lrc.count("]") == 2, repr(lrc))
    ck("立绘复制到 assets/char", os.path.exists(os.path.join(dst, "assets", "char", "art.png")))

    rows = shots_from(tl)
    gaps = [(a[0], a[2], b[0], b[1]) for a, b in zip(rows, rows[1:]) if abs(a[2] - b[1]) > 1e-6]
    ck("镜头首尾相接", not gaps, gaps)
    ck("尾卡 3 秒", abs(rows[-1][2] - rows[-1][1] - 3.0) < 1e-6, rows[-1])


def test_energy(tmp):
    print("[2] 有段落能量：副歌砸巨字 / 导句收白")
    job, art = make_job(tmp)
    dst = os.path.join(tmp, "zrun_e")
    os.makedirs(os.path.join(dst, "engine"), exist_ok=True)
    put_audio(dst, [
        {"name": "A1", "start": 0.0, "end": 8.0, "energy": 0.2, "label": "section", "group": "A"},
        {"name": "B1", "start": 8.0, "end": 16.0, "energy": 0.9, "label": "section", "group": "B"},
        {"name": "C1", "start": 16.0, "end": 22.0, "energy": 0.72, "label": "section", "group": "C"},
        {"name": "D1", "start": 22.0, "end": 24.0, "energy": 0.75, "label": "section", "group": "D"},
        {"name": "E1", "start": 24.0, "end": 60.0, "energy": 0.5, "label": "section", "group": "E"},
    ])
    rep = pv_to_z.build(job, dst, 60.0, "测试曲", "原唱A", "credit", "sub", None, [art])
    ck("副歌首镜砸巨字", rep["scene"].get("kanjiSlam", 0) >= 1, rep["scene"])
    ck("导句末镜收白", rep["scene"].get("converge", 0) >= 1, rep["scene"])

    tl = open(os.path.join(dst, "engine", "timeline.js"), encoding="utf-8").read()
    ck("kanjiSlam 关掉模板素材", "char: null" in tl and "bg: null" in tl)
    ck("kanjiSlam 只留夜空与巨字", "Z.typo.post.kanjiSlam" in tl)
    ck("converge 用上游的 post", "Z.INTRO.post.converge" in tl)


def test_short_tail(tmp):
    print("[3] 尾镜太短时不硬切尾卡")
    job, art = make_job(tmp)
    dst = os.path.join(tmp, "zrun2")
    os.makedirs(os.path.join(dst, "engine"), exist_ok=True)
    sb = json.load(open(os.path.join(job, "storyboard.json"), encoding="utf-8"))
    sb["shots"][-1]["t1"] = 28.0          # 尾镜只剩 4 秒
    json.dump(sb, open(os.path.join(job, "storyboard.json"), "w", encoding="utf-8"), ensure_ascii=False)
    rep = pv_to_z.build(job, dst, 28.0, "测试曲", "原唱A", "credit", "sub", None, [art])
    ck("不切尾卡（镜数仍 4）", rep["shots"] == 4, rep)


def test_limit(tmp):
    print("[4] --shots 区间只翻译区间内的镜")
    job, art = make_job(tmp)
    dst = os.path.join(tmp, "zrun3")
    os.makedirs(os.path.join(dst, "engine"), exist_ok=True)
    rep = pv_to_z.build(job, dst, 60.0, "测试曲", "原唱A", "credit", "sub", (9.0, 20.0), [art])
    ck("只留 2 镜（8-16 / 16-24）", rep["shots"] == 2, rep)
    ck("区间模式不切尾卡", "afterimage" not in rep["scene"], rep["scene"])


def test_density(tmp):
    print("[5] --art-density 0.85 时每个 illust 镜都挂")
    job, art = make_job(tmp)
    dst = os.path.join(tmp, "zrun4")
    os.makedirs(os.path.join(dst, "engine"), exist_ok=True)
    rep = pv_to_z.build(job, dst, 60.0, "测试曲", "原唱A", "credit", "sub", None, [art], 0.9)
    n_illust = rep["scene"].get("illust", 0)
    ck("illust 镜全部挂立绘", rep["art_refs"] == n_illust, (rep["art_refs"], n_illust))


def main():
    tmp = tempfile.mkdtemp(prefix="pvz_")
    try:
        test_build(tmp)
        test_energy(tmp)
        test_short_tail(tmp)
        test_limit(tmp)
        test_density(tmp)
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
